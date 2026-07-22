# SPDX-License-Identifier: AGPL-3.0-only
# Copyright (c) 2026 sol pbc
"""Real uv parser contract for release-driver build argv.

This is intentionally in the unit lane despite AGENTS.md/CLAUDE.md §6's normal
mock-process-boundary rule: it runs only `uv --version`, `uv build --help`,
driver argv plus `--help`, and one instantly parse-failing invalid invocation.
It performs no build, network access, package resolution, or journal I/O, and uv
is guaranteed for `make test` by the Makefile's install path check.

Unlike tests/integration/test_release_tool_uv_boundary.py, this test is not
version-guarded. That integration test checks pin comparison; this one checks
the argv the driver actually constructs against whatever uv is installed.

Limitation: `--help` short-circuits workspace resolution, so this validates flags
only. Package-name drift is covered by the WORKSPACE_SOURCES drift test and the
release driver's exact local-dist inventory validation.
"""

from __future__ import annotations

import re
import shutil
import subprocess
from collections.abc import Iterable

import pytest

import scripts.release_candidate_driver as driver

OPTION_DEFINITION_RE = re.compile(
    r"^(?: {6}| {2}-[A-Za-z], )(?P<flag>--[A-Za-z0-9][A-Za-z0-9-]*)\b"
)


def _uv_or_skip() -> str:
    uv = shutil.which("uv")
    if uv is None:
        pytest.skip(
            "uv is not installed; release-driver uv argv contract needs real uv"
        )
    return uv


def _combined_output(result: subprocess.CompletedProcess[str]) -> str:
    return "\n".join(part for part in (result.stdout, result.stderr) if part)


def _uv_banner(uv: str) -> str:
    result = subprocess.run(
        [uv, "--version"],
        capture_output=True,
        text=True,
        check=False,
    )
    return result.stdout.strip() or result.stderr.strip() or f"exit {result.returncode}"


def _driver_uv_argvs() -> Iterable[tuple[str, ...]]:
    for include_models in (True, False):
        for argv, _maturin_args in driver._expected_local_build_commands(
            include_models=include_models
        ):
            if argv[:2] == ("uv", "build"):
                yield argv


def test_release_driver_uv_build_argv_matches_real_uv_parser() -> None:
    uv = _uv_or_skip()
    banner = _uv_banner(uv)
    help_result = subprocess.run(
        [uv, "build", "--help"],
        capture_output=True,
        text=True,
        check=False,
    )
    assert help_result.returncode == 0, (
        f"ambient uv {banner!r} did not provide build help: "
        f"{_combined_output(help_result)}"
    )
    flags = {
        match.group("flag")
        for line in help_result.stdout.splitlines()
        if (match := OPTION_DEFINITION_RE.match(line))
    }

    assert {"--package", "--all-packages", "--wheel"} <= flags, (
        f"ambient uv {banner!r} produced an unusable build-help flag parse: "
        f"{sorted(flags)}"
    )
    assert "--exclude" not in flags, (
        f"ambient uv {banner!r} unexpectedly exposes unsupported --exclude"
    )

    for argv in _driver_uv_argvs():
        parse_result = subprocess.run(
            [uv, *argv[1:], "--help"],
            capture_output=True,
            text=True,
            check=False,
        )
        assert parse_result.returncode == 0, (
            f"ambient uv {banner!r} rejected release driver argv {argv!r}: "
            f"{_combined_output(parse_result)}"
        )
        for token in argv:
            if token.startswith("--"):
                assert token in flags, (
                    f"release driver argv {argv!r} uses uv flag {token!r} "
                    f"missing from ambient uv {banner!r}; parsed flags={sorted(flags)}"
                )

    # Keep this negative control help-only so a future uv that accepts --exclude
    # fails safely with help output instead of starting a real workspace build.
    invalid = subprocess.run(
        [
            uv,
            "build",
            "--all-packages",
            "--exclude",
            driver.MODELS_WORKSPACE_PACKAGE,
            "--help",
        ],
        capture_output=True,
        text=True,
        check=False,
    )
    invalid_output = _combined_output(invalid)
    assert invalid.returncode != 0, (
        f"ambient uv {banner!r} accepted unsupported --exclude in negative control"
    )
    assert "unexpected argument '--exclude' found" in invalid_output, (
        f"ambient uv {banner!r} rejected --exclude differently: {invalid_output}"
    )
