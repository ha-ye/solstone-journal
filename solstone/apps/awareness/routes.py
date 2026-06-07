# SPDX-License-Identifier: AGPL-3.0-only
# Copyright (c) 2026 sol pbc

"""HTTP API for the awareness self-knowledge system.

API-only app: a thin JSON surface over the owner functions in
``solstone.think.awareness``. There is no workspace page, no menu entry, and
no index route — ``GET /app/awareness/`` is intentionally a 404. All write
paths route through the awareness owner functions; this module only parses
requests, shapes responses, and maps ``LockTimeout`` to an owner-voice busy.
"""

from __future__ import annotations

from typing import Any

from flask import Blueprint, Response, jsonify, request

from solstone.convey.reasons import (
    AWARENESS_BUSY,
    AWARENESS_SECTION_NOT_FOUND,
    INVALID_JSON_REQUEST,
    INVALID_REQUEST_VALUE,
    MISSING_REQUEST_BODY,
    MISSING_REQUIRED_FIELD,
)
from solstone.convey.utils import (
    created,
    error_response,
    parse_pagination_params,
    respond_collection,
)
from solstone.think.awareness import (
    append_log,
    get_current,
    get_imports,
    read_log,
    record_import,
    record_import_nudge,
    record_import_offer_declined,
)
from solstone.think.journal_io import LockTimeout

awareness_bp = Blueprint("app:awareness", __name__, url_prefix="/app/awareness")


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


@awareness_bp.get("/api/state")
def get_state() -> Response | tuple[Response, int]:
    state = get_current()
    section = request.args.get("section")
    if section is None:
        return jsonify(state)
    if section not in state:
        return error_response(
            AWARENESS_SECTION_NOT_FOUND,
            detail=f"no awareness section named '{section}'",
        )
    return jsonify(state[section])


@awareness_bp.get("/api/imports")
def get_imports_state() -> Response:
    return jsonify(get_imports())


@awareness_bp.post("/api/imports")
def update_imports() -> Response | tuple[Response, int]:
    body, error = _read_json_body()
    if error is not None:
        return error

    record = body.get("record")
    declined = body.get("declined")
    nudge = body.get("nudge")

    active: list[str] = []
    if isinstance(record, str) and record:
        active.append("record")
    if declined is True:
        active.append("declined")
    if nudge is True:
        active.append("nudge")

    if len(active) != 1:
        if not active:
            detail = "provide exactly one of record/declined/nudge"
        else:
            detail = (
                f"provide exactly one of record/declined/nudge; got {', '.join(active)}"
            )
        return error_response(INVALID_REQUEST_VALUE, detail=detail)

    try:
        action = active[0]
        if action == "record":
            updated = record_import(record)
        elif action == "declined":
            updated = record_import_offer_declined()
        else:
            updated = record_import_nudge()
    except LockTimeout:
        return error_response(AWARENESS_BUSY, detail="imports are busy; try again")

    return jsonify(updated)


@awareness_bp.get("/api/log")
def get_log() -> tuple[Response, int]:
    entries = read_log(request.args.get("day"))
    kind = request.args.get("kind")
    if kind:
        entries = [entry for entry in entries if entry.get("kind") == kind]
    limit, offset = parse_pagination_params(default_limit=20, max_limit=100)
    page = entries[offset : offset + limit]
    return respond_collection(page, total=len(entries))


@awareness_bp.post("/api/log")
def create_log_entry() -> Response | tuple[Response, int]:
    body, error = _read_json_body()
    if error is not None:
        return error

    kind = body.get("kind")
    if not isinstance(kind, str) or not kind:
        return error_response(MISSING_REQUIRED_FIELD, detail="kind is required")

    entry = append_log(
        kind,
        key=body.get("key"),
        message=body.get("message"),
        data=body.get("data"),
    )
    return created(entry)
