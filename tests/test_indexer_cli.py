# SPDX-License-Identifier: AGPL-3.0-only
# Copyright (c) 2026 sol pbc

"""Tests for the journal indexer CLI."""

from __future__ import annotations

import runpy
import subprocess
from pathlib import Path

import pytest

import solstone.think.utils as think_utils
from solstone.think import core_handshake
from solstone.think.indexer import cli as indexer_cli
from solstone.think.indexer import native_seam
from solstone.think.indexer.journal import ScanReport

EXPECTED_ZERO_EDGE_HINT = (
    "Zero edges indexed: edges are talent-derived, and the --rescan-full edge phase "
    "remains modification-time incremental — run journal indexer --rebuild-edges to "
    "force full edge re-extraction."
)


def test_module_entrypoint_propagates_main_return(monkeypatch):
    monkeypatch.setattr(indexer_cli, "main", lambda: 75)

    with pytest.raises(SystemExit) as exc_info:
        runpy.run_module("solstone.think.indexer.__main__", run_name="__main__")

    assert exc_info.value.code == 75


def _run_indexer_cli(
    monkeypatch,
    journal: Path,
    args: list[str],
    *,
    core_indexer: str | None = None,
) -> None:
    monkeypatch.setenv("SOLSTONE_JOURNAL", str(journal))
    think_utils._journal_path_cache = None
    monkeypatch.setattr(indexer_cli, "get_journal", lambda: str(journal))
    monkeypatch.setattr(indexer_cli, "require_solstone", lambda: None)
    config_path = journal / "config" / "journal.json"
    if core_indexer is not None and not config_path.exists():
        config_path.parent.mkdir(parents=True)
        config_path.write_text(
            f'{{"core": {{"indexer": "{core_indexer}"}}}}\n',
            encoding="utf-8",
        )

    def setup_cli(parser):
        parsed = parser.parse_args(args)
        if not hasattr(parsed, "verbose"):
            parsed.verbose = False
        return parsed

    monkeypatch.setattr(indexer_cli, "setup_cli", setup_cli)
    indexer_cli.main()


def test_rescan_full_zero_edge_rows_prints_hint(tmp_path, monkeypatch, capsys):
    journal = tmp_path / "journal"
    journal.mkdir()
    scans: list[tuple[str, bool, bool]] = []

    def scan_journal(
        journal_arg: str,
        *,
        verbose: bool = False,
        full: bool = False,
    ) -> ScanReport:
        scans.append((journal_arg, verbose, full))
        return ScanReport(changed=True, edge_rows_inserted=0)

    monkeypatch.setattr(indexer_cli, "scan_journal", scan_journal)

    _run_indexer_cli(monkeypatch, journal, ["--rescan-full"], core_indexer="python")

    captured = capsys.readouterr()
    assert scans == [(str(journal), False, True)]
    assert captured.out == EXPECTED_ZERO_EDGE_HINT + "\n"
    assert captured.err == ""


def test_rescan_full_nonzero_edge_rows_suppresses_hint(tmp_path, monkeypatch, capsys):
    journal = tmp_path / "journal"
    journal.mkdir()

    def scan_journal(
        _journal_arg: str,
        *,
        verbose: bool = False,
        full: bool = False,
    ) -> ScanReport:
        assert verbose is False
        assert full is True
        return ScanReport(changed=True, edge_rows_inserted=1)

    monkeypatch.setattr(indexer_cli, "scan_journal", scan_journal)

    _run_indexer_cli(monkeypatch, journal, ["--rescan-full"], core_indexer="python")

    captured = capsys.readouterr()
    assert captured.out == ""
    assert captured.err == ""


def test_rebuild_edges_rescan_full_suppresses_zero_edge_hint(
    tmp_path,
    monkeypatch,
    capsys,
):
    journal = tmp_path / "journal"
    journal.mkdir()
    calls: list[tuple[str, str] | tuple[str, str, bool, bool]] = []

    def rebuild_edges(journal_arg: str) -> dict[str, int]:
        calls.append(("rebuild_edges", journal_arg))
        return {"files": 0, "rows": 0, "drops": 0, "failed": 0}

    def scan_journal(
        journal_arg: str,
        *,
        verbose: bool = False,
        full: bool = False,
    ) -> ScanReport:
        calls.append(("scan", journal_arg, verbose, full))
        return ScanReport(changed=True, edge_rows_inserted=0)

    monkeypatch.setattr(indexer_cli, "rebuild_edges", rebuild_edges)
    monkeypatch.setattr(indexer_cli, "scan_journal", scan_journal)

    _run_indexer_cli(
        monkeypatch,
        journal,
        ["--rebuild-edges", "--rescan-full"],
        core_indexer="python",
    )

    captured = capsys.readouterr()
    assert calls == [
        ("rebuild_edges", str(journal)),
        ("scan", str(journal), False, True),
    ]
    assert captured.out == ""
    assert captured.err == ""


def test_reset_rescan_full_suppresses_zero_edge_hint(tmp_path, monkeypatch, capsys):
    journal = tmp_path / "journal"
    journal.mkdir()
    calls: list[tuple[str, str] | tuple[str, str, bool, bool]] = []

    def reset_journal_index(journal_arg: str) -> None:
        calls.append(("reset", journal_arg))

    def scan_journal(
        journal_arg: str,
        *,
        verbose: bool = False,
        full: bool = False,
    ) -> ScanReport:
        calls.append(("scan", journal_arg, verbose, full))
        return ScanReport(changed=True, edge_rows_inserted=0)

    monkeypatch.setattr(indexer_cli, "reset_journal_index", reset_journal_index)
    monkeypatch.setattr(indexer_cli, "scan_journal", scan_journal)

    _run_indexer_cli(
        monkeypatch,
        journal,
        ["--reset", "--rescan-full"],
        core_indexer="python",
    )

    captured = capsys.readouterr()
    assert calls == [
        ("reset", str(journal)),
        ("scan", str(journal), False, True),
    ]
    assert captured.out == ""
    assert captured.err == ""


def test_rescan_file_does_not_create_root_task_log(tmp_path, monkeypatch):
    journal = tmp_path / "journal"
    journal.mkdir()
    indexed: list[tuple[str, str, bool]] = []

    def index_file(journal_arg: str, path_arg: str, *, verbose: bool = False) -> bool:
        indexed.append((journal_arg, path_arg, verbose))
        return True

    monkeypatch.setattr(indexer_cli, "index_file", index_file)

    _run_indexer_cli(
        monkeypatch,
        journal,
        ["--rescan-file", "chronicle/today.md"],
        core_indexer="python",
    )

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

    _run_indexer_cli(monkeypatch, journal, ["--rebuild-edges"], core_indexer="python")

    assert rebuilt == [str(journal)]
    assert not (journal / "task_log.txt").exists()


@pytest.mark.parametrize("core_indexer", [None, "python", "rust"])
def test_rescan_file_with_rescan_errors_after_reset_and_rebuild(
    tmp_path,
    monkeypatch,
    capsys,
    core_indexer,
):
    journal = tmp_path / "journal"
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
            core_indexer=core_indexer,
        )

    assert exc_info.value.code == 2
    assert calls == [("reset", str(journal)), ("rebuild_edges", str(journal))]
    assert (
        "--rescan-file cannot be used with --rescan or --rescan-full"
        in capsys.readouterr().err
    )


def _raise_unexpected(name: str):
    def fail(*_args, **_kwargs):
        raise AssertionError(f"{name} should not be called")

    return fail


def test_cli_covered_absent_indexer_routes_write_to_native(
    tmp_path,
    monkeypatch,
):
    journal = tmp_path / "journal"
    journal.mkdir()
    python_calls: list[str] = []
    native_argvs: list[list[str]] = []

    def scan_journal(
        journal_arg: str,
        *,
        verbose: bool = False,
        full: bool = False,
    ) -> ScanReport:
        python_calls.append(journal_arg)
        return ScanReport(changed=True, edge_rows_inserted=1)

    def seam(args, journal_arg: str) -> int | None:
        return native_seam.maybe_run_native_indexer(
            args,
            journal_arg,
            coverage_checker=lambda: True,
            handshake_checker=lambda: core_handshake.CoreHandshakeResult("ok"),
            helper_locator=lambda: Path("/tmp/bin/solstone-core"),
            native_runner=lambda argv, *, check=False: (
                native_argvs.append(argv) or subprocess.CompletedProcess(argv, 0)
            ),
        )

    monkeypatch.setattr(indexer_cli, "maybe_run_native_indexer", seam)
    monkeypatch.setattr(indexer_cli, "scan_journal", scan_journal)

    _run_indexer_cli(monkeypatch, journal, ["--rescan"], core_indexer=None)

    assert native_argvs == [
        ["/tmp/bin/solstone-core", "indexer", "--journal", str(journal), "--rescan"]
    ]
    assert python_calls == []


def test_cli_uncovered_absent_indexer_routes_write_to_python_without_native_boundaries(
    tmp_path,
    monkeypatch,
):
    journal = tmp_path / "journal"
    journal.mkdir()
    python_calls: list[tuple[str, bool, bool]] = []
    native_argvs: list[list[str]] = []

    def scan_journal(
        journal_arg: str,
        *,
        verbose: bool = False,
        full: bool = False,
    ) -> ScanReport:
        python_calls.append((journal_arg, verbose, full))
        return ScanReport(changed=True, edge_rows_inserted=1)

    def seam(args, journal_arg: str) -> int | None:
        return native_seam.maybe_run_native_indexer(
            args,
            journal_arg,
            coverage_checker=lambda: False,
            handshake_checker=_raise_unexpected("handshake_checker"),
            helper_locator=_raise_unexpected("helper_locator"),
            native_runner=_raise_unexpected("native_runner"),
        )

    monkeypatch.setattr(indexer_cli, "maybe_run_native_indexer", seam)
    monkeypatch.setattr(indexer_cli, "scan_journal", scan_journal)

    _run_indexer_cli(monkeypatch, journal, ["--rescan"], core_indexer=None)

    assert python_calls == [(str(journal), False, False)]
    assert native_argvs == []


def test_cli_explicit_python_routes_write_to_python_without_coverage_or_native(
    tmp_path,
    monkeypatch,
):
    journal = tmp_path / "journal"
    journal.mkdir()
    python_calls: list[tuple[str, bool, bool]] = []
    native_argvs: list[list[str]] = []

    def scan_journal(
        journal_arg: str,
        *,
        verbose: bool = False,
        full: bool = False,
    ) -> ScanReport:
        python_calls.append((journal_arg, verbose, full))
        return ScanReport(changed=True, edge_rows_inserted=1)

    def seam(args, journal_arg: str) -> int | None:
        return native_seam.maybe_run_native_indexer(
            args,
            journal_arg,
            coverage_checker=_raise_unexpected("coverage_checker"),
            handshake_checker=_raise_unexpected("handshake_checker"),
            helper_locator=_raise_unexpected("helper_locator"),
            native_runner=_raise_unexpected("native_runner"),
        )

    monkeypatch.setattr(indexer_cli, "maybe_run_native_indexer", seam)
    monkeypatch.setattr(indexer_cli, "scan_journal", scan_journal)

    _run_indexer_cli(monkeypatch, journal, ["--rescan"], core_indexer="python")

    assert python_calls == [(str(journal), False, False)]
    assert native_argvs == []


def test_cli_explicit_rust_routes_write_to_native_without_coverage(
    tmp_path,
    monkeypatch,
):
    journal = tmp_path / "journal"
    journal.mkdir()
    python_calls: list[str] = []
    native_argvs: list[list[str]] = []

    def scan_journal(
        journal_arg: str,
        *,
        verbose: bool = False,
        full: bool = False,
    ) -> ScanReport:
        python_calls.append(journal_arg)
        return ScanReport(changed=True, edge_rows_inserted=1)

    def seam(args, journal_arg: str) -> int | None:
        return native_seam.maybe_run_native_indexer(
            args,
            journal_arg,
            coverage_checker=_raise_unexpected("coverage_checker"),
            handshake_checker=lambda: core_handshake.CoreHandshakeResult("ok"),
            helper_locator=lambda: Path("/tmp/bin/solstone-core"),
            native_runner=lambda argv, *, check=False: (
                native_argvs.append(argv) or subprocess.CompletedProcess(argv, 0)
            ),
        )

    monkeypatch.setattr(indexer_cli, "maybe_run_native_indexer", seam)
    monkeypatch.setattr(indexer_cli, "scan_journal", scan_journal)

    _run_indexer_cli(monkeypatch, journal, ["--rescan"], core_indexer="rust")

    assert native_argvs == [
        ["/tmp/bin/solstone-core", "indexer", "--journal", str(journal), "--rescan"]
    ]
    assert python_calls == []


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
    ) -> ScanReport:
        calls.append(("scan", journal_arg, verbose, full))
        return ScanReport(changed=True, edge_rows_inserted=0)

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

    def scan_journal(
        journal_arg: str,
        *,
        verbose: bool = False,
        full: bool = False,
    ) -> ScanReport:
        scans.append((journal_arg, verbose, full))
        return ScanReport(changed=True, edge_rows_inserted=1)

    monkeypatch.setattr(indexer_cli, "scan_journal", scan_journal)

    _run_indexer_cli(monkeypatch, journal, args, core_indexer="python")

    assert scans == [(str(journal), False, full)]
    assert root_log.read_bytes() == original
