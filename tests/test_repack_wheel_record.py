# SPDX-License-Identifier: AGPL-3.0-only
# Copyright (c) 2026 sol pbc

from __future__ import annotations

import base64
import hashlib
import zipfile
from pathlib import Path

import scripts.repack_wheel_record as repacker


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


def _write_core_wheel(path: Path) -> Path:
    wheel_path = path / "solstone_core-1.2.3-py3-none-macosx_14_0_arm64.whl"
    members = {
        "solstone_core-1.2.3.dist-info/METADATA": b"Name: solstone-core\nVersion: 1.2.3\n",
        "solstone_core-1.2.3.dist-info/WHEEL": b"Wheel-Version: 1.0\n",
        "solstone_core-1.2.3.data/scripts/solstone-core": b"#!/bin/sh\necho before\n",
    }
    rows = [
        f"{name},{_record_hash(content)},{len(content)}"
        for name, content in members.items()
    ]
    rows.append("solstone_core-1.2.3.dist-info/RECORD,,")

    with zipfile.ZipFile(wheel_path, "w") as wheel:
        for name, content in members.items():
            mode = 0o755 if name.endswith("/solstone-core") else 0o644
            _write_member(wheel, name, content, mode=mode)
        _write_member(
            wheel,
            "solstone_core-1.2.3.dist-info/RECORD",
            "\n".join(rows).encode(),
        )
    return wheel_path


def _record_rows(wheel: zipfile.ZipFile) -> dict[str, tuple[str, str]]:
    record_name = next(name for name in wheel.namelist() if name.endswith("/RECORD"))
    rows: dict[str, tuple[str, str]] = {}
    for row in wheel.read(record_name).decode("utf-8").splitlines():
        member, hash_value, size = row.split(",")
        rows[member] = (hash_value, size)
    return rows


def test_repack_preserves_original_executable_mode_and_rewrites_record(
    tmp_path: Path,
) -> None:
    wheel_path = _write_core_wheel(tmp_path)
    unpacked = tmp_path / "unpacked"
    with zipfile.ZipFile(wheel_path) as wheel:
        wheel.extractall(unpacked)

    binary = unpacked / "solstone_core-1.2.3.data" / "scripts" / "solstone-core"
    # zipfile extraction applies the caller's umask, so the write bits may be
    # 0644 or 0664. The contract only needs to prove that extraction dropped
    # the archived executable bits before repack restores the original attrs.
    assert (binary.stat().st_mode & 0o111) == 0
    signed_content = b"#!/bin/sh\necho signed\n"
    binary.write_bytes(signed_content)

    repacker.repack(unpacked, wheel_path)

    with zipfile.ZipFile(wheel_path) as wheel:
        info = wheel.getinfo(
            "solstone_core-1.2.3.data/scripts/solstone-core",
        )
        assert ((info.external_attr >> 16) & 0o777) == 0o755
        rows = _record_rows(wheel)
        record_hash, record_size = rows[
            "solstone_core-1.2.3.data/scripts/solstone-core"
        ]
        assert record_hash == _record_hash(signed_content)
        assert record_size == str(len(signed_content))
