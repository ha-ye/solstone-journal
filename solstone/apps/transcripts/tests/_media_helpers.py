# SPDX-License-Identifier: AGPL-3.0-only
# Copyright (c) 2026 sol pbc

"""Media fixture helpers for transcripts app tests."""

import subprocess
from pathlib import Path


def build_moov_at_tail_m4a(path: Path, duration_seconds: float) -> None:
    """Write a non-faststart M4A whose moov atom remains at the file tail."""
    subprocess.run(
        [
            "ffmpeg",
            "-y",
            "-f",
            "lavfi",
            "-i",
            f"sine=frequency=440:duration={duration_seconds:g}",
            "-c:a",
            "aac",
            "-b:a",
            "64k",
            str(path),
        ],
        check=True,
        capture_output=True,
    )


def read_true_duration_seconds(path: Path) -> float:
    """Read the true media duration from ffprobe."""
    result = subprocess.run(
        [
            "ffprobe",
            "-v",
            "error",
            "-show_entries",
            "format=duration",
            "-of",
            "default=noprint_wrappers=1:nokey=1",
            str(path),
        ],
        check=True,
        capture_output=True,
        text=True,
    )
    return float(result.stdout.strip())


def top_level_atom_order(path: Path) -> list[str]:
    """Return ordered top-level MP4 atom fourcc names."""
    order = []
    with path.open("rb") as handle:
        while True:
            header = handle.read(8)
            if not header:
                return order
            if len(header) != 8:
                raise ValueError(f"incomplete MP4 atom header in {path}")

            size = int.from_bytes(header[:4], "big")
            fourcc = header[4:8].decode("latin-1")
            if size == 1:
                largesize = handle.read(8)
                if len(largesize) != 8:
                    raise ValueError(f"incomplete extended MP4 atom header in {path}")
                atom_size = int.from_bytes(largesize, "big")
                payload_size = atom_size - 16
            elif size == 0:
                order.append(fourcc)
                return order
            else:
                payload_size = size - 8

            if payload_size < 0:
                raise ValueError(f"invalid MP4 atom size in {path}")

            order.append(fourcc)
            handle.seek(payload_size, 1)


def head_bytes(path: Path, n: int) -> bytes:
    """Return the first n bytes of path."""
    with path.open("rb") as handle:
        return handle.read(n)
