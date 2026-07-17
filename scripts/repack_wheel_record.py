#!/usr/bin/env python3
# SPDX-License-Identifier: AGPL-3.0-only
# Copyright (c) 2026 sol pbc

"""Repack an unpacked wheel directory after rewriting RECORD."""

from __future__ import annotations

import argparse
import base64
import csv
import hashlib
import os
import tempfile
import zipfile
from pathlib import Path


def _record_hash(path: Path) -> str:
    digest = hashlib.sha256(path.read_bytes()).digest()
    encoded = base64.urlsafe_b64encode(digest).decode("ascii").rstrip("=")
    return f"sha256={encoded}"


def _relative_files(root: Path) -> list[Path]:
    return sorted(path for path in root.rglob("*") if path.is_file())


def _original_file_attrs(wheel_path: Path) -> dict[str, tuple[int, int]]:
    with zipfile.ZipFile(wheel_path) as wheel:
        return {
            info.filename: (info.external_attr, info.create_system)
            for info in wheel.infolist()
            if not info.is_dir()
        }


def _rewrite_record(root: Path) -> None:
    record_paths = list(root.glob("*.dist-info/RECORD"))
    if len(record_paths) != 1:
        raise SystemExit(
            f"expected exactly one *.dist-info/RECORD, found {len(record_paths)}"
        )
    record_path = record_paths[0]

    rows: list[list[str]] = []
    for path in _relative_files(root):
        arcname = path.relative_to(root).as_posix()
        if path == record_path:
            continue
        rows.append([arcname, _record_hash(path), str(path.stat().st_size)])
    rows.append([record_path.relative_to(root).as_posix(), "", ""])

    with record_path.open("w", encoding="utf-8", newline="") as handle:
        csv.writer(handle).writerows(rows)


def repack(unpacked_dir: Path, wheel_path: Path) -> None:
    unpacked_dir = unpacked_dir.resolve()
    wheel_path = wheel_path.resolve()
    if not unpacked_dir.is_dir():
        raise SystemExit(f"unpacked wheel directory does not exist: {unpacked_dir}")
    if not wheel_path.name.endswith(".whl"):
        raise SystemExit(f"wheel path must end in .whl: {wheel_path}")

    original_attrs = _original_file_attrs(wheel_path)
    _rewrite_record(unpacked_dir)
    fd, tmp_name = tempfile.mkstemp(
        prefix=f".{wheel_path.name}.", suffix=".tmp", dir=str(wheel_path.parent)
    )
    os.close(fd)
    tmp_path = Path(tmp_name)
    try:
        with zipfile.ZipFile(
            tmp_path, "w", compression=zipfile.ZIP_DEFLATED
        ) as archive:
            for path in _relative_files(unpacked_dir):
                arcname = path.relative_to(unpacked_dir).as_posix()
                info = zipfile.ZipInfo(arcname)
                info.compress_type = zipfile.ZIP_DEFLATED
                if arcname in original_attrs:
                    info.external_attr, info.create_system = original_attrs[arcname]
                else:
                    info.create_system = 3
                    info.external_attr = (path.stat().st_mode & 0o777) << 16
                archive.writestr(info, path.read_bytes())
        os.replace(tmp_path, wheel_path)
    finally:
        tmp_path.unlink(missing_ok=True)


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("unpacked_dir", type=Path)
    parser.add_argument("wheel_path", type=Path)
    args = parser.parse_args()
    repack(args.unpacked_dir, args.wheel_path)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
