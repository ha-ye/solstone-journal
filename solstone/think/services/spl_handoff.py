# SPDX-License-Identifier: AGPL-3.0-only
# Copyright (c) 2026 sol pbc

"""Presentation-neutral spl browser-consent handoff flow."""

from __future__ import annotations

import logging
import time
import webbrowser
from collections.abc import Callable
from typing import Any

from solstone.think.link.paths import LinkState
from solstone.think.services import operations, outcomes, portal_client, spl
from solstone.think.services.constants import SERVICE_SPL

log = logging.getLogger(__name__)

_STATES = frozenset(
    {
        outcomes.APPROVED,
        outcomes.PENDING,
        outcomes.REVOKED,
        outcomes.NEEDS_SUBSCRIPTION,
    }
)
_COMMON_KEYS = frozenset({"service", "state"})
_APPROVED_KEYS = frozenset({"service", "state", "approved_at"})
_NEEDS_SUBSCRIPTION_KEYS = frozenset({"service", "state", "subscribe_url"})


class MalformedConsent(ValueError):
    """Raised when the spl consent payload violates the wire contract."""


def _open_browser(url: str) -> bool:
    try:
        return bool(webbrowser.open(url, new=2))
    except Exception as exc:
        log.warning("spl browser open failed: %s", exc)
        return False


def _is_approved_at(value: Any) -> bool:
    return isinstance(value, (int, float, str)) and not isinstance(value, bool)


def _classify_spl_payload(payload: dict[str, Any]) -> str:
    state = payload.get("state")
    if state not in _STATES:
        raise MalformedConsent("unsupported spl consent state")
    if payload.get("service") != SERVICE_SPL:
        raise MalformedConsent("spl consent payload service mismatch")

    if state == outcomes.APPROVED:
        allowed = _APPROVED_KEYS
    elif state == outcomes.NEEDS_SUBSCRIPTION:
        allowed = _NEEDS_SUBSCRIPTION_KEYS
    else:
        allowed = _COMMON_KEYS
    if set(payload) - allowed:
        raise MalformedConsent("spl consent payload includes unsupported fields")

    if state == outcomes.APPROVED and not _is_approved_at(payload.get("approved_at")):
        raise MalformedConsent("spl consent payload missing approved_at")
    if state == outcomes.NEEDS_SUBSCRIPTION:
        subscribe_url = payload.get("subscribe_url")
        if not isinstance(subscribe_url, str) or not subscribe_url.startswith(
            "https://"
        ):
            raise MalformedConsent("spl consent payload missing subscribe_url")
    return str(state)


def enable_spl_via_consent(
    *,
    base_url: str | None = None,
    instance_id: str | None = None,
    wait_seconds: int = portal_client.DEFAULT_WAIT_SECONDS,
    open_browser: Callable[[str], bool] | None = None,
    poll_once: Callable[..., portal_client.PollOutcome] | None = None,
    clock: Callable[[], float] = time.monotonic,
) -> outcomes.HandoffOutcome:
    resolved_base_url = base_url or portal_client.portal_base_url()
    nonce = portal_client.mint_nonce()
    if instance_id is None:
        try:
            instance_id = LinkState.load_or_create().instance_id
        except OSError:
            log.warning("spl instance id resolution failed", exc_info=True)
            return outcomes.outcome_for_code(outcomes.LOCAL_ERROR)
    browser_url = portal_client.browser_url(
        resolved_base_url,
        nonce,
        service=SERVICE_SPL,
        instance=instance_id,
    )
    opener = open_browser or _open_browser
    try:
        opener(browser_url)
    except Exception as exc:
        log.warning("spl browser open failed: %s", exc)

    poll = poll_once or portal_client.poll_handoff_once
    deadline = clock() + wait_seconds
    while clock() < deadline:
        timeout = min(
            portal_client.POLL_TIMEOUT_SECONDS,
            max(0.1, deadline - clock()),
        )
        outcome = poll(
            resolved_base_url,
            nonce,
            timeout=timeout,
            component="switchboard",
            service=SERVICE_SPL,
        )
        if outcome.kind == "continue":
            continue
        if outcome.kind == "failed":
            if outcome.reason:
                return outcomes.outcome_from_token(
                    outcome.reason,
                    detail=outcome.detail,
                )
            return outcomes.outcome_for_code(outcomes.MALFORMED, detail=outcome.detail)

        payload = outcome.payload or {}
        if not isinstance(payload, dict):
            return outcomes.outcome_for_code(outcomes.MALFORMED)
        try:
            state = _classify_spl_payload(payload)
        except MalformedConsent as exc:
            return outcomes.outcome_for_code(outcomes.MALFORMED, detail=str(exc))

        if state == outcomes.PENDING:
            continue
        if state == outcomes.REVOKED:
            return outcomes.outcome_for_code(outcomes.REVOKED)
        if state == outcomes.NEEDS_SUBSCRIPTION:
            return outcomes.outcome_for_code(
                outcomes.NEEDS_SUBSCRIPTION,
                detail=payload["subscribe_url"],
            )

        try:
            spl.enable_spl()
        except spl.RelayUnreachableError:
            return outcomes.outcome_for_code(outcomes.NETWORK_ERROR)
        except (
            spl.RelayResponseError,
            spl.JournalNotInitializedError,
        ):
            return outcomes.outcome_for_code(outcomes.LOCAL_ERROR)
        except Exception:
            return outcomes.outcome_for_code(outcomes.LOCAL_ERROR)
        return outcomes.outcome_for_code(outcomes.APPROVED)

    return outcomes.outcome_for_code(outcomes.EXPIRED)


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

    outcome = enable_spl_via_consent(
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
        subscribe_url=outcome.detail,
    )
