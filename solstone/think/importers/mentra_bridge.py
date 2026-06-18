# SPDX-License-Identifier: AGPL-3.0-only
# Copyright (c) 2026 sol pbc

"""Importer for MentraOS Solstone bridge exports."""

from __future__ import annotations

import datetime as dt
import re
from dataclasses import dataclass
from pathlib import Path
from typing import Callable

from solstone.observe.utils import find_available_segment
from solstone.think.importers.file_importer import ImportPreview, ImportResult
from solstone.think.importers.shared import install_source_file
from solstone.think.journal_io import write_json

DAY_RE = re.compile(r"^\d{8}$")
SEGMENT_RE = re.compile(r"^\d{6}_\d+$")
BRIDGE_DIR = "solstone-bridge-demo"
TARGET_STREAM = "import.mentra"


@dataclass(frozen=True)
class BridgeSegment:
    day: str
    source_stream: str
    segment: str
    path: Path
    files: tuple[Path, ...]


def _resolve_bridge_root(path: Path) -> Path | None:
    candidate = path.expanduser().resolve()
    candidates = [candidate]
    if candidate.is_dir():
        candidates.append(candidate / BRIDGE_DIR)
        candidates.append(candidate / "data" / BRIDGE_DIR)

    for root in candidates:
        if (root / "manifest.jsonl").is_file() and (root / "chronicle").is_dir():
            return root
    return None


def _iter_bridge_segments(root: Path) -> list[BridgeSegment]:
    chronicle = root / "chronicle"
    segments: list[BridgeSegment] = []
    if not chronicle.is_dir():
        return segments

    for day_dir in sorted(chronicle.iterdir()):
        if not day_dir.is_dir() or not DAY_RE.match(day_dir.name):
            continue
        for stream_dir in sorted(day_dir.iterdir()):
            if not stream_dir.is_dir():
                continue
            for segment_dir in sorted(stream_dir.iterdir()):
                if not segment_dir.is_dir() or not SEGMENT_RE.match(segment_dir.name):
                    continue
                files = tuple(sorted(p for p in segment_dir.iterdir() if p.is_file()))
                if files:
                    segments.append(
                        BridgeSegment(
                            day=day_dir.name,
                            source_stream=stream_dir.name,
                            segment=segment_dir.name,
                            path=segment_dir,
                            files=files,
                        )
                    )
    return segments


def _copy_bridge_source(root: Path, journal_root: Path, import_id: str) -> list[str]:
    copied: list[str] = []
    target_root = journal_root / "imports" / import_id / "mentra_bridge"
    for src in sorted(p for p in root.rglob("*") if p.is_file()):
        dest = target_root / src.relative_to(root)
        install_source_file(src, dest)
        copied.append(str(dest))
    return copied


def _install_segment(
    segment: BridgeSegment,
    journal_root: Path,
) -> tuple[str, list[str], str | None]:
    stream_dir = journal_root / "chronicle" / segment.day / TARGET_STREAM
    stream_dir.mkdir(parents=True, exist_ok=True)
    target_segment = find_available_segment(stream_dir, segment.segment)
    if target_segment is None:
        return (
            segment.segment,
            [],
            f"no available segment slot for {segment.day}/{segment.segment}",
        )

    target_dir = stream_dir / target_segment
    target_dir.mkdir(parents=True, exist_ok=True)

    created: list[str] = []
    for src in segment.files:
        dest = target_dir / src.name
        install_source_file(src, dest)
        created.append(str(dest))

    write_json(
        target_dir / "mentra_bridge.json",
        {
            "source": "mentra",
            "source_stream": segment.source_stream,
            "source_segment": segment.segment,
            "imported_at": dt.datetime.now(dt.timezone.utc).isoformat(),
        },
    )
    created.append(str(target_dir / "mentra_bridge.json"))

    return target_segment, created, None


class MentraBridgeImporter:
    name = "mentra"
    display_name = "MentraOS Solstone Bridge"
    file_patterns = ["solstone-bridge-demo/"]
    description = (
        "Import MentraOS transcript, photo, raw audio, and signal bridge packets."
    )

    def detect(self, path: Path) -> bool:
        return _resolve_bridge_root(path) is not None

    def preview(self, path: Path) -> ImportPreview:
        root = _resolve_bridge_root(path)
        if root is None:
            return ImportPreview(
                date_range=("", ""),
                item_count=0,
                entity_count=0,
                summary="No Mentra bridge found",
            )

        segments = _iter_bridge_segments(root)
        days = sorted({segment.day for segment in segments})
        file_count = sum(len(segment.files) for segment in segments)
        if not days:
            date_range = ("", "")
        else:
            date_range = (days[0], days[-1])

        return ImportPreview(
            date_range=date_range,
            item_count=len(segments),
            entity_count=0,
            summary=f"{len(days)} day(s), {len(segments)} segment(s), {file_count} file(s)",
        )

    def process(
        self,
        path: Path,
        journal_root: Path,
        *,
        facet: str | None = None,
        import_id: str | None = None,
        progress_callback: Callable | None = None,
        dry_run: bool = False,
    ) -> ImportResult:
        root = _resolve_bridge_root(path)
        if root is None:
            return ImportResult(
                entries_written=0,
                entities_seeded=0,
                files_created=[],
                errors=[f"No Mentra bridge found at {path}"],
                summary="No Mentra bridge found",
                segments=[],
                date_range=None,
            )

        import_id = import_id or dt.datetime.now().strftime("%Y%m%d_%H%M%S")
        segments = _iter_bridge_segments(root)
        preview = self.preview(path)
        if dry_run:
            return ImportResult(
                entries_written=preview.item_count,
                entities_seeded=0,
                files_created=[],
                errors=[],
                summary=preview.summary,
                segments=[],
                date_range=preview.date_range,
            )

        source_files = _copy_bridge_source(root, journal_root, import_id)
        created_files: list[str] = []
        imported_segments: list[tuple[str, str]] = []
        errors: list[str] = []

        total = len(segments)
        for index, segment in enumerate(segments, 1):
            target_segment, files, error = _install_segment(segment, journal_root)
            if error:
                errors.append(error)
            else:
                imported_segments.append((segment.day, target_segment))
                created_files.extend(files)
            if progress_callback:
                progress_callback(index, total)

        import_meta_path = journal_root / "imports" / import_id / "mentra_bridge.json"
        write_json(
            import_meta_path,
            {
                "source": "mentra",
                "source_path": str(root),
                "facet": facet,
                "source_files": source_files,
                "segments": [
                    {"day": day, "segment": segment}
                    for day, segment in imported_segments
                ],
                "errors": errors,
            },
        )

        days = sorted({day for day, _segment in imported_segments})
        date_range = (days[0], days[-1]) if days else preview.date_range
        summary = (
            f"Imported {len(imported_segments)} Mentra segment(s) "
            f"and staged {len(source_files)} source file(s)"
        )

        return ImportResult(
            entries_written=len(imported_segments),
            entities_seeded=0,
            files_created=created_files,
            errors=errors,
            summary=summary,
            segments=imported_segments,
            date_range=date_range,
        )


importer = MentraBridgeImporter()
