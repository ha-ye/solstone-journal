#!/usr/bin/env python3
# SPDX-License-Identifier: AGPL-3.0-only
# Copyright (c) 2026 sol pbc
"""Install the published speakers-analyze helper into a source checkout venv."""

from __future__ import annotations

import os
import shutil
import subprocess
import sys
import tomllib
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass
from importlib.metadata import PackageNotFoundError
from importlib.metadata import version as distribution_version
from pathlib import Path

from packaging.markers import Marker, default_environment
from packaging.requirements import InvalidRequirement, Requirement

from solstone.think.speakers_analyze_installation import (
    HELPER_DIST_NAME,
    speakers_analyze_path_for_executable,
)

ROOT = Path(__file__).resolve().parent.parent

Runner = Callable[..., subprocess.CompletedProcess[str]]
UvFinder = Callable[[str], str | None]
VersionReader = Callable[[str], str]
ExecutablePredicate = Callable[[Path], bool]


class SpeakersAnalyzeHelperInstallError(RuntimeError):
    """Raised when the published helper cannot be installed or verified."""


@dataclass(frozen=True)
class DerivedHelperPin:
    pin: str
    expected_version: str
    markers: tuple[Marker, ...]
    raw_requirements: tuple[str, ...]


def default_executable_predicate(path: Path) -> bool:
    return os.access(path, os.X_OK)


def read_project_dependencies(pyproject_path: Path) -> list[str]:
    try:
        data = tomllib.loads(pyproject_path.read_text(encoding="utf-8"))
    except FileNotFoundError as exc:
        raise SpeakersAnalyzeHelperInstallError(
            f"missing package metadata: {pyproject_path}"
        ) from exc
    dependencies = data.get("project", {}).get("dependencies")
    if not isinstance(dependencies, list):
        raise SpeakersAnalyzeHelperInstallError(
            f"{pyproject_path} project.dependencies must be a list of strings; "
            f"found {type(dependencies).__name__}: {dependencies!r}"
        )
    for item in dependencies:
        if not isinstance(item, str):
            raise SpeakersAnalyzeHelperInstallError(
                f"{pyproject_path} project.dependencies must be a list of strings; "
                f"found non-string entry {type(item).__name__}: {item!r}"
            )
    return dependencies


def derive_helper_pin(
    dependencies: Sequence[str], *, dependency_label: str = "project.dependencies"
) -> DerivedHelperPin:
    raw_requirements: list[str] = []
    pins: set[str] = set()
    markers: list[Marker] = []

    for raw in dependencies:
        try:
            requirement = Requirement(raw)
        except InvalidRequirement as exc:
            raise SpeakersAnalyzeHelperInstallError(
                f"{dependency_label} contains invalid requirement {raw!r}"
            ) from exc
        if requirement.name != HELPER_DIST_NAME:
            continue
        raw_requirements.append(raw)
        pin = f"{requirement.name}{requirement.specifier}"
        pins.add(pin)
        if requirement.marker is None:
            raise SpeakersAnalyzeHelperInstallError(
                f"{dependency_label} {HELPER_DIST_NAME} pins must all be marker-gated; "
                f"missing marker on {raw!r}"
            )
        markers.append(requirement.marker)

    if not raw_requirements:
        raise SpeakersAnalyzeHelperInstallError(
            f"{dependency_label} must contain {HELPER_DIST_NAME}; found none"
        )
    if len(pins) != 1:
        found = ", ".join(sorted(pins))
        raise SpeakersAnalyzeHelperInstallError(
            f"{dependency_label} {HELPER_DIST_NAME} pins must resolve to exactly "
            f"one name==version; found {found}"
        )

    pin = next(iter(pins))
    expected_version = _expected_version(pin, dependency_label=dependency_label)
    return DerivedHelperPin(
        pin=pin,
        expected_version=expected_version,
        markers=tuple(markers),
        raw_requirements=tuple(raw_requirements),
    )


def is_environment_covered(
    markers: Sequence[Marker], environment: Mapping[str, str]
) -> bool:
    return any(marker.evaluate(dict(environment)) for marker in markers)


def ensure_running_target_python(running_python: Path, target_python: Path) -> None:
    expected = target_python.resolve()
    actual = running_python.resolve()
    if actual != expected:
        raise SpeakersAnalyzeHelperInstallError(
            f"speakers-analyze helper install must run under {expected}; "
            f"running under {actual}"
        )


def install_helper(
    pin: str,
    *,
    python: Path,
    uv_executable: str,
    runner: Runner = subprocess.run,
) -> None:
    argv = [
        uv_executable,
        "pip",
        "install",
        "--no-config",
        "--no-deps",
        "--python",
        str(python),
        pin,
    ]
    result = runner(argv, capture_output=True, text=True)
    if result.returncode != 0:
        detail = (result.stderr or result.stdout or "").strip()
        suffix = f": {detail}" if detail else ""
        raise SpeakersAnalyzeHelperInstallError(
            f"speakers-analyze helper install failed: uv pip install exited "
            f"{result.returncode}{suffix}"
        )


def assert_helper_installed(
    pin: str,
    *,
    python: Path,
    version_reader: VersionReader = distribution_version,
    executable_predicate: ExecutablePredicate = default_executable_predicate,
) -> None:
    expected = _expected_version(pin, dependency_label="installed helper pin")
    try:
        actual = version_reader(HELPER_DIST_NAME)
    except PackageNotFoundError as exc:
        raise SpeakersAnalyzeHelperInstallError(
            f"speakers-analyze helper install failed: missing {HELPER_DIST_NAME} "
            "distribution metadata"
        ) from exc
    if actual != expected:
        raise SpeakersAnalyzeHelperInstallError(
            f"speakers-analyze helper install failed: {HELPER_DIST_NAME} is "
            f"{actual} but expected {expected}"
        )

    helper_path = speakers_analyze_path_for_executable(python)
    if not helper_path.exists():
        raise SpeakersAnalyzeHelperInstallError(
            f"speakers-analyze helper install failed: missing executable {helper_path}"
        )
    if not executable_predicate(helper_path):
        raise SpeakersAnalyzeHelperInstallError(
            "speakers-analyze helper install failed: executable is not executable "
            f"{helper_path}"
        )


def run_installation(
    *,
    repo_root: Path,
    running_python: Path,
    environment: Mapping[str, str],
    runner: Runner = subprocess.run,
    uv_finder: UvFinder = shutil.which,
    version_reader: VersionReader = distribution_version,
    executable_predicate: ExecutablePredicate = default_executable_predicate,
) -> int:
    venv_python = repo_root / ".venv" / "bin" / "python"
    ensure_running_target_python(running_python, venv_python)

    dependencies = read_project_dependencies(
        repo_root / "packages" / "solstone-journal" / "pyproject.toml"
    )
    helper_pin = derive_helper_pin(dependencies)
    if not is_environment_covered(helper_pin.markers, environment):
        print(
            "speakers-analyze helper install skipped: this environment is not "
            f"covered by {HELPER_DIST_NAME} markers"
        )
        return 0

    uv_executable = uv_finder("uv")
    if uv_executable is None:
        raise SpeakersAnalyzeHelperInstallError(
            "speakers-analyze helper install failed: uv not found on PATH"
        )
    install_helper(
        helper_pin.pin,
        python=venv_python,
        uv_executable=uv_executable,
        runner=runner,
    )
    assert_helper_installed(
        helper_pin.pin,
        python=venv_python,
        version_reader=version_reader,
        executable_predicate=executable_predicate,
    )
    helper_path = speakers_analyze_path_for_executable(venv_python)
    print(
        f"speakers-analyze helper ready: {helper_path} ({helper_pin.expected_version})"
    )
    return 0


def main(argv: Sequence[str] | None = None) -> int:
    args = list(sys.argv[1:] if argv is None else argv)
    if args:
        print("usage: install_speakers_analyze_helper.py", file=sys.stderr)
        return 2
    try:
        return run_installation(
            repo_root=ROOT,
            running_python=Path(sys.executable),
            environment=default_environment(),
        )
    except SpeakersAnalyzeHelperInstallError as exc:
        print(exc, file=sys.stderr)
        return 1


def _expected_version(pin: str, *, dependency_label: str) -> str:
    try:
        requirement = Requirement(pin)
    except InvalidRequirement as exc:
        raise SpeakersAnalyzeHelperInstallError(
            f"{dependency_label} contains invalid requirement {pin!r}"
        ) from exc
    specifiers = tuple(requirement.specifier)
    if (
        requirement.name != HELPER_DIST_NAME
        or len(specifiers) != 1
        or specifiers[0].operator != "=="
    ):
        raise SpeakersAnalyzeHelperInstallError(
            f"{dependency_label} must pin {HELPER_DIST_NAME} with exactly one "
            f"== version; found {pin!r}"
        )
    return specifiers[0].version


if __name__ == "__main__":
    sys.exit(main())
