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

from solstone.think import core_handshake, probe, sol_cli
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


COVERED_PROVENANCE = (
    "journal indexer defaulted to implementation 'rust' because config key "
    "core.indexer is unset and solstone-core is packaged for this platform"
)
UNCOVERED_PROVENANCE = (
    "journal indexer defaulted to implementation 'python' because config key "
    "core.indexer is unset and solstone-core is not packaged for this platform"
)
EXPLICIT_RUST_PROVENANCE = (
    "journal indexer selected implementation 'rust' from config key core.indexer"
)


def _route(
    args: argparse.Namespace,
    *,
    config: dict[str, Any] | None = None,
    native_returncode: int = 0,
    config_reader=None,
    handshake_checker=None,
    coverage_checker=lambda: False,
    helper_path: Path | None = None,
    journal: str = "/tmp/journal",
) -> tuple[int | None, list[list[str]]]:
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
        coverage_checker=coverage_checker,
    )
    return result, native_argvs


def _raise_unexpected(name: str):
    def fail(*_args, **_kwargs):
        raise AssertionError(f"{name} should not be called")

    return fail


def _native_argv(*flags: str, journal: str = "/tmp/journal") -> list[str]:
    return ["/tmp/bin/solstone-core", "indexer", "--journal", journal, *flags]


@pytest.mark.parametrize(
    ("platform_tuple", "expected"),
    [
        (probe.SOLSTONE_CORE_COVERED_PLATFORMS[0], True),
        (("linux", "riscv64"), False),
    ],
)
def test_platform_has_core_coverage_resolves_through_probe_predicate(
    platform_tuple: tuple[str, str],
    expected: bool,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(probe, "current_solstone_core_platform", lambda: platform_tuple)

    assert native_seam._platform_has_core_coverage() is expected


@pytest.mark.parametrize(
    ("platform_tuple", "expected_native"),
    [
        (probe.SOLSTONE_CORE_COVERED_PLATFORMS[0], True),
        (("linux", "riscv64"), False),
    ],
)
def test_absent_indexer_default_coverage_checker_uses_probe(
    platform_tuple: tuple[str, str],
    expected_native: bool,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(probe, "current_solstone_core_platform", lambda: platform_tuple)
    native_argvs: list[list[str]] = []

    if expected_native:
        result = native_seam.maybe_run_native_indexer(
            _args(rescan=True),
            "/tmp/journal",
            config_reader=lambda _journal: {},
            handshake_checker=_ok,
            helper_locator=lambda: Path("/tmp/bin/solstone-core"),
            native_runner=lambda argv, *, check=False: (
                native_argvs.append(argv) or subprocess.CompletedProcess(argv, 0)
            ),
        )

        assert result == 0
        assert native_argvs == [_native_argv("--rescan")]
        return

    result = native_seam.maybe_run_native_indexer(
        _args(rescan=True),
        "/tmp/journal",
        config_reader=lambda _journal: {},
        handshake_checker=_raise_unexpected("handshake_checker"),
        helper_locator=_raise_unexpected("helper_locator"),
        native_runner=_raise_unexpected("native_runner"),
    )

    assert result is None
    assert native_argvs == []


@pytest.mark.parametrize("config", [{}, {"core": {}}])
@pytest.mark.parametrize(
    ("overrides", "expected_flags"),
    [
        ({"reset": True}, ["--reset"]),
        ({"rebuild_edges": True}, ["--rebuild-edges"]),
        ({"rescan": True}, ["--rescan"]),
        ({"rescan_full": True}, ["--rescan-full"]),
        (
            {"rescan_file": "chronicle/today.md"},
            ["--rescan-file", str(Path("/tmp/journal/chronicle/today.md").resolve())],
        ),
    ],
)
def test_absent_indexer_covered_host_write_operations_select_native(
    config: dict[str, Any],
    overrides: dict[str, Any],
    expected_flags: list[str],
) -> None:
    result, native_argvs = _route(
        _args(**overrides),
        config=config,
        coverage_checker=lambda: True,
    )

    assert result == 0
    assert native_argvs == [_native_argv(*expected_flags)]


@pytest.mark.parametrize("config", [{}, {"core": {}}])
@pytest.mark.parametrize(
    ("overrides", "expected_flags"),
    [
        (
            {"reset": True, "rebuild_edges": True, "rescan_full": True},
            ["--reset", "--rebuild-edges", "--rescan-full"],
        ),
        (
            {"rebuild_edges": True, "rescan_full": True},
            ["--rebuild-edges", "--rescan-full"],
        ),
        ({"reset": True, "rescan_full": True}, ["--reset", "--rescan-full"]),
        ({"rescan": True, "rescan_full": True}, ["--rescan-full"]),
    ],
)
def test_absent_indexer_covered_host_compositions_select_native(
    config: dict[str, Any],
    overrides: dict[str, Any],
    expected_flags: list[str],
) -> None:
    result, native_argvs = _route(
        _args(**overrides),
        config=config,
        coverage_checker=lambda: True,
    )

    assert result == 0
    assert native_argvs == [_native_argv(*expected_flags)]


@pytest.mark.parametrize("config", [{}, {"core": {}}])
def test_absent_indexer_uncovered_host_runs_python_without_native_boundaries(
    config: dict[str, Any],
) -> None:
    result = native_seam.maybe_run_native_indexer(
        _args(rescan=True),
        "/tmp/journal",
        config_reader=lambda _journal: config,
        coverage_checker=lambda: False,
        handshake_checker=_raise_unexpected("handshake_checker"),
        helper_locator=_raise_unexpected("helper_locator"),
        native_runner=_raise_unexpected("native_runner"),
    )

    assert result is None


def test_explicit_python_runs_python() -> None:
    result, native_argvs = _route(
        _args(rescan=True),
        config={"core": {"indexer": "python"}},
        coverage_checker=_raise_unexpected("coverage_checker"),
    )

    assert result is None
    assert native_argvs == []


def test_rust_rescan_invokes_native_with_explicit_journal() -> None:
    result, native_argvs = _route(
        _args(rescan=True),
        config={"core": {"indexer": "rust"}},
        journal="/var/journal",
        helper_path=Path("/venv/bin/solstone-core"),
    )

    assert result == 0
    assert native_argvs == [
        ["/venv/bin/solstone-core", "indexer", "--journal", "/var/journal", "--rescan"]
    ]


def test_rust_tail_drops_verbose_debug_and_query_filters() -> None:
    result, native_argvs = _route(
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
    assert native_argvs == [
        ["/tmp/bin/solstone-core", "indexer", "--journal", "/tmp/journal", "--rescan"]
    ]


def test_rust_composed_write_order() -> None:
    result, native_argvs = _route(
        _args(reset=True, rebuild_edges=True, rescan_full=True),
        config={"core": {"indexer": "rust"}},
    )

    assert result == 0
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
    result, native_argvs = _route(
        _args(rescan=True, rescan_full=True),
        config={"core": {"indexer": "rust"}},
    )

    assert result == 0
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
    result, native_argvs = _route(
        _args(**overrides),
        config={"core": {"indexer": "rust"}},
    )

    assert result == 0
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

    result, native_argvs = _route(
        _args(rescan_file=rel),
        config={"core": {"indexer": "rust"}},
        journal=str(journal),
    )

    assert result == 0
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
    result, native_argvs = _route(
        _args(
            reset=True,
            rebuild_edges=True,
            rescan_file="20240101/talents/flow.md",
            rescan=True,
        ),
        config_reader=_raise_unexpected("config_reader"),
    )

    assert result is None
    assert native_argvs == []


def test_query_only_rust_selection_stays_python_without_reading_config() -> None:
    result, native_argvs = _route(
        _args(query="foo"),
        config_reader=_raise_unexpected("config_reader"),
    )

    assert result is None
    assert native_argvs == []


def test_mixed_write_query_rust_selection_stays_python_without_reading_config() -> None:
    result, native_argvs = _route(
        _args(rescan=True, query="foo"),
        config_reader=_raise_unexpected("config_reader"),
    )

    assert result is None
    assert native_argvs == []


def test_invalid_indexer_value_returns_ex_config(
    capsys: pytest.CaptureFixture[str],
) -> None:
    result, native_argvs = _route(
        _args(rescan=True),
        config={"core": {"indexer": "go"}},
    )

    assert result == core_handshake.EX_CONFIG
    assert native_argvs == []
    assert capsys.readouterr().err.strip() == (
        "journal indexer selected implementation 'invalid' from config key "
        "core.indexer; found 'go'; expected 'python' or 'rust'. Set core.indexer "
        "to 'python' to revert."
    )


def test_invalid_indexer_on_decline_value_returns_ex_config(
    capsys: pytest.CaptureFixture[str],
) -> None:
    result, native_argvs = _route(
        _args(rescan=True),
        config={"core": {"indexer": "python", "indexer_on_decline": "retry"}},
    )

    assert result == core_handshake.EX_CONFIG
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
    result, native_argvs = _route(
        _args(rescan=True),
        config={"core": core_value},
    )

    assert result == core_handshake.EX_CONFIG
    assert native_argvs == []
    assert capsys.readouterr().err.strip() == (
        "journal indexer selected implementation 'invalid' from config key "
        f"core.indexer, but config section core has invalid value {core_value!r}; "
        "expected an object. Set core.indexer to 'python' to revert."
    )


def test_explicit_rust_ignores_coverage_and_handshake_skip_aborts(
    capsys: pytest.CaptureFixture[str],
) -> None:
    result, native_argvs = _route(
        _args(rescan=True),
        config={"core": {"indexer": "rust"}},
        handshake_checker=lambda: core_handshake.CoreHandshakeResult("skip", "reason"),
        coverage_checker=_raise_unexpected("coverage_checker"),
    )

    assert result == core_handshake.EX_CONFIG
    assert native_argvs == []
    assert capsys.readouterr().err.strip() == (
        "journal indexer selected implementation 'rust' from config key "
        "core.indexer, but solstone-core handshake returned 'skip': reason. "
        "Set core.indexer to 'python' to revert."
    )


def test_explicit_rust_ignores_coverage_and_handshake_fail_aborts(
    capsys: pytest.CaptureFixture[str],
) -> None:
    result, native_argvs = _route(
        _args(rescan=True),
        config={"core": {"indexer": "rust"}},
        handshake_checker=lambda: core_handshake.CoreHandshakeResult("fail", "reason"),
        coverage_checker=_raise_unexpected("coverage_checker"),
    )

    assert result == core_handshake.EX_CONFIG
    assert native_argvs == []
    assert capsys.readouterr().err.strip() == (
        "journal indexer selected implementation 'rust' from config key "
        "core.indexer, but solstone-core handshake returned 'fail': reason. "
        "Set core.indexer to 'python' to revert."
    )


@pytest.mark.parametrize(
    ("status", "expected"),
    [
        (
            "skip",
            f"{COVERED_PROVENANCE}, but solstone-core handshake returned "
            "'skip': reason. Set core.indexer to 'python' to revert.",
        ),
        (
            "fail",
            f"{COVERED_PROVENANCE}, but solstone-core handshake returned "
            "'fail': reason. Set core.indexer to 'python' to revert.",
        ),
    ],
)
def test_absent_indexer_covered_host_handshake_abort_messages(
    status: core_handshake.HandshakeStatus,
    expected: str,
    capsys: pytest.CaptureFixture[str],
) -> None:
    result, native_argvs = _route(
        _args(rescan=True),
        config={},
        coverage_checker=lambda: True,
        handshake_checker=lambda: core_handshake.CoreHandshakeResult(status, "reason"),
    )

    assert result == core_handshake.EX_CONFIG
    assert native_argvs == []
    assert capsys.readouterr().err.strip() == expected


def test_native_decline_abort_returns_69_without_python(
    capsys: pytest.CaptureFixture[str],
) -> None:
    result, native_argvs = _route(
        _args(rescan=True),
        config={"core": {"indexer": "rust"}},
        native_returncode=69,
    )

    assert result == 69
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
    result, native_argvs = _route(
        _args(rescan=True),
        config={"core": {"indexer": "rust", "indexer_on_decline": "fallback"}},
        native_returncode=69,
    )

    assert result is None
    assert native_argvs != []
    assert capsys.readouterr().err.strip() == (
        "journal indexer selected implementation 'rust' from config key "
        "core.indexer, but solstone-core indexer declined this input with exit "
        "69; falling back to Python because core.indexer_on_decline is "
        "'fallback'. Set core.indexer to 'python' to revert."
    )


def test_absent_indexer_covered_host_decline_abort_uses_default_policy(
    capsys: pytest.CaptureFixture[str],
) -> None:
    result, native_argvs = _route(
        _args(rescan=True),
        config={},
        coverage_checker=lambda: True,
        native_returncode=69,
    )

    assert result == 69
    assert len(native_argvs) == 1
    assert capsys.readouterr().err.strip() == (
        f"{COVERED_PROVENANCE}, but solstone-core indexer declined this input "
        "with exit 69. Set core.indexer_on_decline to 'fallback' to retry "
        "unsupported inputs through Python, or set core.indexer to 'python' to "
        "revert."
    )


def test_absent_indexer_covered_host_decline_fallback_warns_once(
    capsys: pytest.CaptureFixture[str],
) -> None:
    result, native_argvs = _route(
        _args(rescan=True),
        config={"core": {"indexer_on_decline": "fallback"}},
        coverage_checker=lambda: True,
        native_returncode=69,
    )

    assert result is None
    assert len(native_argvs) == 1
    assert capsys.readouterr().err.strip() == (
        f"{COVERED_PROVENANCE}, but solstone-core indexer declined this input "
        "with exit 69; falling back to Python because core.indexer_on_decline is "
        "'fallback'. Set core.indexer to 'python' to revert."
    )


def test_native_usage_error_64_never_fallbacks(
    capsys: pytest.CaptureFixture[str],
) -> None:
    result, native_argvs = _route(
        _args(rescan=True),
        config={"core": {"indexer": "rust", "indexer_on_decline": "fallback"}},
        native_returncode=64,
    )

    assert result == 64
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
    result, native_argvs = _route(
        _args(rescan=True),
        config={"core": {"indexer": "rust", "indexer_on_decline": "fallback"}},
        native_returncode=75,
    )

    assert result == 75
    assert native_argvs != []
    assert capsys.readouterr().err.strip() == (
        "journal indexer selected implementation 'rust' from config key "
        "core.indexer, but solstone-core indexer exited 75 (temporary failure). "
        "Set core.indexer to 'python' to revert."
    )


def test_native_signal_death_maps_to_tempfail(
    capsys: pytest.CaptureFixture[str],
) -> None:
    result, native_argvs = _route(
        _args(rescan=True),
        config={"core": {"indexer": "rust", "indexer_on_decline": "fallback"}},
        native_returncode=-9,
    )

    assert result == 75
    assert native_argvs != []
    assert capsys.readouterr().err.strip() == (
        "journal indexer selected implementation 'rust' from config key "
        "core.indexer, but solstone-core indexer died from signal 9 (returncode "
        "-9); treating as temporary failure. Set core.indexer to 'python' to "
        "revert."
    )


@pytest.mark.parametrize(
    ("returncode", "expected_result", "expected_error"),
    [
        (
            64,
            64,
            f"{COVERED_PROVENANCE}, but solstone-core indexer exited 64 "
            "(usage error). This is a seam argument-construction bug; set "
            "core.indexer to 'python' to revert.",
        ),
        (
            75,
            75,
            f"{COVERED_PROVENANCE}, but solstone-core indexer exited 75 "
            "(temporary failure). Set core.indexer to 'python' to revert.",
        ),
        (
            -9,
            75,
            f"{COVERED_PROVENANCE}, but solstone-core indexer died from signal "
            "9 (returncode -9); treating as temporary failure. Set core.indexer "
            "to 'python' to revert.",
        ),
    ],
)
def test_absent_indexer_covered_host_native_exit_mappings(
    returncode: int,
    expected_result: int,
    expected_error: str,
    capsys: pytest.CaptureFixture[str],
) -> None:
    result, native_argvs = _route(
        _args(rescan=True),
        config={},
        coverage_checker=lambda: True,
        native_returncode=returncode,
    )

    assert result == expected_result
    assert native_argvs != []
    assert capsys.readouterr().err.strip() == expected_error


def test_native_other_nonzero_returns_code(capsys: pytest.CaptureFixture[str]) -> None:
    result, native_argvs = _route(
        _args(rescan=True),
        config={"core": {"indexer": "rust"}},
        native_returncode=12,
    )

    assert result == 12
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


def test_absent_indexer_covered_host_launch_oserror_maps_to_tempfail(
    capsys: pytest.CaptureFixture[str],
) -> None:
    def native_runner(_argv: list[str], *, check: bool = False):
        assert check is False
        raise OSError("missing helper")

    result = native_seam.maybe_run_native_indexer(
        _args(rescan=True),
        "/tmp/journal",
        config_reader=lambda _journal: {},
        coverage_checker=lambda: True,
        handshake_checker=_ok,
        helper_locator=lambda: Path("/tmp/bin/solstone-core"),
        native_runner=native_runner,
    )

    assert result == 75
    assert capsys.readouterr().err.strip() == (
        f"{COVERED_PROVENANCE}, but launching solstone-core indexer failed: "
        "missing helper. Set core.indexer to 'python' to revert."
    )


@pytest.mark.parametrize(
    ("covered", "expected_provenance"),
    [(True, COVERED_PROVENANCE), (False, UNCOVERED_PROVENANCE)],
)
def test_absent_indexer_invalid_decline_renders_host_provenance(
    covered: bool,
    expected_provenance: str,
    capsys: pytest.CaptureFixture[str],
) -> None:
    result, native_argvs = _route(
        _args(rescan=True),
        config={"core": {"indexer_on_decline": "retry"}},
        coverage_checker=lambda: covered,
    )

    assert result == core_handshake.EX_CONFIG
    assert native_argvs == []
    assert capsys.readouterr().err.strip() == (
        f"{expected_provenance}, but config key core.indexer_on_decline has "
        "invalid value 'retry'; expected 'abort' or 'fallback'. Set "
        "core.indexer to 'python' to revert."
    )


def test_empty_tail_raises_runtime_error() -> None:
    with pytest.MonkeyPatch.context() as monkeypatch:
        monkeypatch.setattr(native_seam, "_has_write_operation", lambda _args: True)
        monkeypatch.setattr(
            native_seam, "_build_operation_flags", lambda _args, _journal: []
        )
        expected = (
            f"{EXPLICIT_RUST_PROVENANCE}, but found no native-supported operation "
            "flags to pass. This is a seam bug; set core.indexer to 'python' to "
            "revert."
        )

        with pytest.raises(RuntimeError, match=re.escape(expected)):
            native_seam.maybe_run_native_indexer(
                _args(),
                "/tmp/journal",
                config_reader=lambda _journal: {"core": {"indexer": "rust"}},
                coverage_checker=_raise_unexpected("coverage_checker"),
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
