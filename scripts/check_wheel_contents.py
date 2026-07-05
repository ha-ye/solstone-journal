#!/usr/bin/env python3
# SPDX-License-Identifier: AGPL-3.0-only
# Copyright (c) 2026 sol pbc

"""Check the solstone/model wheel split after a workspace build."""

from __future__ import annotations

import argparse
import hashlib
import re
import sys
import zipfile
from pathlib import Path

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


def _is_base_wheel(path: Path) -> bool:
    return bool(re.match(r"solstone-\d", path.name))


def _is_models_wheel(path: Path) -> bool:
    return path.name.startswith("solstone_journal_models-")


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


def check_dist(dist_dir: Path, expected: dict[str, str], max_bytes: int) -> list[str]:
    errors: list[str] = []
    wheels = sorted(dist_dir.glob("*.whl"))
    base_wheels = [path for path in wheels if _is_base_wheel(path)]
    models_wheels = [path for path in wheels if _is_models_wheel(path)]

    if not base_wheels:
        errors.append(f"{dist_dir}: no solstone base wheel found")
    if not models_wheels:
        errors.append(f"{dist_dir}: no solstone_journal_models wheel found")

    for path in base_wheels:
        errors.extend(check_base_wheel(path, max_bytes))
    for path in models_wheels:
        errors.extend(check_models_wheel(path, expected))

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
