# SPDX-License-Identifier: AGPL-3.0-only
# Copyright (c) 2026 sol pbc

"""Self-test for scripts/check_convey_bind_imports_clean.py."""

from __future__ import annotations

import importlib.util
import subprocess
import sys
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[1]
SCRIPT = REPO_ROOT / "scripts" / "check_convey_bind_imports_clean.py"
EXPECTED_HEAVY = {
    "numpy",
    "scipy",
    "sklearn",
    "onnxruntime",
    "pyarrow",
    "transformers",
    "cv2",
    "mlx",
    "mlx_lm",
    "av",
    "faster_whisper",
    "torch",
    "pandas",
    "google.genai",
    "huggingface_hub",
    "litellm",
}


def _run(*args: str) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        [sys.executable, str(SCRIPT), *args],
        capture_output=True,
        text=True,
        cwd=REPO_ROOT,
        timeout=180,
    )


def _load_script_module():
    spec = importlib.util.spec_from_file_location(
        "check_convey_bind_imports_clean", SCRIPT
    )
    assert spec is not None
    assert spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def test_repo_tree_is_green() -> None:
    result = _run()

    assert result.returncode == 0, result.stdout + result.stderr
    assert "convey-bind-imports-clean: pass" in result.stdout


def test_injected_heavy_import_goes_red_and_names_offender() -> None:
    result = _run("--inject-heavy-module", "numpy")

    assert result.returncode == 1
    assert "numpy" in result.stdout + result.stderr


def test_heavy_constant_is_single_source_of_truth() -> None:
    module = _load_script_module()

    assert len(module.HEAVY) == 16
    assert set(module.HEAVY) == EXPECTED_HEAVY
