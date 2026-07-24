# SPDX-License-Identifier: AGPL-3.0-only
# Copyright (c) 2026 sol pbc

"""Shared release wheel fixture builders."""

from __future__ import annotations

import base64
import hashlib
import struct
import zipfile
from pathlib import Path
from collections.abc import Mapping, Sequence

import scripts.check_wheel_contents as checker

ELF_HEADER_SIZE = 64
ELF_PROGRAM_HEADER_SIZE = 56


def record_hash(content: bytes) -> str:
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


def minimal_elf(
    machine: int, *, program_type: int = 1, dynamic_needed: bool = False
) -> bytes:
    dynamic_offset = ELF_HEADER_SIZE + ELF_PROGRAM_HEADER_SIZE
    dynamic_size = 32 if dynamic_needed else 0
    content = bytearray(dynamic_offset + dynamic_size)
    content[:16] = b"\x7fELF\x02\x01\x01" + b"\0" * 9
    struct.pack_into("<H", content, 16, 2)
    struct.pack_into("<H", content, 18, machine)
    struct.pack_into("<I", content, 20, 1)
    struct.pack_into("<Q", content, 32, ELF_HEADER_SIZE)
    struct.pack_into("<H", content, 52, ELF_HEADER_SIZE)
    struct.pack_into("<H", content, 54, ELF_PROGRAM_HEADER_SIZE)
    struct.pack_into("<H", content, 56, 1)
    struct.pack_into("<I", content, ELF_HEADER_SIZE, program_type)
    if dynamic_needed:
        struct.pack_into("<Q", content, ELF_HEADER_SIZE + 8, dynamic_offset)
        struct.pack_into("<Q", content, ELF_HEADER_SIZE + 32, dynamic_size)
        struct.pack_into("<qQ", content, dynamic_offset, checker.DT_NEEDED, 1)
        struct.pack_into("<qQ", content, dynamic_offset + 16, checker.DT_NULL, 0)
    return bytes(content)


def minimal_macho(cputype: int) -> bytes:
    content = bytearray(32)
    struct.pack_into("<I", content, 0, checker.MH_MAGIC_64)
    struct.pack_into("<I", content, 4, cputype)
    return bytes(content)


def minimal_fat_macho(cputypes: list[int]) -> bytes:
    content = bytearray(8 + checker.FAT_ARCH_SIZE * len(cputypes))
    struct.pack_into(">I", content, 0, checker.FAT_MAGIC)
    struct.pack_into(">I", content, 4, len(cputypes))
    for index, cputype in enumerate(cputypes):
        struct.pack_into(">I", content, 8 + checker.FAT_ARCH_SIZE * index, cputype)
    return bytes(content)


def write_core_wheel(
    path: Path,
    *,
    tag: str = "manylinux_2_17_x86_64.manylinux2014_x86_64",
    executable: bool = True,
    record_ok: bool = True,
    script_names: Sequence[str] | None = None,
    binary: bytes | None = None,
    binaries: Mapping[str, bytes] | None = None,
    version: str = "1.2.3",
) -> Path:
    wheel_path = path / f"solstone_core-{version}-py3-none-{tag}.whl"
    if binary is None:
        if "aarch64" in tag:
            binary = minimal_elf(checker.ELF_MACHINE["aarch64"])
        elif "macosx" in tag:
            binary = minimal_macho(checker.CPU_TYPE_ARM64)
        else:
            binary = minimal_elf(checker.ELF_MACHINE["x86_64"])
    if script_names is None:
        script_names = tuple(
            f"solstone_core-{version}.data/scripts/{name}"
            for name in checker.CORE_SCRIPT_NAMES
        )
    members = {
        f"solstone_core-{version}.dist-info/METADATA": (
            f"Name: solstone-core\nVersion: {version}\n".encode()
        ),
        f"solstone_core-{version}.dist-info/WHEEL": b"Wheel-Version: 1.0\n",
    }
    for script_name in script_names:
        members[script_name] = binaries.get(script_name, binary) if binaries else binary
    rows = [
        f"{name},{record_hash(content)},{len(content)}"
        for name, content in members.items()
    ]
    rows.append(f"solstone_core-{version}.dist-info/RECORD,,")
    record = "\n".join(rows).encode()
    if not record_ok:
        record = record.replace(b"sha256=", b"sha256=broken", 1)
    with zipfile.ZipFile(wheel_path, "w") as wheel:
        for name, content in members.items():
            mode = (
                0o755
                if Path(name).name in checker.CORE_SCRIPT_NAMES and executable
                else 0o644
            )
            _write_member(wheel, name, content, mode=mode)
        _write_member(wheel, f"solstone_core-{version}.dist-info/RECORD", record)
    return wheel_path


def write_platform_base_wheel(
    path: Path,
    *,
    helper_name: str | None = checker.PARAKEET_HELPER_MEMBER,
    helper_binary: bytes | None = None,
    helper_mode: int = 0o755,
    extra_payload_size: int = 0,
    version: str = "1.2.3",
) -> Path:
    path.mkdir(parents=True, exist_ok=True)
    wheel_path = path / f"solstone-{version}-py3-none-macosx_14_0_arm64.whl"
    if helper_binary is None:
        helper_binary = minimal_macho(checker.CPU_TYPE_ARM64)
    members = {
        f"solstone-{version}.dist-info/METADATA": (
            f"Name: solstone\nVersion: {version}\n".encode()
        ),
        f"solstone-{version}.dist-info/WHEEL": b"Wheel-Version: 1.0\n",
    }
    if helper_name is not None:
        members[helper_name] = helper_binary
    if extra_payload_size:
        members["solstone/observe/transcribe/parakeet_helper/_bin/payload"] = (
            b"x" * extra_payload_size
        )
    rows = [
        f"{name},{record_hash(content)},{len(content)}"
        for name, content in members.items()
    ]
    rows.append(f"solstone-{version}.dist-info/RECORD,,")
    with zipfile.ZipFile(wheel_path, "w") as wheel:
        for name, content in members.items():
            mode = helper_mode if name == helper_name else 0o644
            _write_member(wheel, name, content, mode=mode)
        _write_member(
            wheel,
            f"solstone-{version}.dist-info/RECORD",
            "\n".join(rows).encode("utf-8"),
        )
    return wheel_path
