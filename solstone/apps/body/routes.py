# SPDX-License-Identifier: AGPL-3.0-only
# Copyright (c) 2026 sol pbc

"""Read-only views over health data imported into the journal.

Reads import manifests, month-bounded normalized shards, and the
importer-owned dedupe database. Read paths create nothing on disk.

Two altitudes:

- ARCHIVE (``/app/body``) — what the journal holds about the body across
  all time: coverage, per-day contribution grid, recent days, coverage
  families, sources, and an audit drawer with the raw import bookkeeping.
- DAY (``/app/body/<YYYYMMDD>``) — question-first cards for one day,
  rendered only when that day has the data.
"""

import json
import logging
import math
import re
import sqlite3
from collections import Counter, defaultdict
from contextlib import closing
from datetime import date, datetime, timedelta, timezone
from pathlib import Path
from statistics import mean
from typing import Any, Callable

from flask import Blueprint, jsonify, render_template, request

from solstone.convey import state
from solstone.convey.reasons import INVALID_DAY, INVALID_REQUEST_VALUE
from solstone.convey.utils import error_response
from solstone.think.importers.health_schema import (
    friendly_type_name,
    merge_sleep_sessions,
    pick_day_sleep,
)

logger = logging.getLogger(__name__)

body_bp = Blueprint("app:body", __name__, url_prefix="/app/body")

DAY_RE = re.compile(r"^\d{8}$")
MONTH_RE = re.compile(r"^\d{4}-\d{2}$")
DAY_SUMMARY_STREAM = "import.apple_health"
APPLE_HEALTH_SOURCE_TYPE = "apple_health"
DAY_SUMMARY_FILE = "day_summary_transcript.md"

# Glucose readings further apart than this render as separate curve
# segments instead of a line drawn across the gap.
GLUCOSE_SEGMENT_GAP_MINUTES = 45
# The sleep bar axis runs 6 PM of the previous day to 6 PM of the day.
SLEEP_AXIS_START_HOUR = 18
RECENT_DAY_LIMIT = 4
STALE_SOURCE_DAYS = 30

GLUCOSE_SVG_WIDTH = 1440.0
GLUCOSE_SVG_HEIGHT = 260.0
MAX_WINDOW_DAYS = 7

_MONTH_ABBR = (
    "Jan",
    "Feb",
    "Mar",
    "Apr",
    "May",
    "Jun",
    "Jul",
    "Aug",
    "Sep",
    "Oct",
    "Nov",
    "Dec",
)
_MONTH_FULL = (
    "January",
    "February",
    "March",
    "April",
    "May",
    "June",
    "July",
    "August",
    "September",
    "October",
    "November",
    "December",
)

# Coverage families group record types for owner-facing chips and day cards.
# Matching is by identifier fragment so a new owner's device types land in a
# sensible family with no code change; unmatched types fold into "Other".
# Rule order matters: Sleep claims "Sleeping" types (wrist temperature),
# Heart claims "HeartRate" before the walking rules could see gait names.
_FAMILY_RULES: tuple[tuple[str, tuple[str, ...]], ...] = (
    ("Sleep", ("SleepAnalysis", "Sleeping")),
    ("Glucose", ("Glucose",)),
    (
        "Heart",
        (
            "HeartRate",
            "OxygenSaturation",
            "RespiratoryRate",
            "BloodPressure",
            "VO2Max",
            "PeripheralPerfusionIndex",
            "AtrialFibrillation",
            "Electrocardiogram",
        ),
    ),
    (
        "Walking metrics",
        (
            "WalkingSpeed",
            "WalkingStepLength",
            "WalkingAsymmetry",
            "WalkingDoubleSupport",
            "WalkingSteadiness",
            "SixMinuteWalkTest",
            "StairAscent",
            "StairDescent",
        ),
    ),
    ("Hearing & audio", ("AudioExposure", "EnvironmentalSoundReduction")),
    ("Mindfulness", ("MindfulSession", "StateOfMind")),
    (
        "Body measurements",
        (
            "BodyMass",
            "BodyFat",
            "LeanBodyMass",
            "Height",
            "WaistCircumference",
            "BodyTemperature",
            "BasalBodyTemperature",
        ),
    ),
    (
        "Activity",
        (
            "Workout",
            "StepCount",
            "EnergyBurned",
            "Distance",
            "FlightsClimbed",
            "ExerciseTime",
            "StandTime",
            "StandHour",
            "PhysicalEffort",
            "Running",
            "Cycling",
            "SwimmingStroke",
            "PushCount",
            "TimeInDaylight",
            "UVExposure",
        ),
    ),
)

_FAMILY_ORDER = (
    "Sleep",
    "Glucose",
    "Activity",
    "Heart",
    "Mindfulness",
    "Hearing & audio",
    "Walking metrics",
    "Body measurements",
    "Other",
)


def _journal_root() -> Path:
    return Path(state.journal_root)


def _load_json_file(path: Path) -> dict[str, Any]:
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise ValueError(f"Could not read JSON file: {path}") from exc
    if not isinstance(data, dict):
        raise ValueError(f"JSON file must contain an object: {path}")
    return data


def _iter_health_import_manifests(journal_root: Path) -> list[dict[str, Any]]:
    imports_root = journal_root / "imports"
    if not imports_root.is_dir():
        return []

    manifests: list[dict[str, Any]] = []
    for manifest_path in sorted(imports_root.glob("*/manifest.json")):
        try:
            manifest = _load_json_file(manifest_path)
        except ValueError:
            logger.warning("Skipping unreadable import manifest %s", manifest_path)
            continue
        if manifest.get("source_type") != APPLE_HEALTH_SOURCE_TYPE:
            continue
        import_id = str(manifest.get("import_id") or manifest_path.parent.name)
        manifest["import_id"] = import_id
        manifest["normalized_months"] = [
            path.stem
            for path in sorted((manifest_path.parent / "normalized").glob("*.jsonl"))
        ]
        manifests.append(manifest)
    return manifests


def _read_shard_rows(path: Path) -> list[dict[str, Any]]:
    month = path.stem if MONTH_RE.fullmatch(path.stem) else None
    try:
        lines = path.read_text(encoding="utf-8").splitlines()
    except OSError as exc:
        raise ValueError(f"Could not read normalized shard: {path}") from exc
    rows: list[dict[str, Any]] = []
    for line_number, line in enumerate(lines, start=1):
        if not line.strip():
            continue
        try:
            row = json.loads(line)
        except json.JSONDecodeError as exc:
            raise ValueError(
                f"Could not parse normalized shard {path} line {line_number}"
            ) from exc
        if not isinstance(row, dict):
            raise ValueError(
                f"Normalized shard {path} line {line_number} must be an object"
            )
        if month and not row.get("month"):
            row["month"] = month
        rows.append(row)
    return rows


def _iter_normalized_rows(
    journal_root: Path, *, month: str | None = None
) -> list[dict[str, Any]]:
    imports_root = journal_root / "imports"
    if not imports_root.is_dir():
        return []

    pattern = f"*/normalized/{month}.jsonl" if month else "*/normalized/*.jsonl"
    rows: list[dict[str, Any]] = []
    seen_keys: set[str] = set()
    for path in sorted(imports_root.glob(pattern)):
        for row in _read_shard_rows(path):
            # The same entry appears in every bundle that imported it
            # (e.g. a test-week import overlapped by the full backfill);
            # keep one row per dedupe key.
            dedupe_key = row.get("dedupe_key")
            if isinstance(dedupe_key, str) and dedupe_key:
                if dedupe_key in seen_keys:
                    continue
                seen_keys.add(dedupe_key)
            rows.append(row)
    return rows


def _month_reader(journal_root: Path) -> Callable[[str], list[dict[str, Any]]]:
    """Month-shard reader that caches each month within one request."""

    cache: dict[str, list[dict[str, Any]]] = {}

    def read(month: str) -> list[dict[str, Any]]:
        if month not in cache:
            cache[month] = _iter_normalized_rows(journal_root, month=month)
        return cache[month]

    return read


def _source_label(row: dict[str, Any]) -> str:
    source = row.get("source_name") or row.get("source_family") or "unknown"
    return str(source)


def _row_time(row: dict[str, Any]) -> str | None:
    value = row.get("start_date") or row.get("start_time") or row.get("end_date")
    if value is None:
        return None
    text = str(value).strip()
    return text or None


def _time_sort_key(value: str) -> tuple[int, float] | tuple[int, str]:
    """Chronological sort key that survives mixed UTC offsets."""
    try:
        parsed = datetime.fromisoformat(value)
    except ValueError:
        return (0, value)
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=timezone.utc)
    return (1, parsed.timestamp())


def _parse_record_time(value: object) -> datetime | None:
    """Parse a record timestamp, keeping its own local offset."""
    if value is None:
        return None
    text = str(value).strip()
    if not text:
        return None
    try:
        parsed = datetime.fromisoformat(text)
    except ValueError:
        return None
    if parsed.tzinfo is None:
        # Wall-clock components stay the record's own local time; attaching
        # UTC only makes arithmetic against aware timestamps possible.
        parsed = parsed.replace(tzinfo=timezone.utc)
    return parsed


def _parse_float(value: object) -> float | None:
    try:
        return float(str(value))
    except (TypeError, ValueError):
        return None


# --- Formatting (record-local times, 12-hour; durations "8h 03m") ----------


def _format_number(value: float) -> str:
    if value == int(value):
        return f"{int(value):,}"
    return f"{value:,.1f}"


def _format_clock(moment: datetime) -> str:
    hour = moment.hour % 12 or 12
    meridiem = "AM" if moment.hour < 12 else "PM"
    return f"{hour}:{moment.minute:02d} {meridiem}"


def _format_duration(minutes: float) -> str:
    total = int(round(minutes))
    hours, mins = divmod(max(total, 0), 60)
    if hours:
        return f"{hours}h {mins:02d}m"
    return f"{mins}m"


def _format_day_long(day: str) -> str:
    return f"{_MONTH_FULL[int(day[4:6]) - 1]} {int(day[6:8])}, {day[:4]}"


def _format_day_short(day: str) -> str:
    return f"{_MONTH_ABBR[int(day[4:6]) - 1]} {int(day[6:8])}"


def _format_month_label(month: str) -> str:
    return f"{_MONTH_ABBR[int(month[5:7]) - 1]} {month[:4]}"


def _month_range_label(first: str | None, last: str | None) -> str:
    first_label = _format_month_label(first[:7]) if first else None
    last_label = _format_month_label(last[:7]) if last else None
    if first_label and last_label:
        if first_label == last_label:
            return first_label
        return f"{first_label} – {last_label}"
    return first_label or last_label or ""


def _prior_month(month: str) -> str:
    year, mon = int(month[:4]), int(month[5:7])
    if mon == 1:
        return f"{year - 1}-12"
    return f"{year}-{mon - 1:02d}"


def _month_keys_between(start: datetime, end: datetime) -> list[str]:
    """Month shard keys touched by an inclusive date span."""
    current = date(start.year, start.month, 1)
    final = date(end.year, end.month, 1)
    keys: list[str] = []
    while current <= final:
        keys.append(f"{current.year}-{current.month:02d}")
        if current.month == 12:
            current = date(current.year + 1, 1, 1)
        else:
            current = date(current.year, current.month + 1, 1)
    return keys


def _parse_window_bound(value: object) -> datetime | None:
    if value is None:
        return None
    text = str(value).strip()
    if not text:
        return None
    if text.endswith("Z"):
        text = text[:-1] + "+00:00"
    try:
        parsed = datetime.fromisoformat(text)
    except ValueError:
        return None
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=timezone.utc)
    return parsed


def _row_interval(row: dict[str, Any]) -> tuple[datetime, datetime] | None:
    start = _parse_record_time(row.get("start_date") or row.get("start_time"))
    if start is None:
        return None
    end = _parse_record_time(row.get("end_date")) or start
    if end < start:
        end = start
    return start, end


def _interval_overlaps(
    start: datetime, end: datetime, window_start: datetime, window_end: datetime
) -> bool:
    if end <= start:
        return window_start <= start < window_end
    return start < window_end and end > window_start


def _overlap_minutes(
    start: datetime, end: datetime, window_start: datetime, window_end: datetime
) -> float:
    if not _interval_overlaps(start, end, window_start, window_end):
        return 0.0
    if end <= start:
        return 0.0
    overlap_start = max(start, window_start)
    overlap_end = min(end, window_end)
    return max((overlap_end - overlap_start).total_seconds() / 60, 0.0)


def _rows_for_window(
    journal_root: Path,
    window_start: datetime,
    window_end: datetime,
    *,
    reader: Callable[[str], list[dict[str, Any]]] | None = None,
) -> list[dict[str, Any]]:
    read = reader or _month_reader(journal_root)
    shard_start = window_start - timedelta(days=1)
    rows: list[dict[str, Any]] = []
    seen_keys: set[str] = set()
    for month in _month_keys_between(shard_start, window_end):
        for row in read(month):
            dedupe_key = row.get("dedupe_key")
            if isinstance(dedupe_key, str) and dedupe_key:
                if dedupe_key in seen_keys:
                    continue
                seen_keys.add(dedupe_key)
            interval = _row_interval(row)
            if interval is None:
                continue
            if _interval_overlaps(interval[0], interval[1], window_start, window_end):
                rows.append(row)
    return rows


def _family_for_type(record_type: str) -> str:
    for family, fragments in _FAMILY_RULES:
        for fragment in fragments:
            if fragment in record_type:
                return family
    return "Other"


def _is_glucose_type(record_type: str) -> bool:
    return "BloodGlucose" in record_type or record_type.endswith("Glucose")


def _is_sleep_type(record_type: str) -> bool:
    return "SleepAnalysis" in record_type


# --- Archive aggregates ------------------------------------------------------


def _latest_sources_snapshot(journal_root: Path) -> dict[str, Any]:
    """Per-source counts and latest entry time, bounded to the newest month shard."""
    empty: dict[str, Any] = {"month": None, "by_source": {}, "latest_by_source": {}}
    imports_root = journal_root / "imports"
    if not imports_root.is_dir():
        return empty

    shards = [
        path
        for path in imports_root.glob("*/normalized/*.jsonl")
        if MONTH_RE.fullmatch(path.stem)
    ]
    if not shards:
        return empty

    latest_month = max(path.stem for path in shards)
    by_source: Counter[str] = Counter()
    latest_by_source: dict[str, str] = {}
    seen_keys: set[str] = set()
    for path in sorted(path for path in shards if path.stem == latest_month):
        for row in _read_shard_rows(path):
            dedupe_key = row.get("dedupe_key")
            if isinstance(dedupe_key, str) and dedupe_key:
                if dedupe_key in seen_keys:
                    continue
                seen_keys.add(dedupe_key)
            source = _source_label(row)
            by_source[source] += 1
            row_time = _row_time(row)
            if row_time and (
                source not in latest_by_source
                or _time_sort_key(row_time) > _time_sort_key(latest_by_source[source])
            ):
                latest_by_source[source] = row_time
    return {
        "month": latest_month,
        "by_source": dict(sorted(by_source.items())),
        "latest_by_source": dict(sorted(latest_by_source.items())),
    }


# Aggregates over the dedupe DB cost a full-table scan (~2M rows after the
# 5-year backfill). The DB only changes when an import runs, so cache the
# fold keyed by the database (and WAL) file signature.
_dedupe_stats_cache: dict[str, tuple[tuple[int, int, int, int], dict[str, Any]]] = {}


def _dedupe_db_signature(db_path: Path) -> tuple[int, int, int, int]:
    stat = db_path.stat()
    wal = db_path.with_name(db_path.name + "-wal")
    try:
        wal_stat = wal.stat()
        return (stat.st_mtime_ns, stat.st_size, wal_stat.st_mtime_ns, wal_stat.st_size)
    except FileNotFoundError:
        return (stat.st_mtime_ns, stat.st_size, 0, 0)


def _read_health_dedupe_stats(journal_root: Path) -> dict[str, Any]:
    db_path = journal_root / "imports" / "health-dedupe.sqlite"
    if not db_path.exists():
        return {
            "total": 0,
            "by_type": {},
            "by_source": {},
            "by_month": {},
            "by_day": {},
            "type_ranges": {},
            "coverage_window": {"start": None, "end": None},
        }

    signature = _dedupe_db_signature(db_path)
    cache_key = str(db_path)
    cached = _dedupe_stats_cache.get(cache_key)
    if cached and cached[0] == signature:
        return cached[1]

    uri = f"file:{db_path}?mode=ro"
    with closing(sqlite3.connect(uri, uri=True)) as conn:
        conn.row_factory = sqlite3.Row
        # One scan for every grouped aggregate; folded in Python below.
        grouped = conn.execute(
            """
            SELECT
                record_type,
                source_family,
                replace(substr(start_time, 1, 10), '-', '') AS d,
                COUNT(*) AS n,
                MIN(start_time) AS min_start,
                MAX(start_time) AS max_start
            FROM health_dedupe
            GROUP BY record_type, source_family, d
            """
        ).fetchall()
        window = conn.execute(
            "SELECT MIN(start_time) AS s, MAX(start_time) AS e FROM health_dedupe"
        ).fetchone()

    total = 0
    by_type: Counter[str] = Counter()
    by_source: Counter[str] = Counter()
    by_month: Counter[str] = Counter()
    by_day: Counter[str] = Counter()
    type_first: dict[str, str] = {}
    type_last: dict[str, str] = {}
    for row in grouped:
        n = row["n"]
        day = row["d"]
        record_type = row["record_type"]
        total += n
        by_type[record_type] += n
        by_source[row["source_family"]] += n
        by_day[day] += n
        if len(day) >= 6:
            by_month[f"{day[:4]}-{day[4:6]}"] += n
        min_start = row["min_start"]
        max_start = row["max_start"]
        if min_start and (
            record_type not in type_first
            or _time_sort_key(min_start) < _time_sort_key(type_first[record_type])
        ):
            type_first[record_type] = min_start
        if max_start and (
            record_type not in type_last
            or _time_sort_key(max_start) > _time_sort_key(type_last[record_type])
        ):
            type_last[record_type] = max_start

    result = {
        "total": total,
        "by_type": dict(sorted(by_type.items())),
        "by_source": dict(sorted(by_source.items())),
        "by_month": dict(sorted(by_month.items())),
        "by_day": dict(sorted(by_day.items())),
        "type_ranges": {
            record_type: {
                "first": type_first[record_type],
                "last": type_last.get(record_type),
            }
            for record_type in sorted(type_first)
        },
        "coverage_window": {"start": window["s"], "end": window["e"]},
    }
    _dedupe_stats_cache[cache_key] = (signature, result)
    return result


def _grid_cell(day_key: str, count: int, scale: float) -> dict[str, Any]:
    if count:
        entries = f"{count:,} " + ("entry" if count == 1 else "entries")
        intensity = round(math.log1p(count) / scale, 3)
    else:
        entries = "no entries"
        intensity = 0.0
    return {
        "day": day_key,
        "count": count,
        "intensity": intensity,
        "title": f"{_format_day_short(day_key)}, {day_key[:4]} · {entries}",
    }


def _month_label_positions(
    weeks: list[list[dict[str, Any] | None]],
) -> list[dict[str, Any]]:
    """Month short-name labels keyed to the week column where each month
    first leads a column (by its first rendered weekday cell)."""
    labels: list[dict[str, Any]] = []
    prev_month: str | None = None
    for index, week in enumerate(weeks):
        first_cell = next((cell for cell in week if cell is not None), None)
        if first_cell is None:
            continue
        month = first_cell["day"][4:6]
        if month != prev_month:
            labels.append({"index": index, "label": _MONTH_ABBR[int(month) - 1]})
            prev_month = month
    return labels


def _day_contribution_grid(by_day: dict[str, int]) -> list[dict[str, Any]]:
    """Contribution-style day grid: one row-block per year, weeks as columns.

    Spans the first through the last day with data. Each year block holds
    week columns of seven weekday cells (Mon–Sun); ``None`` pads partial
    first/last weeks. ``month_labels`` names the week column where each
    month starts. Intensity is log-scaled from per-day entry counts so
    a heavy backfill day doesn't wash out ordinary days; empty days inside
    the span carry zero intensity and render pale and unlinked.
    """
    if not by_day:
        return []
    days = sorted(by_day)
    first = datetime.strptime(days[0], "%Y%m%d").date()
    last = datetime.strptime(days[-1], "%Y%m%d").date()
    scale = math.log1p(max(by_day.values()))
    blocks: list[dict[str, Any]] = []
    for year in range(first.year, last.year + 1):
        start = max(first, date(year, 1, 1))
        end = min(last, date(year, 12, 31))
        weeks: list[list[dict[str, Any] | None]] = []
        week: list[dict[str, Any] | None] = [None] * start.weekday()
        current = start
        while current <= end:
            day_key = current.strftime("%Y%m%d")
            week.append(_grid_cell(day_key, by_day.get(day_key, 0), scale))
            if len(week) == 7:
                weeks.append(week)
                week = []
            current += timedelta(days=1)
        if week:
            week.extend([None] * (7 - len(week)))
            weeks.append(week)
        blocks.append(
            {
                "year": year,
                "weeks": weeks,
                "month_labels": _month_label_positions(weeks),
            }
        )
    return blocks


def _coverage_families(dedupe: dict[str, Any]) -> list[dict[str, Any]]:
    """Fold per-type first/last ranges into ordered coverage-family chips."""
    type_ranges = dedupe.get("type_ranges", {})
    folded: dict[str, dict[str, Any]] = {}
    for record_type, count in dedupe.get("by_type", {}).items():
        family = _family_for_type(record_type)
        entry = folded.setdefault(
            family,
            {"count": 0, "first": None, "last": None, "types": set()},
        )
        entry["count"] += count
        entry["types"].add(friendly_type_name(record_type))
        span = type_ranges.get(record_type) or {}
        first = span.get("first")
        last = span.get("last")
        if first and (
            entry["first"] is None
            or _time_sort_key(first) < _time_sort_key(entry["first"])
        ):
            entry["first"] = first
        if last and (
            entry["last"] is None
            or _time_sort_key(last) > _time_sort_key(entry["last"])
        ):
            entry["last"] = last

    chips: list[dict[str, Any]] = []
    for family in _FAMILY_ORDER:
        entry = folded.get(family)
        if not entry:
            continue
        chips.append(
            {
                "name": family,
                "count": entry["count"],
                "count_label": f"{entry['count']:,}",
                "range_label": _month_range_label(entry["first"], entry["last"]),
                "types_label": ", ".join(sorted(entry["types"])),
            }
        )
    return chips


def _source_chips(recent: dict[str, Any]) -> list[dict[str, Any]]:
    """Source chips from the latest-month snapshot with quiet staleness."""
    latest_map = recent.get("latest_by_source", {})
    parsed = {name: _parse_record_time(value) for name, value in latest_map.items()}
    known = [moment for moment in parsed.values() if moment is not None]
    newest = max(known) if known else None
    chips: list[dict[str, Any]] = []
    for name, count in recent.get("by_source", {}).items():
        latest = parsed.get(name)
        stale = bool(
            newest and latest and newest - latest > timedelta(days=STALE_SOURCE_DAYS)
        )
        chips.append(
            {
                "name": name,
                "count": count,
                "count_label": f"{count:,}",
                "stale": stale,
                "last_seen_label": (
                    _format_day_long(latest.strftime("%Y%m%d"))
                    if stale and latest
                    else None
                ),
            }
        )
    return chips


def _recent_day_rail(
    journal_root: Path, by_day: dict[str, int], *, limit: int = RECENT_DAY_LIMIT
) -> list[dict[str, Any]]:
    """Compact facts for the last days that have entries, newest first."""
    days = sorted(by_day)[-limit:][::-1]
    reader = _month_reader(journal_root)
    items: list[dict[str, Any]] = []
    for day in days:
        payload = _build_health_day(journal_root, day, reader=reader)
        glucose = payload["glucose"]
        if glucose["count"] and glucose["unit"] not in (None, "mixed"):
            glucose_label = (
                f"{_format_number(glucose['min'])}–{_format_number(glucose['max'])} "
                f"{glucose['unit']} · avg {_format_number(glucose['mean'])}"
            )
        elif glucose["count"]:
            glucose_label = f"{glucose['count']:,} readings"
        else:
            glucose_label = None
        sleep = payload["sleep"]
        activity = payload["activity"]
        items.append(
            {
                "day": day,
                "label": _format_day_short(day),
                "sleep_duration": sleep["duration"] if sleep else None,
                "glucose_label": glucose_label,
                "workout_count": len(activity["workouts"]) if activity else 0,
                "source_count": len(payload["sources"]["names"])
                if payload["sources"]
                else 0,
            }
        )
    return items


def _build_archive(
    journal_root: Path,
    *,
    dedupe: dict[str, Any],
    imports: list[dict[str, Any]],
    recent: dict[str, Any],
) -> dict[str, Any]:
    by_month = dedupe["by_month"]
    months = sorted(by_month)
    days = sorted(dedupe["by_day"])
    coverage = None
    if months:
        coverage = {
            "start_month": months[0],
            "end_month": months[-1],
            "range_label": (
                f"{_format_month_label(months[0])} – {_format_month_label(months[-1])}"
            ),
        }
    return {
        "entry_total": dedupe["total"],
        "entry_total_label": f"{dedupe['total']:,}",
        "import_count": len(imports),
        "months_observed": len(by_month),
        "coverage": coverage,
        "latest_day": days[-1] if days else None,
        "day_grid": _day_contribution_grid(dedupe["by_day"]),
        "recent_days": _recent_day_rail(journal_root, dedupe["by_day"]),
        "families": _coverage_families(dedupe),
        "sources": _source_chips(recent),
    }


def _build_health_import_status(journal_root: Path) -> dict[str, Any]:
    """Aggregate health-import status without scanning full normalized shards.

    Totals come from the dedupe database (SQL aggregates); per-source-name
    counts and staleness come from a scan bounded to the newest month shard.
    """
    imports = _iter_health_import_manifests(journal_root)
    imports.sort(key=lambda item: str(item.get("imported_at") or ""), reverse=True)
    dedupe = _read_health_dedupe_stats(journal_root)
    recent = _latest_sources_snapshot(journal_root)
    return {
        "imports": imports,
        "normalized": {
            "total": dedupe["total"],
            "by_type": dedupe["by_type"],
            "by_source": recent["by_source"],
            "by_month": dedupe["by_month"],
        },
        "dedupe": {
            "total": dedupe["total"],
            "by_type": dedupe["by_type"],
            "by_source": dedupe["by_source"],
            "by_month": dedupe["by_month"],
        },
        "coverage_window": dedupe["coverage_window"],
        "latest_by_source": recent["latest_by_source"],
        "sources_month": recent["month"],
        "day_counts": dedupe["by_day"],
        "archive": _build_archive(
            journal_root, dedupe=dedupe, imports=imports, recent=recent
        ),
    }


# --- Day cards ---------------------------------------------------------------


def _find_day_summary(journal_root: Path, day: str) -> str:
    day_root = journal_root / "chronicle" / day / DAY_SUMMARY_STREAM
    if not day_root.is_dir():
        return ""
    for path in sorted(day_root.glob(f"*/{DAY_SUMMARY_FILE}")):
        try:
            return path.read_text(encoding="utf-8")
        except OSError as exc:
            raise ValueError(f"Could not read day summary: {path}") from exc
    return ""


def _glucose_stats(rows: list[dict[str, Any]]) -> dict[str, Any]:
    glucose_rows = [
        row for row in rows if _is_glucose_type(str(row.get("record_type") or ""))
    ]
    values = [
        parsed
        for parsed in (_parse_float(row.get("value")) for row in glucose_rows)
        if parsed is not None
    ]
    units = sorted(
        {str(row.get("unit")) for row in glucose_rows if row.get("unit") is not None}
    )
    if not values:
        return {"count": 0, "min": None, "max": None, "mean": None, "unit": None}
    return {
        "count": len(values),
        "min": min(values),
        "max": max(values),
        "mean": mean(values),
        "unit": units[0] if len(units) == 1 else ("mixed" if units else None),
    }


def _glucose_series(rows: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """Per-unit day curves as [minute-of-day, value] points plus SVG geometry."""
    readings_by_unit: dict[str, list[tuple[datetime, float]]] = {}
    for row in rows:
        if not _is_glucose_type(str(row.get("record_type") or "")):
            continue
        moment = _parse_record_time(row.get("start_date") or row.get("start_time"))
        value = _parse_float(row.get("value"))
        if moment is None or value is None:
            continue
        unit = str(row.get("unit") or "").strip() or "value"
        readings_by_unit.setdefault(unit, []).append((moment, value))

    series: list[dict[str, Any]] = []
    for unit in sorted(readings_by_unit):
        readings = sorted(readings_by_unit[unit], key=lambda item: item[0])
        values = [value for _, value in readings]
        points = [
            [moment.hour * 60 + moment.minute, value] for moment, value in readings
        ]
        v_min, v_max = min(values), max(values)
        pad = max((v_max - v_min) * 0.08, 2.0)
        lo, hi = v_min - pad, v_max + pad

        def _y(value: float) -> float:
            return round(
                GLUCOSE_SVG_HEIGHT - (value - lo) / (hi - lo) * GLUCOSE_SVG_HEIGHT, 1
            )

        segments: list[list[list[float]]] = []
        current: list[list[float]] = []
        prev_minute: float | None = None
        for minute, value in points:
            if (
                prev_minute is not None
                and minute - prev_minute > GLUCOSE_SEGMENT_GAP_MINUTES
            ):
                segments.append(current)
                current = []
            current.append([float(minute), _y(value)])
            prev_minute = minute
        if current:
            segments.append(current)

        paths = [
            "M" + " L".join(f"{x:g} {y:g}" for x, y in segment)
            for segment in segments
            if len(segment) > 1
        ]
        dots = [segment[0] for segment in segments if len(segment) == 1]
        mean_value = mean(values)
        series.append(
            {
                "unit": unit,
                "count": len(values),
                "count_label": f"{len(values):,}",
                "min": v_min,
                "max": v_max,
                "mean": mean_value,
                "range_label": (
                    f"{_format_number(v_min)}–{_format_number(v_max)} {unit}"
                ),
                "mean_label": _format_number(mean_value),
                "points": points,
                "svg": {
                    "width": GLUCOSE_SVG_WIDTH,
                    "height": GLUCOSE_SVG_HEIGHT,
                    "paths": paths,
                    "dots": dots,
                    "y_min_label": _format_number(v_min),
                    "y_max_label": _format_number(v_max),
                },
            }
        )
    return series


def _axis_minute(moment: datetime, axis_day: date) -> float:
    """Minutes since 6 PM of ``axis_day`` using the record's own wall clock."""
    day_delta = (moment.date() - axis_day).days
    minute = day_delta * 1440 + moment.hour * 60 + moment.minute
    return float(minute - SLEEP_AXIS_START_HOUR * 60)


def _sleep_analysis(
    day_rows: list[dict[str, Any]],
    prev_rows: list[dict[str, Any]],
    day: str,
) -> dict[str, Any] | None:
    """The day's sleep card: the session ending that morning, naps separate.

    Cross-midnight rule: entries are day-attributed by start time, so the
    night that ends this morning mostly lives on the previous day — both
    days' rows feed the merge. Multiple sources are never summed: the
    longest-coverage source is primary, others are named in the footer.
    The merge + main-session rule is the shared canonical implementation
    in ``health_schema`` — the importer's day cards use the same one.
    """
    target = date(int(day[:4]), int(day[4:6]), int(day[6:8]))
    intervals_by_source: dict[str, list[tuple[datetime, datetime]]] = {}
    for row in prev_rows + day_rows:
        if not _is_sleep_type(str(row.get("record_type") or "")):
            continue
        start = _parse_record_time(row.get("start_date") or row.get("start_time"))
        if start is None:
            continue
        end = _parse_record_time(row.get("end_date")) or start
        intervals_by_source.setdefault(_source_label(row), []).append((start, end))
    if not intervals_by_source:
        return None

    sleep = pick_day_sleep(intervals_by_source, target)
    if sleep is None:
        return None
    main = sleep.main
    naps = list(sleep.naps)
    axis_day = target - timedelta(days=1)

    def _bar_segment(session: tuple[datetime, datetime], kind: str) -> dict[str, Any]:
        left = min(max(_axis_minute(session[0], axis_day), 0.0), 1440.0)
        right = min(max(_axis_minute(session[1], axis_day), 0.0), 1440.0)
        return {
            "x": round(left, 1),
            "width": round(max(right - left, 4.0), 1),
            "kind": kind,
        }

    segments: list[dict[str, Any]] = []
    if main is not None:
        segments.append(_bar_segment(main, "main"))
    segments.extend(_bar_segment(nap, "nap") for nap in naps)

    def _session_view(session: tuple[datetime, datetime]) -> dict[str, str]:
        minutes = (session[1] - session[0]).total_seconds() / 60
        return {
            "window": f"{_format_clock(session[0])} – {_format_clock(session[1])}",
            "duration": _format_duration(minutes),
        }

    return {
        "source": sleep.source,
        "other_sources": list(sleep.other_sources),
        "window": _session_view(main)["window"] if main is not None else None,
        "duration": _session_view(main)["duration"] if main is not None else None,
        "naps": [_session_view(nap) for nap in naps],
        "bar": {
            "segments": segments,
            "ticks": [
                {"x": 360, "label": "12 AM"},
                {"x": 720, "label": "6 AM"},
                {"x": 1080, "label": "12 PM"},
            ],
        },
    }


def _workout_duration_minutes(row: dict[str, Any]) -> float | None:
    metadata = row.get("metadata")
    if isinstance(metadata, dict):
        minutes = _parse_float(metadata.get("duration"))
        if minutes is not None:
            unit = str(metadata.get("durationUnit") or "min").lower()
            if unit.startswith("sec"):
                minutes /= 60
            elif unit.startswith("hour") or unit == "hr":
                minutes *= 60
            return minutes
    start = _parse_record_time(row.get("start_date") or row.get("start_time"))
    end = _parse_record_time(row.get("end_date"))
    if start is not None and end is not None and end >= start:
        return (end - start).total_seconds() / 60
    return None


def _activity_analysis(day_rows: list[dict[str, Any]]) -> dict[str, Any] | None:
    workouts = [row for row in day_rows if row.get("kind") == "workout"]
    activity_rows = [
        row
        for row in day_rows
        if row.get("kind") != "workout"
        and _family_for_type(str(row.get("record_type") or "")) == "Activity"
    ]
    if not workouts and not activity_rows:
        return None

    workout_items: list[dict[str, Any]] = []
    for row in sorted(workouts, key=lambda r: _time_sort_key(_row_time(r) or "")):
        start = _parse_record_time(row.get("start_date") or row.get("start_time"))
        minutes = _workout_duration_minutes(row)
        workout_items.append(
            {
                "name": friendly_type_name(str(row.get("record_type") or "Workout")),
                "start": _format_clock(start) if start else None,
                "duration": _format_duration(minutes) if minutes is not None else None,
            }
        )

    step_rows = [
        row for row in activity_rows if "StepCount" in str(row.get("record_type") or "")
    ]
    steps: dict[str, Any] | None = None
    if step_rows:
        step_sources = sorted({_source_label(row) for row in step_rows})
        values = [
            value
            for value in (_parse_float(row.get("value")) for row in step_rows)
            if value is not None
        ]
        if len(step_sources) == 1 and values:
            # One source contributed every step sample — a real total is
            # honest here, labeled with its source.
            steps = {
                "mode": "total",
                "total": int(round(sum(values))),
                "total_label": f"{int(round(sum(values))):,}",
                "source": step_sources[0],
                "samples": len(step_rows),
            }
        else:
            # Multiple sources double-count; present sample counts only.
            steps = {
                "mode": "samples",
                "samples": len(step_rows),
                "samples_label": f"{len(step_rows):,}",
            }

    other_rows = [row for row in activity_rows if row not in step_rows]
    counters = Counter(
        friendly_type_name(str(row.get("record_type") or "")) for row in other_rows
    )
    counter_items = [
        {"label": name, "count": count, "count_label": f"{count:,}"}
        for name, count in sorted(counters.items(), key=lambda kv: (-kv[1], kv[0]))
    ]

    return {"workouts": workout_items, "steps": steps, "counters": counter_items}


def _fact_items(rows: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """Per-type counts; a single reading (or resting heart rate) shows its value."""
    grouped: dict[str, list[dict[str, Any]]] = defaultdict(list)
    for row in rows:
        grouped[str(row.get("record_type") or "")].append(row)
    items: list[dict[str, Any]] = []
    for record_type in sorted(
        grouped, key=lambda rt: (-len(grouped[rt]), friendly_type_name(rt))
    ):
        rows_for_type = grouped[record_type]
        item: dict[str, Any] = {
            "label": friendly_type_name(record_type),
            "count": len(rows_for_type),
            "count_label": f"{len(rows_for_type):,}",
            "value": None,
        }
        if len(rows_for_type) == 1 or "RestingHeartRate" in record_type:
            latest = max(
                rows_for_type, key=lambda r: _time_sort_key(_row_time(r) or "")
            )
            value = _parse_float(latest.get("value"))
            if value is not None:
                unit = str(latest.get("unit") or "").strip()
                item["value"] = f"{_format_number(value)} {unit}".strip()
        items.append(item)
    return items


def _nearest_days_with_data(by_day: dict[str, int], day: str) -> dict[str, Any]:
    before = [d for d in by_day if d < day]
    after = [d for d in by_day if d > day]
    prev_day = max(before) if before else None
    next_day = min(after) if after else None
    return {
        "prev": prev_day,
        "prev_label": _format_day_short(prev_day) if prev_day else None,
        "next": next_day,
        "next_label": _format_day_short(next_day) if next_day else None,
    }


def _day_lede(
    day_rows: list[dict[str, Any]],
    sleep: dict[str, Any] | None,
    glucose_series: list[dict[str, Any]],
    activity: dict[str, Any] | None,
) -> str:
    parts: list[str] = []
    if sleep and sleep.get("window"):
        parts.append(f"slept {sleep['window']}")
    for series in glucose_series:
        parts.append(f"glucose {series['range_label']}")
    if activity and activity["workouts"]:
        count = len(activity["workouts"])
        parts.append(f"{count} workout" + ("s" if count != 1 else ""))
    if not parts:
        if not day_rows:
            return "No body data present for this day."
        parts.append(f"{len(day_rows):,} entries observed")
    text = ", ".join(parts)
    return text[0].upper() + text[1:] + "."


def _day_prompts(
    date_label: str,
    *,
    has_sleep: bool,
    has_glucose: bool,
    has_workouts: bool,
) -> list[str]:
    prompts = [f"How did my body on {date_label} compare with nearby days?"]
    if has_glucose:
        prompts.append(
            f"What was on my calendar during the glucose peak on {date_label}?"
        )
    if has_workouts:
        prompts.append(
            f"What happened in my journal after the workouts on {date_label}?"
        )
    if has_sleep:
        prompts.append(
            f"What did my evening look like before the sleep ending {date_label}?"
        )
    for filler in (
        f"What does my journal hold for {date_label}?",
        f"Who did I spend {date_label} with?",
    ):
        if len(prompts) >= 3:
            break
        prompts.append(filler)
    return prompts[:3]


def _build_health_day(
    journal_root: Path,
    day: str,
    *,
    reader: Callable[[str], list[dict[str, Any]]] | None = None,
) -> dict[str, Any]:
    if not DAY_RE.fullmatch(day):
        raise ValueError("Day must be YYYYMMDD")
    try:
        target = datetime.strptime(day, "%Y%m%d").date()
    except ValueError as exc:
        raise ValueError("Day must be a valid YYYYMMDD date") from exc

    month = f"{day[:4]}-{day[4:6]}"
    months = [month]
    if int(day[6:8]) <= 2:
        # Cross-midnight sleep: the prior day's entries may live in the
        # prior month's shards near a month boundary.
        months.append(_prior_month(month))
    read = reader or _month_reader(journal_root)
    rows = [row for shard_month in months for row in read(shard_month)]
    day_rows = [row for row in rows if row.get("day") == day]
    prev_day = (target - timedelta(days=1)).strftime("%Y%m%d")
    prev_rows = [row for row in rows if row.get("day") == prev_day]

    families: dict[str, list[dict[str, Any]]] = defaultdict(list)
    for row in day_rows:
        families[_family_for_type(str(row.get("record_type") or ""))].append(row)

    sleep = _sleep_analysis(day_rows, prev_rows, day)
    glucose_series = _glucose_series(day_rows)
    activity = _activity_analysis(day_rows)
    heart_facts = _fact_items(families.get("Heart", []))
    mind_sound_facts = _fact_items(
        families.get("Mindfulness", []) + families.get("Hearing & audio", [])
    )
    walking_facts = _fact_items(families.get("Walking metrics", []))

    source_names = sorted({_source_label(row) for row in day_rows})
    via = (
        " + ".join(
            sorted(
                {
                    str(row.get("source_family") or "").replace("_", " ").title()
                    for row in day_rows
                    if row.get("source_family")
                }
            )
        )
        or "imports"
    )
    sources = (
        {
            "names": source_names,
            "entry_total": len(day_rows),
            "entry_total_label": f"{len(day_rows):,}",
            "via": via,
        }
        if day_rows
        else None
    )

    date_label = _format_day_long(day)
    by_day = _read_health_dedupe_stats(journal_root)["by_day"]

    return {
        "day": day,
        "date_label": date_label,
        "summary_markdown": _find_day_summary(journal_root, day),
        "glucose": _glucose_stats(day_rows),
        "entry_total": len(day_rows),
        "has_data": bool(day_rows),
        "lede": _day_lede(day_rows, sleep, glucose_series, activity),
        "sleep": sleep,
        "glucose_series": glucose_series,
        "activity": activity,
        "heart": {"facts": heart_facts} if heart_facts else None,
        "mind_sound": {"facts": mind_sound_facts} if mind_sound_facts else None,
        "walking": {"facts": walking_facts} if walking_facts else None,
        "sources": sources,
        "prompts": (
            _day_prompts(
                date_label,
                has_sleep=sleep is not None,
                has_glucose=bool(glucose_series),
                has_workouts=bool(activity and activity["workouts"]),
            )
            if day_rows
            else []
        ),
        "audit": {
            "types": dict(
                sorted(
                    Counter(
                        str(row.get("record_type") or "") for row in day_rows
                    ).items()
                )
            ),
            "import_ids": sorted(
                {str(row.get("import_id")) for row in day_rows if row.get("import_id")}
            ),
        },
        "nearest": _nearest_days_with_data(by_day, day),
    }


# --- Window API -------------------------------------------------------------


def _iso(moment: datetime) -> str:
    return moment.isoformat()


def _time_label(moment: datetime) -> str:
    return _format_clock(moment)


def _window_family_items(rows: list[dict[str, Any]]) -> list[dict[str, Any]]:
    counts: Counter[str] = Counter()
    for row in rows:
        counts[_family_for_type(str(row.get("record_type") or ""))] += 1
    return [
        {
            "name": family,
            "count": counts[family],
            "count_label": f"{counts[family]:,}",
        }
        for family in _FAMILY_ORDER
        if counts.get(family)
    ]


def _window_signal_items(rows: list[dict[str, Any]]) -> list[dict[str, Any]]:
    counts: Counter[str] = Counter(
        friendly_type_name(str(row.get("record_type") or "")) for row in rows
    )
    return [
        {"label": label, "count": count, "count_label": f"{count:,}"}
        for label, count in sorted(counts.items(), key=lambda item: (-item[1], item[0]))
    ]


def _window_heart_rate(rows: list[dict[str, Any]]) -> dict[str, Any]:
    heart_rows = [
        row
        for row in rows
        if str(row.get("record_type") or "") == "HKQuantityTypeIdentifierHeartRate"
    ]
    values = [
        value
        for value in (_parse_float(row.get("value")) for row in heart_rows)
        if value is not None
    ]
    if not values:
        return {"count": 0, "min": None, "max": None, "unit": None, "label": None}
    units = sorted(
        {str(row.get("unit")) for row in heart_rows if row.get("unit") is not None}
    )
    unit = units[0] if len(units) == 1 else ("mixed" if units else None)
    range_label = f"{_format_number(min(values))}–{_format_number(max(values))}"
    return {
        "count": len(values),
        "count_label": f"{len(values):,}",
        "min": min(values),
        "max": max(values),
        "unit": unit,
        "label": f"{range_label} {unit}".strip() if unit else range_label,
    }


def _window_glucose(rows: list[dict[str, Any]]) -> dict[str, Any]:
    readings: list[dict[str, Any]] = []
    for row in rows:
        if not _is_glucose_type(str(row.get("record_type") or "")):
            continue
        value = _parse_float(row.get("value"))
        moment = _parse_record_time(row.get("start_date") or row.get("start_time"))
        if value is None or moment is None:
            continue
        unit = str(row.get("unit") or "").strip() or None
        readings.append(
            {
                "time": _time_label(moment),
                "iso": _iso(moment),
                "value": value,
                "value_label": _format_number(value),
                "unit": unit,
                "source": _source_label(row),
            }
        )
    readings.sort(key=lambda item: _time_sort_key(str(item["iso"])))
    if not readings:
        return {
            "count": 0,
            "readings": [],
            "unit": None,
            "delta_label": None,
            "min": None,
            "max": None,
        }
    units = sorted({reading["unit"] for reading in readings if reading["unit"]})
    unit = units[0] if len(units) == 1 else ("mixed" if units else None)
    first = readings[0]
    last = readings[-1]
    delta = f"{first['value_label']} → {last['value_label']}"
    if unit and unit != "mixed":
        delta += f" {unit}"
    values = [float(reading["value"]) for reading in readings]
    return {
        "count": len(readings),
        "count_label": f"{len(readings):,}",
        "readings": readings,
        "unit": unit,
        "delta_label": delta,
        "first": first,
        "last": last,
        "min": min(values),
        "max": max(values),
    }


def _window_steps(rows: list[dict[str, Any]]) -> dict[str, Any]:
    step_rows = [
        row for row in rows if "StepCount" in str(row.get("record_type") or "")
    ]
    if not step_rows:
        return {"samples": 0, "mode": "none", "label": None}
    sources = sorted({_source_label(row) for row in step_rows})
    values = [
        value
        for value in (_parse_float(row.get("value")) for row in step_rows)
        if value is not None
    ]
    if len(sources) == 1 and values:
        total = int(round(sum(values)))
        return {
            "mode": "total",
            "samples": len(step_rows),
            "samples_label": f"{len(step_rows):,}",
            "total": total,
            "total_label": f"{total:,}",
            "source": sources[0],
            "label": f"{total:,} steps",
        }
    samples = len(step_rows)
    return {
        "mode": "samples",
        "samples": samples,
        "samples_label": f"{samples:,}",
        "label": f"{samples:,} step samples",
    }


def _workout_window_items(
    rows: list[dict[str, Any]], window_start: datetime, window_end: datetime
) -> list[dict[str, Any]]:
    items: list[dict[str, Any]] = []
    for row in rows:
        if row.get("kind") != "workout":
            continue
        interval = _row_interval(row)
        if interval is None:
            continue
        start, end = interval
        minutes = _overlap_minutes(start, end, window_start, window_end)
        if minutes <= 0:
            continue
        duration = _workout_duration_minutes(row)
        items.append(
            {
                "name": friendly_type_name(str(row.get("record_type") or "Workout")),
                "start": _iso(start),
                "end": _iso(end),
                "start_label": _time_label(start),
                "end_label": _time_label(end),
                "overlap_minutes": round(minutes, 1),
                "overlap_label": _format_duration(minutes),
                "duration_label": _format_duration(duration)
                if duration is not None
                else None,
                "source": _source_label(row),
            }
        )
    return sorted(items, key=lambda item: _time_sort_key(str(item["start"])))


def _sleep_window_events(
    rows: list[dict[str, Any]], window_start: datetime, window_end: datetime
) -> list[dict[str, Any]]:
    intervals_by_source: dict[str, list[tuple[datetime, datetime]]] = {}
    for row in rows:
        if not _is_sleep_type(str(row.get("record_type") or "")):
            continue
        interval = _row_interval(row)
        if interval is None:
            continue
        intervals_by_source.setdefault(_source_label(row), []).append(interval)

    events: list[dict[str, Any]] = []
    for source, intervals in intervals_by_source.items():
        for start, end in merge_sleep_sessions(intervals):
            minutes = _overlap_minutes(start, end, window_start, window_end)
            if minutes <= 0:
                continue
            events.append(
                {
                    "kind": "sleep",
                    "label": "Sleep",
                    "start": _iso(start),
                    "end": _iso(end),
                    "start_label": _time_label(start),
                    "end_label": _time_label(end),
                    "overlap_minutes": round(minutes, 1),
                    "overlap_label": _format_duration(minutes),
                    "source": source,
                }
            )
    return sorted(events, key=lambda item: _time_sort_key(str(item["start"])))


def _window_events(
    rows: list[dict[str, Any]], window_start: datetime, window_end: datetime
) -> list[dict[str, Any]]:
    workout_events = [
        {
            "kind": "workout",
            "label": item["name"],
            "start": item["start"],
            "end": item["end"],
            "start_label": item["start_label"],
            "end_label": item["end_label"],
            "overlap_minutes": item["overlap_minutes"],
            "overlap_label": item["overlap_label"],
            "source": item["source"],
        }
        for item in _workout_window_items(rows, window_start, window_end)
    ]
    return sorted(
        _sleep_window_events(rows, window_start, window_end) + workout_events,
        key=lambda item: _time_sort_key(str(item["start"])),
    )


def _window_sources(rows: list[dict[str, Any]]) -> dict[str, Any]:
    counts: Counter[str] = Counter(_source_label(row) for row in rows)
    names = sorted(counts)
    return {
        "names": names,
        "count": len(names),
        "chips": [
            {"name": name, "count": counts[name], "count_label": f"{counts[name]:,}"}
            for name in names
        ],
    }


def _window_brief(
    heart_rate: dict[str, Any],
    glucose: dict[str, Any],
    steps: dict[str, Any],
    workouts: list[dict[str, Any]],
) -> list[str]:
    rows: list[str] = []
    if heart_rate.get("count"):
        rows.append(f"Heart rate {heart_rate['label']}")
    if glucose.get("count"):
        rows.append(f"Glucose {glucose['delta_label']}")
    if steps.get("label"):
        rows.append(str(steps["label"]))
    if workouts:
        count = len(workouts)
        rows.append(f"{count} workout" + ("s" if count != 1 else ""))
    return rows


def _build_health_window(
    journal_root: Path,
    window_start: datetime,
    window_end: datetime,
    *,
    reader: Callable[[str], list[dict[str, Any]]] | None = None,
) -> dict[str, Any]:
    rows = _rows_for_window(journal_root, window_start, window_end, reader=reader)
    rows.sort(key=lambda row: _time_sort_key(_row_time(row) or ""))
    heart_rate = _window_heart_rate(rows)
    glucose = _window_glucose(rows)
    steps = _window_steps(rows)
    workouts = _workout_window_items(rows, window_start, window_end)
    brief = _window_brief(heart_rate, glucose, steps, workouts)
    span_minutes = (window_end - window_start).total_seconds() / 60
    return {
        "from": _iso(window_start),
        "to": _iso(window_end),
        "span_minutes": round(span_minutes, 1),
        "has_data": bool(rows),
        "entry_total": len(rows),
        "entry_total_label": f"{len(rows):,}",
        "families": _window_family_items(rows),
        "signals": _window_signal_items(rows),
        "heart_rate": heart_rate,
        "glucose": glucose,
        "steps": steps,
        "workouts": workouts,
        "events": _window_events(rows, window_start, window_end),
        "sources": _window_sources(rows),
        "brief": brief,
        "brief_label": " · ".join(brief),
    }


@body_bp.route("/")
def index():
    # The overview is the stable Body home: no date-nav pill here — the
    # day grid and recent-day rail are the pickers. Day pages own ‹ date ›.
    # (Deliberate divergence from transcripts/timeline, which land on a day:
    # Body's archive-first landing is the product identity.)
    return render_template(
        "app.html",
        body_status=_build_health_import_status(_journal_root()),
    )


@body_bp.route("/<day>")
def day_view(day: str):
    try:
        body_day = _build_health_day(_journal_root(), day)
    except ValueError:
        return error_response(INVALID_DAY)
    return render_template(
        "app.html",
        body_status=None,
        body_day=body_day,
    )


@body_bp.get("/api/status")
def api_status():
    return jsonify(_build_health_import_status(_journal_root()))


@body_bp.get("/api/day/<day>")
def api_day(day: str):
    try:
        return jsonify(_build_health_day(_journal_root(), day))
    except ValueError:
        return error_response(INVALID_DAY)


@body_bp.get("/api/window")
def api_window():
    window_start = _parse_window_bound(request.args.get("from"))
    window_end = _parse_window_bound(request.args.get("to"))
    if window_start is None or window_end is None:
        return error_response(
            INVALID_REQUEST_VALUE,
            detail="Window requires valid from and to ISO timestamps.",
        )
    if window_end <= window_start:
        return error_response(
            INVALID_REQUEST_VALUE,
            detail="Window end must be after window start.",
        )
    if window_end - window_start > timedelta(days=MAX_WINDOW_DAYS):
        return error_response(
            INVALID_REQUEST_VALUE,
            detail=f"Window span must be {MAX_WINDOW_DAYS} days or less.",
        )
    return jsonify(_build_health_window(_journal_root(), window_start, window_end))


@body_bp.get("/api/stats/<month>")
def api_month_stats(month: str):
    if not re.fullmatch(r"\d{6}|\d{4}-\d{2}", month):
        return error_response(INVALID_REQUEST_VALUE, detail="Invalid month")
    month_key = f"{month[:4]}-{month[4:6]}" if len(month) == 6 else month
    day_counts = _read_health_dedupe_stats(_journal_root())["by_day"]
    return jsonify(
        {
            day: count
            for day, count in day_counts.items()
            if f"{day[:4]}-{day[4:6]}" == month_key
        }
    )
