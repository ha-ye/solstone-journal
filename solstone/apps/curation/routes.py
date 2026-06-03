# SPDX-License-Identifier: AGPL-3.0-only
# Copyright (c) 2026 sol pbc

"""Routes for the owner-facing Suggestions app."""

from __future__ import annotations

from typing import Any

from flask import Blueprint, Response, jsonify, render_template, request

from solstone.apps.curation import copy as curation_copy
from solstone.convey.reasons import MISSING_REQUIRED_FIELD
from solstone.convey.utils import error_response
from solstone.think.curation import (
    accept_entity_candidate,
    accept_facet_candidate,
    dismiss_entity_candidate,
    dismiss_facet_candidate,
    load_open_items,
    merge_preview_fields,
)

curation_bp = Blueprint("app:curation", __name__, url_prefix="/app/curation")


@curation_bp.app_context_processor
def _inject_curation_copy() -> dict[str, Any]:
    return {"curation_copy": curation_copy}


@curation_bp.route("/")
def index() -> str:
    items = load_open_items()
    return render_template(
        "app.html",
        curation_facet_items=[
            item.to_dict() for item in items if item.kind == "facet_candidate"
        ],
        curation_entity_items=[
            item.to_dict() for item in items if item.kind == "entity_merge"
        ],
        curation_copy_payload=curation_copy.curation_copy_payload(),
    )


def _json_body() -> dict[str, Any]:
    data = request.get_json(silent=True) or {}
    return data if isinstance(data, dict) else {}


def _required(data: dict[str, Any], field: str) -> Any:
    value = data.get(field)
    if value is None or value == "":
        raise KeyError(field)
    return value


def _missing_field(field: str) -> tuple[Response, int]:
    return error_response(MISSING_REQUIRED_FIELD, detail=f"Missing {field}")


def _result_response(result: dict[str, Any]) -> Response | tuple[Response, int]:
    if result.get("status") == "error":
        return jsonify(result), 400
    return jsonify(result)


@curation_bp.post("/api/facet/accept")
def accept_facet() -> Response | tuple[Response, int]:
    data = _json_body()
    try:
        name_key = str(_required(data, "name_key"))
    except KeyError:
        return _missing_field("name_key")
    return _result_response(accept_facet_candidate(name_key))


@curation_bp.post("/api/facet/dismiss")
def dismiss_facet() -> Response | tuple[Response, int]:
    data = _json_body()
    try:
        name_key = str(_required(data, "name_key"))
    except KeyError:
        return _missing_field("name_key")
    return _result_response(dismiss_facet_candidate(name_key))


@curation_bp.post("/api/entity/preview")
def preview_entity() -> Response | tuple[Response, int]:
    data = _json_body()
    try:
        facet = str(_required(data, "facet"))
        source_slug = str(_required(data, "source_slug"))
        target_slug = str(_required(data, "target_slug"))
    except KeyError as exc:
        return _missing_field(str(exc.args[0]))

    result = accept_entity_candidate(
        facet,
        source_slug,
        target_slug,
        commit=False,
    )
    if result.get("status") == "preview":
        result["preview"] = merge_preview_fields(result.get("merge", {}))
    return _result_response(result)


@curation_bp.post("/api/entity/accept")
def accept_entity() -> Response | tuple[Response, int]:
    data = _json_body()
    try:
        facet = str(_required(data, "facet"))
        source_slug = str(_required(data, "source_slug"))
        target_slug = str(_required(data, "target_slug"))
    except KeyError as exc:
        return _missing_field(str(exc.args[0]))

    return _result_response(
        accept_entity_candidate(
            facet,
            source_slug,
            target_slug,
            commit=True,
        )
    )


@curation_bp.post("/api/entity/dismiss")
def dismiss_entity() -> Response | tuple[Response, int]:
    data = _json_body()
    try:
        facet = str(_required(data, "facet"))
        source_slug = str(_required(data, "source_slug"))
        target_slug = str(_required(data, "target_slug"))
    except KeyError as exc:
        return _missing_field(str(exc.args[0]))

    return _result_response(dismiss_entity_candidate(facet, source_slug, target_slug))
