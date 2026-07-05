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
import threading
from collections import Counter, defaultdict
from contextlib import closing
from datetime import date, datetime, timedelta, timezone
from pathlib import Path
from statistics import mean, median
from typing import Any, Callable

from flask import (
    Blueprint,
    get_template_attribute,
    jsonify,
    render_template,
    request,
)

from solstone.convey import state
from solstone.convey.reasons import INVALID_DAY, INVALID_REQUEST_VALUE
from solstone.convey.utils import error_response
from solstone.think.importers.health_schema import (
    display_number,
    display_value,
    friendly_type_name,
    friendly_unit_label,
    pick_day_sleep,
)

logger = logging.getLogger(__name__)

body_bp = Blueprint("app:body", __name__, url_prefix="/app/body")

DAY_RE = re.compile(r"^\d{8}$")
MONTH_RE = re.compile(r"^\d{4}-\d{2}$")
DAY_SUMMARY_STREAM = "import.apple_health"
APPLE_HEALTH_SOURCE_TYPE = "apple_health"
DAY_SUMMARY_FILE = "day_summary_transcript.md"

# Day-curve readings further apart than this render as separate curve
# segments instead of a line drawn across the gap (glucose and heart rate
# share the constant).
CURVE_SEGMENT_GAP_MINUTES = 45
# The sleep bar axis runs 6 PM of the previous day to 6 PM of the day.
SLEEP_AXIS_START_HOUR = 18
RECENT_DAY_LIMIT = 14
# Largest carousel batch one /api/recent call will build — a month and a
# nudge, so a single fetch never folds an unbounded stretch of the archive.
RECENT_BATCH_LIMIT_CAP = 31
STALE_SOURCE_DAYS = 30

CURVE_SVG_WIDTH = 1440.0
CURVE_SVG_HEIGHT = 260.0
MAX_WINDOW_DAYS = 7

HEART_RATE_TYPE = "HKQuantityTypeIdentifierHeartRate"
# Heart-rate readings fold into buckets this wide; the median draws the
# line and the bucket min–max renders as a translucent band.
HEART_BUCKET_MINUTES = 5
# Below this many readings a day curve would overstate the data — the
# card keeps its text-only range row instead.
HEART_CURVE_MIN_READINGS = 12

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
            "IrregularHeartRhythm",
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
        months = [
            path.stem
            for path in sorted((manifest_path.parent / "normalized").glob("*.jsonl"))
        ]
        manifest["normalized_months"] = months
        if not months:
            manifest["normalized_months_label"] = "—"
        elif len(months) == 1:
            manifest["normalized_months_label"] = months[0]
        else:
            manifest["normalized_months_label"] = (
                f"{months[0]} – {months[-1]} · {len(months)} months"
            )
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
    kept_by_key: dict[str, dict[str, Any]] = {}
    for path in sorted(imports_root.glob(pattern)):
        for row in _read_shard_rows(path):
            # The same entry appears in every bundle that imported it
            # (e.g. a test-week import overlapped by the full backfill);
            # keep one row per dedupe key, remembering every bundle it
            # appeared in so the day audit can list them all.
            dedupe_key = row.get("dedupe_key")
            if isinstance(dedupe_key, str) and dedupe_key:
                kept = kept_by_key.get(dedupe_key)
                if kept is not None:
                    import_id = row.get("import_id")
                    if import_id:
                        bundles = kept.setdefault("import_ids", [])
                        if str(import_id) not in bundles:
                            bundles.append(str(import_id))
                    continue
                kept_by_key[dedupe_key] = row
                if row.get("import_id"):
                    row["import_ids"] = [str(row["import_id"])]
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


def _format_month_full(month: str) -> str:
    return f"{_MONTH_FULL[int(month[5:7]) - 1]} {month[:4]}"


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
        # The GROUP BY below sorts the whole table (~2M rows); with the
        # default temp_store that sort spills hundreds of MB to disk temp
        # files — the same class of spill as the recent disk-full incident.
        # Keep the sorter in memory: the footprint is transient, released
        # at connection close, and an allocation failure fails this one
        # stats build instead of filling the disk. Per-connection pragma;
        # the read-only database itself is untouched.
        conn.execute("PRAGMA temp_store = MEMORY")
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


# The stats cache above dies with the process, so the first request after
# every convey restart used to pay the cold full-table scan (~10s on a 2M-row
# database). solstone/apps/body/events.py kicks this warm once at service
# startup and again when an import completes.
_stats_warm_flight = threading.Lock()


def warm_dedupe_stats_cache() -> threading.Thread | None:
    """Build the dedupe-stats cache entry in a background daemon thread.

    Single-flight: returns ``None`` when a warm is already running instead
    of stacking table scans. The request path never touches the flight
    lock — a request arriving mid-warm computes exactly as before, and the
    two results agree because the cache is keyed by the db+wal signature.
    Failures are logged and swallowed: warming is an optimization and must
    never break startup or serving.
    """
    if not _stats_warm_flight.acquire(blocking=False):
        return None

    journal_root = _journal_root()

    def _warm() -> None:
        try:
            _read_health_dedupe_stats(journal_root)
        except Exception:
            logger.exception("Body dedupe-stats cache warm failed")
        finally:
            _stats_warm_flight.release()

    thread = threading.Thread(target=_warm, name="body-stats-warm", daemon=True)
    thread.start()
    return thread


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
    journal_root: Path,
    by_day: dict[str, int],
    *,
    before: str | None = None,
    limit: int = RECENT_DAY_LIMIT,
) -> tuple[list[dict[str, Any]], bool]:
    """Compact facts for days that have entries, newest first.

    Without ``before`` this is the overview's initial rail: the newest
    ``limit`` days with entries. With ``before`` it is the paged
    continuation the carousel walks the whole archive with — only days
    strictly older than the cursor, still newest first. Days without
    entries never appear (``by_day`` holds only counted days), so gaps
    and month boundaries fold away naturally. The second return value
    says whether days with entries older than this batch remain.
    """
    eligible = sorted(day for day in by_day if before is None or day < before)
    days = eligible[-limit:][::-1]
    has_more = len(eligible) > len(days)
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
        # The rail leads with the asleep figure when stage detail split it
        # from the merged span; the in-bed span rides along as secondary.
        sleep_duration = None
        sleep_in_bed = None
        if sleep:
            sleep_duration = sleep.get("asleep_duration") or sleep.get("duration")
            if sleep.get("asleep_duration") and sleep["asleep_duration"] != sleep.get(
                "in_bed_duration"
            ):
                sleep_in_bed = sleep["in_bed_duration"]
        activity = payload["activity"]
        items.append(
            {
                "day": day,
                "label": _format_day_short(day),
                "sleep_duration": sleep_duration,
                "sleep_in_bed": sleep_in_bed,
                "glucose_label": glucose_label,
                "workout_count": len(activity["workouts"]) if activity else 0,
                "source_count": len(payload["sources"]["names"])
                if payload["sources"]
                else 0,
            }
        )
    return items, has_more


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
    recent_days, recent_days_has_more = _recent_day_rail(journal_root, dedupe["by_day"])
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
        "recent_days": recent_days,
        "recent_days_has_more": recent_days_has_more,
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
        "sources_month_label": (
            _format_month_full(recent["month"]) if recent["month"] else None
        ),
        "day_counts": dedupe["by_day"],
        "archive": _build_archive(
            journal_root, dedupe=dedupe, imports=imports, recent=recent
        ),
    }


# --- Day cards ---------------------------------------------------------------
#
# Presentation parity rule for the card builders below: signals with dense
# timestamped readings render as day curves (glucose, heart rate); signals
# with few readings render value summaries (ranges, totals, paired
# readings); counts alone are a last resort. Future signals (Oura
# readiness and kin) inherit this intent.


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
        # The y-axis labels name the actual rendered domain, so the padded
        # bounds round outward to whole numbers the labels can state.
        pad = max((v_max - v_min) * 0.08, 2.0)
        lo = float(math.floor(v_min - pad))
        hi = float(math.ceil(v_max + pad))

        def _y(value: float) -> float:
            return round(
                CURVE_SVG_HEIGHT - (value - lo) / (hi - lo) * CURVE_SVG_HEIGHT, 1
            )

        segments: list[list[list[float]]] = []
        current: list[list[float]] = []
        prev_minute: float | None = None
        for minute, value in points:
            if (
                prev_minute is not None
                and minute - prev_minute > CURVE_SEGMENT_GAP_MINUTES
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
                    "width": CURVE_SVG_WIDTH,
                    "height": CURVE_SVG_HEIGHT,
                    "paths": paths,
                    "dots": dots,
                    "y_min_label": _format_number(lo),
                    "y_max_label": _format_number(hi),
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
    next_rows: list[dict[str, Any]],
    day: str,
) -> dict[str, Any] | None:
    """The day's sleep card: the session ending that morning, naps separate.

    Cross-midnight rule: entries are day-attributed by start time, so the
    night that ends this morning mostly lives on the previous day, and the
    night that starts this evening continues into the next day — all three
    days' rows feed the merge, so a bedtime fragment merges into the
    following night instead of misreading as this day's nap. Multiple
    sources are never summed: the longest-coverage source is primary,
    others are named in the footer. The merge + main-session rule is the
    shared canonical implementation in ``health_schema`` — the importer's
    day cards use the same one.
    """
    target = date(int(day[:4]), int(day[4:6]), int(day[6:8]))
    intervals_by_source: dict[str, list[tuple[datetime, datetime, str | None]]] = {}
    for row in prev_rows + day_rows + next_rows:
        if not _is_sleep_type(str(row.get("record_type") or "")):
            continue
        start = _parse_record_time(row.get("start_date") or row.get("start_time"))
        if start is None:
            continue
        end = _parse_record_time(row.get("end_date")) or start
        stage = str(row["value"]) if row.get("value") is not None else None
        intervals_by_source.setdefault(_source_label(row), []).append(
            (start, end, stage)
        )
    if not intervals_by_source:
        return None

    sleep = pick_day_sleep(intervals_by_source, target)
    if sleep is None:
        return None
    main = sleep.main
    axis_day = target - timedelta(days=1)
    # A doze at or past the 6 PM axis end has no honest place inside the
    # 6 PM – 6 PM axis: its bar and its list label drop together instead
    # of leaving a clamped sliver at the edge under an orphaned label.
    naps = [nap for nap in sleep.naps if _axis_minute(nap[0], axis_day) < 1440.0]

    def _bar_segment(session: tuple[datetime, datetime], kind: str) -> dict[str, Any]:
        left = min(max(_axis_minute(session[0], axis_day), 0.0), 1440.0)
        right = min(max(_axis_minute(session[1], axis_day), 0.0), 1440.0)
        # A session clipped by the axis edge (a nap straddling 6 PM) keeps
        # its minimum width inside the axis instead of collapsing to nothing.
        width = max(right - left, 4.0)
        left = min(left, 1440.0 - width)
        return {
            "x": round(left, 1),
            "width": round(width, 1),
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

    # ``duration`` stays the merged main-session span — the same figure the
    # importer's day card states — while ``asleep_duration`` carries the
    # stage-aware headline figure the page surfaces prefer when present.
    in_bed_duration = (
        _format_duration(sleep.in_bed_minutes)
        if sleep.in_bed_minutes is not None
        else None
    )
    asleep_duration = (
        _format_duration(sleep.asleep_minutes)
        if sleep.has_stage_detail and sleep.asleep_minutes is not None
        else None
    )

    return {
        "source": sleep.source,
        "other_sources": list(sleep.other_sources),
        "window": _session_view(main)["window"] if main is not None else None,
        "duration": _session_view(main)["duration"] if main is not None else None,
        "asleep_duration": asleep_duration,
        "in_bed_duration": in_bed_duration,
        "has_stage_detail": sleep.has_stage_detail,
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


def _workout_duration_label(minutes: float | None) -> str | None:
    """Duration copy for a workout row.

    Sub-minute workouts read '<1m' instead of a false '0m'; zero-length
    rows carry no duration at all.
    """

    if minutes is None or minutes <= 0:
        return None
    if minutes < 1:
        return "<1m"
    return _format_duration(minutes)


def _workout_metric(
    row: dict[str, Any],
    *,
    value_key: str,
    unit_key: str,
    type_key: str,
    fallback_type: str,
) -> dict[str, Any] | None:
    metadata = row.get("metadata")
    if not isinstance(metadata, dict):
        return None
    value = _parse_float(metadata.get(value_key))
    if value is None:
        return None
    unit = str(metadata.get(unit_key) or "").strip() or None
    record_type = str(metadata.get(type_key) or fallback_type)
    return {
        "value": value,
        "unit": unit,
        "record_type": record_type,
        "label": _summable_total_label(record_type, value, unit),
    }


def _workout_metrics(row: dict[str, Any]) -> dict[str, Any]:
    distance = _workout_metric(
        row,
        value_key="totalDistance",
        unit_key="totalDistanceUnit",
        type_key="totalDistanceType",
        fallback_type="HKQuantityTypeIdentifierDistanceWalkingRunning",
    )
    energy = _workout_metric(
        row,
        value_key="totalEnergyBurned",
        unit_key="totalEnergyBurnedUnit",
        type_key="totalEnergyBurnedType",
        fallback_type="HKQuantityTypeIdentifierActiveEnergyBurned",
    )
    # A recovered total that rounds to zero carries no display label —
    # it stays out of the joined metrics line rather than reading '0 Cal'.
    labels = [
        metric["label"]
        for metric in (distance, energy)
        if metric is not None and metric["label"] is not None
    ]
    return {
        "distance": distance,
        "energy": energy,
        "metric_labels": labels,
        "metrics_label": " · ".join(labels) if labels else None,
    }


def _group_by_type(rows: list[dict[str, Any]]) -> dict[str, list[dict[str, Any]]]:
    grouped: dict[str, list[dict[str, Any]]] = defaultdict(list)
    for row in rows:
        grouped[str(row.get("record_type") or "")].append(row)
    return grouped


def _single_unit(rows: list[dict[str, Any]]) -> tuple[str | None, bool]:
    """The rows' shared unit and whether units were consistent."""
    units = {str(row.get("unit")) for row in rows if row.get("unit") is not None}
    if len(units) > 1:
        return None, False
    return (next(iter(units)) if units else None), True


def _display_range(record_type: str, low: float, high: float, unit: str | None) -> str:
    """Owner-facing 'LOW–HIGH unit' label through the shared normalizers."""
    low_label = display_number(record_type, low, unit)
    high_label = display_number(record_type, high, unit)
    span = low_label if low_label == high_label else f"{low_label}–{high_label}"
    unit_label = friendly_unit_label(record_type, unit)
    if not unit_label:
        return span
    if unit_label == "%":
        return f"{span}%"
    return f"{span} {unit_label}"


def _primary_source_totals(rows: list[dict[str, Any]]) -> dict[str, Any] | None:
    """The primary-source summed value, mirroring ``pick_day_sleep``.

    The largest-coverage source's total is reported and labeled with its
    source; other sources are only named, never summed. Returns ``None``
    when several sources contributed but none has usable coverage to rank
    by — the caller falls back to sample counts.
    """

    per_source: dict[str, dict[str, float]] = {}
    for row in rows:
        value = _parse_float(row.get("value"))
        if value is None:
            continue
        entry = per_source.setdefault(
            _source_label(row), {"total": 0.0, "coverage": 0.0, "samples": 0.0}
        )
        entry["total"] += value
        entry["samples"] += 1
        interval = _row_interval(row)
        if interval is not None:
            entry["coverage"] += (interval[1] - interval[0]).total_seconds()
    if not per_source:
        return None
    if len(per_source) == 1:
        primary = next(iter(per_source))
    else:
        usable = [name for name in per_source if per_source[name]["coverage"] > 0]
        if not usable:
            return None
        primary = max(sorted(usable), key=lambda name: per_source[name]["coverage"])
    others = sorted(name for name in per_source if name != primary)
    return {
        "total": per_source[primary]["total"],
        "source": primary,
        "samples": int(per_source[primary]["samples"]),
        "others": others,
        "others_label": (f"{', '.join(others)} also contributed" if others else None),
    }


def _primary_source_steps(step_rows: list[dict[str, Any]]) -> dict[str, Any] | None:
    """The primary-source step total as the steps payload."""

    picked = _primary_source_totals(step_rows)
    if picked is None:
        return None
    total = int(round(picked["total"]))
    return {
        "mode": "total",
        "total": total,
        "total_label": f"{total:,}",
        "source": picked["source"],
        "samples": picked["samples"],
        "others": picked["others"],
        "others_label": picked["others_label"],
    }


_RUNNING_DYNAMICS_FRAGMENTS = (
    "RunningPower",
    "RunningSpeed",
    "RunningStrideLength",
    "RunningGroundContactTime",
    "RunningVerticalOscillation",
)

# Speed units convertible to a pace per kilometer.
_PACE_SECONDS_PER_KM = {
    "m/s": 1000.0,
    "km/h": 3600.0,
    "km/hr": 3600.0,
}


def _is_running_dynamics_type(record_type: str) -> bool:
    return any(fragment in record_type for fragment in _RUNNING_DYNAMICS_FRAGMENTS)


def _pace_label(speed: float, unit: str) -> str | None:
    """A M:SS pace per kilometer from a speed value, when the unit permits."""
    scale = _PACE_SECONDS_PER_KM.get(unit)
    if scale is None or speed <= 0:
        return None
    minutes, seconds = divmod(int(round(scale / speed)), 60)
    return f"{minutes}:{seconds:02d}"


def _running_dynamics(rows: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """Per-type min/avg/max summaries for a day's running-dynamics rows."""
    items: list[dict[str, Any]] = []
    grouped = _group_by_type(rows)
    for record_type in sorted(grouped, key=friendly_type_name):
        rows_for_type = grouped[record_type]
        values = [
            value
            for value in (_parse_float(row.get("value")) for row in rows_for_type)
            if value is not None
        ]
        unit, consistent = _single_unit(rows_for_type)
        summary: str | None = None
        if values and consistent:
            low, high, avg = min(values), max(values), mean(values)
            if "RunningSpeed" in record_type and unit:
                # Fastest pace comes from the highest speed.
                fast = _pace_label(high, unit)
                slow = _pace_label(low, unit)
                middle = _pace_label(avg, unit)
                if fast and slow and middle:
                    span = fast if fast == slow else f"{fast}–{slow}"
                    summary = f"{span} /km · avg {middle} /km"
            if summary is None:
                span = _display_range(record_type, low, high, unit)
                if low == high:
                    summary = span
                else:
                    summary = f"{span} · avg {display_number(record_type, avg, unit)}"
        items.append(
            {
                "label": friendly_type_name(record_type),
                "count": len(rows_for_type),
                "count_label": f"{len(rows_for_type):,}",
                "summary": summary,
            }
        )
    return items


# Walking metrics that read as an average alone — per-walk gait samples
# swing enough that a min–max span would overstate what the day states.
_WALKING_AVG_ONLY_FRAGMENTS = (
    "WalkingStepLength",
    "WalkingDoubleSupportPercentage",
    "WalkingAsymmetryPercentage",
    "AppleWalkingSteadiness",
)


def _walking_metrics(rows: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """Per-type value summaries for the walking-metrics card.

    Mirrors the running-dynamics treatment: speed-like types summarize as
    min–max plus average ('2.1–3.4 mph · avg 2.8'); step length and the
    gait percentages read as an average ('avg 28.3%'), with the shared
    normalizers scaling HealthKit's 0–1 fraction-percent rows. Entry
    counts stay in the payload as secondary text. Types without usable
    values keep ``summary`` as ``None`` — the card falls back to counts.
    """

    items: list[dict[str, Any]] = []
    grouped = _group_by_type(rows)
    for record_type in sorted(grouped, key=friendly_type_name):
        rows_for_type = grouped[record_type]
        values = [
            value
            for value in (_parse_float(row.get("value")) for row in rows_for_type)
            if value is not None
        ]
        unit, consistent = _single_unit(rows_for_type)
        summary: str | None = None
        if values and consistent:
            if any(fragment in record_type for fragment in _WALKING_AVG_ONLY_FRAGMENTS):
                summary = f"avg {display_value(record_type, mean(values), unit)}"
            else:
                low, high, avg = min(values), max(values), mean(values)
                span = _display_range(record_type, low, high, unit)
                if low == high:
                    summary = span
                else:
                    summary = f"{span} · avg {display_number(record_type, avg, unit)}"
        items.append(
            {
                "label": friendly_type_name(record_type),
                "count": len(rows_for_type),
                "count_label": f"{len(rows_for_type):,}",
                "summary": summary,
            }
        )
    return items


# Quantities whose samples honestly sum to a day total worth showing with
# its unit instead of a bare entry count. Multi-source days go through the
# primary-source rule (largest coverage wins, others only named) so
# overlapping devices never double-count into one figure.
_SUMMABLE_FRAGMENTS = (
    "FlightsClimbed",
    "AppleExerciseTime",
    "AppleStandTime",
    "TimeInDaylight",
    "ActiveEnergyBurned",
    "BasalEnergyBurned",
    "DistanceWalkingRunning",
    "DistanceCycling",
)

# Stand-hour category rows carry stood/idle values, not quantities; the
# day figure is the number of distinct hours with a stood entry.
_STAND_HOUR_FRAGMENT = "AppleStandHour"


def _summable_total_label(
    record_type: str, total: float, unit: str | None
) -> str | None:
    """Owner-facing total for a summable quantity, or ``None`` at zero.

    Distances read with one decimal ('4.2 mi'); energy totals read as
    whole calories ('612 Cal'); everything else goes through the shared
    display normalizers unchanged. A total that rounds to zero at its
    display precision returns ``None`` — a zero-reading day falls back
    to its entry count instead of manufacturing a '0 Cal' health fact.
    """

    if "Distance" in record_type:
        if round(total, 1) == 0:
            return None
        unit_label = friendly_unit_label(record_type, unit)
        label = f"{total:,.1f}"
        return f"{label} {unit_label}" if unit_label else label
    if "EnergyBurned" in record_type:
        total = float(round(total))
    if display_number(record_type, total, unit) in ("0", "0.0"):
        return None
    return display_value(record_type, total, unit)


def _stood_hour_count(rows: list[dict[str, Any]]) -> int:
    """Distinct hours with a stood stand-hour entry, across sources."""

    stood: set[tuple[date, int]] = set()
    for row in rows:
        if "stood" not in str(row.get("value") or "").lower():
            continue
        moment = _parse_record_time(row.get("start_date") or row.get("start_time"))
        if moment is None:
            continue
        stood.add((moment.date(), moment.hour))
    return len(stood)


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
        metrics = _workout_metrics(row)
        workout_items.append(
            {
                "name": friendly_type_name(str(row.get("record_type") or "Workout")),
                "start": _format_clock(start) if start else None,
                "duration": _workout_duration_label(minutes),
                **metrics,
            }
        )

    workout_summary: str | None = None
    if workout_items:
        kinds = Counter(item["name"] for item in workout_items)
        ordered = sorted(kinds.items(), key=lambda kv: (-kv[1], kv[0]))
        parts = [
            name if count == 1 else f"{name} ×{count}" for name, count in ordered[:2]
        ]
        workout_summary = " · ".join(parts)
        if len(ordered) > 2:
            workout_summary += f" +{len(ordered) - 2} more"

    step_rows = [
        row for row in activity_rows if "StepCount" in str(row.get("record_type") or "")
    ]
    steps: dict[str, Any] | None = None
    if step_rows:
        steps = _primary_source_steps(step_rows)
        if steps is None:
            # No source has usable coverage to rank by; totals would
            # double-count, so present sample counts only.
            steps = {
                "mode": "samples",
                "samples": len(step_rows),
                "samples_label": f"{len(step_rows):,}",
            }

    other_rows = [row for row in activity_rows if row not in step_rows]
    running_rows = [
        row
        for row in other_rows
        if _is_running_dynamics_type(str(row.get("record_type") or ""))
    ]
    counter_rows = [row for row in other_rows if row not in running_rows]

    counter_items: list[dict[str, Any]] = []
    grouped = _group_by_type(counter_rows)
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
        if any(fragment in record_type for fragment in _SUMMABLE_FRAGMENTS):
            unit, consistent = _single_unit(rows_for_type)
            picked = _primary_source_totals(rows_for_type) if consistent else None
            if picked is not None:
                value = _summable_total_label(record_type, picked["total"], unit)
                if value is not None:
                    if picked["others"]:
                        value += f" · {picked['source']} — {picked['others_label']}"
                    item["value"] = value
        elif _STAND_HOUR_FRAGMENT in record_type:
            stood = _stood_hour_count(rows_for_type)
            if stood:
                item["value"] = f"{stood:,}"
        counter_items.append(item)

    return {
        "workouts": workout_items,
        "workout_summary": workout_summary,
        "steps": steps,
        "running": _running_dynamics(running_rows) if running_rows else None,
        "counters": counter_items,
    }


def _fact_items(rows: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """Per-type counts; values where the rows honestly support one.

    A single reading (or resting heart rate) shows its value; mindful
    sessions sum to minutes; audio-level rows summarize as their entry
    count plus the day's factual level range in the rows' own unit.
    """
    grouped = _group_by_type(rows)
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
        if "MindfulSession" in record_type:
            sources = {_source_label(row) for row in rows_for_type}
            if len(sources) == 1:
                minutes = 0.0
                for row in rows_for_type:
                    interval = _row_interval(row)
                    if interval is not None:
                        minutes += (interval[1] - interval[0]).total_seconds() / 60
                if minutes > 0:
                    item["value"] = _format_duration(minutes)
        elif "AudioExposure" in record_type and len(rows_for_type) > 1:
            values = [
                value
                for value in (_parse_float(row.get("value")) for row in rows_for_type)
                if value is not None
            ]
            unit, consistent = _single_unit(rows_for_type)
            if values and consistent:
                span = _display_range(record_type, min(values), max(values), unit)
                item["value"] = f"{item['count_label']} entries · {span}"
        elif len(rows_for_type) == 1 or "RestingHeartRate" in record_type:
            latest = max(
                rows_for_type, key=lambda r: _time_sort_key(_row_time(r) or "")
            )
            value = _parse_float(latest.get("value"))
            if value is not None:
                unit = str(latest.get("unit") or "").strip() or None
                item["value"] = display_value(record_type, value, unit)
        items.append(item)
    return items


def _body_measurement_facts(rows: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """Per-type latest-value summaries for the body-measurements card.

    Body measurements are point-in-time readings, so the day's latest
    reading (by start time) is the honest headline even when a smart
    scale wrote several rows: a single reading shows its value; multi-
    entry types read 'latest 172.4 lb · 6 entries'. Values go through
    the shared display normalizers (body fat's 0–1 fraction renders as
    a percentage). Types without parseable values keep ``value`` as
    ``None`` — the card falls back to counts.
    """

    items: list[dict[str, Any]] = []
    grouped = _group_by_type(rows)
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
        valued = [
            (row, value)
            for row in rows_for_type
            if (value := _parse_float(row.get("value"))) is not None
        ]
        if valued:
            latest_row, latest_value = max(
                valued, key=lambda pair: _time_sort_key(_row_time(pair[0]) or "")
            )
            unit = str(latest_row.get("unit") or "").strip() or None
            label = display_value(record_type, latest_value, unit)
            if len(rows_for_type) > 1:
                label = f"latest {label} · {item['count_label']} entries"
            item["value"] = label
        items.append(item)
    return items


_BP_SYSTOLIC_FRAGMENT = "BloodPressureSystolic"
_BP_DIASTOLIC_FRAGMENT = "BloodPressureDiastolic"
# Above this many paired readings the card compresses to per-component
# ranges instead of a time-stamped list.
_BP_READING_LIMIT = 6


def _blood_pressure(rows: list[dict[str, Any]]) -> dict[str, Any] | None:
    """Systolic/diastolic rows paired by identical start time.

    Returns a time-stamped readings list, or per-component min–max when
    the day holds more than ``_BP_READING_LIMIT`` paired readings. ``None``
    when no rows pair up — the caller keeps the rows in the generic facts.
    """

    by_start: dict[str, dict[str, tuple[dict[str, Any], float]]] = {}
    for row in rows:
        record_type = str(row.get("record_type") or "")
        if _BP_SYSTOLIC_FRAGMENT in record_type:
            component = "systolic"
        elif _BP_DIASTOLIC_FRAGMENT in record_type:
            component = "diastolic"
        else:
            continue
        start = str(row.get("start_date") or "").strip()
        value = _parse_float(row.get("value"))
        if not start or value is None:
            continue
        by_start.setdefault(start, {}).setdefault(component, (row, value))

    readings: list[dict[str, Any]] = []
    systolic_values: list[float] = []
    diastolic_values: list[float] = []
    unit: str | None = None
    for start in sorted(by_start, key=_time_sort_key):
        pair = by_start[start]
        if "systolic" not in pair or "diastolic" not in pair:
            continue
        moment = _parse_record_time(start)
        if moment is None:
            continue
        systolic_row, systolic = pair["systolic"]
        _, diastolic = pair["diastolic"]
        pair_unit = str(systolic_row.get("unit") or "").strip() or None
        unit = unit or pair_unit
        label = f"{_format_number(systolic)}/{_format_number(diastolic)}"
        if pair_unit:
            label += f" {pair_unit}"
        systolic_values.append(systolic)
        diastolic_values.append(diastolic)
        readings.append({"time": _format_clock(moment), "label": label})

    if not readings:
        return None
    count = len(readings)
    result: dict[str, Any] = {
        "count": count,
        "count_label": f"{count:,}",
        "unit": unit,
        "mode": "readings" if count <= _BP_READING_LIMIT else "range",
        "readings": readings if count <= _BP_READING_LIMIT else [],
        "range_label": None,
    }
    if count > _BP_READING_LIMIT:
        unit_suffix = f" {unit}" if unit else ""
        systolic_span = (
            f"{_format_number(min(systolic_values))}–"
            f"{_format_number(max(systolic_values))}"
        )
        diastolic_span = (
            f"{_format_number(min(diastolic_values))}–"
            f"{_format_number(max(diastolic_values))}"
        )
        result["range_label"] = (
            f"systolic {systolic_span}{unit_suffix}"
            f" · diastolic {diastolic_span}{unit_suffix}"
        )
    return result


# Device-reported rhythm notifications (category rows) and the AFib-burden
# percentage. These are the most sensitive rows the app renders: each line
# states what the device reported and which device reported it — a count,
# a device-stated value, an attribution — never interpretation, alarm
# framing, or advice (§13).
_RHYTHM_EVENT_FRAGMENTS = (
    "IrregularHeartRhythmEvent",
    "HighHeartRateEvent",
    "LowHeartRateEvent",
)
_AFIB_BURDEN_FRAGMENT = "AtrialFibrillationBurden"


def _is_rhythm_event_type(record_type: str) -> bool:
    return any(fragment in record_type for fragment in _RHYTHM_EVENT_FRAGMENTS)


def _rhythm_summary(rows: list[dict[str, Any]]) -> dict[str, Any] | None:
    """Factual rhythm lines: what the device reported, attributed to it.

    Event lines state the notification type, how many the day holds, and
    the reporting device(s): 'Irregular rhythm notification · 1 event ·
    reported by Apple Watch'. AFib burden states the device's reported
    percentage through the shared display normalizers: 'AFib burden ·
    2.1% · reported by Apple Watch' (multi-entry days lead with the
    latest reading and carry the entry count). Nothing is interpreted,
    ranked, or advised.
    """

    event_rows: list[dict[str, Any]] = []
    burden_rows: list[dict[str, Any]] = []
    for row in rows:
        record_type = str(row.get("record_type") or "")
        if _is_rhythm_event_type(record_type):
            event_rows.append(row)
        elif _AFIB_BURDEN_FRAGMENT in record_type:
            burden_rows.append(row)
    if not event_rows and not burden_rows:
        return None

    events: list[dict[str, Any]] = []
    grouped = _group_by_type(event_rows)
    for record_type in sorted(grouped, key=friendly_type_name):
        rows_for_type = grouped[record_type]
        label = friendly_type_name(record_type)
        count = len(rows_for_type)
        sources = sorted({_source_label(row) for row in rows_for_type})
        event_word = "event" if count == 1 else "events"
        detail = f"{count:,} {event_word} · reported by {', '.join(sources)}"
        events.append(
            {
                "label": label,
                "count": count,
                "count_label": f"{count:,}",
                "sources": sources,
                "detail": detail,
                "line": f"{label} · {detail}",
            }
        )

    burden: dict[str, Any] | None = None
    if burden_rows:
        record_type = str(burden_rows[0].get("record_type") or "")
        label = friendly_type_name(record_type)
        count = len(burden_rows)
        sources = sorted({_source_label(row) for row in burden_rows})
        valued = [
            (row, value)
            for row in burden_rows
            if (value := _parse_float(row.get("value"))) is not None
        ]
        value_label: str | None = None
        if valued:
            latest_row, latest_value = max(
                valued, key=lambda pair: _time_sort_key(_row_time(pair[0]) or "")
            )
            unit = str(latest_row.get("unit") or "").strip() or None
            value_label = display_value(record_type, latest_value, unit)
        attribution = f"reported by {', '.join(sources)}"
        if value_label is None:
            entry_word = "entry" if count == 1 else "entries"
            detail = f"{count:,} {entry_word} · {attribution}"
        elif count > 1:
            detail = f"latest {value_label} · {count:,} entries · {attribution}"
        else:
            detail = f"{value_label} · {attribution}"
        burden = {
            "label": label,
            "count": count,
            "count_label": f"{count:,}",
            "sources": sources,
            "value": value_label,
            "detail": detail,
            "line": f"{label} · {detail}",
        }

    return {"events": events, "burden": burden}


def _heart_rate_series(rows: list[dict[str, Any]]) -> dict[str, Any] | None:
    """Bucketed heart-rate day curve: median line plus min–max band.

    Heart rate swings within any five minutes, so raw readings fold into
    ``HEART_BUCKET_MINUTES`` buckets — the per-bucket median draws the
    line and the per-bucket min–max renders as a translucent band, the
    honest render for the signal's instantaneous variability. Mirrors
    ``_glucose_series`` geometry: 12 AM → 12 AM x-axis, padded y-domain
    with outward-rounded integer labels, gaps over
    ``CURVE_SEGMENT_GAP_MINUTES`` split segments, isolated buckets render
    as dots. Returns ``None`` on sparse days (fewer than
    ``HEART_CURVE_MIN_READINGS`` readings) or mixed units — the card
    keeps its text-only range row.
    """
    readings: list[tuple[int, float]] = []
    units: set[str] = set()
    for row in rows:
        if str(row.get("record_type") or "") != HEART_RATE_TYPE:
            continue
        moment = _parse_record_time(row.get("start_date") or row.get("start_time"))
        value = _parse_float(row.get("value"))
        if moment is None or value is None:
            continue
        if row.get("unit") is not None:
            units.add(str(row["unit"]))
        readings.append((moment.hour * 60 + moment.minute, value))
    if len(readings) < HEART_CURVE_MIN_READINGS or len(units) > 1:
        return None
    unit = next(iter(units)) if units else None

    buckets: dict[int, list[float]] = defaultdict(list)
    for minute, value in readings:
        buckets[minute // HEART_BUCKET_MINUTES].append(value)
    # (x at bucket center, median, min, max) in chronological order.
    stats = [
        (
            index * HEART_BUCKET_MINUTES + HEART_BUCKET_MINUTES / 2,
            median(buckets[index]),
            min(buckets[index]),
            max(buckets[index]),
        )
        for index in sorted(buckets)
    ]

    values = [value for _, value in readings]
    v_min, v_max = min(values), max(values)
    # Same y-axis convention as glucose: padded domain, outward-rounded
    # to whole numbers the axis labels can state.
    pad = max((v_max - v_min) * 0.08, 2.0)
    lo = float(math.floor(v_min - pad))
    hi = float(math.ceil(v_max + pad))

    def _y(value: float) -> float:
        return round(CURVE_SVG_HEIGHT - (value - lo) / (hi - lo) * CURVE_SVG_HEIGHT, 1)

    segments: list[list[tuple[float, float, float, float]]] = []
    current: list[tuple[float, float, float, float]] = []
    prev_x: float | None = None
    for stat in stats:
        if prev_x is not None and stat[0] - prev_x > CURVE_SEGMENT_GAP_MINUTES:
            segments.append(current)
            current = []
        current.append(stat)
        prev_x = stat[0]
    if current:
        segments.append(current)

    paths: list[str] = []
    band_paths: list[str] = []
    dots: list[list[float]] = []
    for segment in segments:
        if len(segment) == 1:
            dots.append([segment[0][0], _y(segment[0][1])])
            continue
        paths.append("M" + " L".join(f"{x:g} {_y(mid):g}" for x, mid, _, _ in segment))
        # Closed band polygon: along the maxima, back along the minima.
        upper = [f"{x:g} {_y(high):g}" for x, _, _, high in segment]
        lower = [f"{x:g} {_y(low):g}" for x, _, low, _ in reversed(segment)]
        band_paths.append("M" + " L".join(upper + lower) + " Z")

    return {
        "unit": unit,
        "unit_label": friendly_unit_label(HEART_RATE_TYPE, unit),
        "count": len(readings),
        "count_label": f"{len(readings):,}",
        "min": v_min,
        "max": v_max,
        "bucket_minutes": HEART_BUCKET_MINUTES,
        "points": [[x, mid] for x, mid, _, _ in stats],
        "bands": [[x, low, high] for x, _, low, high in stats],
        "svg": {
            "width": CURVE_SVG_WIDTH,
            "height": CURVE_SVG_HEIGHT,
            "paths": paths,
            "band_paths": band_paths,
            "dots": dots,
            "y_min_label": _format_number(lo),
            "y_max_label": _format_number(hi),
        },
    }


def _heart_analysis(rows: list[dict[str, Any]]) -> dict[str, Any] | None:
    """The heart card: an HR range and curve, paired blood pressure, facts."""

    if not rows:
        return None
    heart_rate: dict[str, Any] | None = _window_heart_rate(rows)
    if heart_rate and heart_rate["count"]:
        reading_word = "reading" if heart_rate["count"] == 1 else "readings"
        heart_rate = {
            **heart_rate,
            "summary": (
                f"{heart_rate['label']} · {heart_rate['count_label']} {reading_word}"
            ),
        }
    else:
        heart_rate = None
    series = _heart_rate_series(rows) if heart_rate else None
    blood_pressure = _blood_pressure(rows)
    rhythm = _rhythm_summary(rows)

    fact_rows: list[dict[str, Any]] = []
    for row in rows:
        record_type = str(row.get("record_type") or "")
        if heart_rate and record_type == HEART_RATE_TYPE:
            continue
        if blood_pressure and (
            _BP_SYSTOLIC_FRAGMENT in record_type
            or _BP_DIASTOLIC_FRAGMENT in record_type
        ):
            continue
        if _is_rhythm_event_type(record_type) or _AFIB_BURDEN_FRAGMENT in record_type:
            # Rhythm rows render only through their dedicated factual
            # lines, never a second time as generic count facts.
            continue
        fact_rows.append(row)

    facts: list[dict[str, Any]] = []
    grouped = _group_by_type(fact_rows)
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
        values = [
            value
            for value in (_parse_float(row.get("value")) for row in rows_for_type)
            if value is not None
        ]
        unit, consistent = _single_unit(rows_for_type)
        if values and consistent:
            if "RestingHeartRate" in record_type:
                latest = max(
                    rows_for_type, key=lambda r: _time_sort_key(_row_time(r) or "")
                )
                latest_value = _parse_float(latest.get("value"))
                if latest_value is not None:
                    item["value"] = display_value(record_type, latest_value, unit)
            elif len(values) == 1:
                item["value"] = display_value(record_type, values[0], unit)
            else:
                item["value"] = _display_range(
                    record_type, min(values), max(values), unit
                )
        facts.append(item)

    if not (heart_rate or blood_pressure or rhythm or facts):
        return None
    return {
        "heart_rate": heart_rate,
        "series": series,
        "blood_pressure": blood_pressure,
        "rhythm": rhythm,
        "facts": facts,
    }


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
    if (
        sleep
        and sleep.get("asleep_duration")
        and sleep["asleep_duration"] != sleep.get("in_bed_duration")
    ):
        parts.append(
            f"slept {sleep['asleep_duration']} (in bed {sleep['in_bed_duration']})"
        )
    elif sleep and sleep.get("window"):
        parts.append(f"slept {sleep['window']}")
    for series in glucose_series:
        parts.append(f"glucose {series['range_label']}")
    if activity and activity["workouts"]:
        count = len(activity["workouts"])
        parts.append(f"{count} workout" + ("s" if count != 1 else ""))
    if not parts:
        if not day_rows:
            return "No body data present for this day."
        family_counts = Counter(
            _family_for_type(str(row.get("record_type") or "")) for row in day_rows
        )
        names = [
            name
            for name, _ in sorted(family_counts.items(), key=lambda kv: (-kv[1], kv[0]))
        ]
        entries = f"{len(day_rows):,} " + ("entry" if len(day_rows) == 1 else "entries")
        if len(names) == 1:
            parts.append(f"{entries} across {names[0]}")
        elif len(names) == 2:
            parts.append(f"{entries} across {names[0]} and {names[1]}")
        else:
            more = len(names) - 2
            area_word = "area" if more == 1 else "areas"
            parts.append(
                f"{entries} across {names[0]}, {names[1]}, and {more} more {area_word}"
            )
    text = ", ".join(parts)
    return text[0].upper() + text[1:] + "."


def _day_prompts(
    date_label: str,
    *,
    has_sleep: bool,
    has_glucose: bool,
    has_workouts: bool,
    has_journal_day: bool,
) -> list[str]:
    # Prompts that point at the journal only appear when the journal has
    # a chronicle day to point at.
    prompts = [f"How did my body on {date_label} compare with nearby days?"]
    if has_glucose:
        prompts.append(
            f"What was on my calendar during the glucose peak on {date_label}?"
        )
    if has_workouts and has_journal_day:
        prompts.append(
            f"What happened in my journal after the workouts on {date_label}?"
        )
    if has_sleep:
        prompts.append(
            f"What did my evening look like before the sleep ending {date_label}?"
        )
    fillers = [f"Who did I spend {date_label} with?"]
    if has_journal_day:
        fillers.insert(0, f"What does my journal hold for {date_label}?")
    for filler in fillers:
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
    next_date = target + timedelta(days=1)
    next_month = f"{next_date.year}-{next_date.month:02d}"
    if next_month != month:
        # The night that starts this evening continues into the next day —
        # near a month boundary those entries live in the next month's shards.
        months.append(next_month)
    read = reader or _month_reader(journal_root)
    rows = [row for shard_month in months for row in read(shard_month)]
    day_rows = [row for row in rows if row.get("day") == day]
    prev_day = (target - timedelta(days=1)).strftime("%Y%m%d")
    prev_rows = [row for row in rows if row.get("day") == prev_day]
    next_day = next_date.strftime("%Y%m%d")
    next_rows = [row for row in rows if row.get("day") == next_day]

    families: dict[str, list[dict[str, Any]]] = defaultdict(list)
    for row in day_rows:
        families[_family_for_type(str(row.get("record_type") or ""))].append(row)

    sleep = _sleep_analysis(day_rows, prev_rows, next_rows, day)
    glucose_series = _glucose_series(day_rows)
    activity = _activity_analysis(day_rows)
    heart = _heart_analysis(families.get("Heart", []))
    mind_sound_facts = _fact_items(
        families.get("Mindfulness", []) + families.get("Hearing & audio", [])
    )
    walking_facts = _walking_metrics(families.get("Walking metrics", []))
    body_facts = _body_measurement_facts(families.get("Body measurements", []))
    # Leftover signals: the explicit "Other" family plus sleep-family rows
    # that are not sleep-analysis intervals (wrist temperature and kin).
    leftover_rows = families.get("Other", []) + [
        row
        for row in families.get("Sleep", [])
        if not _is_sleep_type(str(row.get("record_type") or ""))
    ]
    other_facts = _fact_items(leftover_rows)

    source_counts: Counter[str] = Counter(_source_label(row) for row in day_rows)
    source_names = sorted(source_counts)
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
            "chips": [
                {
                    "name": name,
                    "count": source_counts[name],
                    "count_label": f"{source_counts[name]:,}",
                    "entries_label": (
                        f"{source_counts[name]:,} "
                        + ("entry" if source_counts[name] == 1 else "entries")
                    ),
                }
                for name in source_names
            ],
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
        "heart": heart,
        "mind_sound": {"facts": mind_sound_facts} if mind_sound_facts else None,
        "walking": {"facts": walking_facts} if walking_facts else None,
        "body_measurements": {"facts": body_facts} if body_facts else None,
        "other_signals": {"facts": other_facts} if other_facts else None,
        "sources": sources,
        "prompts": (
            _day_prompts(
                date_label,
                has_sleep=sleep is not None,
                has_glucose=bool(glucose_series),
                has_workouts=bool(activity and activity["workouts"]),
                has_journal_day=(journal_root / "chronicle" / day).is_dir(),
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
            # Every bundle that contained one of the day's entries, not
            # just the bundle whose copy survived dedupe.
            "import_ids": sorted(
                {
                    import_id
                    for row in day_rows
                    for import_id in (
                        row.get("import_ids")
                        or ([str(row["import_id"])] if row.get("import_id") else [])
                    )
                }
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
        row for row in rows if str(row.get("record_type") or "") == HEART_RATE_TYPE
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
    low = min(values)
    high = max(values)
    range_label = (
        _format_number(low)
        if low == high
        else f"{_format_number(low)}–{_format_number(high)}"
    )
    display_unit = friendly_unit_label(HEART_RATE_TYPE, unit)
    return {
        "count": len(values),
        "count_label": f"{len(values):,}",
        "min": low,
        "max": high,
        "unit": unit,
        "label": (
            f"{range_label} {display_unit}".strip() if display_unit else range_label
        ),
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
            "range_label": None,
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
    low = min(values)
    high = max(values)
    value_range = (
        _format_number(low)
        if low == high
        else f"{_format_number(low)}–{_format_number(high)}"
    )
    if unit and unit != "mixed":
        value_range += f" {unit}"
    return {
        "count": len(readings),
        "count_label": f"{len(readings):,}",
        "readings": readings,
        "unit": unit,
        "delta_label": delta,
        "range_label": value_range,
        "first": first,
        "last": last,
        "min": low,
        "max": high,
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
        metrics = _workout_metrics(row)
        items.append(
            {
                "name": friendly_type_name(str(row.get("record_type") or "Workout")),
                "start": _iso(start),
                "end": _iso(end),
                "start_label": _time_label(start),
                "end_label": _time_label(end),
                "overlap_minutes": round(minutes, 1),
                "overlap_label": _format_duration(minutes),
                "duration_label": _workout_duration_label(duration),
                "source": _source_label(row),
                **metrics,
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
    seen_sessions: set[tuple[str, str, str]] = set()
    final_day = (window_end - timedelta(microseconds=1)).date()
    current_day = window_start.date()
    while current_day <= final_day:
        sleep = pick_day_sleep(intervals_by_source, current_day)
        current_day += timedelta(days=1)
        if sleep is None:
            continue
        sessions = ([sleep.main] if sleep.main is not None else []) + list(sleep.naps)
        for start, end in sessions:
            minutes = _overlap_minutes(start, end, window_start, window_end)
            if minutes <= 0:
                continue
            key = (sleep.source, _iso(start), _iso(end))
            if key in seen_sessions:
                continue
            seen_sessions.add(key)
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
                    "source": sleep.source,
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
            "metric_labels": item["metric_labels"],
            "metrics_label": item["metrics_label"],
            "distance": item["distance"],
            "energy": item["energy"],
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


def _hour_range_label(start: datetime, end: datetime) -> str:
    return f"{_format_clock(start)} – {_format_clock(end)}"


def _hourly_summary(
    heart_rate: dict[str, Any],
    glucose: dict[str, Any],
    steps: dict[str, Any],
    events: list[dict[str, Any]],
) -> list[str]:
    rows: list[str] = []
    event_labels = []
    for event in events:
        label = str(event.get("label") or "").strip()
        if label and label not in event_labels:
            event_labels.append(label)
    rows.extend(event_labels[:2])
    if glucose.get("count"):
        rows.append(f"Glucose {glucose.get('range_label') or glucose['delta_label']}")
    if heart_rate.get("count"):
        rows.append(f"HR {heart_rate['label']}")
    if steps.get("label"):
        rows.append(str(steps["label"]))
    return rows


def _event_slice_for_bucket(
    events: list[dict[str, Any]], bucket_start: datetime, bucket_end: datetime
) -> list[dict[str, Any]]:
    slices: list[dict[str, Any]] = []
    for event in events:
        start = _parse_record_time(event.get("start"))
        end = _parse_record_time(event.get("end"))
        if start is None or end is None:
            continue
        minutes = _overlap_minutes(start, end, bucket_start, bucket_end)
        if minutes <= 0:
            continue
        sliced = dict(event)
        sliced["overlap_minutes"] = round(minutes, 1)
        sliced["overlap_label"] = _format_duration(minutes)
        slices.append(sliced)
    return slices


def _window_hourly_items(
    rows: list[dict[str, Any]],
    window_start: datetime,
    window_end: datetime,
    events: list[dict[str, Any]],
) -> list[dict[str, Any]]:
    """Hour-bounded context that preserves a day's shape for transcripts."""

    current = window_start.replace(minute=0, second=0, microsecond=0)
    hourly: list[dict[str, Any]] = []
    while current < window_end:
        next_hour = current + timedelta(hours=1)
        bucket_start = max(current, window_start)
        bucket_end = min(next_hour, window_end)
        if bucket_end <= bucket_start:
            current = next_hour
            continue

        bucket_rows: list[dict[str, Any]] = []
        for row in rows:
            interval = _row_interval(row)
            if interval is None:
                continue
            if _interval_overlaps(interval[0], interval[1], bucket_start, bucket_end):
                bucket_rows.append(row)

        heart_rate = _window_heart_rate(bucket_rows)
        glucose = _window_glucose(bucket_rows)
        steps = _window_steps(bucket_rows)
        bucket_events = _event_slice_for_bucket(events, bucket_start, bucket_end)
        summary = _hourly_summary(heart_rate, glucose, steps, bucket_events)

        hourly.append(
            {
                "start": _iso(bucket_start),
                "end": _iso(bucket_end),
                "label": _format_clock(bucket_start),
                "range_label": _hour_range_label(bucket_start, bucket_end),
                "has_data": bool(bucket_rows or bucket_events),
                "entry_total": len(bucket_rows),
                "entry_total_label": f"{len(bucket_rows):,}",
                "families": _window_family_items(bucket_rows),
                "events": bucket_events,
                "heart_rate": heart_rate,
                "glucose": glucose,
                "steps": steps,
                "summary": summary,
                "summary_label": " · ".join(summary),
            }
        )
        current = next_hour
    return hourly


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
    events = _window_events(rows, window_start, window_end)
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
        "events": events,
        "hourly": _window_hourly_items(rows, window_start, window_end, events),
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


@body_bp.get("/api/recent")
def api_recent():
    """The next carousel batch: day cards strictly older than ``before``.

    Alongside the card payloads and the older-days-remain flag, the
    response carries the batch rendered through the same ``body_day_card``
    Jinja macro the overview's initial render uses — one card markup,
    never a client-side copy that could drift.
    """
    before = request.args.get("before", "")
    if not DAY_RE.fullmatch(before):
        return error_response(INVALID_DAY)
    try:
        datetime.strptime(before, "%Y%m%d")
    except ValueError:
        return error_response(INVALID_DAY)
    limit = RECENT_DAY_LIMIT
    raw_limit = request.args.get("limit")
    if raw_limit is not None:
        try:
            limit = int(raw_limit)
        except ValueError:
            return error_response(
                INVALID_REQUEST_VALUE, detail="limit must be an integer"
            )
        if limit < 1:
            return error_response(
                INVALID_REQUEST_VALUE, detail="limit must be at least 1"
            )
        limit = min(limit, RECENT_BATCH_LIMIT_CAP)
    journal_root = _journal_root()
    by_day = _read_health_dedupe_stats(journal_root)["by_day"]
    days, has_more = _recent_day_rail(journal_root, by_day, before=before, limit=limit)
    render_card = get_template_attribute("body/workspace.html", "body_day_card")
    return jsonify(
        {
            "days": days,
            "has_more": has_more,
            "html": "".join(render_card(item) for item in days),
        }
    )


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
