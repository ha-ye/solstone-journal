# SPDX-License-Identifier: AGPL-3.0-only
# Copyright (c) 2026 sol pbc

"""Read-only views over health data imported into the journal.

Reads import manifests, month-bounded normalized shards, and the
importer-owned dedupe database. Read paths create nothing on disk.

Two altitudes:

- ARCHIVE (``/app/body``) — what the journal holds about the body across
  all time: coverage, month heat-strip, recent days, coverage families,
  sources, and an audit drawer with the raw import bookkeeping.
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
from datetime import date, datetime, time, timedelta, timezone
from pathlib import Path
from statistics import mean
from typing import Any, Callable

from flask import Blueprint, jsonify, render_template

from solstone.convey import state
from solstone.convey.reasons import INVALID_DAY, INVALID_REQUEST_VALUE
from solstone.convey.utils import error_response
from solstone.think.importers.health_schema import friendly_type_name

logger = logging.getLogger(__name__)

body_bp = Blueprint("app:body", __name__, url_prefix="/app/body")

DAY_RE = re.compile(r"^\d{8}$")
MONTH_RE = re.compile(r"^\d{4}-\d{2}$")
DAY_SUMMARY_STREAM = "import.apple_health"
APPLE_HEALTH_SOURCE_TYPE = "apple_health"
DAY_SUMMARY_FILE = "day_summary_transcript.md"

# Sleep-analysis intervals from one source separated by less than this gap
# merge into one session (brief wake windows stay inside the night).
SLEEP_SESSION_GAP_MINUTES = 60
# Glucose readings further apart than this render as separate curve
# segments instead of a line drawn across the gap.
GLUCOSE_SEGMENT_GAP_MINUTES = 45
# The sleep bar axis runs 6 PM of the previous day to 6 PM of the day.
SLEEP_AXIS_START_HOUR = 18
RECENT_DAY_LIMIT = 4
STALE_SOURCE_DAYS = 30

GLUCOSE_SVG_WIDTH = 1440.0
GLUCOSE_SVG_HEIGHT = 260.0

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


def _iter_month_span(first: str, last: str) -> list[str]:
    year, mon = int(first[:4]), int(first[5:7])
    last_year, last_mon = int(last[:4]), int(last[5:7])
    months: list[str] = []
    while (year, mon) <= (last_year, last_mon):
        months.append(f"{year:04d}-{mon:02d}")
        mon += 1
        if mon == 13:
            year, mon = year + 1, 1
    return months


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


def _month_heat_strip(
    by_month: dict[str, int], by_day: dict[str, int]
) -> list[dict[str, Any]]:
    """Month cells first→last with log-scale intensity; empty months stay pale."""
    if not by_month:
        return []
    months = sorted(by_month)
    first_day_by_month: dict[str, str] = {}
    for day in by_day:
        month = f"{day[:4]}-{day[4:6]}"
        if month not in first_day_by_month or day < first_day_by_month[month]:
            first_day_by_month[month] = day
    scale = math.log1p(max(by_month.values()))
    cells: list[dict[str, Any]] = []
    for month in _iter_month_span(months[0], months[-1]):
        count = by_month.get(month, 0)
        intensity = round(math.log1p(count) / scale, 3) if count and scale else 0.0
        cells.append(
            {
                "month": month,
                "label": _format_month_label(month),
                "count": count,
                "count_label": f"{count:,}",
                "intensity": intensity,
                "first_day": first_day_by_month.get(month),
            }
        )
    return cells


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
        "heat": _month_heat_strip(by_month, dedupe["by_day"]),
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


def _merge_intervals(
    intervals: list[tuple[datetime, datetime]], *, gap_minutes: int
) -> list[list[datetime]]:
    ordered = sorted(intervals, key=lambda interval: interval[0])
    gap = timedelta(minutes=gap_minutes)
    merged: list[list[datetime]] = []
    for start, end in ordered:
        if end < start:
            end = start
        if merged and start <= merged[-1][1] + gap:
            if end > merged[-1][1]:
                merged[-1][1] = end
        else:
            merged.append([start, end])
    return merged


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

    noon = time(12, 0)
    per_source: dict[str, tuple[list[datetime] | None, list[list[datetime]]]] = {}
    for source, intervals in intervals_by_source.items():
        sessions = _merge_intervals(intervals, gap_minutes=SLEEP_SESSION_GAP_MINUTES)
        ending_today = [s for s in sessions if s[1].date() == target]
        main: list[datetime] | None = None
        naps: list[list[datetime]] = []
        for session in sorted(ending_today, key=lambda s: s[1]):
            crosses_midnight = session[0].date() < target
            ends_morning = session[1].time() <= noon
            if main is None and (crosses_midnight or ends_morning):
                main = session
            elif session[0].date() == target:
                naps.append(session)
        if main is not None or naps:
            per_source[source] = (main, naps)
    if not per_source:
        return None

    def _coverage_seconds(source: str) -> float:
        main, naps = per_source[source]
        if main is not None:
            return (main[1] - main[0]).total_seconds()
        return sum((nap[1] - nap[0]).total_seconds() for nap in naps)

    primary = max(sorted(per_source), key=_coverage_seconds)
    main, naps = per_source[primary]
    axis_day = target - timedelta(days=1)

    def _bar_segment(session: list[datetime], kind: str) -> dict[str, Any]:
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

    def _session_view(session: list[datetime]) -> dict[str, str]:
        minutes = (session[1] - session[0]).total_seconds() / 60
        return {
            "window": f"{_format_clock(session[0])} – {_format_clock(session[1])}",
            "duration": _format_duration(minutes),
        }

    return {
        "source": primary,
        "other_sources": [name for name in sorted(per_source) if name != primary],
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


@body_bp.route("/")
def index():
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
