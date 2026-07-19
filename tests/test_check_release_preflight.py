# SPDX-License-Identifier: AGPL-3.0-only
# Copyright (c) 2026 sol pbc

from __future__ import annotations

import subprocess
from pathlib import Path

import scripts.check_release_preflight as preflight


def _completed(stdout: str, returncode: int = 0) -> subprocess.CompletedProcess[str]:
    return subprocess.CompletedProcess(
        args=[],
        returncode=returncode,
        stdout=stdout,
        stderr="",
    )


def test_load_toolchain_spec(tmp_path: Path) -> None:
    (tmp_path / "rust-toolchain.toml").write_text(
        """
[toolchain]
channel = "1.97.1"
profile = "minimal"
components = ["rustfmt", "clippy"]
targets = ["x86_64-unknown-linux-musl"]
""".lstrip(),
        encoding="utf-8",
    )

    spec = preflight.load_toolchain_spec(tmp_path)

    assert spec.channel == "1.97.1"
    assert spec.profile == "minimal"
    assert spec.components == ("rustfmt", "clippy")
    assert spec.targets == ("x86_64-unknown-linux-musl",)


def test_missing_toolchain_reports_repair_without_rustup_call(tmp_path: Path) -> None:
    _toolchain_dir, failures = preflight.check_toolchain_installed(
        "1.95.0",
        tmp_path / "rustup-home",
    )

    assert failures
    assert failures[0].expected == "installed rustup toolchain 1.95.0"
    assert failures[0].repair == "rustup toolchain install 1.95.0"


def test_rustup_toolchain_override_must_match_pin() -> None:
    failures = preflight.check_rustup_override(
        "1.97.1",
        {"RUSTUP_TOOLCHAIN": "1.96.0"},
    )

    assert failures
    assert failures[0].actual == "1.96.0"


def test_rustup_toolchain_override_accepts_exact_pin() -> None:
    assert (
        preflight.check_rustup_override(
            "1.97.1",
            {"RUSTUP_TOOLCHAIN": "1.97.1"},
        )
        == []
    )


def test_rustc_version_mismatch_reports_expected_actual_and_repair(
    tmp_path: Path,
) -> None:
    toolchain = tmp_path / "1.97.1-x86_64-unknown-linux-gnu"
    (toolchain / "bin").mkdir(parents=True)
    (toolchain / "bin" / "rustc").write_text("", encoding="utf-8")

    failures = preflight.check_rustc_version(
        toolchain,
        "1.97.1",
        runner=lambda *_args, **_kwargs: _completed("rustc 1.96.0 (abc 2026-01-01)"),
    )

    assert failures
    assert failures[0].expected == "rustc 1.97.1"
    assert "rustc 1.96.0" in failures[0].actual
    assert failures[0].repair == "rustup toolchain install 1.97.1"


def test_missing_zig_reports_repair() -> None:
    failures = preflight.check_zig(which=lambda _name: None)

    assert failures
    assert failures[0].expected == "zig 0.16.0"
    assert failures[0].actual == "not found"


def test_mismatched_zig_reports_expected_actual_and_repair() -> None:
    failures = preflight.check_zig(
        which=lambda _name: "/usr/bin/zig",
        runner=lambda *_args, **_kwargs: _completed("0.15.0"),
    )

    assert failures
    assert failures[0].expected == "0.16.0"
    assert failures[0].actual == "0.15.0"
    assert "ziglang==0.16.0" in failures[0].repair


def test_dirty_local_status_names_offending_paths() -> None:
    failures = preflight.check_local_clean_status(
        " M core/Cargo.toml\n?? scratch.txt\n"
    )

    assert failures
    assert "core/Cargo.toml" in failures[0].actual
    assert "scratch.txt" in failures[0].actual


def test_remote_state_reports_ref_mismatch_and_dirty_tree() -> None:
    failures = preflight.check_remote_state(
        "abc123",
        "def456",
        "?? remote.txt\n",
        label="mac-builder",
    )

    assert len(failures) == 2
    assert failures[0].expected == "abc123"
    assert failures[0].actual == "def456"
    assert failures[0].repair == "git fetch origin && git checkout abc123"
    assert "remote.txt" in failures[1].actual
