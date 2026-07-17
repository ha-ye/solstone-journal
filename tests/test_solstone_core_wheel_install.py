# SPDX-License-Identifier: AGPL-3.0-only
# Copyright (c) 2026 sol pbc

from __future__ import annotations

import os
import platform
import shutil
import subprocess
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]


@pytest.mark.skipif(
    sys.platform != "linux" or platform.machine() != "x86_64",
    reason="local Linux wheel install test runs on linux/x86_64 only",
)
def test_locally_built_linux_core_wheel_installs_and_runs(
    tmp_path: Path,
) -> None:
    if shutil.which("uv") is None:
        pytest.skip("uv is not installed")
    if shutil.which("rustup") is None:
        pytest.skip("rustup is not installed")
    installed_targets = subprocess.run(
        ["rustup", "target", "list", "--installed"],
        cwd=ROOT,
        capture_output=True,
        text=True,
        check=True,
    ).stdout.splitlines()
    if "x86_64-unknown-linux-musl" not in installed_targets:
        pytest.skip("x86_64-unknown-linux-musl target is not installed")

    dist_dir = tmp_path / "dist"
    env = os.environ.copy()
    env["MATURIN_PEP517_ARGS"] = (
        "--compatibility manylinux2014 --target x86_64-unknown-linux-musl"
    )
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
    wheels = sorted(dist_dir.glob("solstone_core-*manylinux2014_x86_64.whl"))
    assert len(wheels) == 1

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
