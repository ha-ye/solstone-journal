# SPDX-License-Identifier: AGPL-3.0-only
# Copyright (c) 2026 sol pbc

from __future__ import annotations

import argparse
import json
import sqlite3
from pathlib import Path
from types import SimpleNamespace

import pytest

from solstone.apps.observer.prune import result_exit_code, run_prune
from solstone.apps.observer.utils import (
    append_history_record,
    list_observers,
    load_history,
    revoke_observer_record,
    save_observer,
)
from solstone.observe import observer_cli
from solstone.observe.processing_record import (
    HANDLER_TRANSCRIBE,
    SCHEMA,
    STATE_EMPTY,
)
from solstone.think.indexer.journal import get_journal_index
from solstone.think.link.auth import AuthorizedClients
from solstone.think.link.paths import authorized_clients_path
from solstone.think.streams import (
    get_stream_state,
    read_segment_stream,
    write_segment_stream,
)
from solstone.think.utils import iter_segments


@pytest.fixture
def observer_cli_env(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    home = tmp_path / "home"
    journal = tmp_path / "journal"
    home.mkdir()
    journal.mkdir()
    monkeypatch.setattr(Path, "home", lambda: home)
    monkeypatch.setenv("HOME", str(home))
    monkeypatch.setenv("SOLSTONE_JOURNAL", str(journal))

    import solstone.convey.state as convey_state

    convey_state.journal_root = ""
    return SimpleNamespace(home=home, journal=journal)


def _observer(name: str = "archon", key: str = "existing-key-abcdef") -> dict:
    return {
        "key": key,
        "name": name,
        "created_at": 1,
        "last_seen": None,
        "last_segment": None,
        "enabled": True,
        "stats": {"segments_received": 0, "bytes_received": 0},
    }


PRUNE_DAY = "20250103"
PRUNE_STREAM = "field"
PRUNE_AUDIO = b"identical legacy audio bytes"
PRUNE_SCREEN = b"identical legacy screen bytes"


def _observer_for_stream(
    stream: str = PRUNE_STREAM, key: str = "field-key-abcdef"
) -> dict:
    record = _observer(name=stream, key=key)
    record["stream"] = stream
    return record


def _processing_row(size: int) -> str:
    return (
        json.dumps(
            {
                "raw": "audio.flac",
                "_solstone_processing": {
                    "schema": SCHEMA,
                    "state": STATE_EMPTY,
                    "handler": HANDLER_TRANSCRIBE,
                    "input_size": size,
                },
            }
        )
        + "\n"
    )


def _write_prune_segment(
    journal: Path,
    *,
    segment: str,
    seq: int,
    prev_segment: str | None,
    audio: bytes = PRUNE_AUDIO,
    screen: bytes = PRUNE_SCREEN,
    manifest: bool = False,
    marker: bool = True,
    unknown_file: bool = False,
    extra_manifest_content: bool = False,
    proof_only_audio: bool = False,
) -> Path:
    seg_dir = journal / "chronicle" / PRUNE_DAY / PRUNE_STREAM / segment
    seg_dir.mkdir(parents=True)
    if marker:
        write_segment_stream(
            seg_dir,
            PRUNE_STREAM,
            PRUNE_DAY if prev_segment else None,
            prev_segment,
            seq,
        )
    if not proof_only_audio:
        (seg_dir / "audio.flac").write_bytes(audio)
        (seg_dir / "audio.jsonl").write_text(
            json.dumps({"segment": segment, "text": f"transcript {segment}"}) + "\n",
            encoding="utf-8",
        )
    else:
        (seg_dir / "audio.jsonl").write_text(
            _processing_row(len(audio)),
            encoding="utf-8",
        )
    (seg_dir / "screen.mp4").write_bytes(screen)
    (seg_dir / "screen.jsonl").write_text(
        json.dumps({"segment": segment, "text": f"description {segment}"}) + "\n",
        encoding="utf-8",
    )
    (seg_dir / "events.jsonl").write_text(
        json.dumps({"event": segment}) + "\n",
        encoding="utf-8",
    )
    talents = seg_dir / "talents"
    talents.mkdir()
    (talents / "sense.json").write_text(
        json.dumps({"segment": segment}),
        encoding="utf-8",
    )
    if unknown_file:
        (seg_dir / "notes.txt").write_text("operator note", encoding="utf-8")
    manifest_files = None
    if manifest:
        manifest_files = {
            "audio.flac": {"sha256": _sha(audio), "size": len(audio)},
            "screen.mp4": {"sha256": _sha(screen), "size": len(screen)},
        }
        if extra_manifest_content:
            extra = b"unique manifest content"
            (seg_dir / "capture.bin").write_bytes(extra)
            manifest_files["capture.bin"] = {"sha256": _sha(extra), "size": len(extra)}
        (seg_dir / "ingest.json").write_text(
            json.dumps(
                {
                    "schema_version": 1,
                    "requested_segment": segment,
                    "files": manifest_files,
                }
            )
            + "\n",
            encoding="utf-8",
        )
    return seg_dir


def _sha(data: bytes) -> str:
    import hashlib

    return hashlib.sha256(data).hexdigest()


def _append_upload(prefix: str, segment: str, *, sha: str | None = None) -> None:
    append_history_record(
        prefix,
        PRUNE_DAY,
        {
            "ts": 1,
            "segment": segment,
            "stream": PRUNE_STREAM,
            "files": [
                {
                    "submitted": "audio.flac",
                    "written": "audio.flac",
                    "size": len(PRUNE_AUDIO),
                    "sha256": sha or _sha(PRUNE_AUDIO),
                }
            ],
        },
    )


def _write_stream_state(journal: Path, *, last_segment: str, seq: int = 9) -> None:
    streams = journal / "streams"
    streams.mkdir()
    (streams / f"{PRUNE_STREAM}.json").write_text(
        json.dumps(
            {
                "name": PRUNE_STREAM,
                "type": "observer",
                "host": "field-host",
                "platform": "linux",
                "created_at": 123,
                "last_day": PRUNE_DAY,
                "last_segment": last_segment,
                "seq": seq,
            }
        )
        + "\n",
        encoding="utf-8",
    )


def _insert_index_rows(journal: Path, *segments: str) -> None:
    conn, _db_path = get_journal_index(str(journal))
    try:
        for segment in segments:
            rel = f"{PRUNE_DAY}/{PRUNE_STREAM}/{segment}"
            conn.execute(
                "INSERT INTO files(path, mtime) VALUES (?, ?)", (f"{rel}/audio.flac", 1)
            )
            conn.execute(
                "INSERT INTO chunks(content, path, day, facet, agent, stream, idx, time_bucket) "
                "VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
                ("content", rel, PRUNE_DAY, "", "segment", PRUNE_STREAM, 0, "morning"),
            )
        conn.commit()
    finally:
        conn.close()


def _index_count(journal: Path, segment: str) -> int:
    db_path = journal / "indexer" / "journal.sqlite"
    conn = sqlite3.connect(db_path)
    try:
        rel = f"{PRUNE_DAY}/{PRUNE_STREAM}/{segment}"
        return conn.execute(
            "SELECT count(*) FROM chunks WHERE path = ? OR path LIKE ?",
            (rel, f"{rel}/%"),
        ).fetchone()[0]
    finally:
        conn.close()


def _build_legacy_prune_lattice(journal: Path, prefix: str) -> None:
    _write_prune_segment(
        journal, segment="080000_300", seq=1, prev_segment=None, audio=b"before"
    )
    _write_prune_segment(
        journal, segment="090000_300", seq=2, prev_segment="080000_300"
    )
    _write_prune_segment(
        journal, segment="090000_301", seq=3, prev_segment="090000_300"
    )
    _write_prune_segment(
        journal, segment="090000_302", seq=4, prev_segment="090000_301"
    )
    _write_prune_segment(
        journal,
        segment="090000_303",
        seq=5,
        prev_segment="090000_302",
        audio=b"near duplicate audio",
    )
    _write_prune_segment(
        journal,
        segment="090000_304",
        seq=6,
        prev_segment="090000_303",
        unknown_file=True,
    )
    _write_prune_segment(
        journal, segment="100000_300", seq=7, prev_segment="090000_304"
    )
    _write_prune_segment(
        journal,
        segment="100500_300",
        seq=8,
        prev_segment="100000_300",
        audio=PRUNE_AUDIO,
        screen=PRUNE_SCREEN,
    )
    for segment in (
        "090000_300",
        "090000_301",
        "090000_302",
        "090000_303",
        "090000_304",
        "110000_300",
    ):
        _append_upload(prefix, segment)
    _write_stream_state(journal, last_segment="100500_300")
    _insert_index_rows(journal, "090000_301", "090000_302")


def _observer_with_stats(
    *,
    name: str,
    key: str,
    created_at: int,
    segments_received: int,
    bytes_received: int,
    duplicates_rejected: int = 0,
) -> dict:
    record = _observer(name=name, key=key)
    record["created_at"] = created_at
    record["stats"] = {
        "segments_received": segments_received,
        "bytes_received": bytes_received,
        "duplicates_rejected": duplicates_rejected,
    }
    return record


def test_create_observer_record_reuses_existing_without_create_side_effects(
    observer_cli_env,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    existing = _observer()
    assert save_observer(existing)
    monkeypatch.setattr(
        observer_cli,
        "_generate_key",
        lambda: pytest.fail("reuse must not generate a new key"),
    )
    monkeypatch.setattr(
        observer_cli,
        "save_observer",
        lambda _data: pytest.fail("reuse must not save"),
    )
    monkeypatch.setattr(
        observer_cli,
        "log_app_action",
        lambda **_kwargs: pytest.fail("reuse must not log observer_create"),
    )

    record, key, reused = observer_cli.create_observer_record(
        "archon", reuse_existing=True
    )

    assert record["key"] == existing["key"]
    assert record["name"] == existing["name"]
    assert record["filename_prefix"] == "existing"
    assert key == "existing-key-abcdef"
    assert reused is True
    assert list_observers() == [record]


def test_create_observer_record_fresh_create_returns_reused_false_and_logs(
    observer_cli_env,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    logs = []
    monkeypatch.setattr(observer_cli, "_generate_key", lambda: "fresh-key-abcdef")
    monkeypatch.setattr(
        observer_cli, "log_app_action", lambda **kwargs: logs.append(kwargs)
    )

    record, key, reused = observer_cli.create_observer_record("archon")

    assert key == "fresh-key-abcdef"
    assert reused is False
    assert record["name"] == "archon"
    assert list_observers()[0]["key"] == "fresh-key-abcdef"
    assert logs == [
        {
            "app": "observer",
            "facet": None,
            "action": "observer_create",
            "params": {"name": "archon", "key_prefix": "fresh-ke"},
        }
    ]


def test_create_observer_record_duplicate_without_reuse_still_fails(
    observer_cli_env,
) -> None:
    assert save_observer(_observer())

    with pytest.raises(ValueError, match="observer already exists: archon"):
        observer_cli.create_observer_record("archon")


def test_cmd_create_duplicate_without_reuse_exits_one(
    observer_cli_env,
    capsys: pytest.CaptureFixture[str],
) -> None:
    assert save_observer(_observer())
    args = argparse.Namespace(
        name="archon",
        json_output=False,
        reuse_existing=False,
    )

    rc = observer_cli.cmd_create(args)

    captured = capsys.readouterr()
    assert rc == 1
    assert captured.out == ""
    assert captured.err == "Error: observer 'archon' already exists\n"


def test_cmd_create_reuse_existing_json_shape(
    observer_cli_env,
    capsys: pytest.CaptureFixture[str],
) -> None:
    existing = _observer()
    assert save_observer(existing)
    args = argparse.Namespace(
        name="archon",
        json_output=True,
        reuse_existing=True,
    )

    rc = observer_cli.cmd_create(args)

    captured = capsys.readouterr()
    assert rc == 0
    assert captured.err == ""
    assert captured.out == (
        json.dumps(
            {
                "name": "archon",
                "key": "existing-key-abcdef",
                "prefix": "existing",
            }
        )
        + "\n"
    )


def test_cmd_create_reuse_existing_human_header(
    observer_cli_env,
    capsys: pytest.CaptureFixture[str],
) -> None:
    existing = _observer()
    assert save_observer(existing)
    args = argparse.Namespace(
        name="archon",
        json_output=False,
        reuse_existing=True,
    )

    rc = observer_cli.cmd_create(args)

    captured = capsys.readouterr()
    assert rc == 0
    assert captured.err == ""
    assert "Reusing existing observer:" in captured.out
    assert "Observer created:" not in captured.out
    assert "  api key:     existing-key-abcdef" in captured.out


def test_cmd_create_reuse_existing_creates_normally_when_absent(
    observer_cli_env,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    logs = []
    monkeypatch.setattr(observer_cli, "_generate_key", lambda: "fresh-key-abcdef")
    monkeypatch.setattr(
        observer_cli, "log_app_action", lambda **kwargs: logs.append(kwargs)
    )
    args = argparse.Namespace(
        name="archon",
        json_output=False,
        reuse_existing=True,
    )

    rc = observer_cli.cmd_create(args)

    captured = capsys.readouterr()
    assert rc == 0
    assert captured.err == ""
    assert "Observer created:" in captured.out
    assert "Reusing existing observer:" not in captured.out
    assert "  api key:     fresh-key-abcdef" in captured.out
    assert list_observers()[0]["key"] == "fresh-key-abcdef"
    assert logs == [
        {
            "app": "observer",
            "facet": None,
            "action": "observer_create",
            "params": {"name": "archon", "key_prefix": "fresh-ke"},
        }
    ]


def test_reconcile_collapses_duplicates_oldest_survives(observer_cli_env) -> None:
    assert save_observer(
        _observer_with_stats(
            name="fedora.tmux",
            key="newest03-key",
            created_at=3,
            segments_received=5,
            bytes_received=100,
            duplicates_rejected=1,
        )
    )
    assert save_observer(
        _observer_with_stats(
            name="fedora.tmux",
            key="oldest01-key",
            created_at=1,
            segments_received=7,
            bytes_received=200,
            duplicates_rejected=2,
        )
    )
    assert save_observer(
        _observer_with_stats(
            name="fedora.tmux",
            key="middle02-key",
            created_at=2,
            segments_received=11,
            bytes_received=300,
        )
    )
    lone = _observer_with_stats(
        name="fedora",
        key="desktop1-key",
        created_at=4,
        segments_received=13,
        bytes_received=400,
        duplicates_rejected=5,
    )
    assert save_observer(lone)

    plan = observer_cli.reconcile_observers(dry_run=False)

    assert plan == [
        {
            "name": "fedora.tmux",
            "survivor_prefix": "oldest01",
            "revoked_prefixes": ["newest03", "middle02"],
            "stats": {
                "segments_received": 23,
                "bytes_received": 600,
                "duplicates_rejected": 3,
            },
        }
    ]
    records = list_observers()
    tmux_records = [record for record in records if record["name"] == "fedora.tmux"]
    unrevoked_tmux = [
        record for record in tmux_records if not record.get("revoked", False)
    ]
    assert len(unrevoked_tmux) == 1
    assert unrevoked_tmux[0]["created_at"] == 1
    assert unrevoked_tmux[0]["stats"] == {
        "segments_received": 23,
        "bytes_received": 600,
        "duplicates_rejected": 3,
    }
    revoked_tmux = [record for record in tmux_records if record.get("revoked", False)]
    assert {record["created_at"] for record in revoked_tmux} == {2, 3}
    lone_record = next(record for record in records if record["name"] == "fedora")
    assert lone_record.get("revoked", False) is False
    assert lone_record["stats"] == lone["stats"]


def test_reconcile_dry_run_mutates_nothing(observer_cli_env) -> None:
    assert save_observer(
        _observer_with_stats(
            name="fedora.tmux",
            key="newest03-key",
            created_at=3,
            segments_received=5,
            bytes_received=100,
            duplicates_rejected=1,
        )
    )
    assert save_observer(
        _observer_with_stats(
            name="fedora.tmux",
            key="oldest01-key",
            created_at=1,
            segments_received=7,
            bytes_received=200,
            duplicates_rejected=2,
        )
    )
    assert save_observer(
        _observer_with_stats(
            name="fedora.tmux",
            key="middle02-key",
            created_at=2,
            segments_received=11,
            bytes_received=300,
        )
    )
    observers_dir = observer_cli_env.journal / "apps" / "observer" / "observers"
    before = {path.name: path.read_bytes() for path in observers_dir.glob("*.json")}

    plan = observer_cli.reconcile_observers(dry_run=True)

    assert plan == [
        {
            "name": "fedora.tmux",
            "survivor_prefix": "oldest01",
            "revoked_prefixes": ["newest03", "middle02"],
            "stats": {
                "segments_received": 23,
                "bytes_received": 600,
                "duplicates_rejected": 3,
            },
        }
    ]
    after = {path.name: path.read_bytes() for path in observers_dir.glob("*.json")}
    assert after == before


def test_reconcile_lone_stream_returns_empty_plan(observer_cli_env) -> None:
    lone = _observer_with_stats(
        name="fedora",
        key="desktop1-key",
        created_at=1,
        segments_received=13,
        bytes_received=400,
        duplicates_rejected=5,
    )
    assert save_observer(lone)

    plan = observer_cli.reconcile_observers(dry_run=False)

    assert plan == []
    records = list_observers()
    assert len(records) == 1
    assert records[0].get("revoked", False) is False
    assert records[0]["stats"] == lone["stats"]


def test_cmd_reconcile_reports_plan(
    observer_cli_env,
    capsys: pytest.CaptureFixture[str],
) -> None:
    assert save_observer(
        _observer_with_stats(
            name="fedora.tmux",
            key="newest03-key",
            created_at=3,
            segments_received=5,
            bytes_received=100,
        )
    )
    assert save_observer(
        _observer_with_stats(
            name="fedora.tmux",
            key="oldest01-key",
            created_at=1,
            segments_received=7,
            bytes_received=200,
        )
    )

    rc = observer_cli.cmd_reconcile(
        argparse.Namespace(dry_run=False, json_output=False)
    )

    captured = capsys.readouterr()
    assert rc == 0
    assert captured.err == ""
    assert "Reconciled stream 'fedora.tmux':" in captured.out
    assert "  survivor:  oldest01" in captured.out
    assert "  revoking:  newest03" in captured.out


def test_cmd_reconcile_no_duplicates(
    observer_cli_env,
    capsys: pytest.CaptureFixture[str],
) -> None:
    assert save_observer(_observer(name="fedora", key="desktop1-key"))

    rc = observer_cli.cmd_reconcile(
        argparse.Namespace(dry_run=False, json_output=False)
    )

    captured = capsys.readouterr()
    assert rc == 0
    assert captured.err == ""
    assert captured.out == "No duplicate observer streams to reconcile.\n"


def test_cmd_list_json_includes_prefix_and_status(
    observer_cli_env,
    capsys: pytest.CaptureFixture[str],
) -> None:
    assert save_observer(_observer(name="desktop", key="abcdefgh12345678"))
    args = argparse.Namespace(json_output=True)

    rc = observer_cli.cmd_list(args)

    captured = capsys.readouterr()
    assert rc == 0
    rows = {row["name"]: row for row in json.loads(captured.out)}
    assert rows["desktop"]["prefix"] == "abcdefgh"
    assert rows["desktop"]["status"] == "disconnected"
    assert "mode" not in rows["desktop"]


def test_cmd_list_human_shows_prefix_column(
    observer_cli_env,
    capsys: pytest.CaptureFixture[str],
) -> None:
    assert save_observer(_observer(name="desktop", key="abcdefgh12345678"))
    args = argparse.Namespace(json_output=False)

    rc = observer_cli.cmd_list(args)

    captured = capsys.readouterr()
    assert rc == 0
    assert "Name                 Prefix" in captured.out
    assert "Mode" not in captured.out
    assert "desktop              abcdefgh" in captured.out


def test_cmd_status_single_reports_prefix(
    observer_cli_env,
    capsys: pytest.CaptureFixture[str],
) -> None:
    assert save_observer(_observer(name="desktop", key="cdefghij12345678"))

    rc = observer_cli.cmd_status(
        argparse.Namespace(identifier="desktop", json_output=True)
    )

    captured = capsys.readouterr()
    assert rc == 0
    payload = json.loads(captured.out)
    assert payload["prefix"] == "cdefghij"
    assert payload["status"] == "disconnected"
    assert "mode" not in payload


def test_cmd_status_all_table_shows_prefix(
    observer_cli_env,
    capsys: pytest.CaptureFixture[str],
) -> None:
    assert save_observer(_observer(name="desktop", key="abcdefgh12345678"))

    rc = observer_cli.cmd_status(argparse.Namespace(identifier=None, json_output=False))

    captured = capsys.readouterr()
    assert rc == 0
    assert "Name                 Prefix" in captured.out
    assert "Mode" not in captured.out
    assert "desktop              abcdefgh" in captured.out


def test_revoke_dl_observer_leaves_authorized_clients_untouched(
    observer_cli_env,
) -> None:
    assert save_observer(_observer(name="desktop", key="abcdefgh12345678"))
    fingerprint = "sha256:" + ("f" * 64)
    authorized = AuthorizedClients(authorized_clients_path())
    authorized.add(
        fingerprint,
        "phone",
        "inst-1",
        paired_at="2026-05-20T00:00:00Z",
    )
    before = authorized_clients_path().read_bytes()

    record = revoke_observer_record("desktop")

    assert record["revoked"] is True
    assert authorized_clients_path().read_bytes() == before
    assert (
        AuthorizedClients(authorized_clients_path()).is_authorized(fingerprint) is True
    )


def test_prune_dry_run_lists_legacy_duplicates_and_writes_nothing(
    observer_cli_env,
) -> None:
    observer = _observer_for_stream()
    assert save_observer(observer)
    prefix = observer["key"][:8]
    _build_legacy_prune_lattice(observer_cli_env.journal, prefix)

    before_history = load_history(prefix, PRUNE_DAY)
    result = run_prune(days=[PRUNE_DAY], stream=PRUNE_STREAM, execute=False)

    assert len(result.groups) == 1
    assert [c.analysis.segment for c in result.groups[0].candidates] == [
        "090000_301",
        "090000_302",
    ]
    assert {refusal.gate for refusal in result.refusals} == {
        "content-identity",
        "derived-output",
    }
    assert (
        observer_cli_env.journal / "chronicle" / PRUNE_DAY / PRUNE_STREAM / "090000_301"
    ).is_dir()
    assert load_history(prefix, PRUNE_DAY) == before_history
    assert not (observer_cli_env.journal / "chronicle" / PRUNE_DAY / "health").exists()
    assert _index_count(observer_cli_env.journal, "090000_301") == 1


def test_prune_execute_deletes_duplicates_repairs_chain_history_and_index(
    observer_cli_env,
) -> None:
    observer = _observer_for_stream()
    assert save_observer(observer)
    prefix = observer["key"][:8]
    _build_legacy_prune_lattice(observer_cli_env.journal, prefix)
    canonical = (
        observer_cli_env.journal / "chronicle" / PRUNE_DAY / PRUNE_STREAM / "090000_300"
    )
    canonical_hash = _sha((canonical / "audio.flac").read_bytes())

    result = run_prune(days=[PRUNE_DAY], stream=PRUNE_STREAM, execute=True)

    assert {candidate.analysis.segment for candidate in result.deleted} == {
        "090000_301",
        "090000_302",
    }
    assert result.refusals
    assert (canonical / "audio.flac").is_file()
    assert _sha((canonical / "audio.flac").read_bytes()) == canonical_hash
    assert not (
        observer_cli_env.journal / "chronicle" / PRUNE_DAY / PRUNE_STREAM / "090000_301"
    ).exists()
    assert not (
        observer_cli_env.journal / "chronicle" / PRUNE_DAY / PRUNE_STREAM / "090000_302"
    ).exists()
    history = load_history(prefix, PRUNE_DAY)
    pruned = [row for row in history if row.get("type") == "pruned"]
    assert {row["segment"] for row in pruned} == {"090000_301", "090000_302"}
    assert all(row["duplicate_of"] == "090000_300" for row in pruned)
    assert (
        read_segment_stream(
            observer_cli_env.journal
            / "chronicle"
            / PRUNE_DAY
            / PRUNE_STREAM
            / "090000_303"
        )["prev_segment"]
        == "090000_300"
    )
    assert _index_count(observer_cli_env.journal, "090000_301") == 0
    assert _index_count(observer_cli_env.journal, "090000_302") == 0
    assert (
        observer_cli_env.journal / "chronicle" / PRUNE_DAY / "health" / "stream.updated"
    ).exists()

    state = get_stream_state(PRUNE_STREAM)
    assert state["type"] == "observer"
    assert state["host"] == "field-host"
    assert state["platform"] == "linux"
    assert state["created_at"] == 123
    assert state["last_segment"] == "100500_300"
    assert state["seq"] == 9

    existing = {
        segment
        for _stream, segment, _path in iter_segments(PRUNE_DAY)
        if _stream == PRUNE_STREAM
    }
    for _stream, _segment, path in iter_segments(PRUNE_DAY):
        marker = read_segment_stream(path)
        if marker and marker.get("prev_segment"):
            assert marker["prev_segment"] in existing


def test_prune_same_start_grouping_does_not_delete_different_start_identical_bytes(
    observer_cli_env,
) -> None:
    observer = _observer_for_stream()
    assert save_observer(observer)
    prefix = observer["key"][:8]
    _write_prune_segment(
        observer_cli_env.journal,
        segment="120000_300",
        seq=1,
        prev_segment=None,
    )
    _write_prune_segment(
        observer_cli_env.journal,
        segment="120500_300",
        seq=2,
        prev_segment="120000_300",
    )
    _append_upload(prefix, "120000_300")
    _append_upload(prefix, "120500_300")
    _write_stream_state(observer_cli_env.journal, last_segment="120500_300", seq=2)

    result = run_prune(days=[PRUNE_DAY], stream=PRUNE_STREAM, execute=True)

    assert result.groups == []
    assert result.deleted == []
    assert result.refusals == []
    assert (
        observer_cli_env.journal / "chronicle" / PRUNE_DAY / PRUNE_STREAM / "120500_300"
    ).is_dir()


def test_prune_near_duplicate_media_bytes_refuse_content_identity(
    observer_cli_env,
) -> None:
    observer = _observer_for_stream()
    assert save_observer(observer)
    prefix = observer["key"][:8]
    _write_prune_segment(
        observer_cli_env.journal,
        segment="125000_300",
        seq=1,
        prev_segment=None,
    )
    _write_prune_segment(
        observer_cli_env.journal,
        segment="125000_301",
        seq=2,
        prev_segment="125000_300",
    )
    _write_prune_segment(
        observer_cli_env.journal,
        segment="125000_302",
        seq=3,
        prev_segment="125000_301",
        audio=b"near duplicate audio bytes",
    )
    _append_upload(prefix, "125000_300")
    _append_upload(prefix, "125000_301")
    _append_upload(prefix, "125000_302", sha=_sha(b"near duplicate audio bytes"))
    _write_stream_state(observer_cli_env.journal, last_segment="125000_302", seq=3)

    result = run_prune(days=[PRUNE_DAY], stream=PRUNE_STREAM, execute=True)

    assert [candidate.analysis.segment for candidate in result.deleted] == [
        "125000_301"
    ]
    refusal = next(
        refusal
        for refusal in result.refusals
        if refusal.subject.endswith("/125000_302")
    )
    assert refusal.gate == "content-identity"
    assert refusal.file == "audio.flac"
    assert "compared to canonical 125000_300" in refusal.resolution
    assert (
        observer_cli_env.journal / "chronicle" / PRUNE_DAY / PRUNE_STREAM / "125000_302"
    ).is_dir()


def test_prune_markerless_candidate_refuses(
    observer_cli_env,
) -> None:
    observer = _observer_for_stream()
    assert save_observer(observer)
    prefix = observer["key"][:8]
    _write_prune_segment(
        observer_cli_env.journal,
        segment="130000_300",
        seq=1,
        prev_segment=None,
    )
    _write_prune_segment(
        observer_cli_env.journal,
        segment="130000_301",
        seq=2,
        prev_segment="130000_300",
        marker=False,
    )
    _append_upload(prefix, "130000_300")
    _append_upload(prefix, "130000_301")
    _write_stream_state(observer_cli_env.journal, last_segment="130000_301", seq=2)

    result = run_prune(days=[PRUNE_DAY], stream=PRUNE_STREAM, execute=True)

    assert result.deleted == []
    assert any(refusal.gate == "chain-identity" for refusal in result.refusals)
    assert (
        observer_cli_env.journal / "chronicle" / PRUNE_DAY / PRUNE_STREAM / "130000_301"
    ).is_dir()


def test_prune_unrecognized_derived_file_refuses_legacy_and_manifest_candidates(
    observer_cli_env,
) -> None:
    observer = _observer_for_stream()
    assert save_observer(observer)
    prefix = observer["key"][:8]
    _write_prune_segment(
        observer_cli_env.journal,
        segment="132000_300",
        seq=1,
        prev_segment=None,
    )
    _write_prune_segment(
        observer_cli_env.journal,
        segment="132000_301",
        seq=2,
        prev_segment="132000_300",
        unknown_file=True,
    )
    _write_prune_segment(
        observer_cli_env.journal,
        segment="132500_300",
        seq=3,
        prev_segment="132000_301",
        manifest=True,
    )
    _write_prune_segment(
        observer_cli_env.journal,
        segment="132500_301",
        seq=4,
        prev_segment="132500_300",
        manifest=True,
        unknown_file=True,
    )
    for segment in ("132000_300", "132000_301", "132500_300", "132500_301"):
        _append_upload(prefix, segment)
    _write_stream_state(observer_cli_env.journal, last_segment="132500_301", seq=4)

    result = run_prune(days=[PRUNE_DAY], stream=PRUNE_STREAM, execute=True)

    refusals = {
        refusal.subject.rsplit("/", 1)[1]: refusal
        for refusal in result.refusals
        if refusal.gate == "derived-output"
    }
    assert set(refusals) == {"132000_301", "132500_301"}
    for refusal in refusals.values():
        assert refusal.file == "notes.txt"
        assert "remove the file" in refusal.resolution
    assert result.deleted == []
    assert (
        observer_cli_env.journal / "chronicle" / PRUNE_DAY / PRUNE_STREAM / "132000_301"
    ).is_dir()
    assert (
        observer_cli_env.journal / "chronicle" / PRUNE_DAY / PRUNE_STREAM / "132500_301"
    ).is_dir()


def test_prune_manifest_extra_content_refuses_as_content_identity(
    observer_cli_env,
) -> None:
    observer = _observer_for_stream()
    assert save_observer(observer)
    prefix = observer["key"][:8]
    _write_prune_segment(
        observer_cli_env.journal,
        segment="135000_300",
        seq=1,
        prev_segment=None,
        manifest=True,
    )
    _write_prune_segment(
        observer_cli_env.journal,
        segment="135000_301",
        seq=2,
        prev_segment="135000_300",
        manifest=True,
    )
    _write_prune_segment(
        observer_cli_env.journal,
        segment="135000_302",
        seq=3,
        prev_segment="135000_301",
        manifest=True,
        extra_manifest_content=True,
    )
    _append_upload(prefix, "135000_300")
    _append_upload(prefix, "135000_301")
    _append_upload(prefix, "135000_302")
    _write_stream_state(observer_cli_env.journal, last_segment="135000_302", seq=3)

    result = run_prune(days=[PRUNE_DAY], stream=PRUNE_STREAM, execute=True)

    assert [candidate.analysis.segment for candidate in result.deleted] == [
        "135000_301"
    ]
    refusal = next(
        refusal
        for refusal in result.refusals
        if refusal.subject.endswith("/135000_302")
    )
    assert refusal.gate == "content-identity"
    assert refusal.file == "capture.bin"
    assert (
        observer_cli_env.journal / "chronicle" / PRUNE_DAY / PRUNE_STREAM / "135000_302"
    ).is_dir()


def test_prune_refuses_when_canonical_heldness_becomes_unverifiable(
    observer_cli_env,
) -> None:
    observer = _observer_for_stream()
    assert save_observer(observer)
    prefix = observer["key"][:8]
    _write_prune_segment(
        observer_cli_env.journal,
        segment="140000_300",
        seq=1,
        prev_segment=None,
        manifest=True,
    )
    _write_prune_segment(
        observer_cli_env.journal,
        segment="140000_301",
        seq=2,
        prev_segment="140000_300",
        manifest=True,
    )
    _append_upload(prefix, "140000_300")
    _append_upload(prefix, "140000_301")
    _write_stream_state(observer_cli_env.journal, last_segment="140000_301", seq=2)
    dry = run_prune(days=[PRUNE_DAY], stream=PRUNE_STREAM, execute=False)
    assert len(dry.groups) == 1
    assert dry.refusals == []
    assert [candidate.analysis.segment for candidate in dry.groups[0].candidates] == [
        "140000_301"
    ]
    canonical_audio = (
        observer_cli_env.journal
        / "chronicle"
        / PRUNE_DAY
        / PRUNE_STREAM
        / "140000_300"
        / "audio.flac"
    )
    canonical_audio.unlink()

    result = run_prune(days=[PRUNE_DAY], stream=PRUNE_STREAM, execute=True)

    assert result.deleted == []
    assert result_exit_code(result) == 2
    assert any(refusal.gate == "canonical-heldness" for refusal in result.refusals)
    assert (
        observer_cli_env.journal / "chronicle" / PRUNE_DAY / PRUNE_STREAM / "140000_301"
    ).is_dir()


def test_prune_last_physical_copy_is_labeled_in_dry_run_and_execute(
    observer_cli_env,
) -> None:
    observer = _observer_for_stream()
    assert save_observer(observer)
    prefix = observer["key"][:8]
    _write_prune_segment(
        observer_cli_env.journal,
        segment="150000_300",
        seq=1,
        prev_segment=None,
        manifest=True,
        proof_only_audio=True,
    )
    _write_prune_segment(
        observer_cli_env.journal,
        segment="150000_301",
        seq=2,
        prev_segment="150000_300",
        manifest=True,
    )
    _append_upload(prefix, "150000_300")
    _append_upload(prefix, "150000_301")
    _write_stream_state(observer_cli_env.journal, last_segment="150000_301", seq=2)

    dry = run_prune(days=[PRUNE_DAY], stream=PRUNE_STREAM, execute=False)
    assert dry.last_physical_copy_count == 1
    assert dry.groups[0].candidates[0].last_physical_copy is True

    result = run_prune(days=[PRUNE_DAY], stream=PRUNE_STREAM, execute=True)

    assert result.last_physical_copy_count == 1
    assert result.deleted[0].last_physical_copy is True


def test_prune_crash_rerun_repairs_pruned_dangling_prev_and_refuses_unexplained(
    observer_cli_env,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    observer = _observer_for_stream()
    assert save_observer(observer)
    prefix = observer["key"][:8]
    _write_prune_segment(
        observer_cli_env.journal,
        segment="160000_300",
        seq=1,
        prev_segment=None,
    )
    _write_prune_segment(
        observer_cli_env.journal,
        segment="160000_301",
        seq=2,
        prev_segment="160000_300",
    )
    _write_prune_segment(
        observer_cli_env.journal,
        segment="160500_300",
        seq=3,
        prev_segment="160000_301",
        audio=b"after",
    )
    _append_upload(prefix, "160000_300")
    _append_upload(prefix, "160000_301")
    _write_stream_state(observer_cli_env.journal, last_segment="160500_300", seq=3)

    from solstone.apps.observer import prune

    def fail_repair(*_args, **_kwargs):
        raise RuntimeError("interrupt after delete")

    with monkeypatch.context() as patch:
        patch.setattr(
            prune, "repair_crash_leftovers", lambda *_args, **_kwargs: ([], 0)
        )
        patch.setattr(prune, "repair_stream_chain", fail_repair)
        with pytest.raises(RuntimeError, match="interrupt after delete"):
            run_prune(days=[PRUNE_DAY], stream=PRUNE_STREAM, execute=True)
    assert not (
        observer_cli_env.journal / "chronicle" / PRUNE_DAY / PRUNE_STREAM / "160000_301"
    ).exists()
    assert any(
        row.get("type") == "pruned" and row.get("segment") == "160000_301"
        for row in load_history(prefix, PRUNE_DAY)
    )

    result = run_prune(days=[PRUNE_DAY], stream=PRUNE_STREAM, execute=True)
    assert result.refusals == []
    assert (
        read_segment_stream(
            observer_cli_env.journal
            / "chronicle"
            / PRUNE_DAY
            / PRUNE_STREAM
            / "160500_300"
        )["prev_segment"]
        == "160000_300"
    )

    unexplained = (
        observer_cli_env.journal / "chronicle" / PRUNE_DAY / PRUNE_STREAM / "160000_300"
    )
    __import__("shutil").rmtree(unexplained)
    bad = run_prune(days=[PRUNE_DAY], stream=PRUNE_STREAM, execute=True)
    assert any(refusal.gate == "chain-repair" for refusal in bad.refusals)


def test_prune_observer_attribution_refuses_no_owner_and_ambiguous_owner(
    observer_cli_env,
) -> None:
    _write_prune_segment(
        observer_cli_env.journal,
        segment="170000_300",
        seq=1,
        prev_segment=None,
    )
    _write_prune_segment(
        observer_cli_env.journal,
        segment="170000_301",
        seq=2,
        prev_segment="170000_300",
    )
    _write_stream_state(observer_cli_env.journal, last_segment="170000_301", seq=2)

    no_owner = run_prune(days=[PRUNE_DAY], stream=PRUNE_STREAM, execute=True)
    assert any(refusal.gate == "observer-attribution" for refusal in no_owner.refusals)

    first = _observer_for_stream(key="field-one-key")
    second = _observer_for_stream(key="field-two-key")
    assert save_observer(first)
    assert save_observer(second)
    ambiguous = run_prune(days=[PRUNE_DAY], stream=PRUNE_STREAM, execute=True)
    assert any(
        "multiple active observers" in refusal.resolution
        for refusal in ambiguous.refusals
    )
