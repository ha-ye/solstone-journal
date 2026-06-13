# SPDX-License-Identifier: AGPL-3.0-only
# Copyright (c) 2026 sol pbc

"""Services switchboard app routes."""

from __future__ import annotations

import logging
import time
from collections.abc import Callable
from typing import Any

from flask import Blueprint, Response, jsonify, render_template

from solstone.apps.services.copy import services_copy_payload
from solstone.convey.reasons import (
    FEATURE_UNAVAILABLE,
    INVALID_OPERATION_FOR_STATE,
    SERVICE_BUSY,
    SERVICE_OPERATION_FAILED,
    UNKNOWN_SERVICE,
)
from solstone.convey.utils import error_response, time_since
from solstone.think.backup import state as backup_state
from solstone.think.services import (
    operations,
    portal_client,
    scout,
    scout_handoff,
    spl,
    spl_handoff,
)
from solstone.think.services import status as service_status

logger = logging.getLogger(__name__)

services_bp = Blueprint(
    "app:services",
    __name__,
    url_prefix="/app/services",
    static_folder="static",
    static_url_path="/static",
)

SERVICE_SCOUT = "scout"
SERVICE_SPL = "spl"
SERVICE_SPB = "spb"
SERVICE_SPN = "spn"
SERVICES = (SERVICE_SCOUT, SERVICE_SPL, SERVICE_SPB, SERVICE_SPN)
COMING_SOON_SERVICES = frozenset({SERVICE_SPN})
# spb is a local manage-only row: it links to /app/backup and must never
# reach the SPL back-channel. Guarded in every POST handler below.
MANAGE_ONLY_SERVICES = frozenset({SERVICE_SPB})


def run_spl_handoff(
    *,
    poll_once: Callable[
        ..., portal_client.PollOutcome
    ] = portal_client.poll_handoff_once,
    open_browser: Callable[[str], bool] = operations._open_browser,
    clock: Callable[[], float] = time.monotonic,
    wait_seconds: int = portal_client.DEFAULT_WAIT_SECONDS,
) -> operations.HandoffResult:
    """Run the spl browser-consent flow synchronously for route/thread callers."""

    browser_open_succeeded: bool | None = None
    manual_url: str | None = None

    def wrapped_open_browser(url: str) -> bool:
        nonlocal browser_open_succeeded, manual_url
        browser_open_succeeded, manual_url = operations.open_for_handoff(
            url, open_browser
        )
        return browser_open_succeeded

    outcome = spl_handoff.enable_spl_via_consent(
        open_browser=wrapped_open_browser,
        poll_once=poll_once,
        clock=clock,
        wait_seconds=wait_seconds,
    )
    return operations._outcome_result(
        outcome.code,
        outcome.guidance,
        browser_open_succeeded,
        manual_url,
    )


def _scout_actions(state: str) -> dict[str, bool]:
    return {
        "enable": state == "disabled",
        "refresh": state in {"enabled", "pending"},
        "disable": state in {"enabled", "pending"},
    }


def _spl_actions(state: str) -> dict[str, bool]:
    return {
        "enable": state in {"not_enabled", "inconsistent"},
        "refresh": False,
        "disable": state in {"enabled", "inconsistent"},
    }


def _service_status(service: str) -> dict[str, Any]:
    if service == SERVICE_SCOUT:
        resting = service_status.scout_status()
        state = str(resting["state"])
        return {
            "service": SERVICE_SCOUT,
            "state": state,
            "guidance": resting.get("guidance"),
            "provenance": service_status.scout_provenance_view(),
            "actions": _scout_actions(state),
            "operation": operations.operation_for_service(SERVICE_SCOUT),
        }
    if service == SERVICE_SPL:
        resting = service_status.spl_status()
        state = str(resting["state"])
        return {
            "service": SERVICE_SPL,
            "state": state,
            "guidance": resting.get("guidance"),
            "provenance": {},
            "actions": _spl_actions(state),
            "operation": operations.operation_for_service(SERVICE_SPL),
        }
    if service == SERVICE_SPB:
        view = backup_state.status_view()
        last_time = view["last_backup"]["time"]
        guidance = (
            f"last backup {time_since(last_time)}"
            if last_time is not None
            else "not set up"
        )
        return {
            "service": SERVICE_SPB,
            "state": "enabled" if view["enabled"] else "disabled",
            "guidance": guidance,
            "provenance": {},
            "actions": {"enable": False, "refresh": False, "disable": False},
            "operation": None,
        }
    if service in COMING_SOON_SERVICES:
        return {
            "service": service,
            "state": "coming_soon",
            "guidance": None,
            "provenance": {},
            "actions": {"enable": False, "refresh": False, "disable": False},
            "operation": None,
        }
    raise KeyError(service)


def _status_response(service: str) -> tuple[Response, int]:
    try:
        payload = _service_status(service)
    except KeyError:
        return error_response(UNKNOWN_SERVICE)
    return jsonify({"success": True, **payload}), 200


def _unsupported() -> tuple[Response, int]:
    return error_response(FEATURE_UNAVAILABLE, detail="service action unavailable")


def _operation_failed() -> tuple[Response, int]:
    return error_response(SERVICE_OPERATION_FAILED, detail="service operation failed")


def _start_operation_response(
    service: str,
    kind: str,
    flow: Callable[[Callable[[str], bool]], operations.HandoffResult],
) -> tuple[Response, int]:
    try:
        operation = operations.start_operation(service, kind, flow)
    except operations.OperationBusyError:
        return error_response(SERVICE_BUSY, detail="operation already running")
    return jsonify({"success": True, "service": service, "operation": operation}), 202


@services_bp.route("/")
def index() -> str:
    return render_template(
        "app.html",
        services_copy=services_copy_payload(),
        services_initial={service: _service_status(service) for service in SERVICES},
    )


@services_bp.route("/<service>/status")
def status(service: str) -> tuple[Response, int]:
    return _status_response(service)


@services_bp.route("/<service>/enable", methods=["POST"])
def enable(service: str) -> tuple[dict[str, Any], int] | tuple[Response, int]:
    if service not in SERVICES:
        return error_response(UNKNOWN_SERVICE)
    if service in COMING_SOON_SERVICES:
        return _unsupported()
    if service in MANAGE_ONLY_SERVICES:
        return _unsupported()
    if service == SERVICE_SCOUT:
        if scout.is_scout_enabled():
            return error_response(
                INVALID_OPERATION_FOR_STATE,
                detail="scout is already enabled",
            )
        if scout.is_manual_key_present():
            return error_response(
                INVALID_OPERATION_FOR_STATE,
                detail="manual key is present",
            )
        return _start_operation_response(
            SERVICE_SCOUT,
            "enable",
            lambda opener: scout_handoff.run_scout_handoff(
                refresh=False, open_browser=opener
            ),
        )
    return _start_operation_response(
        SERVICE_SPL,
        "spl_enable",
        lambda opener: run_spl_handoff(open_browser=opener),
    )


@services_bp.route("/<service>/refresh", methods=["POST"])
def refresh(service: str) -> tuple[dict[str, Any], int] | tuple[Response, int]:
    if service not in SERVICES:
        return error_response(UNKNOWN_SERVICE)
    if service != SERVICE_SCOUT:
        return _unsupported()
    return _start_operation_response(
        SERVICE_SCOUT,
        "refresh",
        lambda opener: scout_handoff.run_scout_handoff(
            refresh=True, open_browser=opener
        ),
    )


@services_bp.route("/<service>/disable", methods=["POST"])
def disable(service: str) -> tuple[Response, int]:
    if service not in SERVICES:
        return error_response(UNKNOWN_SERVICE)
    if service in COMING_SOON_SERVICES:
        return _unsupported()
    if service in MANAGE_ONLY_SERVICES:
        return _unsupported()
    try:
        if service == SERVICE_SCOUT:
            outcome = scout.disable_scout()
            result = {
                "was_enabled": outcome.was_enabled,
                "env_key_preserved": outcome.env_key_preserved,
            }
        else:
            outcome = spl.disable_spl()
            result = {"was_enabled": outcome.was_enabled}
    except Exception:
        logger.exception("service disable failed")
        return _operation_failed()
    return (
        jsonify(
            {
                "success": True,
                "service": service,
                "result": result,
                "status": _service_status(service),
            }
        ),
        200,
    )
