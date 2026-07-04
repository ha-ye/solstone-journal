# SPDX-License-Identifier: AGPL-3.0-only
# Copyright (c) 2026 sol pbc

"""Apple Health export detector and preview parser."""

from __future__ import annotations

import datetime as dt
import zipfile
from contextlib import contextmanager
from dataclasses import dataclass, field
from pathlib import Path
from typing import BinaryIO, Callable, Iterator
from xml.etree import ElementTree

from solstone.think.importers.file_importer import ImportPreview, ImportResult

SAVE_MODE_BLOCKED_MESSAGE = (
    "Apple Health save-mode import is blocked until the health privacy "
    "preflight and raw-export retention controls are implemented."
)

_EXPORT_XML_CANDIDATES = (
    "apple_health_export/export.xml",
    "export.xml",
)


@dataclass(slots=True)
class _PreviewStats:
    records: int = 0
    workouts: int = 0
    routes: int = 0
    glucose_records: int = 0
    earliest_day: str | None = None
    latest_day: str | None = None
    source_names: set[str] = field(default_factory=set)
    record_types: dict[str, int] = field(default_factory=dict)

    @property
    def item_count(self) -> int:
        return self.records + self.workouts + self.routes

    @property
    def entity_count(self) -> int:
        return 0

    @property
    def date_range(self) -> tuple[str, str]:
        if self.earliest_day is None or self.latest_day is None:
            return ("", "")
        return (self.earliest_day, self.latest_day)

    def add_day(self, day: str | None) -> None:
        if day is None:
            return
        if self.earliest_day is None or day < self.earliest_day:
            self.earliest_day = day
        if self.latest_day is None or day > self.latest_day:
            self.latest_day = day

    def add_source(self, source_name: str | None) -> None:
        if source_name:
            self.source_names.add(source_name)

    def add_record_type(self, record_type: str | None) -> None:
        if not record_type:
            return
        self.record_types[record_type] = self.record_types.get(record_type, 0) + 1


class AppleHealthImporter:
    name = "apple_health"
    display_name = "Apple Health"
    file_patterns = ["apple_health_export/", "export.xml", "*.zip"]
    description = "Preview Apple Health export.xml data without writing to the journal"

    def detect(self, path: Path) -> bool:
        if path.is_dir():
            return _find_export_xml_in_directory(path) is not None
        if path.is_file() and path.suffix.lower() == ".zip":
            try:
                with zipfile.ZipFile(path) as archive:
                    return _find_export_xml_in_zip(archive.namelist()) is not None
            except zipfile.BadZipFile:
                return False
        return False

    def preview(self, path: Path) -> ImportPreview:
        stats = _preview_export(path)
        return ImportPreview(
            date_range=stats.date_range,
            item_count=stats.item_count,
            entity_count=stats.entity_count,
            summary=_summary(stats),
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
        if not dry_run:
            raise RuntimeError(SAVE_MODE_BLOCKED_MESSAGE)

        preview = self.preview(path)
        return ImportResult(
            entries_written=0,
            entities_seeded=0,
            files_created=[],
            errors=[],
            summary=f"Dry run only: {preview.summary}",
            date_range=preview.date_range,
        )


def _find_export_xml_in_directory(path: Path) -> Path | None:
    for candidate in _EXPORT_XML_CANDIDATES:
        candidate_path = path / candidate
        if candidate_path.is_file():
            return candidate_path
    return None


def _find_export_xml_in_zip(names: list[str]) -> str | None:
    normalized = {name.rstrip("/"): name for name in names}
    for candidate in _EXPORT_XML_CANDIDATES:
        if candidate in normalized:
            return normalized[candidate]

    for name in names:
        clean = name.rstrip("/")
        if clean.endswith("/apple_health_export/export.xml"):
            return name
        if clean.endswith("/export.xml") and "apple_health_export/" in clean:
            return name
    return None


def _count_route_files(path: Path) -> int:
    if path.is_dir():
        export_xml = _find_export_xml_in_directory(path)
        if export_xml is None:
            return 0
        route_root = export_xml.parent / "workout-routes"
        if not route_root.is_dir():
            return 0
        return sum(1 for route in route_root.glob("*.gpx") if route.is_file())

    if path.is_file() and path.suffix.lower() == ".zip":
        with zipfile.ZipFile(path) as archive:
            return sum(
                1
                for name in archive.namelist()
                if "/workout-routes/" in name
                and name.lower().endswith(".gpx")
                and not name.endswith("/")
            )
    return 0


@contextmanager
def _open_export_xml(path: Path) -> Iterator[BinaryIO]:
    if path.is_dir():
        export_xml = _find_export_xml_in_directory(path)
        if export_xml is None:
            raise FileNotFoundError(f"No Apple Health export.xml found under {path}")
        with export_xml.open("rb") as handle:
            yield handle
        return

    if path.is_file() and path.suffix.lower() == ".zip":
        with zipfile.ZipFile(path) as archive:
            member = _find_export_xml_in_zip(archive.namelist())
            if member is None:
                raise FileNotFoundError(f"No Apple Health export.xml found in {path}")
            with archive.open(member) as handle:
                yield handle
        return

    raise FileNotFoundError(f"No Apple Health export.xml found at {path}")


def _preview_export(path: Path) -> _PreviewStats:
    stats = _PreviewStats(routes=_count_route_files(path))
    with _open_export_xml(path) as handle:
        root = None
        for event, elem in ElementTree.iterparse(handle, events=("start", "end")):
            if event == "start":
                if root is None:
                    root = elem
                continue

            if elem.tag == "Record":
                stats.records += 1
                record_type = elem.attrib.get("type")
                stats.add_record_type(record_type)
                stats.add_source(elem.attrib.get("sourceName"))
                stats.add_day(_parse_apple_day(elem.attrib.get("startDate")))
                if _is_glucose_record(record_type):
                    stats.glucose_records += 1
            elif elem.tag == "Workout":
                stats.workouts += 1
                stats.add_source(elem.attrib.get("sourceName"))
                stats.add_day(_parse_apple_day(elem.attrib.get("startDate")))
            elem.clear()
            if root is not None:
                root.clear()
    return stats


def _parse_apple_day(value: str | None) -> str | None:
    if not value:
        return None
    value = value.strip()
    for fmt in ("%Y-%m-%d %H:%M:%S %z", "%Y-%m-%d %H:%M:%S"):
        try:
            return dt.datetime.strptime(value, fmt).strftime("%Y%m%d")
        except ValueError:
            pass
    try:
        return dt.datetime.fromisoformat(value.replace("Z", "+00:00")).strftime(
            "%Y%m%d"
        )
    except ValueError:
        return None


def _is_glucose_record(record_type: str | None) -> bool:
    if not record_type:
        return False
    return "BloodGlucose" in record_type or record_type.endswith("Glucose")


def _summary(stats: _PreviewStats) -> str:
    top_types = sorted(stats.record_types.items(), key=lambda item: (-item[1], item[0]))
    top_type_names = ", ".join(
        name.rsplit("Identifier", 1)[-1] for name, _ in top_types[:3]
    )
    parts = [
        f"records={stats.records}",
        f"workouts={stats.workouts}",
        f"routes={stats.routes}",
        f"glucose={stats.glucose_records}",
        f"sources={len(stats.source_names)}",
    ]
    if top_type_names:
        parts.append(f"top_types={top_type_names}")
    return ", ".join(parts)


importer = AppleHealthImporter()
