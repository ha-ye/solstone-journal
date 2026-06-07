# SPDX-License-Identifier: AGPL-3.0-only
# Copyright (c) 2026 sol pbc

"""HTTP API for the ledger tool-group (commitments, closures, decisions).

Core JSON blueprint: a thin HTTP surface over
``solstone.think.surfaces.ledger``. No workspace page, no menu entry —
registered directly in ``create_app`` like ``config_bp`` / ``system.bp``.
Handlers only parse query params, bound collections, shape responses, and map
surface errors to owner-voice Reasons; the one write (close) routes through the
ledger surface, which delegates to the activities domain owner.
"""

from __future__ import annotations

import dataclasses
from datetime import datetime
from typing import Any

from flask import Blueprint, Response, jsonify, request

from solstone.convey.reasons import (
    ACTIVITIES_BUSY,
    INVALID_DAY,
    INVALID_JSON_REQUEST,
    INVALID_REQUEST_VALUE,
    LEDGER_ITEM_NOT_FOUND,
    MISSING_REQUEST_BODY,
    MISSING_REQUIRED_FIELD,
)
from solstone.convey.utils import (
    error_response,
    parse_pagination_params,
    respond_collection,
)
from solstone.think.journal_io import LockTimeout
from solstone.think.surfaces import ledger

bp = Blueprint("ledger", __name__, url_prefix="/api/ledger")

# HTTP input contract — mirrors the values solstone.think.surfaces.ledger
# accepts so a bad query param gets a precise owner-voice reason here instead
# of a string-sniffed surface ValueError.
_VALID_STATES = {"open", "closed", "dropped", "all"}
_VALID_SORTS = {"age_days_desc", "opened_at_desc", "closed_at_desc"}
_VALID_AS_STATES = {"closed", "dropped"}


def _read_json_body() -> tuple[dict[str, Any] | None, tuple[Response, int] | None]:
    """Parse a JSON-object request body (mirrors the awareness/skills siblings)."""
    if not request.get_data():
        return None, error_response(MISSING_REQUEST_BODY, detail="no request body")
    data = request.get_json(silent=True)
    if not isinstance(data, dict):
        return None, error_response(
            INVALID_JSON_REQUEST, detail="request body must be a JSON object"
        )
    return data, None


def _parse_facets_csv(value: str | None) -> list[str] | None:
    if value is None:
        return None
    parts = [part.strip() for part in value.split(",") if part.strip()]
    return parts or None


def _is_valid_day(value: str) -> bool:
    try:
        datetime.strptime(value, "%Y%m%d")
    except ValueError:
        return False
    return True


@bp.route("")
def list_items() -> tuple[Response, int]:
    state = request.args.get("state", "open")
    if state not in _VALID_STATES:
        return error_response(INVALID_REQUEST_VALUE, detail=f"unknown state: {state}")
    sort = request.args.get("sort")
    if sort is not None and sort not in _VALID_SORTS:
        return error_response(INVALID_REQUEST_VALUE, detail=f"unknown sort: {sort}")
    closed_since = request.args.get("closed_since")
    if closed_since is not None and not _is_valid_day(closed_since):
        return error_response(INVALID_DAY, detail="closed_since must match YYYYMMDD")

    age_days_gte_raw = request.args.get("age_days_gte")
    age_days_gte: int | None = None
    if age_days_gte_raw is not None:
        try:
            age_days_gte = int(age_days_gte_raw)
        except (ValueError, TypeError):
            return error_response(
                INVALID_REQUEST_VALUE, detail="age_days_gte must be an integer"
            )

    full = ledger.list(
        state=state,
        owner=request.args.get("owner"),
        counterparty=request.args.get("counterparty"),
        age_days_gte=age_days_gte,
        closed_since=closed_since,
        sort=sort,
        facets=_parse_facets_csv(request.args.get("facets")),
    )
    limit, offset = parse_pagination_params(default_limit=20, max_limit=100)
    page = full[offset : offset + limit]
    return respond_collection(
        [dataclasses.asdict(item) for item in page], total=len(full)
    )


@bp.route("/decisions")
def list_decisions() -> tuple[Response, int]:
    since = request.args.get("since")
    if since is not None and not _is_valid_day(since):
        return error_response(INVALID_DAY, detail="since must match YYYYMMDD")
    full = ledger.decisions(
        owner=request.args.get("owner"),
        since=since,
        involving=request.args.get("involving"),
        facets=_parse_facets_csv(request.args.get("facets")),
    )
    limit, offset = parse_pagination_params(default_limit=20, max_limit=100)
    page = full[offset : offset + limit]
    return respond_collection(
        [dataclasses.asdict(item) for item in page], total=len(full)
    )


@bp.route("/<item_id>")
def get_item(item_id: str) -> Response | tuple[Response, int]:
    item = ledger.get(item_id)
    if item is None:
        return error_response(
            LEDGER_ITEM_NOT_FOUND, detail=f"no ledger item with id '{item_id}'"
        )
    return jsonify(dataclasses.asdict(item))


@bp.route("/<item_id>/close", methods=["POST"])
def close_item(item_id: str) -> Response | tuple[Response, int]:
    body, error = _read_json_body()
    if error is not None:
        return error

    note = body.get("note")
    if not isinstance(note, str) or not note.strip():
        return error_response(MISSING_REQUIRED_FIELD, detail="note is required")

    as_state = body.get("as_state", "closed")
    if as_state not in _VALID_AS_STATES:
        return error_response(
            INVALID_REQUEST_VALUE, detail="as_state must be 'closed' or 'dropped'"
        )

    try:
        updated = ledger.close(item_id, note=note, as_state=as_state)
    except KeyError:
        return error_response(
            LEDGER_ITEM_NOT_FOUND, detail=f"no ledger item with id '{item_id}'"
        )
    except LockTimeout:
        return error_response(ACTIVITIES_BUSY, detail="activities are busy; try again")
    return jsonify(dataclasses.asdict(updated))
