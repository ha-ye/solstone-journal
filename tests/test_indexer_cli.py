# SPDX-License-Identifier: AGPL-3.0-only
# Copyright (c) 2026 sol pbc

"""Tests for the journal indexer CLI."""

from __future__ import annotations

import runpy
from pathlib import Path

import pytest

import solstone.think.utils as think_utils
from solstone.think.indexer import cli as indexer_cli


def test_module_entrypoint_propagates_main_return(monkeypatch):
    monkeypatch.setattr(indexer_cli, "main", lambda: 75)

    with pytest.raises(SystemExit) as exc_info:
        runpy.run_module("solstone.think.indexer.__main__", run_name="__main__")

    assert exc_info.value.code == 75


def _run_indexer_cli(monkeypatch, journal: Path, args: list[str]) -> None:
    monkeypatch.setenv("SOLSTONE_JOURNAL", str(journal))
    think_utils._journal_path_cache = None
    monkeypatch.setattr(indexer_cli, "get_journal", lambda: str(journal))
    monkeypatch.setattr(indexer_cli, "require_solstone", lambda: None)

    def setup_cli(parser):
        parsed = parser.parse_args(args)
        if not hasattr(parsed, "verbose"):
            parsed.verbose = False
        return parsed

    monkeypatch.setattr(indexer_cli, "setup_cli", setup_cli)
    indexer_cli.main()


def test_rescan_file_does_not_create_root_task_log(tmp_path, monkeypatch):
    journal = tmp_path / "journal"
    journal.mkdir()
    indexed: list[tuple[str, str, bool]] = []

    def index_file(journal_arg: str, path_arg: str, *, verbose: bool = False) -> bool:
        indexed.append((journal_arg, path_arg, verbose))
        return True

    monkeypatch.setattr(indexer_cli, "index_file", index_file)

    _run_indexer_cli(monkeypatch, journal, ["--rescan-file", "chronicle/today.md"])

    assert indexed == [(str(journal), "chronicle/today.md", False)]
    assert not (journal / "task_log.txt").exists()


def test_rebuild_edges_does_not_create_root_task_log(tmp_path, monkeypatch):
    journal = tmp_path / "journal"
    journal.mkdir()
    rebuilt: list[str] = []

    def rebuild_edges(journal_arg: str) -> dict[str, int]:
        rebuilt.append(journal_arg)
        return {"files": 1, "rows": 2, "drops": 0, "failed": 0}

    monkeypatch.setattr(indexer_cli, "rebuild_edges", rebuild_edges)

    _run_indexer_cli(monkeypatch, journal, ["--rebuild-edges"])

    assert rebuilt == [str(journal)]
    assert not (journal / "task_log.txt").exists()


def test_rescan_file_with_rescan_errors_after_reset_and_rebuild(
    tmp_path,
    monkeypatch,
    capsys,
):
    journal = tmp_path / "journal"
    (journal / "config").mkdir(parents=True)
    (journal / "config" / "journal.json").write_text(
        '{"core": {"indexer": "rust"}}\n',
        encoding="utf-8",
    )
    calls: list[tuple[str, str]] = []

    def reset_journal_index(journal_arg: str) -> None:
        calls.append(("reset", journal_arg))

    def rebuild_edges(journal_arg: str) -> dict[str, int]:
        calls.append(("rebuild_edges", journal_arg))
        return {"files": 1, "rows": 2, "drops": 0, "failed": 0}

    monkeypatch.setattr(indexer_cli, "reset_journal_index", reset_journal_index)
    monkeypatch.setattr(indexer_cli, "rebuild_edges", rebuild_edges)
    monkeypatch.setattr(
        indexer_cli,
        "index_file",
        lambda *args, **kwargs: pytest.fail("index_file should not run"),
    )
    monkeypatch.setattr(
        indexer_cli,
        "scan_journal",
        lambda *args, **kwargs: pytest.fail("scan_journal should not run"),
    )

    with pytest.raises(SystemExit) as exc_info:
        _run_indexer_cli(
            monkeypatch,
            journal,
            [
                "--reset",
                "--rebuild-edges",
                "--rescan",
                "--rescan-file",
                "chronicle/today.md",
            ],
        )

    assert exc_info.value.code == 2
    assert calls == [("reset", str(journal)), ("rebuild_edges", str(journal))]
    assert (
        "--rescan-file cannot be used with --rescan or --rescan-full"
        in capsys.readouterr().err
    )


def test_native_decline_fallback_runs_python_write_blocks(tmp_path, monkeypatch):
    journal = tmp_path / "journal"
    journal.mkdir()
    seam_calls: list[tuple[str, bool, bool, bool]] = []
    calls: list[tuple[str, str] | tuple[str, str, bool, bool]] = []

    def fallback_after_native_decline(args, journal_arg: str) -> None:
        seam_calls.append(
            (journal_arg, args.reset, args.rebuild_edges, args.rescan_full)
        )
        return None

    def reset_journal_index(journal_arg: str) -> None:
        calls.append(("reset", journal_arg))

    def rebuild_edges(journal_arg: str) -> dict[str, int]:
        calls.append(("rebuild_edges", journal_arg))
        return {"files": 1, "rows": 2, "drops": 0, "failed": 0}

    def scan_journal(
        journal_arg: str,
        *,
        verbose: bool = False,
        full: bool = False,
    ) -> bool:
        calls.append(("scan", journal_arg, verbose, full))
        return True

    monkeypatch.setattr(
        indexer_cli, "maybe_run_native_indexer", fallback_after_native_decline
    )
    monkeypatch.setattr(indexer_cli, "reset_journal_index", reset_journal_index)
    monkeypatch.setattr(indexer_cli, "rebuild_edges", rebuild_edges)
    monkeypatch.setattr(indexer_cli, "scan_journal", scan_journal)

    _run_indexer_cli(
        monkeypatch,
        journal,
        ["--reset", "--rebuild-edges", "--rescan-full"],
    )

    assert seam_calls == [(str(journal), True, True, True)]
    assert calls == [
        ("reset", str(journal)),
        ("rebuild_edges", str(journal)),
        ("scan", str(journal), False, True),
    ]


@pytest.mark.parametrize(
    ("args", "full"),
    [
        (["--rescan"], False),
        (["--rescan-full"], True),
    ],
)
def test_rescan_leaves_existing_root_task_log_unchanged(
    tmp_path,
    monkeypatch,
    args,
    full,
):
    journal = tmp_path / "journal"
    journal.mkdir()
    root_log = journal / "task_log.txt"
    original = b"123\tkeep\n"
    root_log.write_bytes(original)
    scans: list[tuple[str, bool, bool]] = []

    def scan_journal(journal_arg: str, *, verbose: bool = False, full: bool = False):
        scans.append((journal_arg, verbose, full))
        return True

    monkeypatch.setattr(indexer_cli, "scan_journal", scan_journal)

    _run_indexer_cli(monkeypatch, journal, args)

    assert scans == [(str(journal), False, full)]
    assert root_log.read_bytes() == original
