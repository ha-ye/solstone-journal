# SPDX-License-Identifier: AGPL-3.0-only
# Copyright (c) 2026 sol pbc

import shutil
from pathlib import Path

from scripts.render_packaging import check

REPO_ROOT = Path(__file__).resolve().parent.parent


def _repo_copy(tmp_path: Path) -> Path:
    root = tmp_path / "repo"
    root.mkdir()
    shutil.copy2(REPO_ROOT / "pyproject.toml", root / "pyproject.toml")
    for package in ("solstone-journal", "solstone-journal-cuda"):
        target = root / "packages" / package
        target.mkdir(parents=True)
        shutil.copy2(
            REPO_ROOT / "packages" / package / "pyproject.toml",
            target / "pyproject.toml",
        )
    return root


def test_render_packaging_check_accepts_repo():
    assert check(root=REPO_ROOT) == 0


def test_render_packaging_check_reports_leaf_version_drift(tmp_path, capsys):
    root = _repo_copy(tmp_path)
    # Derive the live root version so this test survives release bumps.
    import tomllib

    root_version = tomllib.loads((root / "pyproject.toml").read_text(encoding="utf-8"))[
        "project"
    ]["version"]
    cpu_pyproject = root / "packages" / "solstone-journal" / "pyproject.toml"
    text = cpu_pyproject.read_text(encoding="utf-8")
    assert f'version = "{root_version}"' in text
    cpu_pyproject.write_text(
        text.replace(f'version = "{root_version}"', 'version = "0.0.0"', 1),
        encoding="utf-8",
    )

    assert check(root=root) == 1
    captured = capsys.readouterr()
    assert "packages/solstone-journal/pyproject.toml" in captured.out
