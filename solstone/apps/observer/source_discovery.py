# SPDX-License-Identifier: AGPL-3.0-only
# Copyright (c) 2026 sol pbc

"""Read-only discovery helpers for observer-owned source files."""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path

from solstone.think.segment_files import RESERVED_SEGMENT_FILENAMES
from solstone.think.utils import day_dirs, iter_segments

LOCATION_ORIGINAL = "location.jsonl"
# Files that don't count as "other content" when deciding location-only vs mixed.
_NON_CONTENT_NAMES = {LOCATION_ORIGINAL, "item.json"} | RESERVED_SEGMENT_FILENAMES


@dataclass(frozen=True)
class LocationSource:
    day: str
    stream: str
    segment: str
    path: Path
    is_mixed: bool


def _segment_has_other_content(seg_path: Path) -> bool:
    """True if the segment holds anything besides the location original + markers."""
    return any(child.name not in _NON_CONTENT_NAMES for child in seg_path.iterdir())


def find_location_sources() -> list[LocationSource]:
    """Scan every day/stream for segments containing location.jsonl on disk."""
    sources: list[LocationSource] = []
    for day in sorted(day_dirs()):
        for stream, segment, seg_path in iter_segments(day):
            if not (seg_path / LOCATION_ORIGINAL).is_file():
                continue
            sources.append(
                LocationSource(
                    day=day,
                    stream=stream,
                    segment=segment,
                    path=seg_path,
                    is_mixed=_segment_has_other_content(seg_path),
                )
            )
    return sources
