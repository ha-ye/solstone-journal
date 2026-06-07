# SPDX-License-Identifier: AGPL-3.0-only
# Copyright (c) 2026 sol pbc

"""HTTP API for the profile tool-group (entity profiles, briefs, cadence).

Core JSON blueprints: a thin, read-only HTTP surface over
``solstone.think.surfaces.profile``. No workspace page, no menu entry —
registered directly in ``create_app`` like ``config_bp`` / ``system.bp``.
``/api/profiles/active`` lives on a second blueprint so it can't be shadowed by
``/api/profile/<name>``.
"""

from __future__ import annotations

import dataclasses

from flask import Blueprint, Response, jsonify, request

from solstone.convey.reasons import ENTITY_NOT_FOUND, INVALID_REQUEST_VALUE
from solstone.convey.utils import (
    error_response,
    parse_pagination_params,
    respond_collection,
)
from solstone.think.surfaces import profile

bp = Blueprint("profile", __name__, url_prefix="/api/profile")
profiles_bp = Blueprint("profiles", __name__, url_prefix="/api/profiles")

_TRUTHY = {"1", "true", "yes", "on"}


def _parse_bool_flag(name: str) -> bool:
    return request.args.get(name, "").strip().lower() in _TRUTHY


def _parse_facets_csv(value: str | None) -> list[str] | None:
    if value is None:
        return None
    parts = [part.strip() for part in value.split(",") if part.strip()]
    return parts or None


@bp.route("/<name>")
def get_profile(name: str) -> Response | tuple[Response, int]:
    result = profile.full(
        name,
        facets=_parse_facets_csv(request.args.get("facets")),
        include_mentions=_parse_bool_flag("include_mentions"),
    )
    if result is None:
        return error_response(ENTITY_NOT_FOUND, detail=f"no entity named '{name}'")
    return jsonify(dataclasses.asdict(result))


@bp.route("/<name>/brief")
def get_brief(name: str) -> Response | tuple[Response, int]:
    result = profile.brief(name)
    if result is None:
        return error_response(ENTITY_NOT_FOUND, detail=f"no entity named '{name}'")
    return jsonify(dataclasses.asdict(result))


@bp.route("/<name>/cadence")
def get_cadence(name: str) -> Response | tuple[Response, int]:
    result = profile.cadence(
        name, include_mentions=_parse_bool_flag("include_mentions")
    )
    if result is None:
        return error_response(ENTITY_NOT_FOUND, detail=f"no entity named '{name}'")
    return jsonify(dataclasses.asdict(result))


@profiles_bp.route("/active")
def list_active() -> tuple[Response, int]:
    window_days_raw = request.args.get("window_days")
    window_days = 30
    if window_days_raw is not None:
        try:
            window_days = int(window_days_raw)
        except (ValueError, TypeError):
            return error_response(
                INVALID_REQUEST_VALUE, detail="window_days must be an integer"
            )
    full = profile.list_active(window_days=window_days)
    limit, offset = parse_pagination_params(default_limit=20, max_limit=100)
    page = full[offset : offset + limit]
    return respond_collection(page, total=len(full))
