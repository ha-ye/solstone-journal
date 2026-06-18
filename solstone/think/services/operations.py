# SPDX-License-Identifier: AGPL-3.0-only
# Copyright (c) 2026 sol pbc

"""Shared optional-service operation registry and handoff result helpers."""

from __future__ import annotations

import logging
import threading
import time
from collections.abc import Callable
from dataclasses import dataclass
from typing import Any

from solstone.think.services import outcomes

logger = logging.getLogger(__name__)

RETRYABLE_CODES = frozenset(
    {outcomes.EXPIRED, outcomes.NETWORK_ERROR, outcomes.LOCAL_ERROR}
)
OPERATION_GRACE_SECONDS = 30.0
# Phases at which an operation is finished — no actionable consent CTA should
# be surfaced (the portal page is already satisfied or moot). Mirrors the JS
# PRIVATE_LINK_TERMINAL_PHASES set in solstone/apps/link/workspace.html — the
# two sit on opposite sides of the Python/JS boundary with no shared source,
# so keep them in lockstep.
TERMINAL_PHASES = frozenset({"enabled", "needs_subscription", "revoked", "error"})


class OperationBusyError(RuntimeError):
    """Raised when a service already has an active operation."""


@dataclass(frozen=True)
class HandoffResult:
    phase: str
    guidance: str | None
    retryable: bool
    subscribe_url: str | None = None


@dataclass
class OperationEntry:
    service: str
    kind: str
    phase: str
    guidance: str | None
    retryable: bool
    portal_url: str | None
    started_monotonic: float
    ended_monotonic: float | None = None
    subscribe_url: str | None = None


_REGISTRY_LOCK = threading.Lock()
_REGISTRY: dict[str, OperationEntry] = {}


def clear_registry() -> None:
    """Clear process-local operation state for tests."""

    with _REGISTRY_LOCK:
        _REGISTRY.clear()


def _outcome_result(
    code: str,
    guidance: str | None,
    subscribe_url: str | None = None,
) -> HandoffResult:
    if code == outcomes.APPROVED:
        return HandoffResult("enabled", None, False)
    if code == outcomes.PENDING:
        return HandoffResult("pending", guidance, False)
    if code == outcomes.REVOKED:
        return HandoffResult("revoked", guidance, False)
    if code == outcomes.NEEDS_SUBSCRIPTION:
        return HandoffResult(
            "needs_subscription",
            guidance,
            False,
            subscribe_url=subscribe_url,
        )
    return HandoffResult(
        "error",
        guidance,
        code in RETRYABLE_CODES,
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
    portal_url = None if entry.phase in TERMINAL_PHASES else entry.portal_url
    return {
        "kind": entry.kind,
        "phase": entry.phase,
        "guidance": entry.guidance,
        "retryable": entry.retryable,
        "portal_url": portal_url,
        "subscribe_url": entry.subscribe_url,
        "elapsed_ms": int(max(0.0, ts - entry.started_monotonic) * 1000),
    }


def _update_entry_from_result(
    entry: OperationEntry,
    result: HandoffResult,
) -> None:
    with _REGISTRY_LOCK:
        current = _REGISTRY.get(entry.service)
        if current is not entry:
            return
        entry.phase = result.phase
        entry.guidance = result.guidance
        entry.retryable = result.retryable
        entry.subscribe_url = result.subscribe_url
        entry.ended_monotonic = time.monotonic()


def _run_operation(
    entry: OperationEntry,
    flow: Callable[[], HandoffResult],
) -> None:
    with _REGISTRY_LOCK:
        current = _REGISTRY.get(entry.service)
        if current is entry:
            entry.phase = "waiting"
    try:
        result = flow()
    except Exception:
        logger.exception("service operation failed")
        result = HandoffResult(
            phase="error",
            guidance=None,
            retryable=True,
        )
    _update_entry_from_result(entry, result)


def start_operation(
    service: str,
    kind: str,
    portal_url: str | None,
    flow: Callable[[], HandoffResult],
) -> dict[str, Any]:
    with _REGISTRY_LOCK:
        now = time.monotonic()
        _sweep_operations_locked(now)
        if _active_operation_locked(service) is not None:
            raise OperationBusyError(service)
        entry = OperationEntry(
            service=service,
            kind=kind,
            phase="starting",
            guidance=None,
            retryable=False,
            portal_url=portal_url,
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
    return operation


def operation_for_service(service: str) -> dict[str, Any] | None:
    with _REGISTRY_LOCK:
        now = time.monotonic()
        _sweep_operations_locked(now)
        entry = _REGISTRY.get(service)
        return _operation_payload(entry, now) if entry is not None else None
