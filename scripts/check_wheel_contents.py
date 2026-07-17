#!/usr/bin/env python3
# SPDX-License-Identifier: AGPL-3.0-only
# Copyright (c) 2026 sol pbc

"""Check the solstone/model wheel split after a workspace build."""

from __future__ import annotations

import argparse
import base64
import hashlib
import re
import sys
import tarfile
import zipfile
from pathlib import Path

from solstone.think.probe import (
    SOLSTONE_CORE_PLATFORM_TAGS,
    current_solstone_core_platform,
    is_solstone_core_covered_platform,
)

EXPECTED_MODEL_SHA256 = {
    "silero_vad_v6.onnx": (
        "4cbf549b8326f60f80f2536d9eefeb450a9abe83365a098031c89719f1be17d2"
    ),
    "pyannote-segmentation-3.0.onnx": (
        "057ee564753071c0b09b5b611648b50ac188d50846bff5f01e9f7bbf1591ea25"
    ),
    "wespeaker-resnet34-256.onnx": (
        "5ef208a9da1453335308a6b6f4e6dfbd7e183a38b604de0a57664f45d257fe94"
    ),
}
MAX_BASE_WHEEL_BYTES = 4 * 1024 * 1024
MAX_CORE_WHEEL_BYTES = 30 * 1024 * 1024
CORE_REQUIRED_SDIST_MEMBERS = {
    "core/Cargo.lock",
    "core/Cargo.toml",
    "core/crates/solstone-core/Cargo.toml",
    "core/crates/solstone-core/src/main.rs",
    "core/crates/solstone-core-cli/Cargo.toml",
    "core/crates/solstone-core-cli/src/lib.rs",
    "core/crates/solstone-core-journal/Cargo.toml",
    "core/crates/solstone-core-journal/src/lib.rs",
}


def _is_base_wheel(path: Path) -> bool:
    return bool(re.match(r"solstone-\d", path.name))


def _is_models_wheel(path: Path) -> bool:
    return path.name.startswith("solstone_journal_models-")


def _is_core_wheel(path: Path) -> bool:
    return path.name.startswith("solstone_core-") and path.name.endswith(".whl")


def _is_core_sdist(path: Path) -> bool:
    return path.name.startswith("solstone_core-") and path.name.endswith(".tar.gz")


def _core_wheel_tag(path: Path) -> str:
    stem = path.name.removesuffix(".whl")
    return stem.split("-")[-1]


def _onnx_members(path: Path) -> list[str]:
    with zipfile.ZipFile(path) as wheel:
        return [name for name in wheel.namelist() if name.endswith(".onnx")]


def check_base_wheel(path: Path, max_bytes: int) -> list[str]:
    errors: list[str] = []
    size = path.stat().st_size
    if size > max_bytes:
        errors.append(f"{path.name}: base wheel is {size} bytes; max is {max_bytes}")
    onnx_members = _onnx_members(path)
    if onnx_members:
        errors.append(f"{path.name}: base wheel contains ONNX members: {onnx_members}")
    return errors


def check_models_wheel(path: Path, expected: dict[str, str]) -> list[str]:
    errors: list[str] = []
    with zipfile.ZipFile(path) as wheel:
        onnx_members = [name for name in wheel.namelist() if name.endswith(".onnx")]
        basenames = [Path(name).name for name in onnx_members]
        expected_names = set(expected)
        found_names = set(basenames)
        if len(onnx_members) != len(expected) or found_names != expected_names:
            errors.append(
                f"{path.name}: expected ONNX basenames {sorted(expected_names)}, "
                f"found {sorted(basenames)}"
            )
        for member, basename in zip(onnx_members, basenames):
            expected_sha256 = expected.get(basename)
            if expected_sha256 is None:
                continue
            actual_sha256 = hashlib.sha256(wheel.read(member)).hexdigest()
            if actual_sha256 != expected_sha256:
                errors.append(
                    f"{path.name}: {basename} sha256 mismatch; "
                    f"expected {expected_sha256}, actual {actual_sha256}"
                )
    return errors


def _record_hash(content: bytes) -> str:
    digest = hashlib.sha256(content).digest()
    encoded = base64.urlsafe_b64encode(digest).decode("ascii").rstrip("=")
    return f"sha256={encoded}"


def _check_record(path: Path, wheel: zipfile.ZipFile) -> list[str]:
    errors: list[str] = []
    names = set(wheel.namelist())
    record_names = [name for name in names if name.endswith(".dist-info/RECORD")]
    if len(record_names) != 1:
        return [f"{path.name}: expected exactly one RECORD, found {len(record_names)}"]

    record_name = record_names[0]
    rows = wheel.read(record_name).decode("utf-8").splitlines()
    seen: set[str] = set()
    for row in rows:
        columns = row.split(",")
        if len(columns) != 3:
            errors.append(f"{path.name}: malformed RECORD row: {row!r}")
            continue
        member, expected_hash, expected_size = columns
        if member not in names:
            errors.append(f"{path.name}: RECORD references missing member {member}")
            continue
        seen.add(member)
        if member == record_name:
            if expected_hash or expected_size:
                errors.append(
                    f"{path.name}: RECORD row for RECORD must have empty hash/size"
                )
            continue
        content = wheel.read(member)
        if expected_hash != _record_hash(content):
            errors.append(f"{path.name}: RECORD hash mismatch for {member}")
        if expected_size != str(len(content)):
            errors.append(f"{path.name}: RECORD size mismatch for {member}")

    missing = sorted(names - seen)
    if missing:
        errors.append(f"{path.name}: members missing from RECORD: {missing}")
    return errors


def check_core_wheel(path: Path, max_bytes: int) -> list[str]:
    errors: list[str] = []
    size = path.stat().st_size
    if size > max_bytes:
        errors.append(f"{path.name}: core wheel is {size} bytes; max is {max_bytes}")

    tag = _core_wheel_tag(path)
    allowed_tags = set(SOLSTONE_CORE_PLATFORM_TAGS.values())
    if tag not in allowed_tags:
        errors.append(f"{path.name}: unsupported solstone-core wheel tag {tag}")
    if "-linux_" in path.name:
        errors.append(f"{path.name}: bare linux tag is not publishable")

    with zipfile.ZipFile(path) as wheel:
        scripts = [
            info
            for info in wheel.infolist()
            if info.filename.endswith(".data/scripts/solstone-core")
        ]
        if len(scripts) != 1:
            errors.append(
                f"{path.name}: expected exactly one .data/scripts/solstone-core; "
                f"found {len(scripts)}"
            )
        elif ((scripts[0].external_attr >> 16) & 0o111) == 0:
            errors.append(f"{path.name}: solstone-core script is not executable")
        errors.extend(_check_record(path, wheel))

    return errors


def check_core_sdist(path: Path) -> list[str]:
    errors: list[str] = []
    with tarfile.open(path, "r:gz") as archive:
        names = archive.getnames()
    prefixes = {name.split("/", 1)[0] for name in names if "/" in name}
    if len(prefixes) != 1:
        return [
            f"{path.name}: expected one top-level sdist directory, found {sorted(prefixes)}"
        ]
    prefix = next(iter(prefixes))
    normalized = {
        name.removeprefix(prefix + "/")
        for name in names
        if name.startswith(prefix + "/")
    }
    missing = sorted(CORE_REQUIRED_SDIST_MEMBERS - normalized)
    if missing:
        errors.append(
            f"{path.name}: core sdist missing Rust workspace members: {missing}"
        )
    return errors


def check_dist(dist_dir: Path, expected: dict[str, str], max_bytes: int) -> list[str]:
    errors: list[str] = []
    wheels = sorted(dist_dir.glob("*.whl"))
    base_wheels = [path for path in wheels if _is_base_wheel(path)]
    models_wheels = [path for path in wheels if _is_models_wheel(path)]
    core_wheels = [path for path in wheels if _is_core_wheel(path)]
    core_sdists = sorted(
        path for path in dist_dir.glob("*.tar.gz") if _is_core_sdist(path)
    )

    if not base_wheels:
        errors.append(f"{dist_dir}: no solstone base wheel found")
    if not models_wheels:
        errors.append(f"{dist_dir}: no solstone_journal_models wheel found")
    system, machine = current_solstone_core_platform()
    if is_solstone_core_covered_platform(system, machine) and not core_wheels:
        errors.append(
            f"{dist_dir}: no solstone_core wheel found for {system}/{machine}"
        )
    if not core_sdists:
        errors.append(f"{dist_dir}: no solstone_core sdist found")

    for path in base_wheels:
        errors.extend(check_base_wheel(path, max_bytes))
    for path in models_wheels:
        errors.extend(check_models_wheel(path, expected))
    for path in core_wheels:
        errors.extend(check_core_wheel(path, MAX_CORE_WHEEL_BYTES))
    for path in core_sdists:
        errors.extend(check_core_sdist(path))

    return errors


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("dist_dir", type=Path)
    args = parser.parse_args(argv)

    errors = check_dist(args.dist_dir, EXPECTED_MODEL_SHA256, MAX_BASE_WHEEL_BYTES)
    if errors:
        print("ERROR: wheel content check failed", file=sys.stderr)
        for error in errors:
            print(f"  {error}", file=sys.stderr)
        return 1
    print("wheel contents ok")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
