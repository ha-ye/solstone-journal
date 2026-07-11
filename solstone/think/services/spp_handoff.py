# SPDX-License-Identifier: AGPL-3.0-only
# Copyright (c) 2026 sol pbc

"""Confidential processing consent handoff runner."""

from __future__ import annotations

import logging
import time
from collections.abc import Callable

from solstone.think.services import operations, outcomes, portal_client, spp
from solstone.think.services.constants import SERVICE_SPP

logger = logging.getLogger(__name__)

_NOT_VERIFIED_GUIDANCE = (
    "Hardware attestation is not yet verified. "
    "Thinking stays blocked until verification finishes."
)


def build_confidential_handoff_url() -> tuple[str, str, str]:
    """Mint a nonce and build the confidential processing consent URL.

    Returns ``(consent_url, nonce, base_url)``.
    """

    return portal_client.build_consent_url(SERVICE_SPP)


def _handoff_error_result(
    token: str,
    *,
    detail: str | None = None,
) -> operations.HandoffResult:
    try:
        outcome = outcomes.outcome_from_token(token, detail=detail)
    except ValueError:
        outcome = outcomes.outcome_for_code(outcomes.LOCAL_ERROR, detail=detail)
    return operations._outcome_result(outcome.code, outcome.guidance)


def run_confidential_handoff(
    *,
    refresh: bool,
    nonce: str,
    base_url: str,
    poll_once: Callable[
        ..., portal_client.PollOutcome
    ] = portal_client.poll_handoff_once,
    clock: Callable[[], float] = time.monotonic,
    wait_seconds: int = portal_client.DEFAULT_WAIT_SECONDS,
) -> operations.HandoffResult:
    """Run the confidential consent flow synchronously for route/thread callers."""

    _ = refresh

    deadline = clock() + wait_seconds
    while clock() < deadline:
        timeout = min(portal_client.POLL_TIMEOUT_SECONDS, max(0.1, deadline - clock()))
        outcome = poll_once(
            base_url,
            nonce,
            timeout=timeout,
            component="switchboard",
            service=SERVICE_SPP,
        )
        if outcome.kind == "continue":
            continue
        if outcome.kind == "failed":
            if outcome.reason:
                return _handoff_error_result(
                    outcome.reason,
                    detail=outcome.detail,
                )
            return _handoff_error_result(
                "unexpected_payload",
                detail=outcome.detail,
            )
        if outcome.kind != "success":
            return _handoff_error_result("unexpected_payload")

        try:
            spp.provision_confidential_handoff(outcome.payload or {})
        except ValueError as exc:
            return _handoff_error_result("unexpected_payload", detail=str(exc))
        except spp.JournalNotInitializedError:
            return _handoff_error_result("journal_not_initialized")
        except Exception:
            logger.exception("confidential handoff write failed")
            return _handoff_error_result("write_failed")

        return operations.HandoffResult(
            phase="enabled",
            guidance=_NOT_VERIFIED_GUIDANCE,
            retryable=False,
        )

    return _handoff_error_result("consent_timeout")
