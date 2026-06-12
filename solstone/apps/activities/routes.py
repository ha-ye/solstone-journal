# SPDX-License-Identifier: AGPL-3.0-only
# Copyright (c) 2026 sol pbc

from __future__ import annotations

import calendar
from datetime import date
from pathlib import Path
from typing import Any

from flask import Blueprint, jsonify, redirect, render_template, request, url_for

from solstone.convey import state
from solstone.convey.reasons import (
    ACTIVITIES_BUSY,
    ACTIVITY_ALREADY_EXISTS,
    ACTIVITY_INVALID,
    ACTIVITY_NOT_FOUND,
    FILE_NOT_FOUND,
    FILE_READ_FAILED,
    INVALID_DAY,
    INVALID_MONTH,
    INVALID_PATH,
)
from solstone.convey.utils import (
    DATE_RE,
    error_response,
    format_date,
    respond_collection,
)
from solstone.think.activities import (
    append_activity_record,
    append_edit,
    estimate_duration_minutes,
    format_activities,
    get_activity_by_id,
    get_activity_record,
    get_default_activity_by_id,
    load_activity_records,
    make_activity_id,
    mute_activity_record,
    unmute_activity_record,
    update_activity_record,
)
from solstone.think.entities.loading import load_entities
from solstone.think.entities.matching import find_matching_entity
from solstone.think.facets import get_facets, log_call_action
from solstone.think.journal_io import LockTimeout
from solstone.think.utils import now_ms, segment_parse

activities_bp = Blueprint(
    "app:activities",
    __name__,
    url_prefix="/app/activities",
)

_GENERIC_ACTIVITY_ICON = "\U0001f5d3"


@activities_bp.route("/")
def index():
    """Redirect to today's activities view."""
    today = date.today().strftime("%Y%m%d")
    return redirect(url_for("app:activities.activities_day", day=today))


@activities_bp.route("/<day>")
def activities_day(day: str) -> str:
    """Render the day view for a specific day."""
    if not DATE_RE.fullmatch(day):
        return "", 404

    title = format_date(day)

    return render_template(
        "app.html",
        view="day",
        title=title,
    )


def _month_activity_counts(month: str) -> dict[str, dict[str, int]]:
    year = int(month[:4])
    month_num = int(month[4:6])
    _, days_in_month = calendar.monthrange(year, month_num)
    facet_names = list(get_facets().keys())
    stats: dict[str, dict[str, int]] = {}

    for day_num in range(1, days_in_month + 1):
        day = f"{month}{day_num:02d}"
        day_counts: dict[str, int] = {}
        for facet in facet_names:
            count = len(load_activity_records(facet, day))
            if count:
                day_counts[facet] = count
        if day_counts:
            stats[day] = day_counts

    return stats


@activities_bp.route("/api/stats/<month>")
def activities_stats(month: str) -> Any:
    """Return activity counts per facet for a specific month."""
    if len(month) != 6 or not month.isdigit():
        return error_response(
            INVALID_MONTH,
            detail="Invalid month format, expected YYYYMM",
        )

    try:
        return jsonify(_month_activity_counts(month))
    except ValueError:
        return error_response(
            INVALID_MONTH,
            detail="Invalid month format, expected YYYYMM",
        )


def _record_payload(record: dict[str, Any]) -> dict[str, Any]:
    chunks, _meta = format_activities([record])
    return {"record": record, "markdown": chunks[0]["markdown"]}


def _resolve_participation_entity_ids(
    entries: list[dict[str, Any]], *, facet: str, day: str
) -> list[dict[str, Any]]:
    entities_list = load_entities(facet=facet, day=day)

    resolved_entries = []
    for entry in entries:
        resolved = dict(entry)
        match = find_matching_entity(resolved["name"], entities_list)
        resolved["entity_id"] = match.get("id") if match else None
        resolved_entries.append(resolved)

    return resolved_entries


@activities_bp.route("/api/day/<day>/records")
def activities_day_records(day: str) -> Any:
    """Return CLI-facing activity records plus per-record markdown."""
    facet_filter = request.args.get("facet")
    include_hidden = request.args.get("include_hidden") == "1"
    facet_names = [facet_filter] if facet_filter else list(get_facets().keys())

    items = []
    for facet in facet_names:
        for record in load_activity_records(facet, day, include_hidden=include_hidden):
            rec = {**record, "facet": facet, "day": day}
            items.append(_record_payload(rec))
    return jsonify({"items": items})


@activities_bp.route("/api/day/<day>/record/<span_id>")
def activities_get_record(day: str, span_id: str) -> Any:
    """Return one CLI-facing activity record plus markdown."""
    facet = request.args.get("facet") or ""
    record = get_activity_record(facet, day, span_id)
    if record is None:
        return error_response(ACTIVITY_NOT_FOUND, detail=span_id)
    return jsonify(_record_payload(record))


@activities_bp.route("/api/day/<day>/records", methods=["POST"])
def activities_create_record(day: str) -> Any:
    """Create one CLI-facing activity record."""
    facet = request.args.get("facet") or ""
    body = request.get_json(silent=True) or {}
    if not isinstance(body, dict):
        body = {}

    title = str(body.get("title") or "").strip()
    source = str(body.get("source") or "user")
    if not title:
        return error_response(ACTIVITY_INVALID, detail="title must not be empty")
    if source not in {"user", "cogitate"}:
        return error_response(
            ACTIVITY_INVALID, detail="source must be 'user' or 'cogitate'"
        )

    activity_type = str(body.get("activity") or "").strip()
    if not get_activity_by_id(facet, activity_type):
        return error_response(ACTIVITY_NOT_FOUND, detail=activity_type)

    if "since_segment" in body and body["since_segment"] is not None:
        anchor = str(body["since_segment"])
        segments = [anchor]
    else:
        anchor = f"user_{now_ms()}"
        segments = []

    description = str(body.get("description") or title).strip() or title
    details = str(body.get("details") or "")
    participation_provided = "participation" in body
    participation: list[dict[str, Any]] = []
    if participation_provided:
        raw_participation = body.get("participation")
        participation = raw_participation if isinstance(raw_participation, list) else []
        participation = _resolve_participation_entity_ids(
            participation, facet=facet, day=day
        )

    actor = "cogitate:activities" if source == "cogitate" else "cli:create"
    span_id = make_activity_id(activity_type, anchor)
    record: dict[str, Any] = {
        "id": span_id,
        "activity": activity_type,
        "title": title,
        "description": description,
        "details": details,
        "segments": segments,
        "active_entities": [],
        "created_at": now_ms(),
        "source": source,
        "hidden": False,
        "edits": [],
    }
    if participation_provided:
        record["participation"] = participation

    edit_fields = ["activity", "title", "description", "details", "source"]
    if participation_provided:
        edit_fields.append("participation")

    record = append_edit(
        record,
        actor=actor,
        fields=edit_fields,
        note="created",
    )

    try:
        created = append_activity_record(facet, day, record)
    except LockTimeout:
        return error_response(ACTIVITIES_BUSY)

    if not created:
        return error_response(ACTIVITY_ALREADY_EXISTS, detail=span_id)

    log_call_action(
        facet=facet,
        action="activity_create",
        params={"id": span_id, "activity": activity_type, "source": source},
        day=day,
    )
    return jsonify(_record_payload(record))


@activities_bp.route("/api/day/<day>/record/<span_id>/update", methods=["POST"])
def activities_update_record(day: str, span_id: str) -> Any:
    """Update one CLI-facing activity record."""
    facet = request.args.get("facet") or ""
    body = request.get_json(silent=True) or {}
    if not isinstance(body, dict):
        body = {}
    patch = body.get("patch")
    note = body.get("note")
    patch = patch if isinstance(patch, dict) else {}
    note = str(note or "")

    try:
        updated = update_activity_record(
            facet,
            day,
            span_id,
            patch,
            actor="cli:update",
            note=note,
        )
    except LockTimeout:
        return error_response(ACTIVITIES_BUSY)
    if updated is None:
        return error_response(ACTIVITY_NOT_FOUND, detail=span_id)

    log_call_action(
        facet=facet,
        action="activity_update",
        params={"id": span_id, "fields": sorted(patch)},
        day=day,
    )
    return jsonify(_record_payload(updated))


def _set_record_muted(day: str, span_id: str, *, hidden: bool) -> Any:
    facet = request.args.get("facet") or ""
    body = request.get_json(silent=True) or {}
    if not isinstance(body, dict):
        body = {}
    raw_reason = body.get("reason")
    reason = raw_reason if isinstance(raw_reason, str) else None
    actor = "cli:mute" if hidden else "cli:unmute"
    action = "activity_mute" if hidden else "activity_unmute"
    mutator = mute_activity_record if hidden else unmute_activity_record

    try:
        record = mutator(facet, day, span_id, actor=actor, reason=reason)
    except LockTimeout:
        return error_response(ACTIVITIES_BUSY)
    if record is None:
        return error_response(ACTIVITY_NOT_FOUND, detail=span_id)

    log_call_action(
        facet=facet,
        action=action,
        params={"id": span_id, "reason": reason},
        day=day,
    )
    return jsonify(_record_payload(record))


@activities_bp.route("/api/day/<day>/record/<span_id>/mute", methods=["POST"])
def activities_mute_record(day: str, span_id: str) -> Any:
    """Mute one CLI-facing activity record."""
    return _set_record_muted(day, span_id, hidden=True)


@activities_bp.route("/api/day/<day>/record/<span_id>/unmute", methods=["POST"])
def activities_unmute_record(day: str, span_id: str) -> Any:
    """Unmute one CLI-facing activity record."""
    return _set_record_muted(day, span_id, hidden=False)


def _enrich_activity_record(
    record: dict[str, Any],
    facet: str,
    day: str,
) -> dict[str, Any] | None:
    activity_type = record.get("activity", "")
    activity_def = get_activity_by_id(facet, activity_type)
    if activity_def is None:
        activity_def = get_default_activity_by_id(activity_type)

    name = activity_def.get("name", activity_type) if activity_def else activity_type
    icon = activity_def.get("icon", "") if activity_def else ""
    if not icon:
        icon = _GENERIC_ACTIVITY_ICON

    segments = record.get("segments", [])
    start_time = end_time = None
    duration_minutes: int | None = None

    if record.get("source") == "anticipated":
        start = record.get("start")
        end = record.get("end")
        if start:
            start_time = f"{day[:4]}-{day[4:6]}-{day[6:]}T{start}"
        if end:
            end_time = f"{day[:4]}-{day[4:6]}-{day[6:]}T{end}"
        if start and end:
            start_h, start_m, start_s = (int(part) for part in start.split(":"))
            end_h, end_m, end_s = (int(part) for part in end.split(":"))
            delta_seconds = (
                end_h * 3600
                + end_m * 60
                + end_s
                - (start_h * 3600 + start_m * 60 + start_s)
            )
            if delta_seconds >= 0:
                duration_minutes = delta_seconds // 60
    else:
        if segments:
            first_start, _ = segment_parse(segments[0])
            _, last_end = segment_parse(segments[-1])
            if first_start:
                start_time = (
                    f"{day[:4]}-{day[4:6]}-{day[6:]}T{first_start.strftime('%H:%M:%S')}"
                )
            if last_end:
                end_time = (
                    f"{day[:4]}-{day[4:6]}-{day[6:]}T{last_end.strftime('%H:%M:%S')}"
                )
        computed_minutes = estimate_duration_minutes(segments)
        if computed_minutes >= 0:
            duration_minutes = computed_minutes

    outputs = []
    journal_root = Path(state.journal_root)
    output_dir = journal_root / "facets" / facet / "activities" / day / record["id"]
    if output_dir.is_dir():
        for f in sorted(output_dir.iterdir()):
            if f.is_file():
                rel = f.relative_to(journal_root)
                outputs.append({"filename": f.name, "path": str(rel)})

    enriched = {
        "id": record["id"],
        "activity": activity_type,
        "name": name,
        "icon": icon,
        "facet": facet,
        "description": record.get("description", ""),
        "level_avg": record.get("level_avg", 0.5),
        "segments": segments,
        "active_entities": record.get("active_entities", []),
        "outputs": outputs,
    }
    if start_time:
        enriched["startTime"] = start_time
    if end_time:
        enriched["endTime"] = end_time
    if duration_minutes is not None:
        enriched["duration_minutes"] = duration_minutes

    return enriched


@activities_bp.route("/api/day/<day>/activities")
def activities_day_activities(day: str) -> Any:
    """Return enriched activity records for a specific day.

    Returns enriched activity records: timing comes from ``start``/``end`` for
    anticipated records and from segment keys for realized records.

    Returns JSON collection envelope of activity objects.
    """
    if not DATE_RE.fullmatch(day):
        return error_response(INVALID_DAY, detail="Invalid day format")

    facet_filter = request.args.get("facet")

    if facet_filter:
        facet_names = [facet_filter]
    else:
        facet_names = list(get_facets().keys())

    enriched_records = []
    for facet in facet_names:
        records = load_activity_records(facet, day)
        for record in records:
            enriched = _enrich_activity_record(record, facet, day)
            if enriched is not None:
                enriched_records.append(enriched)

    # Sort by start time (activities without times go last)
    enriched_records.sort(key=lambda a: a.get("startTime", "z"))
    return respond_collection(enriched_records)


@activities_bp.route("/api/activity_output/<path:filename>")
def activities_activity_output(filename: str) -> Any:
    """Serve an activity output file.

    Only serves files under ``facets/`` in the journal directory.
    Returns JSON with content, format, and filename.
    """
    if not filename.startswith("facets/"):
        return error_response(INVALID_PATH, detail="Invalid path")

    journal_root = Path(state.journal_root).resolve()
    file_path = (journal_root / filename).resolve()

    try:
        file_path.relative_to(journal_root)
    except ValueError:
        return error_response(INVALID_PATH, status=403, detail="Invalid path")

    if not file_path.is_file():
        return error_response(FILE_NOT_FOUND, detail="File not found")

    ext = file_path.suffix.lower()
    fmt = "json" if ext == ".json" else "md"

    try:
        content = file_path.read_text(encoding="utf-8")
    except IOError:
        return error_response(FILE_READ_FAILED, detail="Could not read file")

    return jsonify(content=content, format=fmt, filename=file_path.name)
