# SPDX-License-Identifier: AGPL-3.0-only
# Copyright (c) 2026 sol pbc

from __future__ import annotations

import json
import os
import shlex
import socket
import sqlite3
import subprocess
import sys
from pathlib import Path

from tests import verify_indexer_differential as harness

FIXTURE_JOURNAL = Path("tests/fixtures/journal").resolve()


def _quote_command(*parts: str | Path) -> str:
    return " ".join(shlex.quote(str(part)) for part in parts)


def _writer_script(tmp_path: Path) -> Path:
    script = tmp_path / "write_index.py"
    script.write_text(
        """
import os
import sqlite3
import sys
from pathlib import Path

mode = sys.argv[1]
if mode == "fail":
    sys.exit(7)
if mode == "missing":
    sys.exit(0)

journal = Path(os.environ["SOLSTONE_JOURNAL"])
db = journal / "indexer" / "journal.sqlite"
db.parent.mkdir(parents=True, exist_ok=True)
conn = sqlite3.connect(db)
conn.execute("CREATE TABLE files(path TEXT PRIMARY KEY, mtime INTEGER)")
conn.execute(\"\"\"
CREATE VIRTUAL TABLE chunks USING fts5(
    content,
    path UNINDEXED,
    day UNINDEXED,
    facet UNINDEXED,
    agent UNINDEXED,
    stream UNINDEXED,
    idx UNINDEXED,
    time_bucket UNINDEXED
)
\"\"\")
conn.execute("CREATE TABLE edge_files(path TEXT PRIMARY KEY, mtime INTEGER)")
conn.execute(\"\"\"
CREATE TABLE edges(
    src TEXT NOT NULL,
    dst TEXT NOT NULL,
    kind TEXT NOT NULL,
    directed INTEGER NOT NULL,
    src_name TEXT,
    dst_name TEXT,
    day TEXT,
    facet TEXT,
    source TEXT NOT NULL,
    path TEXT NOT NULL,
    anchor TEXT,
    label TEXT,
    ts INTEGER,
    weight INTEGER NOT NULL
)
\"\"\")
if mode != "empty":
    mtime = 20 if mode == "mtime2" else 10
    content = "different token" if mode == "different" else "same token"
    conn.execute(
        "INSERT INTO files(path, mtime) VALUES (?, ?)",
        ("entity_search:__mtime__", mtime),
    )
    conn.execute(
        "INSERT INTO chunks(content, path, day, facet, agent, stream, idx, time_bucket) "
        "VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
        (content, "source.md", "20260101", "test", "test", "", 0, ""),
    )
conn.commit()
conn.close()
""".lstrip(),
        encoding="utf-8",
    )
    return script


def _command(tmp_path: Path, mode: str) -> str:
    return _quote_command(sys.executable, _writer_script(tmp_path), mode)


def _tree_inventory(root: Path) -> dict[str, tuple[int, int]]:
    return {
        path.relative_to(root).as_posix(): (stat.st_size, stat.st_mtime_ns)
        for path in sorted(root.rglob("*"))
        for stat in [path.lstat()]
    }


def _create_index_db(path: Path, *, content: str = "same token") -> None:
    script = _writer_script(path.parent)
    env = os.environ.copy()
    journal = path.parent / f"{path.stem}_journal"
    env["SOLSTONE_JOURNAL"] = str(journal)
    subprocess.run(
        [sys.executable, str(script), "same"],
        env=env,
        check=True,
        capture_output=True,
        text=True,
    )
    db = journal / "indexer" / "journal.sqlite"
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(db.read_bytes())
    if content != "same token":
        with sqlite3.connect(path) as conn:
            conn.execute("DELETE FROM chunks")
            conn.execute(
                "INSERT INTO chunks(content, path, day, facet, agent, stream, idx, time_bucket) "
                "VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
                (content, "source.md", "20260101", "test", "test", "", 0, ""),
            )
            conn.commit()


def test_stderr_classifier_only_allows_traceback_continuations() -> None:
    stderr = "\n".join(
        [
            f"{harness.EDGE_SKIP_PREFIX}20240102/default/234567_300/screen.jsonl",
            "Traceback (most recent call last):",
            '  File "/tmp/example.py", line 1, in <module>',
            "    raise ValueError('bad')",
            "ValueError: Invalid segment key: 234567_300",
            "unexpected diagnostic",
        ]
    )

    classified = harness.classify_stderr(stderr)

    assert classified["rules"][0]["count"] == 1
    assert classified["unclassified"] == ["unexpected diagnostic"]


def test_runner_prepares_tracked_clean_copies_with_equal_mtimes(tmp_path: Path) -> None:
    copies = harness._prepare_working_copies(FIXTURE_JOURNAL, tmp_path / "work")

    assert set(copies) == {"left", "right"}
    assert not (copies["left"] / harness.DB_REL).exists()
    assert not (copies["right"] / harness.DB_REL).exists()
    assert harness._mtime_mismatches(copies["left"], copies["right"]) == []


def test_runner_sets_journal_env_and_captures_exit_codes(tmp_path: Path) -> None:
    report = harness.run_differential(
        journal=FIXTURE_JOURNAL,
        command_a=_command(tmp_path, "same"),
        command_b=_command(tmp_path, "same"),
        work_root=tmp_path / "work",
    )

    assert report["classification"] == "equal"
    assert [command["exit_code"] for command in report["commands"]] == [0, 0]
    assert report["commands"][0]["journal"] != report["commands"][1]["journal"]
    assert all(
        command["checks"]["database"]["status"] == "ok"
        for command in report["commands"]
    )


def test_missing_database_is_failed_not_equal(tmp_path: Path) -> None:
    report = harness.run_differential(
        journal=FIXTURE_JOURNAL,
        command_a=_command(tmp_path, "missing"),
        command_b=_command(tmp_path, "missing"),
        work_root=tmp_path / "work",
    )

    assert report["classification"] == "failed"
    assert report["failure"]["class"] == "db_missing"


def test_empty_database_is_failed_not_equal(tmp_path: Path) -> None:
    report = harness.run_differential(
        journal=FIXTURE_JOURNAL,
        command_a=_command(tmp_path, "empty"),
        command_b=_command(tmp_path, "empty"),
        work_root=tmp_path / "work",
    )

    assert report["classification"] == "failed"
    assert report["failure"]["class"] == "db_empty"


def test_wal_representation_invariance_canonicalizes_equal(tmp_path: Path) -> None:
    left = tmp_path / "left.sqlite"
    right = tmp_path / "right.sqlite"
    _create_index_db(right)

    conn = sqlite3.connect(left)
    try:
        conn.execute("PRAGMA journal_mode=WAL")
        conn.execute("CREATE TABLE files(path TEXT PRIMARY KEY, mtime INTEGER)")
        conn.execute(
            """
            CREATE VIRTUAL TABLE chunks USING fts5(
                content,
                path UNINDEXED,
                day UNINDEXED,
                facet UNINDEXED,
                agent UNINDEXED,
                stream UNINDEXED,
                idx UNINDEXED,
                time_bucket UNINDEXED
            )
            """
        )
        conn.execute("CREATE TABLE edge_files(path TEXT PRIMARY KEY, mtime INTEGER)")
        conn.execute(
            """
            CREATE TABLE edges(
                src TEXT NOT NULL,
                dst TEXT NOT NULL,
                kind TEXT NOT NULL,
                directed INTEGER NOT NULL,
                src_name TEXT,
                dst_name TEXT,
                day TEXT,
                facet TEXT,
                source TEXT NOT NULL,
                path TEXT NOT NULL,
                anchor TEXT,
                label TEXT,
                ts INTEGER,
                weight INTEGER NOT NULL
            )
            """
        )
        conn.execute(
            "INSERT INTO files(path, mtime) VALUES (?, ?)",
            ("entity_search:__mtime__", 10),
        )
        conn.execute(
            "INSERT INTO chunks(content, path, day, facet, agent, stream, idx, time_bucket) "
            "VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
            ("same token", "source.md", "20260101", "test", "test", "", 0, ""),
        )
        conn.commit()

        assert left.read_bytes() != right.read_bytes()
        _normalized, comparison = harness.canonicalize_pair(
            left, right, tmp_path / "scratch"
        )
    finally:
        conn.close()

    assert comparison["classification"] == "equal"


def test_shadow_table_only_change_still_equal(tmp_path: Path) -> None:
    left = tmp_path / "left.sqlite"
    right = tmp_path / "right.sqlite"
    _create_index_db(left)
    _create_index_db(right)
    with sqlite3.connect(right) as conn:
        conn.execute("INSERT INTO chunks(chunks, rank) VALUES('automerge', 2)")
        conn.commit()

    assert left.read_bytes() != right.read_bytes()
    _normalized, comparison = harness.canonicalize_pair(
        left, right, tmp_path / "scratch"
    )

    assert comparison["classification"] == "equal"


def test_seeded_divergence_is_unexpected_differs_and_cli_nonzero(
    tmp_path: Path,
    capsys,
) -> None:
    exit_code = harness.main(
        [
            "--journal",
            str(FIXTURE_JOURNAL),
            "--a",
            _command(tmp_path, "same"),
            "--b",
            _command(tmp_path, "different"),
            "--work-dir",
            str(tmp_path / "work"),
        ]
    )

    report = json.loads(capsys.readouterr().out)
    assert exit_code != 0
    assert report["classification"] == "unexpected-differs"


def test_mtime_only_divergence_is_functionally_equal(tmp_path: Path) -> None:
    report = harness.run_differential(
        journal=FIXTURE_JOURNAL,
        command_a=_command(tmp_path, "same"),
        command_b=_command(tmp_path, "mtime2"),
        work_root=tmp_path / "work",
    )

    assert report["classification"] == "functionally-equal"
    assert [rule["name"] for rule in report["normalization"]["rules_fired"]] == [
        harness.ENTITY_SEARCH_MTIME_RULE
    ]


def test_command_failure_is_distinct_and_cli_nonzero(tmp_path: Path, capsys) -> None:
    exit_code = harness.main(
        [
            "--journal",
            str(FIXTURE_JOURNAL),
            "--a",
            _command(tmp_path, "same"),
            "--b",
            _command(tmp_path, "fail"),
            "--work-dir",
            str(tmp_path / "work"),
        ]
    )

    report = json.loads(capsys.readouterr().out)
    assert exit_code != 0
    assert report["classification"] == "failed"
    assert report["failure"]["class"] == "command_nonzero"
    assert report["failure"]["command_id"] == "right"


def test_fixture_corpus_reports_equal_with_visible_edge_skips(tmp_path: Path) -> None:
    journal_bin = Path(sys.executable).with_name("journal")
    command = _quote_command(journal_bin, "indexer", "--rescan-full")

    report = harness.run_differential(
        journal=FIXTURE_JOURNAL,
        command_a=command,
        command_b=command,
        work_root=tmp_path / "work",
    )

    table_counts = {
        table["name"]: table["row_counts"]["left"]
        for table in report["canonical"]["tables"]
    }
    skip_counts = [
        command_report["stderr_classification"]["rules"][0]["count"]
        for command_report in report["commands"]
    ]
    corpus = report["provenance"]["corpus"]

    assert report["classification"] == "equal"
    assert report["normalization"]["rules_fired"] == []
    assert corpus["copy_route"] == "git-archive-head"
    assert corpus["identity"]["kind"] == "git-archive-head"
    assert corpus["identity"]["repo_commit"]
    assert table_counts == {
        "files": 176,
        "chunks": 590,
        "edge_files": 101,
        "edges": 30,
    }
    assert skip_counts == [1, 1]


def test_harness_does_not_use_network_or_write_outside_workdir(
    tmp_path: Path,
    monkeypatch,
) -> None:
    private_repo = tmp_path / "private-repo"
    private_corpus = private_repo / "tests" / "fixtures" / "journal"
    harness.copytree_tracked(FIXTURE_JOURNAL, private_corpus)
    subprocess.run(
        ["git", "init", "-q"],
        cwd=private_repo,
        capture_output=True,
        text=True,
        check=True,
    )
    subprocess.run(
        ["git", "add", "."],
        cwd=private_repo,
        capture_output=True,
        text=True,
        check=True,
    )
    before_inventory = _tree_inventory(private_corpus)
    connect_calls: list[tuple[object, object]] = []

    def fail_connect(self: socket.socket, address: object) -> None:
        connect_calls.append((self, address))
        raise AssertionError("network call attempted")

    # This catches in-process harness networking; subprocess networking is out of scope.
    monkeypatch.setattr(socket.socket, "connect", fail_connect)

    report = harness.run_differential(
        journal=private_corpus,
        command_a=_command(tmp_path, "same"),
        command_b=_command(tmp_path, "same"),
        work_root=tmp_path / "work",
    )

    after_inventory = _tree_inventory(private_corpus)

    assert report["classification"] == "equal"
    assert report["provenance"]["corpus"]["copy_route"] == "git-ls-files-live"
    assert report["provenance"]["corpus"]["identity"] is None
    assert connect_calls == []
    assert before_inventory == after_inventory
