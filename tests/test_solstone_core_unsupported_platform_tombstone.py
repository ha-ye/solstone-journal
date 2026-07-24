# SPDX-License-Identifier: AGPL-3.0-only
# Copyright (c) 2026 sol pbc

from __future__ import annotations

import os
import re
import subprocess
import sys
import tarfile
import tomllib
from pathlib import Path

from packaging.markers import Marker
from packaging.requirements import Requirement

from solstone.think.probe import (
    SOLSTONE_CORE_COVERED_PLATFORMS,
    solstone_core_marker_pins,
    solstone_core_unsupported_platform_pin,
)

REPO_ROOT = Path(__file__).resolve().parent.parent
TOMBSTONE_DIR = (
    REPO_ROOT / "scripts" / "solstone-core-unsupported-platform-tombstone"
)
ALLOW_BUILD_ENV = "SOLSTONE_CORE_UNSUPPORTED_PLATFORM_TOMBSTONE_ALLOW_BUILD"
FROZEN_MESSAGE = """solstone requires a native solstone-core wheel for this platform.

Supported platform triples:
    x86_64-unknown-linux-musl
    aarch64-unknown-linux-musl
    aarch64-apple-darwin

A nominally successful install without a working `sol` is impossible.

Nothing was changed by this failed command.
See https://github.com/solpbc/solstone-journal/blob/main/INSTALL.md
"""


def _tombstone_version() -> str:
    setup_text = (TOMBSTONE_DIR / "setup.py").read_text(encoding="utf-8")
    match = re.search(r'^TOMBSTONE_VERSION = "([^"]+)"', setup_text, re.MULTILINE)
    assert match is not None
    return match.group(1)


def _root_dependencies() -> list[str]:
    pyproject = tomllib.loads(
        (REPO_ROOT / "pyproject.toml").read_text(encoding="utf-8")
    )
    return pyproject["project"]["dependencies"]


def _matching_root_reqs(system: str, machine: str) -> set[str]:
    matches: set[str] = set()
    for raw in _root_dependencies():
        req = Requirement(raw)
        if req.name not in {"solstone-core", "solstone-core-unsupported-platform"}:
            continue
        marker = req.marker
        if marker is None or marker.evaluate(
            {"sys_platform": system, "platform_machine": machine}
        ):
            matches.add(req.name)
    return matches


def test_tombstone_metadata_prep_fails_with_frozen_message(tmp_path: Path) -> None:
    env = os.environ.copy()
    env[ALLOW_BUILD_ENV] = "1"
    subprocess.run(
        [sys.executable, "setup.py", "sdist", "-d", str(tmp_path)],
        cwd=TOMBSTONE_DIR,
        env=env,
        capture_output=True,
        text=True,
        check=True,
        timeout=15,
    )
    archive = next(tmp_path.glob("solstone_core_unsupported_platform-*.tar.gz"))
    extract_dir = tmp_path / "extract"
    extract_dir.mkdir()
    with tarfile.open(archive) as tar:
        tar.extractall(extract_dir, filter="data")
    source_dir = next(path for path in extract_dir.iterdir() if path.is_dir())

    no_allow_env = os.environ.copy()
    no_allow_env.pop(ALLOW_BUILD_ENV, None)
    result = subprocess.run(
        [
            sys.executable,
            "-c",
            "import os; os.makedirs('meta', exist_ok=True); "
            "from setuptools import build_meta as b; "
            "b.prepare_metadata_for_build_wheel('meta')",
        ],
        cwd=source_dir,
        env=no_allow_env,
        capture_output=True,
        text=True,
        timeout=15,
    )

    assert result.returncode != 0
    assert FROZEN_MESSAGE in result.stdout + result.stderr


def test_root_base_pins_match_probe_partition() -> None:
    version = _tombstone_version()
    deps = _root_dependencies()

    assert sorted(dep for dep in deps if dep.startswith("solstone-core==")) == sorted(
        solstone_core_marker_pins(version)
    )
    assert solstone_core_unsupported_platform_pin(version) in deps

    for system, machine in SOLSTONE_CORE_COVERED_PLATFORMS:
        assert _matching_root_reqs(system, machine) == {"solstone-core"}


def test_unsupported_platform_resolves_to_tombstone_only() -> None:
    for system, machine in (
        ("win32", "AMD64"),
        ("darwin", "x86_64"),
        ("linux", "ppc64le"),
    ):
        assert _matching_root_reqs(system, machine) == {
            "solstone-core-unsupported-platform"
        }


def test_unsupported_marker_is_non_vacuous() -> None:
    marker = Marker(solstone_core_unsupported_platform_pin("0.0.0").split(";", 1)[1])

    assert marker.evaluate(
        {"sys_platform": "linux", "platform_machine": "ppc64le"}
    )
    assert not marker.evaluate(
        {"sys_platform": "linux", "platform_machine": "x86_64"}
    )
