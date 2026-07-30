# SPDX-License-Identifier: AGPL-3.0-only
# Copyright (c) 2026 sol pbc

from __future__ import annotations

import json
import subprocess
import venv
from pathlib import Path

import pytest

import scripts.release_install_smoke as smoke

ROOT = Path(__file__).resolve().parents[1]


@pytest.mark.integration
@pytest.mark.release
@pytest.mark.timeout(300)
def test_speakers_analyze_wheel_installs_and_runs_real_inference(
    tmp_path: Path,
) -> None:
    build = subprocess.run(
        ["make", "wheel-speakers-analyze-linux-x86_64"],
        cwd=ROOT,
        capture_output=True,
        text=True,
        check=False,
        timeout=300,
    )
    assert build.returncode == 0, build.stderr or build.stdout
    models_build = subprocess.run(
        [
            "uv",
            "build",
            "--package",
            "solstone-journal-models",
            "--wheel",
            "--out-dir",
            "dist",
        ],
        cwd=ROOT,
        capture_output=True,
        text=True,
        check=False,
        timeout=300,
    )
    assert models_build.returncode == 0, models_build.stderr or models_build.stdout
    wheels = sorted(ROOT.glob("dist/solstone_core_speakers_analyze-*.whl"))
    assert wheels
    wheel = wheels[-1]
    models_wheels = sorted(ROOT.glob("dist/solstone_journal_models-*.whl"))
    assert models_wheels
    models_wheel = models_wheels[-1]

    env_root = tmp_path / "venv"
    venv.EnvBuilder(with_pip=True, symlinks=False).create(env_root)
    python = env_root / "bin" / "python"
    install = subprocess.run(
        [
            str(python),
            "-m",
            "pip",
            "install",
            "--no-index",
            "--no-deps",
            str(wheel),
            str(models_wheel),
        ],
        capture_output=True,
        text=True,
        check=False,
        timeout=120,
    )
    assert install.returncode == 0, install.stderr or install.stdout

    executable = env_root / "bin" / "solstone-core-speakers-analyze"
    request_text, request_error = smoke._speakers_analyze_request(env_root, python)
    assert request_text is not None, request_error
    run = subprocess.run(
        [str(executable)],
        input=request_text,
        capture_output=True,
        text=True,
        check=False,
        timeout=120,
    )
    assert run.returncode == 0, run.stderr or run.stdout
    response = json.loads(run.stdout)
    assert response["schema"] == smoke.SPEAKERS_ANALYZE_RESPONSE_SCHEMA
