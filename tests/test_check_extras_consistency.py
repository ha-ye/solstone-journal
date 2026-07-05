# SPDX-License-Identifier: AGPL-3.0-only
# Copyright (c) 2026 sol pbc

import shutil
from pathlib import Path

from scripts.check_extras_consistency import _check_models_pin, main

REPO_ROOT = Path(__file__).resolve().parent.parent


def test_check_models_pin_accepts_exact_pin():
    extras = {
        "journal-host": [
            "solstone-journal-models==1.0.0",
            "solstone[pdf]",
        ]
    }

    assert _check_models_pin(extras, "1.0.0") == []


def test_check_models_pin_reports_missing_pin():
    extras = {"journal-host": ["solstone[pdf]"]}

    errors = _check_models_pin(extras, "1.0.0")

    assert errors
    assert "exactly one solstone-journal-models== pin; found 0" in errors[0]


def test_check_models_pin_reports_wrong_version():
    extras = {"journal-host": ["solstone-journal-models==0.9.0"]}

    errors = _check_models_pin(extras, "1.0.0")

    assert errors
    assert "solstone-journal-models==1.0.0" in errors[0]
    assert "solstone-journal-models==0.9.0" in errors[0]


def _repo_copy(tmp_path: Path) -> Path:
    root = tmp_path / "repo"
    root.mkdir()
    for filename in ("pyproject.toml", "Makefile"):
        shutil.copy2(REPO_ROOT / filename, root / filename)
    for package in (
        "solstone-journal",
        "solstone-journal-cuda",
        "solstone-journal-models",
    ):
        target = root / "packages" / package
        target.mkdir(parents=True)
        shutil.copy2(
            REPO_ROOT / "packages" / package / "pyproject.toml",
            target / "pyproject.toml",
        )
    return root


def _replace_once(path: Path, old: str, new: str) -> None:
    text = path.read_text(encoding="utf-8")
    assert old in text
    path.write_text(text.replace(old, new, 1), encoding="utf-8")


def test_check_extras_consistency_accepts_repo():
    assert main(REPO_ROOT) == 0


def test_check_extras_consistency_rejects_journal_tombstone_extra_dep(tmp_path):
    root = _repo_copy(tmp_path)
    _replace_once(
        root / "pyproject.toml",
        'journal = ["solstone-journal-host==0.7.0"]',
        'journal = ["solstone-journal-host==0.7.0", "onnxruntime"]',
    )

    assert main(root) != 0


def test_check_extras_consistency_rejects_cpu_leaf_gpu_runtime(tmp_path):
    root = _repo_copy(tmp_path)
    _replace_once(
        root / "packages" / "solstone-journal" / "pyproject.toml",
        "dependencies = [\n",
        'dependencies = [\n    "onnxruntime-gpu>=1.25.0",\n',
    )

    assert main(root) != 0


def test_check_extras_consistency_rejects_cuda_leaf_cpu_runtime(tmp_path):
    root = _repo_copy(tmp_path)
    _replace_once(
        root / "packages" / "solstone-journal-cuda" / "pyproject.toml",
        "dependencies = [\n",
        'dependencies = [\n    "onnxruntime>=1.20.0,!=1.24.1",\n',
    )

    assert main(root) != 0


def test_check_extras_consistency_rejects_leaf_cross_dependency(tmp_path):
    root = _repo_copy(tmp_path)
    _replace_once(
        root / "packages" / "solstone-journal" / "pyproject.toml",
        "dependencies = [\n",
        'dependencies = [\n    "solstone-journal-cuda",\n',
    )

    assert main(root) != 0


def test_check_extras_consistency_rejects_missing_uv_override(tmp_path):
    root = _repo_copy(tmp_path)
    path = root / "pyproject.toml"
    text = path.read_text(encoding="utf-8")
    start = text.index("[tool.uv]\n")
    end = text.index("[tool.uv.workspace]\n")
    path.write_text(text[:start] + text[end:], encoding="utf-8")

    assert main(root) != 0


def test_check_extras_consistency_rejects_makefile_old_extra_spelling(tmp_path):
    root = _repo_copy(tmp_path)
    path = root / "Makefile"
    path.write_text(
        path.read_text(encoding="utf-8") + "\n# old spelling: --extra journal\n",
        encoding="utf-8",
    )

    assert main(root) != 0


def test_check_extras_consistency_rejects_old_host_workspace_member(tmp_path):
    root = _repo_copy(tmp_path)
    old_leaf_name = "host"
    old_host_member = f"packages/solstone-journal-{old_leaf_name}"
    _replace_once(
        root / "pyproject.toml",
        'members = ["packages/solstone-journal", "packages/solstone-journal-cuda", "packages/solstone-journal-models"]',
        f'members = ["{old_host_member}", "packages/solstone-journal-models"]',
    )

    assert main(root) != 0
