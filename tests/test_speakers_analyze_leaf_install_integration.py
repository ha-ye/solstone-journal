# SPDX-License-Identifier: AGPL-3.0-only
# Copyright (c) 2026 sol pbc

from __future__ import annotations

import subprocess
import venv
from pathlib import Path

import pytest

from solstone.think.speakers_analyze_handshake import (
    runtime_has_speakers_analyze_wheel_coverage,
)

ROOT = Path(__file__).resolve().parents[1]


@pytest.mark.integration
@pytest.mark.timeout(600)
def test_cpu_leaf_install_reaches_speakers_analyze_helper(tmp_path: Path) -> None:
    if not runtime_has_speakers_analyze_wheel_coverage():
        pytest.skip("host is not covered by solstone-core-speakers-analyze wheels")

    dist_dir = tmp_path / "dist"
    build = subprocess.run(
        [
            "uv",
            "build",
            "--package",
            "solstone-journal",
            "--wheel",
            "--out-dir",
            str(dist_dir),
        ],
        cwd=ROOT,
        capture_output=True,
        text=True,
        check=False,
        timeout=120,
    )
    assert build.returncode == 0, build.stderr or build.stdout
    wheels = sorted(dist_dir.glob("solstone_journal-*.whl"))
    assert len(wheels) == 1

    env_root = tmp_path / "venv"
    venv.EnvBuilder(with_pip=True, symlinks=False).create(env_root)
    python = env_root / "bin" / "python"
    install = subprocess.run(
        [str(python), "-m", "pip", "install", str(wheels[0])],
        capture_output=True,
        text=True,
        check=False,
        timeout=600,
    )
    assert install.returncode == 0, install.stderr or install.stdout

    handshake = subprocess.run(
        [
            str(python),
            "-c",
            "\n".join(
                [
                    "from solstone.think.speakers_analyze_handshake import check_speakers_analyze_handshake",
                    "result = check_speakers_analyze_handshake()",
                    'print(f"handshake status={result.status!r} message={result.message!r}")',
                    "raise SystemExit(0 if result.status == 'ok' else 1)",
                ]
            ),
        ],
        capture_output=True,
        text=True,
        check=False,
        timeout=120,
    )
    print(handshake.stdout, end="")
    assert handshake.returncode == 0, handshake.stderr or handshake.stdout
