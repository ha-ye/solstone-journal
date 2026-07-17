# SPDX-License-Identifier: AGPL-3.0-only
# Copyright (c) 2026 sol pbc

from __future__ import annotations

import subprocess
from importlib.metadata import PackageNotFoundError
from pathlib import Path

from solstone.think import core_handshake


def _raise_missing(_name: str) -> str:
    raise PackageNotFoundError("solstone-core")


def _write_helper(tmp_path: Path, version: str, *, executable: bool = True) -> Path:
    python = tmp_path / "venv" / "bin" / "python"
    python.parent.mkdir(parents=True)
    python.write_text("", encoding="utf-8")
    helper = python.with_name("solstone-core")
    helper.write_text(f"#!/bin/sh\necho solstone-core {version}\n", encoding="utf-8")
    helper.chmod(0o755 if executable else 0o644)
    return python


def _runner(stdout: str, returncode: int = 0):
    def run(cmd, **_kwargs):
        return subprocess.CompletedProcess(cmd, returncode, stdout=stdout, stderr="")

    return run


def test_source_checkout_without_core_dist_metadata_skips() -> None:
    result = core_handshake.check_solstone_core_handshake(
        source_checkout=lambda: True,
        version_reader=_raise_missing,
        platform_reader=lambda: ("linux", "x86_64"),
    )

    assert result.status == "skip"
    assert result.message is not None
    assert "source checkout" in result.message


def test_source_checkout_with_core_dist_metadata_compares_binary(
    tmp_path: Path,
) -> None:
    python = _write_helper(tmp_path, "1.2.3")

    result = core_handshake.check_solstone_core_handshake(
        executable=python,
        source_checkout=lambda: True,
        version_reader=lambda _name: "1.2.3",
        platform_reader=lambda: ("linux", "x86_64"),
    )

    assert result.status == "ok"


def test_packaged_covered_platform_requires_core_dist_metadata() -> None:
    result = core_handshake.check_solstone_core_handshake(
        source_checkout=lambda: False,
        version_reader=_raise_missing,
        platform_reader=lambda: ("linux", "x86_64"),
    )

    assert result.status == "fail"
    assert result.message is not None
    assert "missing solstone-core distribution metadata" in result.message


def test_packaged_uncovered_platform_skips_without_metadata() -> None:
    result = core_handshake.check_solstone_core_handshake(
        source_checkout=lambda: False,
        version_reader=_raise_missing,
        platform_reader=lambda: ("linux", "riscv64"),
    )

    assert result.status == "skip"
    assert result.message is not None
    assert "linux/riscv64" in result.message
    assert "not covered" in result.message


def test_packaged_covered_platform_requires_binary(tmp_path: Path) -> None:
    python = tmp_path / "venv" / "bin" / "python"
    python.parent.mkdir(parents=True)

    result = core_handshake.check_solstone_core_handshake(
        executable=python,
        source_checkout=lambda: False,
        version_reader=lambda _name: "1.2.3",
        platform_reader=lambda: ("linux", "x86_64"),
    )

    assert result.status == "fail"
    assert result.message is not None
    assert "missing binary" in result.message
    assert "1.2.3" in result.message


def test_packaged_covered_platform_rejects_non_executable_binary(
    tmp_path: Path,
) -> None:
    python = _write_helper(tmp_path, "1.2.3", executable=False)

    result = core_handshake.check_solstone_core_handshake(
        executable=python,
        source_checkout=lambda: False,
        version_reader=lambda _name: "1.2.3",
        platform_reader=lambda: ("linux", "x86_64"),
    )

    assert result.status == "fail"
    assert result.message is not None
    assert "not executable" in result.message


def test_packaged_covered_platform_rejects_unparseable_version(
    tmp_path: Path,
) -> None:
    python = _write_helper(tmp_path, "1.2.3")

    result = core_handshake.check_solstone_core_handshake(
        executable=python,
        source_checkout=lambda: False,
        version_reader=lambda _name: "1.2.3",
        platform_reader=lambda: ("linux", "x86_64"),
        runner=_runner("unexpected\n"),
    )

    assert result.status == "fail"
    assert result.message is not None
    assert "could not parse" in result.message
    assert "1.2.3" in result.message


def test_packaged_covered_platform_rejects_version_mismatch(
    tmp_path: Path,
) -> None:
    python = _write_helper(tmp_path, "9.9.9")

    result = core_handshake.check_solstone_core_handshake(
        executable=python,
        source_checkout=lambda: False,
        version_reader=lambda _name: "1.2.3",
        platform_reader=lambda: ("linux", "x86_64"),
    )

    assert result.status == "fail"
    assert result.message is not None
    assert "1.2.3" in result.message
    assert "9.9.9" in result.message


def test_helper_locator_uses_executable_scripts_dir(tmp_path: Path) -> None:
    executable = tmp_path / "env" / "bin" / "python"

    assert core_handshake.helper_path_for_executable(executable) == (
        tmp_path / "env" / "bin" / "solstone-core"
    )
