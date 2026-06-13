# SPDX-License-Identifier: AGPL-3.0-only
# Copyright (c) 2026 sol pbc

"""Scout browser-consent handoff runner."""

from __future__ import annotations

import logging
import time
from collections.abc import Callable

from solstone.think.services import operations, outcomes, portal_client, scout

logger = logging.getLogger(__name__)

SERVICE_SCOUT = "scout"


def _handoff_error_result(
    token: str,
    *,
    detail: str | None = None,
    browser_open_succeeded: bool | None,
    portal_url: str | None,
) -> operations.HandoffResult:
    try:
        outcome = outcomes.outcome_from_token(token, detail=detail)
    except ValueError:
        outcome = outcomes.outcome_for_code(outcomes.LOCAL_ERROR, detail=detail)
    return operations._outcome_result(
        outcome.code,
        outcome.guidance,
        browser_open_succeeded,
        portal_url,
    )


def run_scout_handoff(
    *,
    refresh: bool,
    poll_once: Callable[
        ..., portal_client.PollOutcome
    ] = portal_client.poll_handoff_once,
    open_browser: Callable[[str], bool] = operations._open_browser,
    clock: Callable[[], float] = time.monotonic,
    wait_seconds: int = portal_client.DEFAULT_WAIT_SECONDS,
) -> operations.HandoffResult:
    """Run the scout browser-consent flow synchronously for route/thread callers."""

    _ = refresh
    base_url = portal_client.portal_base_url()
    nonce = portal_client.mint_nonce()
    browser_url = portal_client.browser_url(base_url, nonce, service=SERVICE_SCOUT)
    browser_open_succeeded, manual_url = operations.open_for_handoff(
        browser_url, open_browser
    )

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
        return operations.HandoffResult(
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
