# SPDX-License-Identifier: AGPL-3.0-only
# Copyright (c) 2026 sol pbc

from __future__ import annotations

import gzip
import shutil
import subprocess
import sys
import tarfile
import tomllib
from collections.abc import Callable
from io import BytesIO
from pathlib import Path

import pytest

from scripts.core_compile_inputs import (
    CoreCompileInputAsset,
    discover_core_compile_inputs,
)
from scripts.normalize_maturin_sdist import normalize_core_sdist_workspace_lock
from scripts.release_candidate_driver import (
    CORE_X86_64_MATURIN_ARGS,
    _scrubbed_build_env,
)
from scripts.release_tool_pins import RUSTC_RELEASE_PIN

REPO_ROOT = Path(__file__).resolve().parents[1]
IGNORE_NAMES = {
    ".git",
    ".venv",
    "journal",
    "target",
    "dist",
    "htmlcov",
    ".pytest_cache",
    "__pycache__",
    "node_modules",
    "logs",
    "scratch",
    "tmp",
}
ESCAPED_INCLUDE_ARGUMENT = "../../../../../../missing-outside-extracted-root.txt"


@pytest.mark.integration
@pytest.mark.timeout(900)
def test_core_sdist_compile_inputs_are_required_by_real_wheel_build(
    tmp_path: Path,
) -> None:
    _require_build_tools()
    source_root = _copy_source_root(tmp_path)
    sdist = _build_normalized_sdist(source_root)
    assets = discover_core_compile_inputs(source_root)
    assert len(assets) == 1
    asset = assets[0]

    _run_gate_archive_mode(source_root, sdist)

    control = _build_wheel(tmp_path, "control", sdist)
    assert control.returncode == 0, control.combined_output

    # The checkout contains this asset at the same relative layout. If the build
    # could reach sideways into checkout source, this removal case would pass.
    removed = _mutated_sdist(
        tmp_path, sdist, "removed", lambda root: _remove_asset(root, asset)
    )
    removed_result = _build_wheel(tmp_path, "removed", removed)
    _assert_missing_include_failure(removed_result, asset)

    wrong_path = _mutated_sdist(
        tmp_path,
        sdist,
        "wrong-path",
        lambda root: _move_asset_to_wrong_path(root, asset),
    )
    wrong_path_result = _build_wheel(tmp_path, "wrong-path", wrong_path)
    _assert_missing_include_failure(wrong_path_result, asset)

    redirected = _mutated_sdist(
        tmp_path,
        sdist,
        "redirected",
        lambda root: _redirect_include_outside_root(root, asset),
    )
    redirected_result = _build_wheel(tmp_path, "redirected", redirected)
    _assert_redirected_include_failure(redirected_result, asset)


def _copy_source_root(tmp_path: Path) -> Path:
    source_root = tmp_path / "source"
    shutil.copytree(REPO_ROOT, source_root, ignore=_ignore_entries)
    return source_root


def _ignore_entries(_directory: str, names: list[str]) -> set[str]:
    ignored = set()
    for name in names:
        if name in IGNORE_NAMES or name.startswith(".sandbox."):
            ignored.add(name)
    return ignored


def _build_normalized_sdist(source_root: Path) -> Path:
    env = _build_env(source_root, source_root / "cargo-target-sdist", "")
    result = subprocess.run(
        ("uv", "build", "--package", "solstone-core", "--sdist"),
        cwd=source_root,
        env=env,
        text=True,
        capture_output=True,
        timeout=900,
    )
    assert result.returncode == 0, result.stdout + result.stderr
    archives = sorted((source_root / "dist").glob("solstone_core-*.tar.gz"))
    assert len(archives) == 1
    sdist = archives[0]
    normalize_core_sdist_workspace_lock(source_root, sdist)
    return sdist


def _run_gate_archive_mode(source_root: Path, sdist: Path) -> None:
    result = subprocess.run(
        (
            sys.executable,
            str(source_root / "scripts" / "check_core_sdist_compile_inputs.py"),
            "--sdist",
            str(sdist),
        ),
        cwd=source_root,
        text=True,
        capture_output=True,
        timeout=60,
    )
    assert result.returncode == 0, result.stdout + result.stderr


class _BuildResult:
    def __init__(self, result: subprocess.CompletedProcess[str]) -> None:
        self.returncode = result.returncode
        self.combined_output = result.stdout + result.stderr


def _build_wheel(tmp_path: Path, name: str, sdist: Path) -> _BuildResult:
    case = tmp_path / "wheel-builds" / name
    out_dir = case / "wheel"
    target_dir = case / "target"
    out_dir.mkdir(parents=True)
    target_dir.mkdir()
    env = _build_env(case, target_dir, CORE_X86_64_MATURIN_ARGS)
    result = subprocess.run(
        ("uv", "build", str(sdist), "--wheel", "--out-dir", str(out_dir)),
        cwd=case,
        env=env,
        text=True,
        capture_output=True,
        timeout=900,
    )
    return _BuildResult(result)


def _build_env(root: Path, target_dir: Path, maturin_args: str) -> dict[str, str]:
    env = _scrubbed_build_env(root, maturin_args)
    env["CARGO_TARGET_DIR"] = str(target_dir)
    env["CARGO_INCREMENTAL"] = "0"
    env["CARGO_NET_OFFLINE"] = "true"
    env["RUSTUP_TOOLCHAIN"] = _pinned_toolchain()
    return env


def _pinned_toolchain() -> str:
    data = tomllib.loads(
        (REPO_ROOT / "rust-toolchain.toml").read_text(encoding="utf-8")
    )
    channel = data.get("toolchain", {}).get("channel")
    assert channel == RUSTC_RELEASE_PIN
    return channel


def _require_build_tools() -> None:
    for tool in ("uv", "cargo", "zig", "maturin", "rustup"):
        if shutil.which(tool) is None:
            pytest.skip(f"{tool} is not installed")
    result = subprocess.run(
        ("rustup", "which", "--toolchain", _pinned_toolchain(), "rustc"),
        text=True,
        capture_output=True,
        timeout=30,
    )
    if result.returncode != 0:
        pytest.skip(f"pinned Rust toolchain {_pinned_toolchain()} is not installed")


def _mutated_sdist(
    tmp_path: Path,
    source: Path,
    name: str,
    mutate: Callable[[Path], None],
) -> Path:
    extracted = tmp_path / "mutations" / name / "extracted"
    archive_root = _extract_sdist(source, extracted)
    root = extracted / archive_root
    mutate(root)
    target = tmp_path / "mutations" / name / f"{source.stem}.{name}.tar.gz"
    _write_sdist(root, target)
    return target


def _extract_sdist(source: Path, destination: Path) -> str:
    destination.mkdir(parents=True)
    with tarfile.open(source, "r:gz") as archive:
        members = archive.getmembers()
        roots = {Path(member.name).parts[0] for member in members if member.name}
        assert len(roots) == 1
        for member in members:
            parts = Path(member.name).parts
            assert parts and not Path(member.name).is_absolute() and ".." not in parts
        archive.extractall(destination, filter="data")
        return next(iter(roots))


def _write_sdist(root: Path, target: Path) -> None:
    target.parent.mkdir(parents=True, exist_ok=True)
    with target.open("wb") as raw:
        with gzip.GzipFile(
            filename="", mode="wb", fileobj=raw, compresslevel=9, mtime=0
        ) as gz:
            with tarfile.open(
                fileobj=gz, mode="w", format=tarfile.PAX_FORMAT
            ) as archive:
                for path in sorted(root.rglob("*")):
                    relative = path.relative_to(root.parent).as_posix()
                    member = tarfile.TarInfo(relative)
                    member.mtime = 0
                    if path.is_dir():
                        member.type = tarfile.DIRTYPE
                        member.mode = 0o755
                        archive.addfile(member)
                    elif path.is_file():
                        data = path.read_bytes()
                        member.mode = 0o644
                        member.size = len(data)
                        archive.addfile(member, BytesIO(data))


def _remove_asset(root: Path, asset: CoreCompileInputAsset) -> None:
    (root / asset.sdist_path).unlink()


def _move_asset_to_wrong_path(root: Path, asset: CoreCompileInputAsset) -> None:
    correct = root / asset.sdist_path
    wrong = correct.with_name(f"wrong-{correct.name}")
    correct.rename(wrong)


def _redirect_include_outside_root(root: Path, asset: CoreCompileInputAsset) -> None:
    source = root / asset.source_file.relative_to(_asset_source_root(asset))
    text = source.read_text(encoding="utf-8")
    replacement = f'"{ESCAPED_INCLUDE_ARGUMENT}"'
    assert asset.raw_argument in text
    source.write_text(text.replace(asset.raw_argument, replacement), encoding="utf-8")


def _assert_missing_include_failure(
    result: _BuildResult, asset: CoreCompileInputAsset
) -> None:
    assert result.returncode != 0, result.combined_output
    expected_path = _diagnostic_include_path(asset)
    assert "error: couldn't read" in result.combined_output
    assert expected_path in result.combined_output
    assert (
        f"{asset.source_file.name}:{asset.line}:{asset.column}"
        in result.combined_output
    )


def _assert_redirected_include_failure(
    result: _BuildResult, asset: CoreCompileInputAsset
) -> None:
    assert result.returncode != 0, result.combined_output
    expected_path = (
        asset.source_file.relative_to(_asset_source_root(asset) / "core").parent
        / ESCAPED_INCLUDE_ARGUMENT
    ).as_posix()
    assert "error: couldn't read" in result.combined_output
    assert expected_path in result.combined_output
    assert (
        f"{asset.source_file.name}:{asset.line}:{asset.column}"
        in result.combined_output
    )


def _diagnostic_include_path(asset: CoreCompileInputAsset) -> str:
    raw_value = asset.raw_argument.strip()[1:-1]
    source_root = _asset_source_root(asset)
    core_relative_source_parent = asset.source_file.relative_to(
        source_root / "core"
    ).parent
    return (core_relative_source_parent / raw_value).as_posix()


def _asset_source_root(asset: CoreCompileInputAsset) -> Path:
    for parent in asset.resolved_path.parents:
        if parent / asset.sdist_path == asset.resolved_path:
            return parent
    raise AssertionError(f"could not derive source root for {asset.sdist_path}")
