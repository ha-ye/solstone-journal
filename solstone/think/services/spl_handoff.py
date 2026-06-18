# SPDX-License-Identifier: AGPL-3.0-only
# Copyright (c) 2026 sol pbc

"""Presentation-neutral spl consent handoff flow."""

from __future__ import annotations

import time
from collections.abc import Callable
from typing import Any

from solstone.think.link.paths import LinkState
from solstone.think.services import operations, outcomes, portal_client, spl
from solstone.think.services.constants import SERVICE_SPL

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


def build_spl_handoff_url() -> tuple[str, str, str]:
    """Resolve the link instance, mint a nonce, and build the spl consent URL.

    Returns ``(consent_url, nonce, base_url)``.
    """

    instance_id = LinkState.load_or_create().instance_id
    return portal_client.build_consent_url(SERVICE_SPL, instance=instance_id)


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
    base_url: str,
    nonce: str,
    wait_seconds: int = portal_client.DEFAULT_WAIT_SECONDS,
    poll_once: Callable[..., portal_client.PollOutcome] | None = None,
    clock: Callable[[], float] = time.monotonic,
) -> outcomes.HandoffOutcome:
    poll = poll_once or portal_client.poll_handoff_once
    deadline = clock() + wait_seconds
    while clock() < deadline:
        timeout = min(
            portal_client.POLL_TIMEOUT_SECONDS,
            max(0.1, deadline - clock()),
        )
        outcome = poll(
            base_url,
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
    nonce: str,
    base_url: str,
    poll_once: Callable[
        ..., portal_client.PollOutcome
    ] = portal_client.poll_handoff_once,
    clock: Callable[[], float] = time.monotonic,
    wait_seconds: int = portal_client.DEFAULT_WAIT_SECONDS,
) -> operations.HandoffResult:
    """Run the spl consent flow synchronously for route/thread callers."""

    outcome = enable_spl_via_consent(
        base_url=base_url,
        nonce=nonce,
        poll_once=poll_once,
        clock=clock,
        wait_seconds=wait_seconds,
    )
    return operations._outcome_result(
        outcome.code,
        outcome.guidance,
        subscribe_url=outcome.detail,
    )
