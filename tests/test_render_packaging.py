# SPDX-License-Identifier: AGPL-3.0-only
# Copyright (c) 2026 sol pbc

from __future__ import annotations

import importlib.util
import os
import subprocess
import sys
from pathlib import Path
from textwrap import dedent

import pytest

from solstone.think.probe import (
    SOLSTONE_CORE_PLATFORM_MARKERS,
    solstone_core_unsupported_platform_pin,
)

SCRIPT = Path(__file__).resolve().parents[1] / "scripts" / "render_packaging.py"
SPEC = importlib.util.spec_from_file_location("render_packaging", SCRIPT)
assert SPEC is not None
render_packaging = importlib.util.module_from_spec(SPEC)
assert SPEC.loader is not None
SPEC.loader.exec_module(render_packaging)


def test_script_runs_without_site_packages_from_outside_repo(tmp_path: Path) -> None:
    env = os.environ.copy()
    env.pop("PYTHONPATH", None)
    env.pop("VIRTUAL_ENV", None)

    result = subprocess.run(
        [sys.executable, "-S", "-E", str(SCRIPT), "--check"],
        cwd=tmp_path,
        env=env,
        capture_output=True,
        text=True,
        check=False,
        timeout=15,
    )

    assert result.returncode == 0, result.stderr
    assert "packaging metadata is up to date" in result.stdout


def _write(path: Path, text: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(dedent(text).lstrip(), encoding="utf-8")


def _fixture_root(tmp_path: Path, *, root_version: str = "1.2.3") -> Path:
    core_pins = "\n".join(
        f'    "solstone-core==0.0.1; {marker}",'
        for marker in SOLSTONE_CORE_PLATFORM_MARKERS
    )
    unsupported_pin = solstone_core_unsupported_platform_pin("0.0.1")
    _write(
        tmp_path / "pyproject.toml",
        f"""
        [project]
        name = "solstone"
        version = "{root_version}"
        dependencies = [
        {core_pins}
            "{unsupported_pin}",
        ]

        [project.optional-dependencies]
        journal-host = [
            "solstone-journal-models==1.0.0",
        ]
        journal = ["solstone-journal-host==0.7.0"]
        journal-cuda = ["solstone-journal-host==0.7.0"]

        [tool.uv]
        override-dependencies = [
            "solstone-core-unsupported-platform==0.0.1; python_version < '3.12'",
        ]
        """,
    )
    for package_name in ("solstone-journal", "solstone-journal-cuda"):
        _write(
            tmp_path / "packages" / package_name / "pyproject.toml",
            f"""
            [project]
            name = "{package_name}"
            version = "0.0.1"
            dependencies = ["solstone[journal-host]==0.0.1"]
            """,
        )
    _write(
        tmp_path / "packages" / "solstone-core" / "pyproject.toml",
        """
        [build-system]
        requires = ["maturin==1.14.1"]
        build-backend = "maturin"

        [project]
        name = "solstone-core"
        version = "0.0.1"

        [tool.maturin]
        bindings = "bin"
        manifest-path = "../../core/crates/solstone-core/Cargo.toml"
        profile = "release"
        strip = true
        """,
    )
    _write(
        tmp_path
        / "scripts"
        / "solstone-core-unsupported-platform-tombstone"
        / "setup.py",
        """
        TOMBSTONE_VERSION = "0.0.1"
        """,
    )
    _write(
        tmp_path / "core" / "Cargo.toml",
        """
        [workspace]
        members = ["crates/solstone-core", "crates/solstone-core-cli", "crates/solstone-core-journal"]
        resolver = "3"

        [workspace.package]
        version = "0.0.1"
        edition = "2024"
        rust-version = "1.95"
        license = "AGPL-3.0-only"

        [workspace.dependencies]
        solstone-core-cli = { path = "crates/solstone-core-cli" }
        """,
    )
    _write(
        tmp_path / "core" / "crates" / "solstone-core" / "Cargo.toml",
        """
        [package]
        name = "solstone-core"
        version.workspace = true
        edition.workspace = true
        rust-version.workspace = true
        license.workspace = true
        """,
    )
    _write(
        tmp_path / "core" / "crates" / "solstone-core-cli" / "Cargo.toml",
        """
        [package]
        name = "solstone-core-cli"
        version.workspace = true
        edition.workspace = true
        rust-version.workspace = true
        license.workspace = true
        """,
    )
    _write(
        tmp_path / "core" / "crates" / "solstone-core-journal" / "Cargo.toml",
        """
        [package]
        name = "solstone-core-journal"
        version.workspace = true
        edition.workspace = true
        rust-version.workspace = true
        license.workspace = true
        """,
    )
    _write_cargo_lock(tmp_path, _cargo_lock_text())
    return tmp_path


def _write_cargo_lock(root: Path, text: str) -> None:
    _write(root / "core" / "Cargo.lock", text)


def _cargo_lock_text(
    *,
    cli_block: str | None = None,
    journal_block: str | None = None,
) -> str:
    if cli_block is None:
        cli_block = """
        [[package]]
        name = "solstone-core-cli"
        version = "0.0.1"
        """
    if journal_block is None:
        journal_block = """
        [[package]]
        name = "solstone-core-journal"
        version = "0.0.1"
        """
    core_block = dedent(
        """
        # This file is automatically @generated by Cargo.
        # It is not intended for manual editing.
        version = 4

        [[package]]
        name = "solstone-core"
        version = "0.0.1"
        dependencies = [
         "solstone-core-cli",
         "solstone-core-journal",
        ]
        """
    ).lstrip()
    return (
        f"{core_block}\n"
        f"{dedent(cli_block).strip()}\n\n"
        f"{dedent(journal_block).strip()}\n"
    )


def test_render_updates_python_leaves_and_cargo_lockstep(tmp_path: Path) -> None:
    root = _fixture_root(tmp_path, root_version="2.3.4")

    rendered = render_packaging.render(root)

    assert 'version = "2.3.4"' in rendered[root / "core" / "Cargo.toml"]
    assert (
        'name = "solstone-core"\nversion = "2.3.4"'
        in rendered[root / "core" / "Cargo.lock"]
    )
    assert (
        'name = "solstone-core-cli"\nversion = "2.3.4"'
        in rendered[root / "core" / "Cargo.lock"]
    )
    assert (
        'name = "solstone-core-journal"\nversion = "2.3.4"'
        in rendered[root / "core" / "Cargo.lock"]
    )
    for package_name in ("solstone-journal", "solstone-journal-cuda"):
        leaf = rendered[root / "packages" / package_name / "pyproject.toml"]
        assert 'version = "2.3.4"' in leaf
        assert '"solstone[journal-host]==2.3.4"' in leaf
    core_leaf = rendered[root / "packages" / "solstone-core" / "pyproject.toml"]
    assert 'version = "2.3.4"' in core_leaf
    assert "solstone[journal-host]==" not in core_leaf
    root_pyproject = rendered[root / "pyproject.toml"]
    for marker in SOLSTONE_CORE_PLATFORM_MARKERS:
        assert f'"solstone-core==2.3.4; {marker}"' in root_pyproject
    assert f'"{solstone_core_unsupported_platform_pin("2.3.4")}"' in root_pyproject
    assert (
        '"solstone-core-unsupported-platform==2.3.4; python_version < '
        "'3.12'\"" in root_pyproject
    )
    assert '"solstone-journal-models==1.0.0"' in root_pyproject
    tombstone = rendered[
        root / "scripts" / "solstone-core-unsupported-platform-tombstone" / "setup.py"
    ]
    assert 'TOMBSTONE_VERSION = "2.3.4"' in tombstone


def test_check_reports_synthetic_packaging_drift(
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
) -> None:
    root = _fixture_root(tmp_path, root_version="2.3.4")

    rc = render_packaging.check(root)

    assert rc == 1
    out = capsys.readouterr().out
    assert "packaging metadata is stale" in out
    assert "drifted: pyproject.toml" in out
    assert "drifted: packages/solstone-core/pyproject.toml" in out
    assert (
        "drifted: scripts/solstone-core-unsupported-platform-tombstone/setup.py" in out
    )
    assert "drifted: core/Cargo.lock" in out


def test_render_raises_when_cargo_lock_is_missing_member_block(tmp_path: Path) -> None:
    root = _fixture_root(tmp_path)
    _write_cargo_lock(root, _cargo_lock_text(cli_block=""))

    with pytest.raises(render_packaging.PackagingRenderError, match="missing"):
        render_packaging.render(root)


def test_render_raises_when_cargo_lock_member_block_has_no_version(
    tmp_path: Path,
) -> None:
    root = _fixture_root(tmp_path)
    _write_cargo_lock(
        root,
        _cargo_lock_text(
            cli_block="""
            [[package]]
            name = "solstone-core-cli"
            """,
        ),
    )

    with pytest.raises(render_packaging.PackagingRenderError, match="version line"):
        render_packaging.render(root)


def test_render_raises_when_cargo_lock_member_block_has_source(
    tmp_path: Path,
) -> None:
    root = _fixture_root(tmp_path)
    _write_cargo_lock(
        root,
        _cargo_lock_text(
            cli_block="""
            [[package]]
            name = "solstone-core-cli"
            version = "0.0.1"
            source = "registry+https://github.com/rust-lang/crates.io-index"
            """,
        ),
    )

    with pytest.raises(render_packaging.PackagingRenderError, match="source-less"):
        render_packaging.render(root)


def test_render_raises_when_cargo_lock_has_duplicate_member_block(
    tmp_path: Path,
) -> None:
    root = _fixture_root(tmp_path)
    duplicate = """
    [[package]]
    name = "solstone-core-cli"
    version = "0.0.1"

    [[package]]
    name = "solstone-core-cli"
    version = "0.0.1"
    """
    _write_cargo_lock(root, _cargo_lock_text(cli_block=duplicate))

    with pytest.raises(render_packaging.PackagingRenderError, match="duplicate"):
        render_packaging.render(root)
