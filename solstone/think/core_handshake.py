# SPDX-License-Identifier: AGPL-3.0-only
# Copyright (c) 2026 sol pbc

"""Install-skew check for the solstone-core helper binary."""

from __future__ import annotations

import os
import re
import subprocess
import sys
from dataclasses import dataclass
from importlib.metadata import PackageNotFoundError
from importlib.metadata import version as distribution_version
from pathlib import Path
from typing import Callable, Literal

from solstone.think.probe import (
    current_solstone_core_platform,
    is_solstone_core_covered_platform,
)
from solstone.think.utils import is_source_checkout

EX_CONFIG = 78
CORE_DIST_NAME = "solstone-core"
CORE_BINARY_NAME = "solstone-core"
CORE_VERSION_RE = re.compile(r"^solstone-core\s+(\S+)\s*$")

HandshakeStatus = Literal["ok", "skip", "fail"]


@dataclass(frozen=True)
class CoreHandshakeResult:
    status: HandshakeStatus
    message: str | None = None


def helper_path_for_executable(executable: str | Path | None = None) -> Path:
    return Path(executable or sys.executable).with_name(CORE_BINARY_NAME)


def _run_version(
    helper_path: Path,
    *,
    runner: Callable[..., subprocess.CompletedProcess[str]] = subprocess.run,
) -> tuple[str | None, str | None]:
    try:
        completed = runner(
            [str(helper_path), "--version"],
            capture_output=True,
            text=True,
            timeout=5,
            check=False,
        )
    except OSError as exc:
        return None, f"{type(exc).__name__}: {exc}"
    except subprocess.TimeoutExpired:
        return None, "timed out running --version"

    stdout = completed.stdout.strip()
    stderr = completed.stderr.strip()
    if completed.returncode != 0:
        detail = stderr or stdout or "<empty>"
        return None, f"--version exited {completed.returncode}: {detail}"

    match = CORE_VERSION_RE.match(stdout)
    if not match:
        detail = stdout or stderr or "<empty>"
        return None, f"could not parse --version output: {detail}"
    return match.group(1), None


def check_solstone_core_handshake(
    *,
    executable: str | Path | None = None,
    source_checkout: Callable[[], bool] | None = None,
    version_reader: Callable[[str], str] | None = None,
    platform_reader: Callable[[], tuple[str, str]] | None = None,
    coverage_predicate: Callable[[str, str], bool] | None = None,
    executable_predicate: Callable[[Path], bool] = lambda path: os.access(
        path, os.X_OK
    ),
    runner: Callable[..., subprocess.CompletedProcess[str]] = subprocess.run,
) -> CoreHandshakeResult:
    source_checkout = source_checkout or is_source_checkout
    version_reader = version_reader or distribution_version
    platform_reader = platform_reader or current_solstone_core_platform
    coverage_predicate = coverage_predicate or is_solstone_core_covered_platform

    source = source_checkout()
    system, machine = platform_reader()
    covered = coverage_predicate(system, machine)

    try:
        dist_version = version_reader(CORE_DIST_NAME)
    except PackageNotFoundError:
        if source:
            return CoreHandshakeResult(
                "skip",
                "solstone-core handshake skipped: source checkout without solstone-core distribution metadata",
            )
        if not covered:
            return CoreHandshakeResult(
                "skip",
                f"solstone-core handshake skipped: {system}/{machine} is not covered by solstone-core wheel markers",
            )
        return CoreHandshakeResult(
            "fail",
            f"solstone-core install check failed: missing {CORE_DIST_NAME} distribution metadata on covered platform {system}/{machine}; reinstall solstone-journal",
        )

    if not source and not covered:
        return CoreHandshakeResult(
            "skip",
            f"solstone-core handshake skipped: {system}/{machine} is not covered by solstone-core wheel markers",
        )

    helper_path = helper_path_for_executable(executable)
    if not helper_path.exists():
        return CoreHandshakeResult(
            "fail",
            f"solstone-core install check failed: missing binary {helper_path} for {CORE_DIST_NAME} {dist_version}; reinstall solstone-journal",
        )
    if not executable_predicate(helper_path):
        return CoreHandshakeResult(
            "fail",
            f"solstone-core install check failed: binary {helper_path} is not executable for {CORE_DIST_NAME} {dist_version}; reinstall solstone-journal",
        )

    binary_version, error = _run_version(helper_path, runner=runner)
    if error is not None:
        return CoreHandshakeResult(
            "fail",
            f"solstone-core install check failed: {helper_path} --version failed for {CORE_DIST_NAME} {dist_version}: {error}; reinstall solstone-journal",
        )
    if binary_version != dist_version:
        return CoreHandshakeResult(
            "fail",
            f"solstone-core install check failed: {CORE_DIST_NAME} metadata is {dist_version} but {helper_path} reports {binary_version}; reinstall solstone-journal",
        )
    return CoreHandshakeResult("ok")
