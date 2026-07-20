# SPDX-License-Identifier: AGPL-3.0-only
# Copyright (c) 2026 sol pbc

from __future__ import annotations

import argparse
import re
import subprocess
import sys
from pathlib import Path
from typing import Any

import pytest

from solstone.think import core_handshake, sol_cli
from solstone.think.indexer import cli as indexer_cli
from solstone.think.indexer import native_seam


def _args(**overrides: Any) -> argparse.Namespace:
    values: dict[str, Any] = {
        "rescan": False,
        "rescan_full": False,
        "rescan_file": None,
        "rebuild_edges": False,
        "reset": False,
        "day": None,
        "day_from": None,
        "day_to": None,
        "facet": None,
        "agent": None,
        "stream": None,
        "query": None,
        "limit": 10,
        "offset": 0,
        "top": 5,
        "verbose": False,
        "debug": False,
    }
    values.update(overrides)
    return argparse.Namespace(**values)


def _ok() -> core_handshake.CoreHandshakeResult:
    return core_handshake.CoreHandshakeResult("ok")


def _route(
    args: argparse.Namespace,
    *,
    config: dict[str, Any] | None = None,
    native_returncode: int = 0,
    config_reader=None,
    handshake_checker=None,
    helper_path: Path | None = None,
    journal: str = "/tmp/journal",
) -> tuple[int | None, list[str], list[list[str]]]:
    python_calls: list[str] = []
    native_argvs: list[list[str]] = []

    def default_config_reader(_journal: str) -> dict[str, Any]:
        return config if config is not None else {}

    def native_runner(argv: list[str], *, check: bool = False):
        assert check is False
        native_argvs.append(argv)
        return subprocess.CompletedProcess(argv, native_returncode)

    result = native_seam.maybe_run_native_indexer(
        args,
        journal,
        config_reader=config_reader or default_config_reader,
        handshake_checker=handshake_checker or _ok,
        helper_locator=lambda: helper_path or Path("/tmp/bin/solstone-core"),
        native_runner=native_runner,
    )
    if result is None:
        python_calls.append("python")
    return result, python_calls, native_argvs


def _raise_unexpected(name: str):
    def fail(*_args, **_kwargs):
        raise AssertionError(f"{name} should not be called")

    return fail


def test_absent_core_section_runs_python() -> None:
    result, python_calls, native_argvs = _route(_args(rescan=True), config={})

    assert result is None
    assert python_calls == ["python"]
    assert native_argvs == []


def test_absent_indexer_key_runs_python() -> None:
    result, python_calls, native_argvs = _route(_args(rescan=True), config={"core": {}})

    assert result is None
    assert python_calls == ["python"]
    assert native_argvs == []


def test_explicit_python_runs_python() -> None:
    result, python_calls, native_argvs = _route(
        _args(rescan=True),
        config={"core": {"indexer": "python"}},
    )

    assert result is None
    assert python_calls == ["python"]
    assert native_argvs == []


def test_rust_rescan_invokes_native_with_explicit_journal() -> None:
    result, python_calls, native_argvs = _route(
        _args(rescan=True),
        config={"core": {"indexer": "rust"}},
        journal="/var/journal",
        helper_path=Path("/venv/bin/solstone-core"),
    )

    assert result == 0
    assert python_calls == []
    assert native_argvs == [
        ["/venv/bin/solstone-core", "indexer", "--journal", "/var/journal", "--rescan"]
    ]


def test_rust_tail_drops_verbose_debug_and_query_filters() -> None:
    result, python_calls, native_argvs = _route(
        _args(
            rescan=True,
            verbose=True,
            debug=True,
            day="20260101",
            facet="work",
            agent="flow",
            stream="archon",
            limit=99,
            offset=4,
            top=7,
        ),
        config={"core": {"indexer": "rust"}},
    )

    assert result == 0
    assert python_calls == []
    assert native_argvs == [
        ["/tmp/bin/solstone-core", "indexer", "--journal", "/tmp/journal", "--rescan"]
    ]


def test_rust_composed_write_order() -> None:
    result, python_calls, native_argvs = _route(
        _args(reset=True, rebuild_edges=True, rescan_full=True),
        config={"core": {"indexer": "rust"}},
    )

    assert result == 0
    assert python_calls == []
    assert native_argvs == [
        [
            "/tmp/bin/solstone-core",
            "indexer",
            "--journal",
            "/tmp/journal",
            "--reset",
            "--rebuild-edges",
            "--rescan-full",
        ]
    ]


def test_rust_prefers_rescan_full_when_both_scan_flags_are_set() -> None:
    result, python_calls, native_argvs = _route(
        _args(rescan=True, rescan_full=True),
        config={"core": {"indexer": "rust"}},
    )

    assert result == 0
    assert python_calls == []
    assert native_argvs == [
        [
            "/tmp/bin/solstone-core",
            "indexer",
            "--journal",
            "/tmp/journal",
            "--rescan-full",
        ]
    ]


@pytest.mark.parametrize(
    ("overrides", "expected_flags"),
    [
        ({"rescan_full": True}, ["--rescan-full"]),
        ({"rescan": True}, ["--rescan"]),
        (
            {"rebuild_edges": True, "rescan_full": True},
            ["--rebuild-edges", "--rescan-full"],
        ),
        ({"reset": True, "rescan_full": True}, ["--reset", "--rescan-full"]),
    ],
)
def test_rust_zero_edge_hint_suppression_options_are_forwarded_in_native_argv(
    overrides: dict[str, Any],
    expected_flags: list[str],
) -> None:
    # The seam inherits native stdio, so direct Rust tests assert hint output.
    result, python_calls, native_argvs = _route(
        _args(**overrides),
        config={"core": {"indexer": "rust"}},
    )

    assert result == 0
    assert python_calls == []
    assert native_argvs == [
        [
            "/tmp/bin/solstone-core",
            "indexer",
            "--journal",
            "/tmp/journal",
            *expected_flags,
        ]
    ]


def test_rust_rescan_file_normalizes_chronicle_prefixed_relative_to_absolute(
    tmp_path: Path,
) -> None:
    journal = tmp_path / "journal"
    rel = "chronicle/20240101/talents/flow.md"
    (journal / "chronicle" / "20240101" / "talents").mkdir(parents=True)

    result, python_calls, native_argvs = _route(
        _args(rescan_file=rel),
        config={"core": {"indexer": "rust"}},
        journal=str(journal),
    )

    assert result == 0
    assert python_calls == []
    assert native_argvs == [
        [
            "/tmp/bin/solstone-core",
            "indexer",
            "--journal",
            str(journal),
            "--rescan-file",
            str((journal / rel).resolve()),
        ]
    ]


def test_rust_rescan_file_with_rescan_stays_python() -> None:
    result, python_calls, native_argvs = _route(
        _args(
            reset=True,
            rebuild_edges=True,
            rescan_file="20240101/talents/flow.md",
            rescan=True,
        ),
        config_reader=_raise_unexpected("config_reader"),
    )

    assert result is None
    assert python_calls == ["python"]
    assert native_argvs == []


def test_query_only_rust_selection_stays_python_without_reading_config() -> None:
    result, python_calls, native_argvs = _route(
        _args(query="foo"),
        config_reader=_raise_unexpected("config_reader"),
    )

    assert result is None
    assert python_calls == ["python"]
    assert native_argvs == []


def test_mixed_write_query_rust_selection_stays_python_without_reading_config() -> None:
    result, python_calls, native_argvs = _route(
        _args(rescan=True, query="foo"),
        config_reader=_raise_unexpected("config_reader"),
    )

    assert result is None
    assert python_calls == ["python"]
    assert native_argvs == []


def test_invalid_indexer_value_returns_ex_config(
    capsys: pytest.CaptureFixture[str],
) -> None:
    result, python_calls, native_argvs = _route(
        _args(rescan=True),
        config={"core": {"indexer": "go"}},
    )

    assert result == core_handshake.EX_CONFIG
    assert python_calls == []
    assert native_argvs == []
    assert capsys.readouterr().err.strip() == (
        "journal indexer selected implementation 'invalid' from config key "
        "core.indexer; found 'go'; expected 'python' or 'rust'. Set core.indexer "
        "to 'python' to revert."
    )


def test_invalid_indexer_on_decline_value_returns_ex_config(
    capsys: pytest.CaptureFixture[str],
) -> None:
    result, python_calls, native_argvs = _route(
        _args(rescan=True),
        config={"core": {"indexer": "python", "indexer_on_decline": "retry"}},
    )

    assert result == core_handshake.EX_CONFIG
    assert python_calls == []
    assert native_argvs == []
    assert capsys.readouterr().err.strip() == (
        "journal indexer selected implementation 'python' from config key "
        "core.indexer, but config key core.indexer_on_decline has invalid value "
        "'retry'; expected 'abort' or 'fallback'. Set core.indexer to 'python' "
        "to revert."
    )


@pytest.mark.parametrize("core_value", [[], "yes"])
def test_non_object_core_returns_ex_config(
    core_value: object,
    capsys: pytest.CaptureFixture[str],
) -> None:
    result, python_calls, native_argvs = _route(
        _args(rescan=True),
        config={"core": core_value},
    )

    assert result == core_handshake.EX_CONFIG
    assert python_calls == []
    assert native_argvs == []
    assert capsys.readouterr().err.strip() == (
        "journal indexer selected implementation 'invalid' from config key "
        f"core.indexer, but config section core has invalid value {core_value!r}; "
        "expected an object. Set core.indexer to 'python' to revert."
    )


def test_handshake_skip_under_rust_aborts(capsys: pytest.CaptureFixture[str]) -> None:
    result, python_calls, native_argvs = _route(
        _args(rescan=True),
        config={"core": {"indexer": "rust"}},
        handshake_checker=lambda: core_handshake.CoreHandshakeResult("skip", "reason"),
    )

    assert result == core_handshake.EX_CONFIG
    assert python_calls == []
    assert native_argvs == []
    assert capsys.readouterr().err.strip() == (
        "journal indexer selected implementation 'rust' from config key "
        "core.indexer, but solstone-core handshake returned 'skip': reason. "
        "Set core.indexer to 'python' to revert."
    )


def test_handshake_fail_under_rust_aborts(capsys: pytest.CaptureFixture[str]) -> None:
    result, python_calls, native_argvs = _route(
        _args(rescan=True),
        config={"core": {"indexer": "rust"}},
        handshake_checker=lambda: core_handshake.CoreHandshakeResult("fail", "reason"),
    )

    assert result == core_handshake.EX_CONFIG
    assert python_calls == []
    assert native_argvs == []
    assert capsys.readouterr().err.strip() == (
        "journal indexer selected implementation 'rust' from config key "
        "core.indexer, but solstone-core handshake returned 'fail': reason. "
        "Set core.indexer to 'python' to revert."
    )


def test_native_decline_abort_returns_69_without_python(
    capsys: pytest.CaptureFixture[str],
) -> None:
    result, python_calls, native_argvs = _route(
        _args(rescan=True),
        config={"core": {"indexer": "rust"}},
        native_returncode=69,
    )

    assert result == 69
    assert python_calls == []
    assert native_argvs != []
    assert capsys.readouterr().err.strip() == (
        "journal indexer selected implementation 'rust' from config key "
        "core.indexer, but solstone-core indexer declined this input with exit "
        "69. Set core.indexer_on_decline to 'fallback' to retry unsupported "
        "inputs through Python, or set core.indexer to 'python' to revert."
    )


def test_native_decline_fallback_continues_to_python(
    capsys: pytest.CaptureFixture[str],
) -> None:
    result, python_calls, native_argvs = _route(
        _args(rescan=True),
        config={"core": {"indexer": "rust", "indexer_on_decline": "fallback"}},
        native_returncode=69,
    )

    assert result is None
    assert python_calls == ["python"]
    assert native_argvs != []
    assert capsys.readouterr().err.strip() == (
        "journal indexer selected implementation 'rust' from config key "
        "core.indexer, but solstone-core indexer declined this input with exit "
        "69; falling back to Python because core.indexer_on_decline is "
        "'fallback'. Set core.indexer to 'python' to revert."
    )


def test_native_usage_error_64_never_fallbacks(
    capsys: pytest.CaptureFixture[str],
) -> None:
    result, python_calls, native_argvs = _route(
        _args(rescan=True),
        config={"core": {"indexer": "rust", "indexer_on_decline": "fallback"}},
        native_returncode=64,
    )

    assert result == 64
    assert python_calls == []
    assert native_argvs != []
    assert capsys.readouterr().err.strip() == (
        "journal indexer selected implementation 'rust' from config key "
        "core.indexer, but solstone-core indexer exited 64 (usage error). This "
        "is a seam argument-construction bug; set core.indexer to 'python' to "
        "revert."
    )


def test_native_tempfail_75_never_fallbacks(
    capsys: pytest.CaptureFixture[str],
) -> None:
    result, python_calls, native_argvs = _route(
        _args(rescan=True),
        config={"core": {"indexer": "rust", "indexer_on_decline": "fallback"}},
        native_returncode=75,
    )

    assert result == 75
    assert python_calls == []
    assert native_argvs != []
    assert capsys.readouterr().err.strip() == (
        "journal indexer selected implementation 'rust' from config key "
        "core.indexer, but solstone-core indexer exited 75 (temporary failure). "
        "Set core.indexer to 'python' to revert."
    )


def test_native_signal_death_maps_to_tempfail(
    capsys: pytest.CaptureFixture[str],
) -> None:
    result, python_calls, native_argvs = _route(
        _args(rescan=True),
        config={"core": {"indexer": "rust", "indexer_on_decline": "fallback"}},
        native_returncode=-9,
    )

    assert result == 75
    assert python_calls == []
    assert native_argvs != []
    assert capsys.readouterr().err.strip() == (
        "journal indexer selected implementation 'rust' from config key "
        "core.indexer, but solstone-core indexer died from signal 9 (returncode "
        "-9); treating as temporary failure. Set core.indexer to 'python' to "
        "revert."
    )


def test_native_other_nonzero_returns_code(capsys: pytest.CaptureFixture[str]) -> None:
    result, python_calls, native_argvs = _route(
        _args(rescan=True),
        config={"core": {"indexer": "rust"}},
        native_returncode=12,
    )

    assert result == 12
    assert python_calls == []
    assert native_argvs != []
    assert capsys.readouterr().err.strip() == (
        "journal indexer selected implementation 'rust' from config key "
        "core.indexer, but solstone-core indexer exited 12. Set core.indexer to "
        "'python' to revert."
    )


def test_native_launch_oserror_maps_to_tempfail(
    capsys: pytest.CaptureFixture[str],
) -> None:
    def native_runner(_argv: list[str], *, check: bool = False):
        assert check is False
        raise OSError("missing helper")

    result = native_seam.maybe_run_native_indexer(
        _args(rescan=True),
        "/tmp/journal",
        config_reader=lambda _journal: {"core": {"indexer": "rust"}},
        handshake_checker=_ok,
        helper_locator=lambda: Path("/tmp/bin/solstone-core"),
        native_runner=native_runner,
    )

    assert result == 75
    assert capsys.readouterr().err.strip() == (
        "journal indexer selected implementation 'rust' from config key "
        "core.indexer, but launching solstone-core indexer failed: missing "
        "helper. Set core.indexer to 'python' to revert."
    )


def test_empty_tail_raises_runtime_error() -> None:
    with pytest.MonkeyPatch.context() as monkeypatch:
        monkeypatch.setattr(native_seam, "_has_write_operation", lambda _args: True)
        monkeypatch.setattr(
            native_seam, "_build_operation_flags", lambda _args, _journal: []
        )

        with pytest.raises(
            RuntimeError, match=re.escape(native_seam.EMPTY_TAIL_MESSAGE)
        ):
            native_seam.maybe_run_native_indexer(
                _args(),
                "/tmp/journal",
                config_reader=lambda _journal: {"core": {"indexer": "rust"}},
                handshake_checker=_ok,
                helper_locator=lambda: Path("/tmp/bin/solstone-core"),
                native_runner=_raise_unexpected("native_runner"),
            )


def test_run_command_propagates_native_nonzero_indexer_return(monkeypatch) -> None:
    def setup_cli(parser):
        parsed = parser.parse_args(["--rescan"])
        parsed.verbose = False
        parsed.debug = False
        return parsed

    def seam(args: argparse.Namespace, journal: str) -> int | None:
        return native_seam.maybe_run_native_indexer(
            args,
            journal,
            config_reader=lambda _journal: {"core": {"indexer": "rust"}},
            handshake_checker=_ok,
            helper_locator=lambda: Path("/tmp/bin/solstone-core"),
            native_runner=lambda argv, *, check=False: subprocess.CompletedProcess(
                argv,
                75,
            ),
        )

    monkeypatch.setattr(indexer_cli, "setup_cli", setup_cli)
    monkeypatch.setattr(indexer_cli, "require_solstone", lambda: None)
    monkeypatch.setattr(indexer_cli, "get_journal", lambda: "/tmp/journal")
    monkeypatch.setattr(indexer_cli, "maybe_run_native_indexer", seam)
    monkeypatch.setattr(sys, "argv", ["journal indexer", "--rescan"])

    assert sol_cli.run_command("solstone.think.indexer") == 75
