# SPDX-License-Identifier: AGPL-3.0-only
# Copyright (c) 2026 sol pbc

"""HTTP API for the owner-wide skills system.

API-only app: a thin JSON surface over the owner functions in
``solstone.think.skills``. There is no workspace page, no menu entry, and no
index route; ``GET /app/skills/`` is intentionally a 404. All write paths
route through the skills owner functions; this module only parses requests,
shapes responses, and maps ``LockTimeout`` to an owner-voice busy.
"""

from __future__ import annotations

from collections.abc import Callable
from typing import Any

from flask import Blueprint, Response, jsonify, request

from solstone.convey.reasons import (
    INVALID_JSON_REQUEST,
    MISSING_REQUEST_BODY,
    MISSING_REQUIRED_FIELD,
    SKILL_ALREADY_EXISTS,
    SKILL_NOT_FOUND,
    SKILL_NOT_MATURE,
    SKILLS_BUSY,
    Reason,
)
from solstone.convey.utils import (
    created,
    error_response,
    parse_pagination_params,
    respond_collection,
)
from solstone.think.journal_io import LockTimeout
from solstone.think.skills import (
    find_pattern,
    load_patterns,
    load_profile,
    locked_modify_edit_requests,
    locked_modify_patterns,
    make_request_id,
    observation_key,
    profile_path,
    rename_profile,
    touch_updated,
    utc_now_iso,
)

skills_bp = Blueprint("app:skills", __name__, url_prefix="/app/skills")


def _read_json_body() -> tuple[dict[str, Any] | None, tuple[Response, int] | None]:
    """Parse a JSON-object request body.

    Returns ``(body, None)`` on success, or ``(None, error)`` where ``error`` is
    a ready-to-return owner-voice response: ``MISSING_REQUEST_BODY`` for an empty
    body, ``INVALID_JSON_REQUEST`` for an unparseable or non-object body. Never
    raises on a malformed request.
    """
    if not request.get_data():
        return None, error_response(MISSING_REQUEST_BODY, detail="no request body")
    data = request.get_json(silent=True)
    if not isinstance(data, dict):
        return None, error_response(
            INVALID_JSON_REQUEST, detail="request body must be a JSON object"
        )
    return data, None


class _SkillError(Exception):
    """Maps a locked-transition failure to an owner-voice Reason + detail."""

    def __init__(self, reason: Reason, detail: str) -> None:
        super().__init__(reason.code)
        self.reason = reason
        self.detail = detail


class _SkillNoOp(Exception):
    """Signals an idempotent no-op: skip the write, return the current row unchanged."""


def _parse_status_filter(raw: str | None) -> set[str] | None:
    if raw is None:
        return None
    statuses = {item.strip() for item in raw.split(",") if item.strip()}
    return statuses or None


def _pattern_observation_key(
    pattern: dict[str, Any], observation: dict[str, Any]
) -> str:
    return observation_key(
        str(pattern.get("slug") or ""),
        str(observation.get("day") or ""),
        [str(item) for item in observation.get("activity_ids", [])],
    )


def _recompute_derived_fields(pattern: dict[str, Any]) -> None:
    observations = pattern.get("observations", [])
    facets = sorted(
        {
            str(observation.get("facet") or "")
            for observation in observations
            if observation.get("facet")
        }
    )
    days = sorted(
        str(observation.get("day") or "")
        for observation in observations
        if observation.get("day")
    )
    pattern["facets_touched"] = facets
    if days:
        pattern["first_seen"] = days[0]
        pattern["last_seen"] = days[-1]


def _require_field(
    body: dict[str, Any], field: str
) -> tuple[str | None, tuple[Response, int] | None]:
    value = body.get(field)
    if not isinstance(value, str) or not value:
        return None, error_response(
            MISSING_REQUIRED_FIELD, detail=f"{field} is required"
        )
    return value, None


def _require_activity_ids(
    body: dict[str, Any],
) -> tuple[list[str] | None, tuple[Response, int] | None]:
    raw = body.get("activity_ids")
    if not isinstance(raw, list):
        return None, error_response(
            MISSING_REQUIRED_FIELD, detail="activity_ids is required"
        )
    ids = [str(item).strip() for item in raw if str(item).strip()]
    if not ids:
        return None, error_response(
            MISSING_REQUIRED_FIELD,
            detail="activity_ids requires at least one id",
        )
    return ids, None


def _apply_transition(
    slug: str, mutate_fn: Callable[[dict[str, Any]], None]
) -> tuple[dict[str, Any] | None, bool, tuple[Response, int] | None]:
    """Run a locked single-pattern transition.

    Returns (pattern, changed, error):
      - (pattern, True, None)   mutate happened and was saved
      - (pattern, False, None)  idempotent no-op (no write; current row returned)
      - (None, False, error)    real failure -> ready owner-voice response (404/409/503)
    """
    found: dict[str, Any] | None = None

    def mutate(rows: list[dict[str, Any]]) -> list[dict[str, Any]]:
        nonlocal found
        pattern = find_pattern(slug, rows)
        if pattern is None:
            raise _SkillError(SKILL_NOT_FOUND, f"no skill with slug '{slug}'")
        found = pattern
        mutate_fn(pattern)
        return rows

    try:
        locked_modify_patterns(mutate)
    except _SkillNoOp:
        return found, False, None
    except _SkillError as exc:
        return None, False, error_response(exc.reason, detail=exc.detail)
    except LockTimeout:
        return (
            None,
            False,
            error_response(SKILLS_BUSY, detail="skills are busy; try again"),
        )
    return found, True, None


@skills_bp.get("/api/patterns")
def list_patterns() -> tuple[Response, int]:
    rows = load_patterns()
    status_filter = _parse_status_filter(request.args.get("status"))
    if status_filter is not None:
        rows = [row for row in rows if str(row.get("status") or "") in status_filter]
    limit, offset = parse_pagination_params(default_limit=20, max_limit=100)
    page = rows[offset : offset + limit]
    return respond_collection(page, total=len(rows))


@skills_bp.get("/api/patterns/<slug>")
def get_pattern(slug: str) -> Response | tuple[Response, int]:
    pattern = find_pattern(slug)
    if pattern is None:
        return error_response(SKILL_NOT_FOUND, detail=f"no skill with slug '{slug}'")
    return jsonify({"pattern": pattern, "profile": load_profile(slug)})


@skills_bp.post("/api/patterns")
def seed_pattern() -> tuple[Response, int]:
    body, error = _read_json_body()
    if error is not None:
        return error

    slug, error = _require_field(body, "slug")
    if error is not None:
        return error
    name, error = _require_field(body, "name")
    if error is not None:
        return error
    day, error = _require_field(body, "day")
    if error is not None:
        return error
    facet, error = _require_field(body, "facet")
    if error is not None:
        return error
    activity_ids, error = _require_activity_ids(body)
    if error is not None:
        return error

    notes = body.get("notes", "")
    created_at = utc_now_iso()
    new_pattern = {
        "slug": slug,
        "name": name,
        "status": "emerging",
        "observations": [
            {
                "day": day,
                "facet": facet,
                "activity_ids": activity_ids,
                "notes": notes,
                "recorded_at": created_at,
            }
        ],
        "facets_touched": [facet],
        "first_seen": day,
        "last_seen": day,
        "needs_profile": False,
        "needs_refresh": False,
        "profile_generated_at": None,
        "created_at": created_at,
        "updated_at": created_at,
    }

    def mutate(rows: list[dict[str, Any]]) -> list[dict[str, Any]]:
        if find_pattern(slug, rows) is not None:
            raise _SkillError(
                SKILL_ALREADY_EXISTS, f"a skill named '{slug}' already exists"
            )
        return [*rows, new_pattern]

    try:
        locked_modify_patterns(mutate)
    except _SkillError as exc:
        return error_response(exc.reason, detail=exc.detail)
    except LockTimeout:
        return error_response(SKILLS_BUSY, detail="skills are busy; try again")
    return created(new_pattern)


@skills_bp.post("/api/patterns/<slug>/observations")
def observe_pattern(slug: str) -> Response | tuple[Response, int]:
    body, error = _read_json_body()
    if error is not None:
        return error

    day, error = _require_field(body, "day")
    if error is not None:
        return error
    facet, error = _require_field(body, "facet")
    if error is not None:
        return error
    activity_ids, error = _require_activity_ids(body)
    if error is not None:
        return error

    notes = body.get("notes", "")
    target_key = observation_key(slug, day, activity_ids)

    def mutate(pattern: dict[str, Any]) -> None:
        existing = pattern.get("observations", [])
        if any(
            _pattern_observation_key(pattern, observation) == target_key
            for observation in existing
        ):
            raise _SkillNoOp()
        existing.append(
            {
                "day": day,
                "facet": facet,
                "activity_ids": activity_ids,
                "notes": notes,
                "recorded_at": utc_now_iso(),
            }
        )
        _recompute_derived_fields(pattern)
        if pattern.get("status") == "dormant":
            pattern["status"] = "mature"
        touch_updated(pattern)

    pattern, changed, error = _apply_transition(slug, mutate)
    if error is not None:
        return error
    return created(pattern) if changed else jsonify(pattern)


@skills_bp.post("/api/patterns/<slug>/promote")
def promote_pattern(slug: str) -> Response | tuple[Response, int]:
    _, error = _read_json_body()
    if error is not None:
        return error

    def mutate(pattern: dict[str, Any]) -> None:
        if pattern.get("status") == "mature":
            raise _SkillNoOp()
        if bool(pattern.get("needs_profile")):
            raise _SkillNoOp()
        pattern["needs_profile"] = True
        touch_updated(pattern)

    pattern, _, error = _apply_transition(slug, mutate)
    if error is not None:
        return error
    return jsonify(pattern)


@skills_bp.post("/api/patterns/<slug>/refresh")
def refresh_pattern(slug: str) -> Response | tuple[Response, int]:
    _, error = _read_json_body()
    if error is not None:
        return error

    def mutate(pattern: dict[str, Any]) -> None:
        if pattern.get("status") != "mature":
            raise _SkillError(SKILL_NOT_MATURE, "only mature skills can be refreshed")
        if bool(pattern.get("needs_refresh")):
            raise _SkillNoOp()
        pattern["needs_refresh"] = True
        touch_updated(pattern)

    pattern, _, error = _apply_transition(slug, mutate)
    if error is not None:
        return error
    return jsonify(pattern)


@skills_bp.post("/api/patterns/<slug>/mark-dormant")
def mark_dormant_pattern(slug: str) -> Response | tuple[Response, int]:
    _, error = _read_json_body()
    if error is not None:
        return error

    def mutate(pattern: dict[str, Any]) -> None:
        if pattern.get("status") == "dormant":
            raise _SkillNoOp()
        pattern["status"] = "dormant"
        touch_updated(pattern)

    pattern, _, error = _apply_transition(slug, mutate)
    if error is not None:
        return error
    return jsonify(pattern)


@skills_bp.post("/api/patterns/<slug>/retire")
def retire_pattern(slug: str) -> Response | tuple[Response, int]:
    _, error = _read_json_body()
    if error is not None:
        return error

    def mutate(pattern: dict[str, Any]) -> None:
        if pattern.get("status") == "retired":
            raise _SkillNoOp()
        pattern["status"] = "retired"
        touch_updated(pattern)

    pattern, _, error = _apply_transition(slug, mutate)
    if error is not None:
        return error
    return jsonify(pattern)


@skills_bp.post("/api/patterns/<slug>/edit-requests")
def create_edit_request(slug: str) -> tuple[Response, int]:
    body, error = _read_json_body()
    if error is not None:
        return error

    instructions, error = _require_field(body, "instructions")
    if error is not None:
        return error

    requested_by = body.get("requested_by") or "chat"
    if find_pattern(slug) is None:
        return error_response(SKILL_NOT_FOUND, detail=f"no skill with slug '{slug}'")

    request_id = make_request_id()
    record = {
        "id": request_id,
        "slug": slug,
        "instructions": instructions,
        "requested_at": utc_now_iso(),
        "requested_by": requested_by,
        "processed_at": None,
    }

    def mutate(rows: list[dict[str, Any]]) -> list[dict[str, Any]]:
        return [*rows, record]

    try:
        locked_modify_edit_requests(mutate)
    except LockTimeout:
        return error_response(SKILLS_BUSY, detail="skills are busy; try again")
    return created({"request_id": request_id, "slug": slug})


@skills_bp.post("/api/patterns/<old_slug>/rename")
def rename_pattern(old_slug: str) -> Response | tuple[Response, int]:
    body, error = _read_json_body()
    if error is not None:
        return error

    new_slug, error = _require_field(body, "new_slug")
    if error is not None:
        return error

    patterns = load_patterns()
    if find_pattern(old_slug, patterns) is None:
        return error_response(
            SKILL_NOT_FOUND, detail=f"no skill with slug '{old_slug}'"
        )
    if find_pattern(new_slug, patterns) is not None or profile_path(new_slug).exists():
        return error_response(
            SKILL_ALREADY_EXISTS, detail=f"the slug '{new_slug}' is already taken"
        )

    rename_profile(old_slug, new_slug)
    pattern, _, error = _apply_transition(
        old_slug,
        lambda row: (row.__setitem__("slug", new_slug), touch_updated(row)),
    )
    if error is not None:
        return error
    return jsonify(pattern)
