# SPDX-License-Identifier: AGPL-3.0-only
# Copyright (c) 2026 sol pbc

import json
import logging
import os
import re
import socket
import sqlite3
from collections import Counter
from contextlib import closing
from datetime import datetime, timezone
from pathlib import Path
from statistics import mean
from typing import Any

from flask import Blueprint, jsonify, render_template, request

from solstone.apps.health import copy as health_copy
from solstone.convey import backlog_copy, state
from solstone.convey.backlog_view import stuck_rows, verdict
from solstone.convey.readiness_snapshot import (
    build_readiness_snapshot,
    unavailable_snapshot,
)
from solstone.convey.reasons import (
    FILE_NOT_FOUND,
    FILE_READ_FAILED,
    INVALID_DAY,
    INVALID_PATH,
    INVALID_REQUEST_VALUE,
    MISSING_REQUIRED_FIELD,
    OBSERVER_RESTART_FAILED,
    REPROCESS_ALREADY_COMPLETE,
    REPROCESS_PAST_ONLY,
    REPROCESS_UNREACHABLE,
)
from solstone.convey.utils import error_response
from solstone.think.callosum import callosum_send
from solstone.think.reprocess import (
    FLAVOR_FROM_SCRATCH,
    FLAVOR_PROCESS_NOW,
    ReprocessCode,
    reprocess_day,
)
from solstone.think.streams import stream_name
from solstone.think.talent_runs import AgentFailureScan, read_unresolved_agent_failures

logger = logging.getLogger(__name__)

health_bp = Blueprint("app:health", __name__, url_prefix="/app/health")

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


def _month_for_row(row: dict[str, Any]) -> str:
    month = row.get("month")
    if isinstance(month, str) and MONTH_RE.fullmatch(month):
        return month
    start = str(row.get("start_date") or row.get("start_time") or "")
    if len(start) >= 7 and MONTH_RE.fullmatch(start[:7]):
        return start[:7]
    if len(start) >= 6 and start[:6].isdigit():
        return f"{start[:4]}-{start[4:6]}"
    return "undated"


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
    """Per-source counts and latest record time, bounded to the newest month shard."""
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


@health_bp.app_context_processor
def _inject_health_copy() -> dict:
    return {"health_copy": health_copy}


@health_bp.app_context_processor
def _inject_backlog_copy() -> dict:
    return {"backlog_copy": backlog_copy}


# Supervisor currently registers one observer-facing processing service: "sense".
# Observer rows are per registration key, but reconnect restarts this shared worker.
# Keep this endpoint whitelist local until supervisor exposes a public service list.
OBSERVER_RESTART_SERVICES = {"sense"}


def _load_backlog() -> dict | None:
    stats_path = os.path.join(state.journal_root, "stats.json")
    if not os.path.isfile(stats_path):
        return None
    try:
        with open(stats_path, "r", encoding="utf-8") as f:
            data = json.load(f)
    except (OSError, ValueError):
        logger.exception("Failed to read backlog from stats.json")
        return None
    backlog = data.get("backlog")
    return backlog if isinstance(backlog, dict) else None


def _safe_readiness_snapshot() -> dict:
    try:
        return build_readiness_snapshot()
    except Exception:
        logger.exception("Failed to build health readiness snapshot")
        return unavailable_snapshot()


def _build_agent_error_seed(scan: AgentFailureScan) -> list[dict]:
    return [
        {
            "type": "agent",
            "id": failure.use_id,
            "name": failure.name,
            "ts": failure.ts,
            "service": "cortex",
            "error": "talent error",
            "reason_code": failure.reason_code,
            "provider": failure.provider,
            "model": failure.model,
        }
        for failure in scan.failures
    ]


def _errors_today_label(count: int | None) -> str:
    return "error today" if count == 1 else "errors today"


@health_bp.route("/")
def index():
    backlog = _load_backlog()
    agent_failure_scan = read_unresolved_agent_failures()
    agent_error_seed = _build_agent_error_seed(agent_failure_scan)
    agent_error_count = len(agent_error_seed)
    return render_template(
        "app.html",
        health_backlog_verdict=verdict(backlog),
        health_stuck_rows=stuck_rows(backlog),
        health_readiness=_safe_readiness_snapshot(),
        health_agent_errors=agent_error_seed,
        health_agent_errors_ok=agent_failure_scan.ok,
        health_agent_errors_count=agent_error_count,
        health_agent_errors_label=_errors_today_label(
            agent_error_count if agent_failure_scan.ok else None
        ),
    )


@health_bp.route("/imports")
def imports_view():
    return render_template(
        "health/imports.html",
        health_view="status",
        health_status=_build_health_import_status(_journal_root()),
    )


@health_bp.route("/imports/<day>")
def imports_day_view(day: str):
    try:
        health_day = _build_health_day(_journal_root(), day)
    except ValueError:
        return error_response(INVALID_DAY)
    return render_template(
        "health/imports.html",
        health_view="day",
        health_status=None,
        health_day=health_day,
    )


@health_bp.get("/api/status")
def api_status():
    return jsonify(_build_health_import_status(_journal_root()))


@health_bp.get("/api/day/<day>")
def api_day(day: str):
    try:
        return jsonify(_build_health_day(_journal_root(), day))
    except ValueError:
        return error_response(INVALID_DAY)


@health_bp.get("/api/stats/<month>")
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


@health_bp.get("/api/log")
def get_log():
    path = request.args.get("path")
    if not path:
        return error_response(MISSING_REQUIRED_FIELD, detail="Missing path parameter")

    if not re.fullmatch(r"\d{8}/health/[^/]+\.log", path):
        return error_response(INVALID_PATH, detail="Invalid path")

    journal_root = Path(state.journal_root).resolve()
    try:
        file_path = (Path(state.journal_root) / path).resolve()
    except ValueError:
        return error_response(INVALID_PATH, detail="Invalid path")
    try:
        file_path.relative_to(journal_root)
    except ValueError:
        return error_response(INVALID_PATH, detail="Invalid path")

    if not file_path.exists():
        return error_response(FILE_NOT_FOUND, detail="Log file not found")

    try:
        content = file_path.read_text(encoding="utf-8")
    except IOError:
        return error_response(FILE_READ_FAILED, detail="Failed to read log file")

    return jsonify(content=content, path=path)


@health_bp.route("/api/info")
def api_info():
    return jsonify(
        {
            "hostname": stream_name(host=socket.gethostname()),
            "readiness": _safe_readiness_snapshot(),
        }
    )


@health_bp.post("/api/retry-import")
def retry_import():
    data = request.get_json(silent=True) or {}
    if not data.get("import_id"):
        return error_response(MISSING_REQUIRED_FIELD, detail="Missing import_id")
    stage = data.get("stage")
    message = "Import retry will be available in a future update"
    if stage:
        message = (
            f"Import retry from stage {stage} will be available in a future update"
        )
    return jsonify(
        status="not_implemented",
        message=message,
    ), 501


@health_bp.post("/api/restart-observer")
def restart_observer():
    data = request.get_json(silent=True) or {}
    service = data.get("service")
    if not service:
        return error_response(MISSING_REQUIRED_FIELD, detail="Missing service")
    if service not in OBSERVER_RESTART_SERVICES:
        return error_response(INVALID_REQUEST_VALUE, detail="Unknown observer service")

    ok = callosum_send("supervisor", "restart", service=service)
    if not ok:
        return error_response(
            OBSERVER_RESTART_FAILED,
            detail="Could not reach the supervisor",
        )

    return jsonify(status="restart_requested", service=service)


@health_bp.post("/api/reprocess")
def reprocess():
    data = request.get_json(silent=True) or {}
    day = data.get("day")
    flavor = data.get("flavor")
    if not day:
        return error_response(MISSING_REQUIRED_FIELD, detail="Missing day")
    if flavor not in (FLAVOR_PROCESS_NOW, FLAVOR_FROM_SCRATCH):
        return error_response(INVALID_REQUEST_VALUE, detail="Unknown reprocess flavor")

    outcome = reprocess_day(day, flavor)
    code = outcome.code
    if code in (
        ReprocessCode.PROCESS_NOW_SUBMITTED,
        ReprocessCode.FROM_SCRATCH_SUBMITTED,
    ):
        return jsonify(status="queued", day=day)
    if code is ReprocessCode.ALREADY_COMPLETE:
        return jsonify(
            status="already_complete",
            day=day,
            message=REPROCESS_ALREADY_COMPLETE.message,
            reason_code=REPROCESS_ALREADY_COMPLETE.code,
        )
    if code is ReprocessCode.PAST_ONLY:
        return error_response(REPROCESS_PAST_ONLY)
    if code is ReprocessCode.UNREACHABLE:
        return error_response(REPROCESS_UNREACHABLE)
    return error_response(INVALID_DAY)
