# SPDX-License-Identifier: AGPL-3.0-only
# Copyright (c) 2026 sol pbc

"""Routes for the owner-facing Suggestions app."""

from __future__ import annotations

from typing import Any

from flask import Blueprint, Response, jsonify, render_template, request

from solstone.apps.curation import copy as curation_copy
from solstone.convey.reasons import (
    ENTITY_BUSY,
    INVALID_REQUEST_VALUE,
    MISSING_REQUIRED_FIELD,
)
from solstone.convey.utils import error_response, respond_collection
from solstone.think import speaker_review_candidates
from solstone.think.curation import (
    KIND_ENTITY_MERGE,
    KIND_FACET_CANDIDATE,
    KIND_SPEAKER_NAME_VARIANT,
    accept_entity_candidate,
    accept_facet_candidate,
    accept_speaker_candidate,
    dismiss_entity_candidate,
    dismiss_facet_candidate,
    dismiss_speaker_candidate,
    load_open_items,
    merge_preview_fields,
)
from solstone.think.facet_review_candidates import load_candidates
from solstone.think.journal_io import LockTimeout

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
            item.to_dict() for item in items if item.kind == KIND_FACET_CANDIDATE
        ],
        curation_entity_items=[
            item.to_dict() for item in items if item.kind == KIND_ENTITY_MERGE
        ],
        curation_speaker_items=[
            item.to_dict() for item in items if item.kind == KIND_SPEAKER_NAME_VARIANT
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


def _speaker_payload(
    data: dict[str, Any],
) -> tuple[str, str, str] | tuple[Response, int]:
    try:
        key = str(_required(data, "key"))
        source_id = str(_required(data, "source_id"))
        target_id = str(_required(data, "target_id"))
    except KeyError as exc:
        return _missing_field(str(exc.args[0]))

    expected = speaker_review_candidates.candidate_key(source_id, target_id)
    if key != expected:
        return error_response(
            INVALID_REQUEST_VALUE,
            detail="key does not match source_id/target_id",
        )
    return key, source_id, target_id


@curation_bp.route("/api/facet/candidates")
def facet_candidates() -> Response:
    return respond_collection(load_candidates())


@curation_bp.post("/api/facet/accept")
def accept_facet() -> Response | tuple[Response, int]:
    data = _json_body()
    try:
        name_key = str(_required(data, "name_key"))
    except KeyError:
        return _missing_field("name_key")
    try:
        return _result_response(accept_facet_candidate(name_key))
    except LockTimeout:
        return error_response(ENTITY_BUSY, detail="suggestions are busy; try again")


@curation_bp.post("/api/facet/dismiss")
def dismiss_facet() -> Response | tuple[Response, int]:
    data = _json_body()
    try:
        name_key = str(_required(data, "name_key"))
    except KeyError:
        return _missing_field("name_key")
    try:
        return _result_response(dismiss_facet_candidate(name_key))
    except LockTimeout:
        return error_response(ENTITY_BUSY, detail="suggestions are busy; try again")


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


@curation_bp.post("/api/speaker/preview")
def preview_speaker() -> Response | tuple[Response, int]:
    payload = _speaker_payload(_json_body())
    if not isinstance(payload, tuple) or len(payload) != 3:
        return payload
    _, source_id, target_id = payload

    result = accept_speaker_candidate(
        source_id,
        target_id,
        commit=False,
    )
    if result.get("status") == "preview":
        result["preview"] = merge_preview_fields(result.get("merge", {}))
    return _result_response(result)


@curation_bp.post("/api/speaker/accept")
def accept_speaker() -> Response | tuple[Response, int]:
    payload = _speaker_payload(_json_body())
    if not isinstance(payload, tuple) or len(payload) != 3:
        return payload
    _, source_id, target_id = payload

    try:
        result = accept_speaker_candidate(
            source_id,
            target_id,
            commit=True,
        )
    except LockTimeout:
        return error_response(
            ENTITY_BUSY, detail="speaker suggestions are busy; try again"
        )
    return _result_response(result)


@curation_bp.post("/api/speaker/dismiss")
def dismiss_speaker() -> Response | tuple[Response, int]:
    payload = _speaker_payload(_json_body())
    if not isinstance(payload, tuple) or len(payload) != 3:
        return payload
    _, source_id, target_id = payload

    try:
        result = dismiss_speaker_candidate(source_id, target_id)
    except LockTimeout:
        return error_response(
            ENTITY_BUSY, detail="speaker suggestions are busy; try again"
        )
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

    try:
        result = accept_entity_candidate(
            facet,
            source_slug,
            target_slug,
            commit=True,
        )
    except LockTimeout:
        return error_response(
            ENTITY_BUSY, detail="entity merge candidates are busy; try again"
        )
    return _result_response(result)


@curation_bp.post("/api/entity/dismiss")
def dismiss_entity() -> Response | tuple[Response, int]:
    data = _json_body()
    try:
        facet = str(_required(data, "facet"))
        source_slug = str(_required(data, "source_slug"))
        target_slug = str(_required(data, "target_slug"))
    except KeyError as exc:
        return _missing_field(str(exc.args[0]))

    try:
        result = dismiss_entity_candidate(facet, source_slug, target_slug)
    except LockTimeout:
        return error_response(
            ENTITY_BUSY, detail="entity merge candidates are busy; try again"
        )
    return _result_response(result)
