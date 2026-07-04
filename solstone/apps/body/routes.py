# SPDX-License-Identifier: AGPL-3.0-only
# Copyright (c) 2026 sol pbc

"""Read-only views over health data imported into the journal.

Reads import manifests, month-bounded normalized shards, and the
importer-owned dedupe database. Read paths create nothing on disk.
"""

import json
import logging
import re
import sqlite3
from collections import Counter
from contextlib import closing
from datetime import datetime, timezone
from pathlib import Path
from statistics import mean
from typing import Any

from flask import Blueprint, jsonify, render_template

from solstone.convey import state
from solstone.convey.reasons import INVALID_DAY, INVALID_REQUEST_VALUE
from solstone.convey.utils import error_response

logger = logging.getLogger(__name__)

body_bp = Blueprint("app:body", __name__, url_prefix="/app/body")

DAY_RE = re.compile(r"^\d{8}$")
MONTH_RE = re.compile(r"^\d{4}-\d{2}$")
GLUCOSE_RECORD_TYPE = "HKQuantityTypeIdentifierBloodGlucose"
DAY_SUMMARY_STREAM = "import.apple_health"
APPLE_HEALTH_SOURCE_TYPE = "apple_health"
DAY_SUMMARY_FILE = "day_summary_transcript.md"


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
    for path in sorted(imports_root.glob(pattern)):
        rows.extend(_read_shard_rows(path))
    return rows


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
    for path in sorted(path for path in shards if path.stem == latest_month):
        for row in _read_shard_rows(path):
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


def _read_health_dedupe_stats(journal_root: Path) -> dict[str, Any]:
    db_path = journal_root / "imports" / "health-dedupe.sqlite"
    if not db_path.exists():
        return {
            "total": 0,
            "by_type": {},
            "by_source": {},
            "by_month": {},
            "by_day": {},
            "coverage_window": {"start": None, "end": None},
        }

    uri = f"file:{db_path}?mode=ro"
    with closing(sqlite3.connect(uri, uri=True)) as conn:
        conn.row_factory = sqlite3.Row
        total = conn.execute("SELECT COUNT(*) FROM health_dedupe").fetchone()[0]
        by_type = {
            row["record_type"]: row["n"]
            for row in conn.execute(
                """
                SELECT record_type, COUNT(*) AS n
                FROM health_dedupe
                GROUP BY record_type
                ORDER BY record_type
                """
            )
        }
        by_source = {
            row["source_family"]: row["n"]
            for row in conn.execute(
                """
                SELECT source_family, COUNT(*) AS n
                FROM health_dedupe
                GROUP BY source_family
                ORDER BY source_family
                """
            )
        }
        by_month = {
            row["m"]: row["n"]
            for row in conn.execute(
                """
                SELECT substr(start_time, 1, 7) AS m, COUNT(*) AS n
                FROM health_dedupe
                GROUP BY m
                ORDER BY m
                """
            )
        }
        by_day = {
            row["d"]: row["n"]
            for row in conn.execute(
                """
                SELECT replace(substr(start_time, 1, 10), '-', '') AS d, COUNT(*) AS n
                FROM health_dedupe
                GROUP BY d
                ORDER BY d
                """
            )
        }
        window = conn.execute(
            "SELECT MIN(start_time) AS s, MAX(start_time) AS e FROM health_dedupe"
        ).fetchone()

    return {
        "total": total,
        "by_type": by_type,
        "by_source": by_source,
        "by_month": by_month,
        "by_day": by_day,
        "coverage_window": {"start": window["s"], "end": window["e"]},
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
    }


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


def _parse_float(value: object) -> float | None:
    try:
        return float(str(value))
    except (TypeError, ValueError):
        return None


def _glucose_stats(rows: list[dict[str, Any]]) -> dict[str, Any]:
    glucose_rows = [
        row for row in rows if str(row.get("record_type") or "") == GLUCOSE_RECORD_TYPE
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


def _build_health_day(journal_root: Path, day: str) -> dict[str, Any]:
    if not DAY_RE.fullmatch(day):
        raise ValueError("Day must be YYYYMMDD")
    month = f"{day[:4]}-{day[4:6]}"
    rows = [
        row
        for row in _iter_normalized_rows(journal_root, month=month)
        if row.get("day") == day
    ]
    return {
        "day": day,
        "summary_markdown": _find_day_summary(journal_root, day),
        "glucose": _glucose_stats(rows),
        "entry_total": len(rows),
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
