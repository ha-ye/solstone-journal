# SPDX-License-Identifier: AGPL-3.0-only
# Copyright (c) 2026 sol pbc

"""Read-only measurement helpers for media offload."""

from __future__ import annotations

import shutil
from dataclasses import dataclass
from pathlib import Path

from solstone.think.retention import get_raw_media_files
from solstone.think.utils import day_dirs, get_journal, iter_segments

MIN_FLOOR_BYTES = 20_000_000_000


@dataclass(frozen=True)
class RawMediaDayUsage:
    day: str
    bytes: int
    files: int


@dataclass(frozen=True)
class RawMediaUsage:
    total_bytes: int
    total_files: int
    per_day: tuple[RawMediaDayUsage, ...]


@dataclass(frozen=True)
class SuggestedOffloadDefaults:
    budget_bytes: int
    floor_bytes: int


def measure_raw_media_usage() -> RawMediaUsage:
    per_day: list[RawMediaDayUsage] = []
    total_bytes = 0
    total_files = 0

    for day in sorted(day_dirs().keys()):
        day_bytes = 0
        day_files = 0
        for _stream, _segment, segment_path in iter_segments(day):
            for raw_file in get_raw_media_files(segment_path):
                try:
                    size = raw_file.stat().st_size
                except FileNotFoundError:
                    continue
                day_bytes += size
                day_files += 1
        per_day.append(RawMediaDayUsage(day=day, bytes=day_bytes, files=day_files))
        total_bytes += day_bytes
        total_files += day_files

    return RawMediaUsage(
        total_bytes=total_bytes,
        total_files=total_files,
        per_day=tuple(per_day),
    )


def device_free_bytes() -> int:
    return shutil.disk_usage(Path(get_journal())).free


def suggest_offload_defaults(total_bytes: int) -> SuggestedOffloadDefaults:
    if type(total_bytes) is not int or total_bytes <= 0:
        raise ValueError("total_bytes must be a positive integer")
    budget = total_bytes // 2
    floor = min(max(total_bytes // 10, MIN_FLOOR_BYTES), total_bytes // 4)
    return SuggestedOffloadDefaults(budget_bytes=budget, floor_bytes=floor)


__all__ = [
    "MIN_FLOOR_BYTES",
    "RawMediaDayUsage",
    "RawMediaUsage",
    "SuggestedOffloadDefaults",
    "device_free_bytes",
    "measure_raw_media_usage",
    "suggest_offload_defaults",
]
