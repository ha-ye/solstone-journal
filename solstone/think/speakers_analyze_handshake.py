# SPDX-License-Identifier: AGPL-3.0-only
# Copyright (c) 2026 sol pbc

"""Install checks for the solstone-core-speakers-analyze helper binary."""

from __future__ import annotations

import os
import sys
from dataclasses import dataclass
from importlib.metadata import PackageNotFoundError
from importlib.metadata import version as distribution_version
from pathlib import Path
from typing import Callable, Literal

from packaging import tags

from solstone.think import probe

HELPER_DIST_NAME = "solstone-core-speakers-analyze"
HELPER_BINARY_NAME = "solstone-core-speakers-analyze"
ROOT_DIST_NAME = "solstone"

SpeakersAnalyzeStatus = Literal["ok", "missing", "non-executable", "incompatible"]


@dataclass(frozen=True)
class SpeakersAnalyzeHandshakeResult:
    status: SpeakersAnalyzeStatus
    message: str | None = None


def speakers_analyze_path_for_executable(executable: str | Path | None = None) -> Path:
    return Path(executable or sys.executable).with_name(HELPER_BINARY_NAME)


def _packaging_platform_tags() -> set[str]:
    return {tag.platform for tag in tags.sys_tags()}


def runtime_has_speakers_analyze_wheel_coverage(
    *,
    platform_reader: Callable[
        [], probe.CorePlatform
    ] = probe.current_solstone_core_platform,
    platform_tag_reader: Callable[[], set[str]] = _packaging_platform_tags,
) -> bool:
    """Return whether this runtime can install the packaged speakers helper wheel."""
    platform_tuple = platform_reader()
    if platform_tuple not in probe.SOLSTONE_CORE_SPEAKERS_ANALYZE_COVERED_PLATFORMS:
        return False
    expected_platforms = probe.SOLSTONE_CORE_SPEAKERS_ANALYZE_PLATFORM_TAGS.get(
        platform_tuple
    )
    if expected_platforms is None:
        return False
    return not set(expected_platforms.split(".")).isdisjoint(platform_tag_reader())


def check_speakers_analyze_handshake(
    *,
    executable: str | Path | None = None,
    version_reader: Callable[[str], str] = distribution_version,
    platform_reader: Callable[
        [], probe.CorePlatform
    ] = probe.current_solstone_core_platform,
    platform_tag_reader: Callable[[], set[str]] = _packaging_platform_tags,
    executable_predicate: Callable[[Path], bool] = lambda path: os.access(
        path, os.X_OK
    ),
) -> SpeakersAnalyzeHandshakeResult:
    """Check helper metadata, platform coverage, path presence, and execute bit.

    The helper rejects every argument (core/crates/solstone-core-speakers-analyze/
    src/lib.rs:71-79), so binary-vs-distribution version skew is unprobeable.
    The version lockstep check is still sound: scripts/render_packaging.py rewrites
    every leaf pyproject from one version source (_speakers_analyze_leaf_path at
    :80-81, _rewrite_speakers_analyze_leaf at :118, and the rewrite map at
    :374-376). A distribution-version mismatch therefore indicates a partial
    upgrade even though the binary itself cannot self-report.
    """
    if not runtime_has_speakers_analyze_wheel_coverage(
        platform_reader=platform_reader,
        platform_tag_reader=platform_tag_reader,
    ):
        system, machine = platform_reader()
        return SpeakersAnalyzeHandshakeResult(
            "incompatible",
            "solstone-core-speakers-analyze install check failed: "
            f"{system}/{machine} is not covered by speakers-analyze wheel markers; "
            "set core.speakers_analyze to 'python' to revert.",
        )

    try:
        expected_version = version_reader(ROOT_DIST_NAME)
    except PackageNotFoundError:
        return SpeakersAnalyzeHandshakeResult(
            "incompatible",
            "solstone-core-speakers-analyze install check failed: missing solstone "
            "distribution metadata; reinstall solstone-journal or set "
            "core.speakers_analyze to 'python' to revert.",
        )

    try:
        helper_version = version_reader(HELPER_DIST_NAME)
    except PackageNotFoundError:
        return SpeakersAnalyzeHandshakeResult(
            "missing",
            "solstone-core-speakers-analyze install check failed: missing "
            f"{HELPER_DIST_NAME} distribution metadata; reinstall solstone-journal "
            "or set core.speakers_analyze to 'python' to revert.",
        )

    if helper_version != expected_version:
        return SpeakersAnalyzeHandshakeResult(
            "incompatible",
            "solstone-core-speakers-analyze install check failed: "
            f"{HELPER_DIST_NAME} metadata is {helper_version} but {ROOT_DIST_NAME} "
            f"is {expected_version}; reinstall solstone-journal or set "
            "core.speakers_analyze to 'python' to revert.",
        )

    helper_path = speakers_analyze_path_for_executable(executable)
    if not helper_path.exists():
        return SpeakersAnalyzeHandshakeResult(
            "missing",
            "solstone-core-speakers-analyze install check failed: missing binary "
            f"{helper_path} for {HELPER_DIST_NAME} {helper_version}; reinstall "
            "solstone-journal or set core.speakers_analyze to 'python' to revert.",
        )
    if not executable_predicate(helper_path):
        return SpeakersAnalyzeHandshakeResult(
            "non-executable",
            "solstone-core-speakers-analyze install check failed: binary "
            f"{helper_path} is not executable for {HELPER_DIST_NAME} {helper_version}; "
            "reinstall solstone-journal or set core.speakers_analyze to 'python' "
            "to revert.",
        )

    return SpeakersAnalyzeHandshakeResult("ok")


__all__ = [
    "HELPER_BINARY_NAME",
    "HELPER_DIST_NAME",
    "ROOT_DIST_NAME",
    "SpeakersAnalyzeHandshakeResult",
    "check_speakers_analyze_handshake",
    "runtime_has_speakers_analyze_wheel_coverage",
    "speakers_analyze_path_for_executable",
]
