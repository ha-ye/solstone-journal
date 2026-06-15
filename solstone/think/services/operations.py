# SPDX-License-Identifier: AGPL-3.0-only
# Copyright (c) 2026 sol pbc

"""Shared optional-service operation registry and handoff result helpers."""

from __future__ import annotations

import logging
import threading
import time
import webbrowser
from collections.abc import Callable
from dataclasses import dataclass
from typing import Any

from solstone.think.services import outcomes

logger = logging.getLogger(__name__)

RETRYABLE_CODES = frozenset(
    {outcomes.EXPIRED, outcomes.NETWORK_ERROR, outcomes.LOCAL_ERROR}
)
OPERATION_GRACE_SECONDS = 30.0


class OperationBusyError(RuntimeError):
    """Raised when a service already has an active operation."""


@dataclass(frozen=True)
class HandoffResult:
    phase: str
    guidance: str | None
    retryable: bool
    browser_open_succeeded: bool | None
    portal_url: str | None
    subscribe_url: str | None = None


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
    subscribe_url: str | None = None


_REGISTRY_LOCK = threading.Lock()
_REGISTRY: dict[str, OperationEntry] = {}


def clear_registry() -> None:
    """Clear process-local operation state for tests."""

    with _REGISTRY_LOCK:
        _REGISTRY.clear()


def _open_browser(url: str) -> bool:
    try:
        return bool(webbrowser.open(url, new=2))
    except Exception as exc:
        logger.warning("service browser open failed: %s", exc)
        return False


def open_for_handoff(
    browser_url: str,
    open_browser: Callable[[str], bool],
) -> tuple[bool, str | None]:
    try:
        browser_open_succeeded = bool(open_browser(browser_url))
    except Exception as exc:
        logger.warning("service browser open failed: %s", exc)
        browser_open_succeeded = False
    return browser_open_succeeded, browser_url if not browser_open_succeeded else None


def _outcome_result(
    code: str,
    guidance: str | None,
    browser_open_succeeded: bool | None,
    portal_url: str | None,
    subscribe_url: str | None = None,
) -> HandoffResult:
    if code == outcomes.APPROVED:
        return HandoffResult("enabled", None, False, browser_open_succeeded, portal_url)
    if code == outcomes.PENDING:
        return HandoffResult(
            "pending", guidance, False, browser_open_succeeded, portal_url
        )
    if code == outcomes.REVOKED:
        return HandoffResult(
            "revoked", guidance, False, browser_open_succeeded, portal_url
        )
    if code == outcomes.NEEDS_SUBSCRIPTION:
        return HandoffResult(
            "needs_subscription",
            guidance,
            False,
            browser_open_succeeded,
            portal_url,
            subscribe_url=subscribe_url,
        )
    return HandoffResult(
        "error",
        guidance,
        code in RETRYABLE_CODES,
        browser_open_succeeded,
        portal_url,
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
        entry.browser_open_succeeded = result.browser_open_succeeded
        entry.portal_url = result.portal_url
        entry.subscribe_url = result.subscribe_url
        entry.ended_monotonic = time.monotonic()


def _tracked_opener(entry: OperationEntry) -> Callable[[str], bool]:
    def opener(url: str) -> bool:
        browser_open_succeeded, manual_url = open_for_handoff(url, _open_browser)
        with _REGISTRY_LOCK:
            current = _REGISTRY.get(entry.service)
            if current is entry:
                entry.browser_open_succeeded = browser_open_succeeded
                entry.portal_url = manual_url
        return browser_open_succeeded

    return opener


def _run_operation(
    entry: OperationEntry,
    flow: Callable[[Callable[[str], bool]], HandoffResult],
) -> None:
    with _REGISTRY_LOCK:
        current = _REGISTRY.get(entry.service)
        if current is entry:
            entry.phase = "waiting"
    try:
        result = flow(_tracked_opener(entry))
    except Exception:
        logger.exception("service operation failed")
        result = HandoffResult(
            phase="error",
            guidance=None,
            retryable=True,
            browser_open_succeeded=entry.browser_open_succeeded,
            portal_url=entry.portal_url,
        )
    _update_entry_from_result(entry, result)


def start_operation(
    service: str,
    kind: str,
    flow: Callable[[Callable[[str], bool]], HandoffResult],
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
    return operation


def operation_for_service(service: str) -> dict[str, Any] | None:
    with _REGISTRY_LOCK:
        now = time.monotonic()
        _sweep_operations_locked(now)
        entry = _REGISTRY.get(service)
        return _operation_payload(entry, now) if entry is not None else None
