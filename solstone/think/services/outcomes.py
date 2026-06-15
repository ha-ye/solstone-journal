# SPDX-License-Identifier: AGPL-3.0-only
# Copyright (c) 2026 sol pbc

"""Presentation-neutral service handoff outcome taxonomy."""

from __future__ import annotations

import logging
from dataclasses import dataclass

log = logging.getLogger(__name__)

APPROVED = "approved"
PENDING = "pending"
REVOKED = "revoked"
EXPIRED = "expired"
MALFORMED = "malformed"
NETWORK_ERROR = "network_error"
LOCAL_ERROR = "local_error"
NEEDS_SUBSCRIPTION = "needs_subscription"

CODES = frozenset(
    {
        APPROVED,
        PENDING,
        REVOKED,
        EXPIRED,
        MALFORMED,
        NETWORK_ERROR,
        LOCAL_ERROR,
        NEEDS_SUBSCRIPTION,
    }
)

GUIDANCE: dict[str, str | None] = {
    APPROVED: None,
    PENDING: "Keep the browser open while the request finishes.",
    REVOKED: "Consent was not granted. Start a new enable flow when ready.",
    EXPIRED: "This enable link is no longer active. Start a new enable flow.",
    MALFORMED: (
        "The service response was not understood. Update solstone and try again."
    ),
    NETWORK_ERROR: (
        "The service could not be reached. Check network access and try again."
    ),
    LOCAL_ERROR: (
        "Local service state could not be written. "
        "Check journal permissions and try again."
    ),
    NEEDS_SUBSCRIPTION: (
        "private link needs an active subscription before it can turn on. "
        "your consent is saved; set one up, then enable private link again."
    ),
}

TOKEN_TO_CODE: dict[str, str] = {
    "consent_link_expired": EXPIRED,
    "consent_timeout": EXPIRED,
    "nonce_invalid": MALFORMED,
    "unexpected_payload": MALFORMED,
    "scout_server_bad_payload": MALFORMED,
    "portal_unreachable": NETWORK_ERROR,
    "tls_verification_failed": NETWORK_ERROR,
    "relay_unreachable": NETWORK_ERROR,
    "write_failed": LOCAL_ERROR,
    "journal_not_initialized": LOCAL_ERROR,
}

OUT_OF_DOMAIN_TOKENS = frozenset(
    {
        "already_enabled",
        "manual_key_present",
        "already_disabled",
        "spl_already_enabled",
        "spl_already_disabled",
        "unknown_service",
    }
)


@dataclass(frozen=True)
class HandoffOutcome:
    code: str
    guidance: str | None
    detail: str | None = None


def outcome_for_code(code: str, *, detail: str | None = None) -> HandoffOutcome:
    if code not in GUIDANCE:
        raise ValueError(f"unsupported handoff outcome code: {code!r}")
    return HandoffOutcome(code=code, guidance=GUIDANCE[code], detail=detail)


def outcome_from_token(token: str, *, detail: str | None = None) -> HandoffOutcome:
    if token in OUT_OF_DOMAIN_TOKENS:
        raise ValueError(f"token is not a handoff outcome: {token!r}")
    code = TOKEN_TO_CODE.get(token)
    if code is None:
        log.error("unmapped handoff outcome token: %s", token)
        code = LOCAL_ERROR
        detail = detail or token
    return outcome_for_code(code, detail=detail)
