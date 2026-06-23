# SPDX-License-Identifier: AGPL-3.0-only
# Copyright (c) 2026 sol pbc

"""Presentation-neutral spb consent handoff flow."""

from __future__ import annotations

import time
from collections.abc import Callable
from dataclasses import dataclass
from typing import Any

from solstone.think.backup.hosted import HostedBinding
from solstone.think.link.paths import LinkState
from solstone.think.services import outcomes, portal_client
from solstone.think.services.constants import SERVICE_BACKUP

_BINDING_FIELDS = (
    "broker_endpoint",
    "account_id",
    "instance_id",
    "bucket",
    "prefix",
    "broker_token",
)


class MalformedConsent(ValueError):
    """Raised when the spb consent payload violates the wire contract."""


@dataclass(frozen=True)
class SpbHandoffResult:
    state: str
    binding: HostedBinding | None
    subscribe_url: str | None
    reason_code: str | None


def build_spb_handoff_url() -> tuple[str, str, str]:
    """Resolve the link instance, mint a nonce, and build the spb consent URL.

    Returns ``(consent_url, nonce, base_url)``.
    """

    instance_id = LinkState.load_or_create().instance_id
    return portal_client.build_consent_url(SERVICE_BACKUP, instance=instance_id)


def _non_blank_string(payload: dict[str, Any], key: str) -> str | None:
    value = payload.get(key)
    if not isinstance(value, str) or not value.strip():
        return None
    return value


def _binding_from_payload(payload: dict[str, Any]) -> HostedBinding:
    values = {key: _non_blank_string(payload, key) for key in _BINDING_FIELDS}
    if any(value is None for value in values.values()):
        raise MalformedConsent("spb consent payload missing hosted binding field")
    return HostedBinding(
        broker_endpoint=values["broker_endpoint"] or "",
        account_id=values["account_id"] or "",
        instance_id=values["instance_id"] or "",
        bucket=values["bucket"] or "",
        prefix=values["prefix"] or "",
        broker_token=values["broker_token"] or "",
    )


def _classify_spb_payload(payload: dict[str, Any]) -> SpbHandoffResult:
    status = payload.get("status")
    if status == outcomes.APPROVED:
        return SpbHandoffResult(
            outcomes.APPROVED,
            _binding_from_payload(payload),
            None,
            None,
        )
    if status == outcomes.NEEDS_SUBSCRIPTION:
        subscribe_url = payload.get("subscribe_url")
        if not isinstance(subscribe_url, str) or not subscribe_url.startswith(
            "https://"
        ):
            raise MalformedConsent("spb consent payload missing subscribe_url")
        return SpbHandoffResult(
            outcomes.NEEDS_SUBSCRIPTION,
            None,
            subscribe_url,
            None,
        )
    raise MalformedConsent("unsupported spb consent status")


def _failed_result(reason: str | None) -> SpbHandoffResult:
    if reason is None:
        code = outcomes.MALFORMED
    else:
        try:
            code = outcomes.outcome_from_token(reason).code
        except ValueError:
            code = outcomes.MALFORMED
    return SpbHandoffResult(code, None, None, code)


def enable_spb_via_consent(
    *,
    base_url: str,
    nonce: str,
    wait_seconds: int = portal_client.DEFAULT_WAIT_SECONDS,
    poll_once: Callable[..., portal_client.PollOutcome] | None = None,
    clock: Callable[[], float] = time.monotonic,
) -> SpbHandoffResult:
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
            service=SERVICE_BACKUP,
        )
        if outcome.kind == "continue":
            continue
        if outcome.kind == "failed":
            return _failed_result(outcome.reason)

        payload = outcome.payload
        if not isinstance(payload, dict):
            return SpbHandoffResult(
                outcomes.MALFORMED,
                None,
                None,
                outcomes.MALFORMED,
            )
        try:
            return _classify_spb_payload(payload)
        except MalformedConsent:
            return SpbHandoffResult(
                outcomes.MALFORMED,
                None,
                None,
                outcomes.MALFORMED,
            )

    return SpbHandoffResult(outcomes.EXPIRED, None, None, outcomes.EXPIRED)


def run_spb_handoff(
    *,
    nonce: str,
    base_url: str,
    poll_once: Callable[
        ..., portal_client.PollOutcome
    ] = portal_client.poll_handoff_once,
    clock: Callable[[], float] = time.monotonic,
    wait_seconds: int = portal_client.DEFAULT_WAIT_SECONDS,
) -> SpbHandoffResult:
    """Run the spb consent flow synchronously for route/thread callers."""

    return enable_spb_via_consent(
        base_url=base_url,
        nonce=nonce,
        poll_once=poll_once,
        clock=clock,
        wait_seconds=wait_seconds,
    )


__all__ = [
    "MalformedConsent",
    "SpbHandoffResult",
    "_classify_spb_payload",
    "build_spb_handoff_url",
    "enable_spb_via_consent",
    "run_spb_handoff",
]
