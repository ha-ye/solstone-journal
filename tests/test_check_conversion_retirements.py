# SPDX-License-Identifier: AGPL-3.0-only
# Copyright (c) 2026 sol pbc

"""Self-tests for the conversion-wave retirement CI contract."""

from __future__ import annotations

import importlib.util
import subprocess
import sys
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parents[1]
SCRIPT = REPO_ROOT / "scripts" / "check_conversion_retirements.py"
MANIFEST = REPO_ROOT / "conversion-retirements.toml"


def _load_script_module():
    spec = importlib.util.spec_from_file_location(
        "check_conversion_retirements", SCRIPT
    )
    assert spec is not None
    assert spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def _manifest(
    root: Path,
    *,
    status: str = "done",
    python_roots: tuple[str, ...] = (),
) -> Path:
    manifest = root / "conversion-retirements.toml"
    roots = ", ".join(f'"{path}"' for path in python_roots)
    manifest.write_text(
        "\n".join(
            [
                "schema_version = 1",
                'dependency_files = ["pyproject.toml"]',
                'content_roots = ["solstone"]',
                "content_exclusions = []",
                "",
                "[[waves]]",
                'id = "seeded-wave"',
                f'status = "{status}"',
                'distribution = "scikit-learn"',
                f"python_roots = [{roots}]",
                'import_roots = ["sklearn"]',
                "test_only_dependency_locations = []",
                "",
            ]
        ),
        encoding="utf-8",
    )
    return manifest


def _check(
    root: Path,
    *,
    tracked_paths: list[str],
    status: str = "done",
    python_roots: tuple[str, ...] = (),
):
    module = _load_script_module()
    manifest = _manifest(root, status=status, python_roots=python_roots)
    return module.check_repository(
        root,
        manifest,
        tracked_paths=tracked_paths,
    )


def test_checked_in_manifest_passes() -> None:
    result = subprocess.run(
        [sys.executable, str(SCRIPT), "--manifest", str(MANIFEST)],
        cwd=REPO_ROOT,
        capture_output=True,
        text=True,
        timeout=60,
    )

    assert result.returncode == 0, result.stdout + result.stderr
    assert "W1b-discovery-kernel" in result.stdout


@pytest.mark.parametrize("alias", ["scikit-learn", "scikit_learn", "sklearn"])
def test_done_wave_fails_each_distribution_or_import_spelling(
    tmp_path: Path,
    alias: str,
) -> None:
    runtime = tmp_path / "solstone" / "runtime.py"
    runtime.parent.mkdir()
    runtime.write_text(f'consumer = "{alias}"\n', encoding="utf-8")
    (tmp_path / "pyproject.toml").write_text(
        "[project]\ndependencies = []\n",
        encoding="utf-8",
    )

    result = _check(
        tmp_path,
        tracked_paths=["pyproject.toml", "solstone/runtime.py"],
    )

    assert result.ok is False
    assert any(alias in violation for violation in result.violations)


def test_done_wave_fails_alias_in_tracked_pathname(tmp_path: Path) -> None:
    offender = tmp_path / "solstone" / "scikit_learn" / "runtime.py"
    offender.parent.mkdir(parents=True)
    offender.write_text("value = 1\n", encoding="utf-8")
    (tmp_path / "pyproject.toml").write_text(
        "[project]\ndependencies = []\n",
        encoding="utf-8",
    )

    result = _check(
        tmp_path,
        tracked_paths=["pyproject.toml", "solstone/scikit_learn/runtime.py"],
    )

    assert result.ok is False
    assert any("pathname" in violation for violation in result.violations)


@pytest.mark.parametrize(
    "dependency",
    ["scikit-learn>=1.3", "scikit_learn==1.8.0"],
)
def test_done_wave_fails_semantic_dependency_alias(
    tmp_path: Path,
    dependency: str,
) -> None:
    (tmp_path / "pyproject.toml").write_text(
        f'[project]\ndependencies = ["{dependency}"]\n',
        encoding="utf-8",
    )
    runtime = tmp_path / "solstone" / "__init__.py"
    runtime.parent.mkdir()
    runtime.write_text("", encoding="utf-8")

    result = _check(
        tmp_path,
        tracked_paths=["pyproject.toml", "solstone/__init__.py"],
    )

    assert result.ok is False
    assert any("project.dependencies" in violation for violation in result.violations)


def test_done_wave_fails_declared_python_root_that_still_exists(
    tmp_path: Path,
) -> None:
    retired = tmp_path / "solstone" / "retired_kernel.py"
    retired.parent.mkdir()
    retired.write_text("value = 1\n", encoding="utf-8")
    (tmp_path / "pyproject.toml").write_text(
        "[project]\ndependencies = []\n",
        encoding="utf-8",
    )

    result = _check(
        tmp_path,
        tracked_paths=["pyproject.toml", "solstone/retired_kernel.py"],
        python_roots=("solstone/retired_kernel.py",),
    )

    assert result.ok is False
    assert any(
        "declared Python root still exists" in item for item in result.violations
    )


def test_in_progress_wave_does_not_claim_retirement(tmp_path: Path) -> None:
    runtime = tmp_path / "solstone" / "runtime.py"
    runtime.parent.mkdir()
    runtime.write_text('consumer = "sklearn"\n', encoding="utf-8")
    (tmp_path / "pyproject.toml").write_text(
        '[project]\ndependencies = ["scikit-learn"]\n',
        encoding="utf-8",
    )

    result = _check(
        tmp_path,
        tracked_paths=["pyproject.toml", "solstone/runtime.py"],
        status="in_progress",
    )

    assert result.ok is True
    assert result.checked_waves == ()


def test_manifest_rejects_broad_content_exclusion(tmp_path: Path) -> None:
    manifest = _manifest(tmp_path)
    manifest.write_text(
        manifest.read_text(encoding="utf-8").replace(
            "content_exclusions = []",
            'content_exclusions = ["solstone/*"]',
        ),
        encoding="utf-8",
    )
    module = _load_script_module()

    result = module.check_repository(
        tmp_path,
        manifest,
        tracked_paths=["pyproject.toml"],
    )

    assert result.ok is False
    assert result.violations == (
        "content_exclusions must be exact paths, not glob patterns",
    )


def test_done_wave_allows_explicit_test_only_oracle_dependency(
    tmp_path: Path,
) -> None:
    manifest = _manifest(tmp_path)
    manifest.write_text(
        manifest.read_text(encoding="utf-8").replace(
            "test_only_dependency_locations = []",
            (
                "test_only_dependency_locations = "
                '["pyproject.toml:dependency-groups.dev"]'
            ),
        ),
        encoding="utf-8",
    )
    (tmp_path / "pyproject.toml").write_text(
        '[dependency-groups]\ndev = ["scikit-learn>=1.3"]\n',
        encoding="utf-8",
    )
    runtime = tmp_path / "solstone" / "__init__.py"
    runtime.parent.mkdir()
    runtime.write_text("", encoding="utf-8")
    module = _load_script_module()

    result = module.check_repository(
        tmp_path,
        manifest,
        tracked_paths=["pyproject.toml", "solstone/__init__.py"],
    )

    assert result.ok is True


def test_done_wave_cannot_exempt_shipping_dependency_group(
    tmp_path: Path,
) -> None:
    manifest = _manifest(tmp_path)
    manifest.write_text(
        manifest.read_text(encoding="utf-8").replace(
            "test_only_dependency_locations = []",
            (
                "test_only_dependency_locations = "
                '["pyproject.toml:project.optional-dependencies.journal-host"]'
            ),
        ),
        encoding="utf-8",
    )
    (tmp_path / "pyproject.toml").write_text(
        (
            "[project]\n"
            "[project.optional-dependencies]\n"
            'journal-host = ["scikit-learn>=1.3"]\n'
        ),
        encoding="utf-8",
    )
    runtime = tmp_path / "solstone" / "__init__.py"
    runtime.parent.mkdir()
    runtime.write_text("", encoding="utf-8")
    module = _load_script_module()

    result = module.check_repository(
        tmp_path,
        manifest,
        tracked_paths=["pyproject.toml", "solstone/__init__.py"],
    )

    assert result.ok is False
    assert any(
        "test-only dependency exception is not a test group" in violation
        for violation in result.violations
    )
