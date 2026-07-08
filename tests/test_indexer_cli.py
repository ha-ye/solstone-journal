# SPDX-License-Identifier: AGPL-3.0-only
# Copyright (c) 2026 sol pbc

"""Tests for the journal indexer CLI."""

from __future__ import annotations

from pathlib import Path

import pytest

import solstone.think.utils as think_utils
from solstone.think.indexer import cli as indexer_cli


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
