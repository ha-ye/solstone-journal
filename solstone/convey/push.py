# SPDX-License-Identifier: AGPL-3.0-only
# Copyright (c) 2026 sol pbc

"""Root-level push API."""

from __future__ import annotations

import uuid
from typing import Any

from flask import Blueprint, g, jsonify, request
from werkzeug.exceptions import BadRequest

from solstone.convey.reasons import (
    FEATURE_UNAVAILABLE,
    INVALID_JSON_REQUEST,
    PUSH_REQUEST_INVALID,
)
from solstone.convey.sol_initiated.copy import APNS_CATEGORY_SOL_CHAT_REQUEST
from solstone.convey.utils import error_response
from solstone.think.push.devices import (
    load_devices,
    register_device,
    remove_device,
    status_view,
)
from solstone.think.push.portal_dispatch import dispatch_via_portal
from solstone.think.push.relay_auth import push_relay_token

push_bp = Blueprint("push", __name__, url_prefix="/api/push")


def _connection_fingerprint() -> str | None:
    identity = getattr(g, "identity", None)
    fingerprint = getattr(identity, "fingerprint", None)
    if isinstance(fingerprint, str) and fingerprint.strip():
        return fingerprint
    return None


def _required_json_object() -> tuple[dict[str, Any], Any | None]:
    try:
        data = request.get_json(silent=False)
    except BadRequest:
        return {}, error_response(
            INVALID_JSON_REQUEST,
            detail="request body must be valid JSON",
        )
    if not isinstance(data, dict):
        return {}, error_response(
            INVALID_JSON_REQUEST,
            detail="request body must be a JSON object",
        )
    return data, None


def _optional_json_object() -> tuple[dict[str, Any], Any | None]:
    if not request.get_data(cache=True):
        return {}, None
    return _required_json_object()


def _require_fingerprint() -> tuple[str | None, Any | None]:
    fingerprint = _connection_fingerprint()
    if fingerprint is None:
        return None, error_response(
            PUSH_REQUEST_INVALID,
            detail="push registration requires a paired device",
        )
    return fingerprint, None


@push_bp.post("/register")
def register_push_device():
    fingerprint, error = _require_fingerprint()
    if error is not None:
        return error
    body, error = _required_json_object()
    if error is not None:
        return error
    token = str(body.get("device_token") or "").strip()
    bundle_id = str(body.get("bundle_id") or "").strip()
    environment = str(body.get("environment") or "").strip()
    platform = str(body.get("platform") or "").strip()
    if not token:
        return error_response(PUSH_REQUEST_INVALID, detail="device_token is required")
    if not bundle_id:
        return error_response(PUSH_REQUEST_INVALID, detail="bundle_id is required")
    if environment not in {"development", "production"}:
        return error_response(
            PUSH_REQUEST_INVALID,
            detail="environment must be development or production",
        )
    if platform != "ios":
        return error_response(PUSH_REQUEST_INVALID, detail="platform must be ios")
    count = register_device(
        fingerprint=fingerprint,
        token="".join(token.split()).lower(),
        bundle_id=bundle_id,
        environment=environment,
        platform=platform,
    )
    return jsonify({"registered": True, "device_count": count})


@push_bp.delete("/register")
def unregister_push_device():
    fingerprint, error = _require_fingerprint()
    if error is not None:
        return error
    removed = remove_device(fingerprint)
    return jsonify({"removed": removed, "device_count": len(load_devices())})


@push_bp.get("/status")
def push_status():
    devices = sorted(
        load_devices(),
        key=lambda device: int(device.get("registered_at", 0)),
        reverse=True,
    )
    return jsonify(
        {
            "device_count": len(devices),
            "relay_available": bool(push_relay_token()),
            "devices": [status_view(device) for device in devices],
        }
    )


@push_bp.post("/test")
def send_push_test():
    body, error = _optional_json_object()
    if error is not None:
        return error
    if not push_relay_token():
        return error_response(
            FEATURE_UNAVAILABLE,
            status=503,
            detail="push relay unavailable",
        )
    if not load_devices():
        return error_response(
            FEATURE_UNAVAILABLE,
            status=503,
            detail="no devices to reach",
        )
    request_id = f"push-test-{uuid.uuid4().hex[:12]}"
    summary = str(body.get("body") or "This is a test notification.")
    result = dispatch_via_portal(
        request_id=request_id,
        summary=summary,
        category=APNS_CATEGORY_SOL_CHAT_REQUEST,
    )
    if result is None:
        return error_response(
            FEATURE_UNAVAILABLE,
            status=503,
            detail="push relay dispatch failed",
        )
    return jsonify({"dispatched": True, "request_id": request_id})


__all__ = ["push_bp"]
