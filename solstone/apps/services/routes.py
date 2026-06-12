# SPDX-License-Identifier: AGPL-3.0-only
# Copyright (c) 2026 sol pbc

"""Services switchboard app routes."""

from __future__ import annotations

import logging
import threading
import time
import webbrowser
from collections.abc import Callable
from dataclasses import dataclass
from datetime import datetime, timezone
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
from solstone.think.services import outcomes, portal_client, scout, spl, spl_handoff
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
TERMINAL_PHASES = frozenset({"enabled", "pending", "revoked", "error"})
RETRYABLE_CODES = frozenset(
    {outcomes.EXPIRED, outcomes.NETWORK_ERROR, outcomes.LOCAL_ERROR}
)
OPERATION_GRACE_SECONDS = 30.0


@dataclass
class OperationEntry:
    service: str
    kind: str
    phase: str
    guidance: str | None
    retryable: bool
    browser_open_succeeded: bool | None
    portal_url: str | None
    started_monotonic: float
    ended_monotonic: float | None = None


@dataclass(frozen=True)
class ScoutOpResult:
    phase: str
    guidance: str | None
    retryable: bool
    browser_open_succeeded: bool | None
    portal_url: str | None


@dataclass(frozen=True)
class SplOpResult:
    phase: str
    guidance: str | None
    retryable: bool
    browser_open_succeeded: bool | None
    portal_url: str | None


_REGISTRY_LOCK = threading.Lock()
_REGISTRY: dict[str, OperationEntry] = {}


def _clear_registry() -> None:
    """Clear process-local operation state for tests."""

    with _REGISTRY_LOCK:
        _REGISTRY.clear()


def _open_browser(url: str) -> bool:
    try:
        return bool(webbrowser.open(url, new=2))
    except Exception as exc:
        logger.warning("service browser open failed: %s", exc)
        return False


def _format_since(since: Any) -> str:
    try:
        return datetime.fromtimestamp(int(since) / 1000, tz=timezone.utc).strftime(
            "%Y-%m-%d"
        )
    except (TypeError, ValueError, OverflowError, OSError):
        return "recently"


def _outcome_result(
    code: str,
    guidance: str | None,
    browser_open_succeeded: bool | None,
    portal_url: str | None,
) -> tuple[str, str | None, bool, bool | None, str | None]:
    if code == outcomes.APPROVED:
        return "enabled", None, False, browser_open_succeeded, portal_url
    if code == outcomes.PENDING:
        return "pending", guidance, False, browser_open_succeeded, portal_url
    if code == outcomes.REVOKED:
        return "revoked", guidance, False, browser_open_succeeded, portal_url
    return (
        "error",
        guidance,
        code in RETRYABLE_CODES,
        browser_open_succeeded,
        portal_url,
    )


def _handoff_error_result(
    token: str,
    *,
    detail: str | None = None,
    browser_open_succeeded: bool | None,
    portal_url: str | None,
) -> ScoutOpResult:
    try:
        outcome = outcomes.outcome_from_token(token, detail=detail)
    except ValueError:
        outcome = outcomes.outcome_for_code(outcomes.LOCAL_ERROR, detail=detail)
    phase, guidance, retryable, browser_ok, manual_url = _outcome_result(
        outcome.code,
        outcome.guidance,
        browser_open_succeeded,
        portal_url,
    )
    return ScoutOpResult(
        phase=phase,
        guidance=guidance,
        retryable=retryable,
        browser_open_succeeded=browser_ok,
        portal_url=manual_url,
    )


def _open_for_handoff(
    browser_url: str,
    open_browser: Callable[[str], bool],
) -> tuple[bool, str | None]:
    try:
        browser_open_succeeded = bool(open_browser(browser_url))
    except Exception as exc:
        logger.warning("service browser open failed: %s", exc)
        browser_open_succeeded = False
    return browser_open_succeeded, browser_url if not browser_open_succeeded else None


def run_scout_handoff(
    *,
    refresh: bool,
    poll_once: Callable[
        ..., portal_client.PollOutcome
    ] = portal_client.poll_handoff_once,
    open_browser: Callable[[str], bool] = _open_browser,
    clock: Callable[[], float] = time.monotonic,
    wait_seconds: int = portal_client.DEFAULT_WAIT_SECONDS,
) -> ScoutOpResult:
    """Run the scout browser-consent flow synchronously for route/thread callers."""

    base_url = portal_client.portal_base_url()
    nonce = portal_client.mint_nonce()
    browser_url = portal_client.browser_url(base_url, nonce, service=SERVICE_SCOUT)
    browser_open_succeeded, manual_url = _open_for_handoff(browser_url, open_browser)

    deadline = clock() + wait_seconds
    while clock() < deadline:
        timeout = min(portal_client.POLL_TIMEOUT_SECONDS, max(0.1, deadline - clock()))
        outcome = poll_once(
            base_url,
            nonce,
            timeout=timeout,
            component="switchboard",
            service=SERVICE_SCOUT,
        )
        if outcome.kind == "continue":
            continue
        if outcome.kind == "failed":
            if outcome.reason:
                return _handoff_error_result(
                    outcome.reason,
                    detail=outcome.detail,
                    browser_open_succeeded=browser_open_succeeded,
                    portal_url=manual_url,
                )
            return _handoff_error_result(
                "unexpected_payload",
                detail=outcome.detail,
                browser_open_succeeded=browser_open_succeeded,
                portal_url=manual_url,
            )
        if outcome.kind != "success":
            return _handoff_error_result(
                "unexpected_payload",
                browser_open_succeeded=browser_open_succeeded,
                portal_url=manual_url,
            )

        try:
            result = scout.apply_scout_state(outcome.payload or {})
        except scout.ScoutPayloadError as exc:
            return _handoff_error_result(
                exc.token,
                detail=exc.detail,
                browser_open_succeeded=browser_open_succeeded,
                portal_url=manual_url,
            )
        except scout.JournalNotInitializedError:
            return _handoff_error_result(
                "journal_not_initialized",
                browser_open_succeeded=browser_open_succeeded,
                portal_url=manual_url,
            )
        except Exception:
            logger.exception("scout handoff write failed")
            return _handoff_error_result(
                "write_failed",
                browser_open_succeeded=browser_open_succeeded,
                portal_url=manual_url,
            )

        if result.kind == "approved":
            guidance = None
            phase = "enabled"
        elif result.kind == "pending":
            guidance = outcomes.GUIDANCE[outcomes.PENDING]
            phase = "pending"
        else:
            guidance = outcomes.GUIDANCE[outcomes.REVOKED]
            phase = "revoked"
        return ScoutOpResult(
            phase=phase,
            guidance=guidance,
            retryable=False,
            browser_open_succeeded=browser_open_succeeded,
            portal_url=manual_url,
        )

    return _handoff_error_result(
        "consent_timeout",
        browser_open_succeeded=browser_open_succeeded,
        portal_url=manual_url,
    )


def run_spl_handoff(
    *,
    poll_once: Callable[
        ..., portal_client.PollOutcome
    ] = portal_client.poll_handoff_once,
    open_browser: Callable[[str], bool] = _open_browser,
    clock: Callable[[], float] = time.monotonic,
    wait_seconds: int = portal_client.DEFAULT_WAIT_SECONDS,
) -> SplOpResult:
    """Run the spl browser-consent flow synchronously for route/thread callers."""

    browser_open_succeeded: bool | None = None
    manual_url: str | None = None

    def wrapped_open_browser(url: str) -> bool:
        nonlocal browser_open_succeeded, manual_url
        browser_open_succeeded, manual_url = _open_for_handoff(url, open_browser)
        return browser_open_succeeded

    outcome = spl_handoff.enable_spl_via_consent(
        open_browser=wrapped_open_browser,
        poll_once=poll_once,
        clock=clock,
        wait_seconds=wait_seconds,
    )
    phase, guidance, retryable, browser_ok, portal_url = _outcome_result(
        outcome.code,
        outcome.guidance,
        browser_open_succeeded,
        manual_url,
    )
    return SplOpResult(
        phase=phase,
        guidance=guidance,
        retryable=retryable,
        browser_open_succeeded=browser_ok,
        portal_url=portal_url,
    )


def _sweep_operations_locked(now: float) -> None:
    expired = [
        service
        for service, entry in _REGISTRY.items()
        if entry.ended_monotonic is not None
        and entry.ended_monotonic + OPERATION_GRACE_SECONDS < now
    ]
    for service in expired:
        _REGISTRY.pop(service, None)


def _active_operation_locked(service: str) -> OperationEntry | None:
    entry = _REGISTRY.get(service)
    if entry is None or entry.ended_monotonic is not None:
        return None
    return entry


def _operation_payload(
    entry: OperationEntry, now: float | None = None
) -> dict[str, Any]:
    ts = time.monotonic() if now is None else now
    return {
        "kind": entry.kind,
        "phase": entry.phase,
        "guidance": entry.guidance,
        "retryable": entry.retryable,
        "browser_open_succeeded": entry.browser_open_succeeded,
        "portal_url": entry.portal_url,
        "elapsed_ms": int(max(0.0, ts - entry.started_monotonic) * 1000),
    }


def _update_entry_from_result(
    entry: OperationEntry,
    result: ScoutOpResult | SplOpResult,
) -> None:
    with _REGISTRY_LOCK:
        current = _REGISTRY.get(entry.service)
        if current is not entry:
            return
        entry.phase = result.phase
        entry.guidance = result.guidance
        entry.retryable = result.retryable
        entry.browser_open_succeeded = result.browser_open_succeeded
        entry.portal_url = result.portal_url
        entry.ended_monotonic = time.monotonic()


def _tracked_opener(entry: OperationEntry) -> Callable[[str], bool]:
    def opener(url: str) -> bool:
        browser_open_succeeded, manual_url = _open_for_handoff(url, _open_browser)
        with _REGISTRY_LOCK:
            current = _REGISTRY.get(entry.service)
            if current is entry:
                entry.browser_open_succeeded = browser_open_succeeded
                entry.portal_url = manual_url
        return browser_open_succeeded

    return opener


def _run_operation(
    entry: OperationEntry,
    flow: Callable[[Callable[[str], bool]], ScoutOpResult | SplOpResult],
) -> None:
    with _REGISTRY_LOCK:
        current = _REGISTRY.get(entry.service)
        if current is entry:
            entry.phase = "waiting"
    try:
        result = flow(_tracked_opener(entry))
    except Exception:
        logger.exception("service operation failed")
        result = ScoutOpResult(
            phase="error",
            guidance=None,
            retryable=True,
            browser_open_succeeded=entry.browser_open_succeeded,
            portal_url=entry.portal_url,
        )
    _update_entry_from_result(entry, result)


def _start_operation(
    service: str,
    kind: str,
    flow: Callable[[Callable[[str], bool]], ScoutOpResult | SplOpResult],
) -> tuple[dict[str, Any], int] | tuple[Response, int]:
    with _REGISTRY_LOCK:
        now = time.monotonic()
        _sweep_operations_locked(now)
        if _active_operation_locked(service) is not None:
            return error_response(SERVICE_BUSY, detail="operation already running")
        entry = OperationEntry(
            service=service,
            kind=kind,
            phase="starting",
            guidance=None,
            retryable=False,
            browser_open_succeeded=None,
            portal_url=None,
            started_monotonic=now,
        )
        _REGISTRY[service] = entry
        operation = _operation_payload(entry, now)

    thread = threading.Thread(
        target=_run_operation,
        args=(entry, flow),
        daemon=True,
    )
    thread.start()
    return {"success": True, "service": service, "operation": operation}, 202


def _scout_provenance() -> dict[str, Any]:
    raw = scout.scout_provenance()
    if not isinstance(raw, dict):
        return {}
    provenance: dict[str, Any] = {}
    for key in (
        "enabled_at",
        "key_created_at",
        "key_fingerprint_sha256",
        "since",
        "checked_at",
    ):
        if key in raw:
            provenance[key] = raw[key]
    if "since" in provenance:
        provenance["since_label"] = _format_since(provenance["since"])
    return provenance


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


def _operation_for_service(service: str) -> dict[str, Any] | None:
    with _REGISTRY_LOCK:
        now = time.monotonic()
        _sweep_operations_locked(now)
        entry = _REGISTRY.get(service)
        return _operation_payload(entry, now) if entry is not None else None


def _service_status(service: str) -> dict[str, Any]:
    if service == SERVICE_SCOUT:
        resting = service_status.scout_status()
        state = str(resting["state"])
        return {
            "service": SERVICE_SCOUT,
            "state": state,
            "guidance": resting.get("guidance"),
            "provenance": _scout_provenance(),
            "actions": _scout_actions(state),
            "operation": _operation_for_service(SERVICE_SCOUT),
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
            "operation": _operation_for_service(SERVICE_SPL),
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
        return _start_operation(
            SERVICE_SCOUT,
            "enable",
            lambda opener: run_scout_handoff(refresh=False, open_browser=opener),
        )
    return _start_operation(
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
    return _start_operation(
        SERVICE_SCOUT,
        "refresh",
        lambda opener: run_scout_handoff(refresh=True, open_browser=opener),
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
