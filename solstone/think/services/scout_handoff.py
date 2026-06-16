# SPDX-License-Identifier: AGPL-3.0-only
# Copyright (c) 2026 sol pbc

"""Scout consent handoff runner."""

from __future__ import annotations

import logging
import time
from collections.abc import Callable

from solstone.think.services import operations, outcomes, portal_client, scout

logger = logging.getLogger(__name__)

SERVICE_SCOUT = "scout"


def build_scout_handoff_url() -> tuple[str, str, str]:
    """Mint a nonce and build the scout consent URL.

    Returns ``(consent_url, nonce, base_url)``.
    """

    return portal_client.build_consent_url(SERVICE_SCOUT)


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


def run_scout_handoff(
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
    """Run the scout consent flow synchronously for route/thread callers."""

    _ = refresh

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
                )
            return _handoff_error_result(
                "unexpected_payload",
                detail=outcome.detail,
            )
        if outcome.kind != "success":
            return _handoff_error_result("unexpected_payload")

        try:
            result = scout.apply_scout_state(outcome.payload or {})
        except scout.ScoutPayloadError as exc:
            return _handoff_error_result(
                exc.token,
                detail=exc.detail,
            )
        except scout.JournalNotInitializedError:
            return _handoff_error_result("journal_not_initialized")
        except Exception:
            logger.exception("scout handoff write failed")
            return _handoff_error_result("write_failed")

        if result.kind == "approved":
            guidance = None
            phase = "enabled"
        elif result.kind == "pending":
            guidance = outcomes.GUIDANCE[outcomes.PENDING]
            phase = "pending"
        else:
            guidance = outcomes.GUIDANCE[outcomes.REVOKED]
            phase = "revoked"
        return operations.HandoffResult(
            phase=phase,
            guidance=guidance,
            retryable=False,
        )

    return _handoff_error_result("consent_timeout")
