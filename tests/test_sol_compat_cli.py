# SPDX-License-Identifier: AGPL-3.0-only
# Copyright (c) 2026 sol pbc

from __future__ import annotations

import io
import os
import sys
from pathlib import Path

import pytest

from scripts.check_native_sol_compat import frozen_journal_remainder_paths
from solstone.think import sol_compat_cli
from solstone.think.sol_compat_inventory import (
    EXIT_SOFTWARE,
    EXIT_USAGE,
    JOURNAL_CALL_MODULE,
    RECURSION_ERROR,
    SENTINEL,
    SENTINEL_ACTIVE,
    SENTINEL_ARMED,
    TOP_LEVEL_COMPAT_MODULES,
    UNSUPPORTED_ERROR,
    CompatTarget,
    marker_for_public_argv0,
)


def _capture_forwarding(
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
    argv: list[str],
    *,
    public_argv0: str = "sol",
    stdin: str = "stdin-payload",
) -> tuple[int, pytest.CaptureResult[str], list[dict[str, object]]]:
    seen: list[dict[str, object]] = []

    def runner(target: CompatTarget) -> int:
        seen.append(
            {
                "target": target,
                "argv": sys.argv[:],
                "stdin": sys.stdin.read(),
                "sentinel": os.environ.get(SENTINEL),
                "kept_env": os.environ.get("SOLSTONE_COMPAT_TEST_ENV"),
                "cwd": Path.cwd(),
            }
        )
        print("target stdout")
        print("target stderr", file=sys.stderr)
        return 17

    monkeypatch.setenv(SENTINEL, SENTINEL_ARMED)
    monkeypatch.setenv("SOLSTONE_COMPAT_TEST_ENV", "kept")
    monkeypatch.setattr(sys, "stdin", io.StringIO(stdin))
    cwd = Path.cwd()

    code = sol_compat_cli.main(
        [marker_for_public_argv0(public_argv0), *argv],
        runner=runner,
    )
    captured = capsys.readouterr()

    assert seen[0]["cwd"] == cwd
    return code, captured, seen


@pytest.mark.parametrize("command,module", sorted(TOP_LEVEL_COMPAT_MODULES.items()))
def test_allowed_top_level_commands_forward_exact_process_state(
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
    command: str,
    module: str,
) -> None:
    code, captured, seen = _capture_forwarding(
        monkeypatch,
        capsys,
        [command, "--probe"],
    )

    assert code == 17
    assert captured.out == "target stdout\n"
    assert captured.err == "target stderr\n"
    assert seen == [
        {
            "target": CompatTarget(
                module=module,
                kind="main",
                argv=[f"sol {command}", "--probe"],
            ),
            "argv": [f"sol {command}", "--probe"],
            "stdin": "stdin-payload",
            "sentinel": SENTINEL_ACTIVE,
            "kept_env": "kept",
            "cwd": Path.cwd(),
        }
    ]


JOURNAL_ERRORS, JOURNAL_PATHS = frozen_journal_remainder_paths()
assert not JOURNAL_ERRORS
assert JOURNAL_PATHS


@pytest.mark.parametrize("path", sorted(JOURNAL_PATHS))
def test_allowed_journal_subtree_forwards_exact_process_state(
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
    path: tuple[str, ...],
) -> None:
    code, captured, seen = _capture_forwarding(
        monkeypatch,
        capsys,
        ["call", *path],
        public_argv0="solstone",
    )

    assert code == 17
    assert captured.out == "target stdout\n"
    assert captured.err == "target stderr\n"
    assert seen == [
        {
            "target": CompatTarget(
                module=JOURNAL_CALL_MODULE,
                kind="typer-app",
                argv=["solstone call journal", *path[1:]],
            ),
            "argv": ["solstone call journal", *path[1:]],
            "stdin": "stdin-payload",
            "sentinel": SENTINEL_ACTIVE,
            "kept_env": "kept",
            "cwd": Path.cwd(),
        }
    ]


@pytest.mark.parametrize(
    "argv",
    [
        ["does-not-exist"],
        ["importer"],
        ["call", "identity"],
        ["call", "navigate"],
        ["call", "link", "observer-pause"],
        ["call", "entities", "search"],
    ],
)
def test_compat_rejects_everything_outside_closed_allowlist(
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
    argv: list[str],
) -> None:
    def runner(target: CompatTarget) -> int:
        raise AssertionError(f"unexpected compat target: {target}")

    monkeypatch.setenv(SENTINEL, SENTINEL_ARMED)

    code = sol_compat_cli.main([marker_for_public_argv0("sol"), *argv], runner=runner)
    captured = capsys.readouterr()

    assert code == EXIT_USAGE
    assert captured.out == ""
    assert captured.err == f"{UNSUPPORTED_ERROR}\n"


@pytest.mark.parametrize("state", [None, SENTINEL_ACTIVE, "bogus"])
def test_compat_refuses_missing_active_or_unknown_sentinel(
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
    state: str | None,
) -> None:
    def runner(target: CompatTarget) -> int:
        raise AssertionError(f"unexpected compat target: {target}")

    if state is None:
        monkeypatch.delenv(SENTINEL, raising=False)
    else:
        monkeypatch.setenv(SENTINEL, state)

    code = sol_compat_cli.main(
        [marker_for_public_argv0("sol"), "notify"],
        runner=runner,
    )
    captured = capsys.readouterr()

    assert code == EXIT_SOFTWARE
    assert captured.out == ""
    assert captured.err == f"{RECURSION_ERROR}\n"


def test_compat_cli_has_no_process_invocation_path() -> None:
    source = Path(sol_compat_cli.__file__).read_text(encoding="utf-8")

    for needle in ("subprocess", "os.system", "os.exec", "Popen"):
        assert needle not in source
    assert "solstone-python-compat" not in source
