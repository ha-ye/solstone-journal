# SPDX-License-Identifier: AGPL-3.0-only
# Copyright (c) 2026 sol pbc

"""Presentation-neutral spl browser-consent handoff flow."""

from __future__ import annotations

import logging
import time
import webbrowser
from collections.abc import Callable
from typing import Any

from solstone.think.services import outcomes, portal_client, spl
from solstone.think.services.constants import SERVICE_SPL

log = logging.getLogger(__name__)

_STATES = frozenset({outcomes.APPROVED, outcomes.PENDING, outcomes.REVOKED})
_COMMON_KEYS = frozenset({"service", "state"})
_APPROVED_KEYS = frozenset({"service", "state", "approved_at"})


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

    allowed = _APPROVED_KEYS if state == outcomes.APPROVED else _COMMON_KEYS
    if set(payload) - allowed:
        raise MalformedConsent("spl consent payload includes unsupported fields")

    if state == outcomes.APPROVED and not _is_approved_at(payload.get("approved_at")):
        raise MalformedConsent("spl consent payload missing approved_at")
    return str(state)


def enable_spl_via_consent(
    *,
    base_url: str | None = None,
    wait_seconds: int = portal_client.DEFAULT_WAIT_SECONDS,
    open_browser: Callable[[str], bool] | None = None,
    poll_once: Callable[..., portal_client.PollOutcome] | None = None,
    clock: Callable[[], float] = time.monotonic,
) -> outcomes.HandoffOutcome:
    resolved_base_url = base_url or portal_client.portal_base_url()
    nonce = portal_client.mint_nonce()
    browser_url = portal_client.browser_url(
        resolved_base_url,
        nonce,
        service=SERVICE_SPL,
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
