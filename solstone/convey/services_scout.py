# SPDX-License-Identifier: AGPL-3.0-only
# Copyright (c) 2026 sol pbc

"""Convey backend orchestration for enabling the scout service."""

from __future__ import annotations

import json
import logging
import queue
import secrets
import threading
import time
import webbrowser
from dataclasses import dataclass, field
from typing import Any

from flask import Blueprint, Response, abort, jsonify, request, stream_with_context

from solstone.think.services import portal_client
from solstone.think.services.scout import (
    JournalNotInitializedError,
    is_manual_key_present,
    is_scout_enabled,
    provision_scout_handoff,
)

logger = logging.getLogger(__name__)

bp = Blueprint("services_scout", __name__, url_prefix="/init/services/scout")

WALL_CLOCK_BUDGET_SECONDS = 900
TRANSIENT_RETRY_BACKOFF_SECONDS = 5
GRACE_SECONDS = 60
SSE_HEARTBEAT_SECONDS = 20

_TERMINAL_EVENTS = frozenset({"scout-enabled", "failed", "timeout"})
_REGISTRY_LOCK = threading.Lock()


@dataclass
class OrchestratorEntry:
    nonce: str
    nonce_id: str
    portal_url: str
    start_time_monotonic: float
    event_queue: queue.Queue[tuple[str, dict[str, Any]]] = field(
        default_factory=queue.Queue
    )
    browser_open_attempted: bool = False
    browser_open_succeeded: bool | None = None
    terminal_event: tuple[str, dict[str, Any]] | None = None
    cleanup_at_monotonic: float | None = None


_REGISTRY: dict[str, OrchestratorEntry] = {}


def _sweep_expired_locked() -> None:
    now = time.monotonic()
    expired = [
        nonce_id
        for nonce_id, entry in _REGISTRY.items()
        if entry.cleanup_at_monotonic is not None and entry.cleanup_at_monotonic < now
    ]
    for nonce_id in expired:
        _REGISTRY.pop(nonce_id, None)


def _active_entry_locked() -> OrchestratorEntry | None:
    now = time.monotonic()
    for entry in _REGISTRY.values():
        if entry.terminal_event is not None:
            continue
        if entry.start_time_monotonic + WALL_CLOCK_BUDGET_SECONDS > now:
            return entry
    return None


def _entry_response(entry: OrchestratorEntry) -> dict[str, str]:
    return {
        "nonce_id": entry.nonce_id,
        "subscribe_url": f"/init/services/scout/status?nonce_id={entry.nonce_id}",
        "portal_url": entry.portal_url,
    }


def _record_terminal(
    entry: OrchestratorEntry,
    name: str,
    data: dict[str, Any],
) -> None:
    with _REGISTRY_LOCK:
        if entry.terminal_event is not None:
            return
        entry.terminal_event = (name, data)
    entry.event_queue.put((name, data))
    with _REGISTRY_LOCK:
        entry.cleanup_at_monotonic = time.monotonic() + GRACE_SECONDS


def _sse_frame(name: str, data: dict[str, Any]) -> str:
    return f"event: {name}\ndata: {json.dumps(data)}\n\n"


def _run_orchestrator(entry: OrchestratorEntry, base_url: str) -> None:
    try:
        entry.browser_open_attempted = True
        try:
            entry.browser_open_succeeded = bool(
                webbrowser.open(entry.portal_url, new=2)
            )
        except Exception as exc:
            logger.warning("scout orchestrator browser open failed: %s", exc)
            entry.browser_open_succeeded = False

        start_time = entry.start_time_monotonic
        while True:
            elapsed = time.monotonic() - start_time
            if elapsed >= WALL_CLOCK_BUDGET_SECONDS:
                _record_terminal(
                    entry,
                    "timeout",
                    {"elapsed_ms": int(elapsed * 1000)},
                )
                return

            outcome = portal_client.poll_handoff_once(
                base_url,
                entry.nonce,
                component="convey",
            )
            if outcome.kind == "success":
                payload = outcome.payload or {}
                try:
                    provision_scout_handoff(payload)
                except ValueError as exc:
                    _record_terminal(
                        entry,
                        "failed",
                        {"reason": "unexpected_payload", "detail": str(exc)},
                    )
                    return
                except JournalNotInitializedError:
                    _record_terminal(
                        entry,
                        "failed",
                        {"reason": "journal_not_initialized", "detail": None},
                    )
                    return
                except Exception as exc:
                    logger.exception("scout provision write_failed")
                    _record_terminal(
                        entry,
                        "failed",
                        {"reason": "write_failed", "detail": str(exc)},
                    )
                    return
                _record_terminal(
                    entry,
                    "scout-enabled",
                    {"account_id": str(payload["account_id"])},
                )
                return

            if outcome.kind == "failed":
                if outcome.reason == "portal_unreachable":
                    time.sleep(TRANSIENT_RETRY_BACKOFF_SECONDS)
                    continue
                if outcome.reason in {
                    "consent_link_expired",
                    "nonce_invalid",
                    "tls_verification_failed",
                    "unexpected_payload",
                }:
                    _record_terminal(
                        entry,
                        "failed",
                        {"reason": outcome.reason, "detail": outcome.detail},
                    )
                    return
                _record_terminal(
                    entry,
                    "failed",
                    {"reason": "write_failed", "detail": outcome.detail},
                )
                return
    except Exception as exc:
        logger.exception("scout orchestrator internal error")
        _record_terminal(
            entry,
            "failed",
            {"reason": "internal_error", "detail": str(exc)},
        )
    finally:
        if entry.terminal_event is None:
            _record_terminal(
                entry,
                "failed",
                {
                    "reason": "internal_error",
                    "detail": "orchestrator exited without terminal",
                },
            )


@bp.route("/start", methods=["POST"])
def start() -> tuple[Response, int] | Response:
    from solstone.convey.root import _is_setup_complete

    if _is_setup_complete():
        abort(404)
    if is_scout_enabled():
        return jsonify({"error": "already_enabled"}), 409
    if is_manual_key_present():
        return jsonify({"error": "manual_key_present"}), 409

    with _REGISTRY_LOCK:
        _sweep_expired_locked()
        active = _active_entry_locked()
        if active is not None:
            return jsonify(_entry_response(active)), 202

        base_url = portal_client.portal_base_url()
        nonce = portal_client.mint_nonce()
        nonce_id = secrets.token_urlsafe(8)
        portal_url = portal_client.browser_url(base_url, nonce)
        entry = OrchestratorEntry(
            nonce=nonce,
            nonce_id=nonce_id,
            portal_url=portal_url,
            start_time_monotonic=time.monotonic(),
        )
        _REGISTRY[nonce_id] = entry

    thread = threading.Thread(
        target=_run_orchestrator,
        args=(entry, base_url),
        name=f"scout-orchestrator-{nonce_id}",
        daemon=True,
    )
    thread.start()

    return jsonify(_entry_response(entry)), 202


@bp.route("/status", methods=["GET"])
def status() -> Response:
    from solstone.convey.root import _is_setup_complete

    if _is_setup_complete():
        abort(404)
    nonce_id = request.args.get("nonce_id", "")
    with _REGISTRY_LOCK:
        _sweep_expired_locked()
        entry = _REGISTRY.get(nonce_id)
    if entry is None:
        abort(404)

    def generate():
        disconnect_event = request.environ.get("pl.disconnect_event")

        def disconnected() -> bool:
            is_set = getattr(disconnect_event, "is_set", None)
            return bool(is_set is not None and is_set())

        yield _sse_frame(
            "subscribed",
            {
                "nonce_id": entry.nonce_id,
                "portal_url": entry.portal_url,
                "browser_open_attempted": entry.browser_open_attempted,
                "browser_open_succeeded": entry.browser_open_succeeded,
            },
        )

        terminal_event = entry.terminal_event
        if terminal_event is not None:
            name, data = terminal_event
            yield _sse_frame(name, data)
            return

        next_heartbeat_at = time.monotonic() + SSE_HEARTBEAT_SECONDS
        while True:
            if disconnected():
                return
            timeout = max(0.0, next_heartbeat_at - time.monotonic())
            if disconnect_event is not None:
                timeout = min(timeout, 0.1)
            try:
                name, data = entry.event_queue.get(timeout=timeout)
            except queue.Empty:
                if disconnected():
                    return
                if time.monotonic() < next_heartbeat_at:
                    continue
                elapsed_ms = int((time.monotonic() - entry.start_time_monotonic) * 1000)
                yield _sse_frame("waiting", {"elapsed_ms": elapsed_ms})
                next_heartbeat_at = time.monotonic() + SSE_HEARTBEAT_SECONDS
                continue
            yield _sse_frame(name, data)
            next_heartbeat_at = time.monotonic() + SSE_HEARTBEAT_SECONDS
            if name in _TERMINAL_EVENTS:
                return

    return Response(
        stream_with_context(generate()),
        mimetype="text/event-stream",
        headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"},
    )
