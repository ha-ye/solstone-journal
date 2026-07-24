# SPDX-License-Identifier: AGPL-3.0-only
# Copyright (c) 2026 sol pbc

from __future__ import annotations

import os
import subprocess
import sys
import tarfile
import zipfile
from io import BytesIO
from pathlib import Path

import scripts.check_wheel_contents as checker
from tests.helpers.release_wheel_fixtures import (
    minimal_elf,
    minimal_fat_macho,
    minimal_macho,
    write_core_wheel,
    write_platform_base_wheel,
)

SCRIPT = Path(__file__).resolve().parents[1] / "scripts" / "check_wheel_contents.py"
CPU_TYPE_X86_64 = 0x01000007


def _write_member(
    wheel: zipfile.ZipFile,
    name: str,
    content: bytes,
    *,
    mode: int = 0o644,
) -> None:
    info = zipfile.ZipInfo(name)
    info.external_attr = mode << 16
    wheel.writestr(info, content)


def test_script_runs_without_site_packages_from_outside_repo(tmp_path: Path) -> None:
    env = os.environ.copy()
    env.pop("PYTHONPATH", None)
    env.pop("VIRTUAL_ENV", None)

    result = subprocess.run(
        [sys.executable, "-S", "-E", str(SCRIPT), "--help"],
        cwd=tmp_path,
        env=env,
        capture_output=True,
        text=True,
        check=False,
        timeout=15,
    )

    assert result.returncode == 0, result.stderr
    assert "usage:" in result.stdout


def test_core_wheel_validator_accepts_static_manylinux_wheel(tmp_path: Path) -> None:
    wheel = write_core_wheel(tmp_path)

    assert checker.check_core_wheel(wheel, checker.MAX_CORE_WHEEL_BYTES) == []


def test_core_wheel_validator_rejects_wrong_script_member(tmp_path: Path) -> None:
    wheel = write_core_wheel(
        tmp_path,
        script_names=(
            "solstone_core-1.2.3.data/scripts/sol",
            "solstone_core-1.2.3.data/scripts/solstone",
            "solstone_core-1.2.3.data/scripts/solstone-core-renamed",
        ),
    )

    errors = checker.check_core_wheel(wheel, checker.MAX_CORE_WHEEL_BYTES)

    assert any("wrong solstone-core script member set" in error for error in errors)


def test_core_wheel_validator_rejects_bare_linux_tag(tmp_path: Path) -> None:
    wheel = write_core_wheel(tmp_path, tag="linux_x86_64")

    errors = checker.check_core_wheel(wheel, checker.MAX_CORE_WHEEL_BYTES)

    assert any("unsupported solstone-core wheel tag" in error for error in errors)
    assert any("bare linux tag" in error for error in errors)


def test_core_wheel_validator_rejects_non_executable_binary(
    tmp_path: Path,
) -> None:
    wheel = write_core_wheel(tmp_path, executable=False)

    errors = checker.check_core_wheel(wheel, checker.MAX_CORE_WHEEL_BYTES)

    assert any("not executable" in error for error in errors)


def test_core_wheel_validator_rejects_record_drift(tmp_path: Path) -> None:
    wheel = write_core_wheel(tmp_path, record_ok=False)

    errors = checker.check_core_wheel(wheel, checker.MAX_CORE_WHEEL_BYTES)

    assert any("RECORD hash mismatch" in error for error in errors)


def test_core_wheel_validator_rejects_wrong_binary_format(tmp_path: Path) -> None:
    wheel = write_core_wheel(tmp_path, binary=b"not an elf")

    errors = checker.check_core_wheel(wheel, checker.MAX_CORE_WHEEL_BYTES)

    assert any("ELF binary is too short" in error for error in errors)


def test_core_wheel_validator_rejects_wrong_elf_architecture(tmp_path: Path) -> None:
    wheel = write_core_wheel(
        tmp_path,
        tag="manylinux_2_17_aarch64.manylinux2014_aarch64",
        binary=minimal_elf(checker.ELF_MACHINE["x86_64"]),
    )

    errors = checker.check_core_wheel(wheel, checker.MAX_CORE_WHEEL_BYTES)

    assert any("ELF machine does not match wheel tag" in error for error in errors)


def test_core_wheel_validator_rejects_elf_interp(tmp_path: Path) -> None:
    wheel = write_core_wheel(
        tmp_path,
        binary=minimal_elf(
            checker.ELF_MACHINE["x86_64"], program_type=checker.PT_INTERP
        ),
    )

    errors = checker.check_core_wheel(wheel, checker.MAX_CORE_WHEEL_BYTES)

    assert any("ELF binary has PT_INTERP" in error for error in errors)


def test_core_wheel_validator_rejects_elf_needed_entry(tmp_path: Path) -> None:
    wheel = write_core_wheel(
        tmp_path,
        binary=minimal_elf(
            checker.ELF_MACHINE["x86_64"],
            program_type=checker.PT_DYNAMIC,
            dynamic_needed=True,
        ),
    )

    errors = checker.check_core_wheel(wheel, checker.MAX_CORE_WHEEL_BYTES)

    assert any("ELF binary has DT_NEEDED" in error for error in errors)


def test_core_wheel_validator_rejects_wrong_macho_architecture(tmp_path: Path) -> None:
    wheel = write_core_wheel(
        tmp_path,
        tag="macosx_14_0_arm64",
        binary=minimal_macho(CPU_TYPE_X86_64),
    )

    errors = checker.check_core_wheel(wheel, checker.MAX_CORE_WHEEL_BYTES)

    assert any("Mach-O cputype does not match wheel tag" in error for error in errors)


def test_core_wheel_validator_accepts_fat_macho_with_arm64(tmp_path: Path) -> None:
    wheel = write_core_wheel(
        tmp_path,
        tag="macosx_14_0_arm64",
        binary=minimal_fat_macho([CPU_TYPE_X86_64, checker.CPU_TYPE_ARM64]),
    )

    assert checker.check_core_wheel(wheel, checker.MAX_CORE_WHEEL_BYTES) == []


def test_core_wheel_validator_rejects_fat_macho_without_arm64(tmp_path: Path) -> None:
    wheel = write_core_wheel(
        tmp_path,
        tag="macosx_14_0_arm64",
        binary=minimal_fat_macho([CPU_TYPE_X86_64]),
    )

    errors = checker.check_core_wheel(wheel, checker.MAX_CORE_WHEEL_BYTES)

    assert any("fat Mach-O has no arm64 slice" in error for error in errors)


def test_base_wheel_validator_rejects_tests_path_segment(tmp_path: Path) -> None:
    clean = _write_minimal_wheel(tmp_path, "solstone")

    clean_errors = checker.check_base_wheel(clean, checker.MAX_BASE_WHEEL_BYTES)
    assert not [
        error for error in clean_errors if "base wheel ships test path" in error
    ]

    dirty = _write_minimal_wheel(tmp_path, "solstone_dirty")
    test_member = "solstone/apps/search/tests/test_routes.py"
    with zipfile.ZipFile(dirty, "a") as wheel:
        _write_member(wheel, test_member, b"")

    dirty_errors = checker.check_base_wheel(dirty, checker.MAX_BASE_WHEEL_BYTES)
    assert any(
        f"base wheel ships test path {test_member}" in error for error in dirty_errors
    )


def test_base_wheel_platform_cap_allows_bundled_helper(tmp_path: Path) -> None:
    within = write_platform_base_wheel(
        tmp_path,
        extra_payload_size=5 * 1024 * 1024,
    )
    within_errors = checker.check_base_wheel(within, checker.MAX_BASE_WHEEL_BYTES)
    assert not [error for error in within_errors if "base wheel is" in error]

    oversized = write_platform_base_wheel(
        tmp_path / "oversized",
        extra_payload_size=7 * 1024 * 1024,
    )
    oversized_errors = checker.check_base_wheel(oversized, checker.MAX_BASE_WHEEL_BYTES)
    assert any("base wheel is" in error for error in oversized_errors)


def test_base_wheel_validator_rejects_missing_platform_helper(
    tmp_path: Path,
) -> None:
    wheel = write_platform_base_wheel(tmp_path, helper_name=None)

    errors = checker.check_base_wheel(wheel, checker.MAX_BASE_WHEEL_BYTES)

    assert any("wrong parakeet helper member count" in error for error in errors)


def test_base_wheel_validator_rejects_renamed_platform_helper(
    tmp_path: Path,
) -> None:
    wheel = write_platform_base_wheel(
        tmp_path,
        helper_name=f"{checker.PARAKEET_HELPER_MEMBER}-renamed",
    )

    errors = checker.check_base_wheel(wheel, checker.MAX_BASE_WHEEL_BYTES)

    assert any("wrong parakeet helper member count" in error for error in errors)


def test_base_wheel_validator_rejects_wrong_arch_platform_helper(
    tmp_path: Path,
) -> None:
    wheel = write_platform_base_wheel(
        tmp_path,
        helper_binary=minimal_macho(CPU_TYPE_X86_64),
    )

    errors = checker.check_base_wheel(wheel, checker.MAX_BASE_WHEEL_BYTES)

    assert any(
        "parakeet helper" in error
        and checker.PARAKEET_HELPER_MEMBER in error
        and "Mach-O cputype" in error
        for error in errors
    )


def _add_tar_member(archive: tarfile.TarFile, name: str) -> None:
    content = b"x"
    info = tarfile.TarInfo(name)
    info.size = len(content)
    archive.addfile(info, BytesIO(content))


def _write_core_sdist(path: Path, *, missing: str | None = None) -> Path:
    sdist = path / "solstone_core-1.2.3.tar.gz"
    with tarfile.open(sdist, "w:gz") as archive:
        for member in sorted(checker.CORE_REQUIRED_SDIST_MEMBERS):
            if member == missing:
                continue
            _add_tar_member(archive, f"solstone_core-1.2.3/{member}")
    return sdist


def _write_minimal_wheel(path: Path, name: str) -> Path:
    wheel_path = path / f"{name}-1.2.3-py3-none-any.whl"
    with zipfile.ZipFile(wheel_path, "w") as wheel:
        _write_member(
            wheel,
            f"{name}-1.2.3.dist-info/METADATA",
            f"Name: {name}\nVersion: 1.2.3\n".encode(),
        )
    return wheel_path


def _write_minimal_dist(path: Path) -> None:
    _write_minimal_wheel(path, "solstone")
    _write_minimal_wheel(path, "solstone_journal_models")
    _write_core_sdist(path)


def test_dist_check_requires_requested_core_platform(tmp_path: Path) -> None:
    _write_minimal_dist(tmp_path)
    write_core_wheel(tmp_path)

    errors = checker.check_dist(
        tmp_path,
        {},
        checker.MAX_BASE_WHEEL_BYTES,
        required_core_platforms=(("darwin", "arm64"),),
    )

    assert any("darwin/arm64" in error for error in errors)
    assert any("macosx_14_0_arm64" in error for error in errors)


def test_dist_check_accepts_requested_core_platform(tmp_path: Path) -> None:
    _write_minimal_dist(tmp_path)
    write_core_wheel(tmp_path)
    write_core_wheel(tmp_path, tag="macosx_14_0_arm64")

    errors = checker.check_dist(
        tmp_path,
        {},
        checker.MAX_BASE_WHEEL_BYTES,
        required_core_platforms=(("darwin", "arm64"),),
    )

    assert not [error for error in errors if "darwin/arm64" in error]


def test_release_artifact_manifest_requires_core_targets(tmp_path: Path) -> None:
    errors = checker.check_release_artifacts(
        tmp_path,
        release_scope="all-hosts",
        models_decision="skip",
    )

    assert any("core wheel for linux/x86_64" in error for error in errors)
    assert any("core wheel for linux/aarch64" in error for error in errors)
    assert any("core wheel for darwin/arm64" in error for error in errors)


def test_release_artifacts_derive_core_tags_from_probe(tmp_path: Path) -> None:
    artifacts = checker.release_artifacts(
        tmp_path,
        release_scope="all-hosts",
        models_decision="skip",
    )
    artifact_names = {path.name for path in artifacts}

    for tag in checker.SOLSTONE_CORE_PLATFORM_TAGS.values():
        assert any(
            name.startswith("solstone_core-") and name.endswith(f"-py3-none-{tag}.whl")
            for name in artifact_names
        )


def test_release_artifacts_include_models_only_when_gate_publishes(
    tmp_path: Path,
) -> None:
    publish_artifacts = checker.release_artifacts(
        tmp_path,
        release_scope="linux",
        models_decision="publish",
    )
    skip_artifacts = checker.release_artifacts(
        tmp_path,
        release_scope="linux",
        models_decision="skip",
    )

    assert any(
        path.name.startswith("solstone_journal_models-") for path in publish_artifacts
    )
    assert not any(
        path.name.startswith("solstone_journal_models-") for path in skip_artifacts
    )


def test_release_artifacts_exclude_core_unsupported_tombstone(
    tmp_path: Path,
) -> None:
    artifacts = checker.release_artifacts(
        tmp_path,
        release_scope="all-hosts",
        models_decision="skip",
    )

    assert not any(
        path.name.startswith("solstone_core_unsupported_platform-")
        for path in artifacts
    )


def test_core_sdist_validator_requires_rust_workspace_sources(
    tmp_path: Path,
) -> None:
    sdist = _write_core_sdist(tmp_path)

    assert checker.check_core_sdist(sdist) == []


def test_core_sdist_validator_rejects_missing_workspace_source(
    tmp_path: Path,
) -> None:
    sdist = _write_core_sdist(tmp_path, missing="core/crates/solstone-core/src/main.rs")

    errors = checker.check_core_sdist(sdist)

    assert any("missing Rust workspace members" in error for error in errors)
