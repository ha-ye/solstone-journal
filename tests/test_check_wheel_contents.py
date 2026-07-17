# SPDX-License-Identifier: AGPL-3.0-only
# Copyright (c) 2026 sol pbc

from __future__ import annotations

import base64
import hashlib
import tarfile
import zipfile
from io import BytesIO
from pathlib import Path

import scripts.check_wheel_contents as checker


def _record_hash(content: bytes) -> str:
    digest = hashlib.sha256(content).digest()
    encoded = base64.urlsafe_b64encode(digest).decode("ascii").rstrip("=")
    return f"sha256={encoded}"


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


def _write_core_wheel(
    path: Path,
    *,
    tag: str = "manylinux_2_17_x86_64.manylinux2014_x86_64",
    executable: bool = True,
    record_ok: bool = True,
) -> Path:
    wheel_path = path / f"solstone_core-1.2.3-py3-none-{tag}.whl"
    members = {
        "solstone_core-1.2.3.dist-info/METADATA": b"Name: solstone-core\nVersion: 1.2.3\n",
        "solstone_core-1.2.3.dist-info/WHEEL": b"Wheel-Version: 1.0\n",
        "solstone_core-1.2.3.data/scripts/solstone-core": b"#!/bin/sh\n",
    }
    rows = [
        f"{name},{_record_hash(content)},{len(content)}"
        for name, content in members.items()
    ]
    rows.append("solstone_core-1.2.3.dist-info/RECORD,,")
    record = "\n".join(rows).encode()
    if not record_ok:
        record = record.replace(b"sha256=", b"sha256=broken", 1)
    with zipfile.ZipFile(wheel_path, "w") as wheel:
        for name, content in members.items():
            mode = 0o755 if name.endswith("/solstone-core") and executable else 0o644
            _write_member(wheel, name, content, mode=mode)
        _write_member(wheel, "solstone_core-1.2.3.dist-info/RECORD", record)
    return wheel_path


def test_core_wheel_validator_accepts_static_manylinux_wheel(tmp_path: Path) -> None:
    wheel = _write_core_wheel(tmp_path)

    assert checker.check_core_wheel(wheel, checker.MAX_CORE_WHEEL_BYTES) == []


def test_core_wheel_validator_rejects_bare_linux_tag(tmp_path: Path) -> None:
    wheel = _write_core_wheel(tmp_path, tag="linux_x86_64")

    errors = checker.check_core_wheel(wheel, checker.MAX_CORE_WHEEL_BYTES)

    assert any("unsupported solstone-core wheel tag" in error for error in errors)
    assert any("bare linux tag" in error for error in errors)


def test_core_wheel_validator_rejects_non_executable_binary(
    tmp_path: Path,
) -> None:
    wheel = _write_core_wheel(tmp_path, executable=False)

    errors = checker.check_core_wheel(wheel, checker.MAX_CORE_WHEEL_BYTES)

    assert any("not executable" in error for error in errors)


def test_core_wheel_validator_rejects_record_drift(tmp_path: Path) -> None:
    wheel = _write_core_wheel(tmp_path, record_ok=False)

    errors = checker.check_core_wheel(wheel, checker.MAX_CORE_WHEEL_BYTES)

    assert any("RECORD hash mismatch" in error for error in errors)


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
