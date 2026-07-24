# SPDX-License-Identifier: AGPL-3.0-only
# Copyright (c) 2026 sol pbc

from __future__ import annotations

import os
import shutil
import subprocess
import sys
import zipfile
from pathlib import Path

import pytest

import scripts.check_wheel_contents as checker

ROOT = Path(__file__).resolve().parents[2]
pytestmark = pytest.mark.integration


@pytest.mark.skipif(
    sys.platform != "linux",
    reason="local Linux wheel install test runs on Linux only",
)
# A cold checkout compiles the bundled SQLite amalgamation (solstone-core-indexer-store
# depends on rusqlite with the `bundled` feature) in the release profile, which far
# exceeds the 15s global timeout. Give the from-scratch build ample headroom.
@pytest.mark.timeout(600)
def test_locally_built_linux_core_wheel_installs_and_runs(
    tmp_path: Path,
) -> None:
    if shutil.which("uv") is None:
        pytest.skip("uv is not installed")
    if shutil.which("cargo") is None:
        pytest.skip("cargo is not installed")

    dist_dir = tmp_path / "dist"
    env = os.environ.copy()
    env.pop("MATURIN_PEP517_ARGS", None)
    subprocess.run(
        [
            "uv",
            "build",
            "--offline",
            "--package",
            "solstone-core",
            "--wheel",
            "--out-dir",
            str(dist_dir),
        ],
        cwd=ROOT,
        env=env,
        check=True,
    )
    wheels = sorted(dist_dir.glob("solstone_core-*.whl"))
    assert len(wheels) == 1
    with zipfile.ZipFile(wheels[0]) as wheel:
        script_members = {
            Path(name).name
            for name in wheel.namelist()
            if ".data/scripts/" in name
        }
    assert script_members == set(checker.CORE_SCRIPT_NAMES)
    assert checker.check_core_wheel(wheels[0], checker.MAX_CORE_WHEEL_BYTES) == []

    venv = tmp_path / "venv"
    subprocess.run([sys.executable, "-m", "venv", str(venv)], check=True)
    bin_dir = venv / "bin"
    subprocess.run(
        [str(bin_dir / "pip"), "install", "--no-index", str(wheels[0])],
        check=True,
    )
    metadata_version = subprocess.check_output(
        [
            str(bin_dir / "python"),
            "-c",
            "from importlib.metadata import version; print(version('solstone-core'))",
        ],
        text=True,
    ).strip()
    binary_version = subprocess.check_output(
        [str(bin_dir / "solstone-core"), "--version"],
        text=True,
    ).strip()

    assert binary_version == f"solstone-core {metadata_version}"
