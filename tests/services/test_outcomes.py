# SPDX-License-Identifier: AGPL-3.0-only
# Copyright (c) 2026 sol pbc

from __future__ import annotations

import logging

import pytest

from solstone.think.services import outcomes


def test_outcome_codes_are_machine_distinct() -> None:
    assert len(outcomes.CODES) == 12
    assert outcomes.CODES == {
        outcomes.APPROVED,
        outcomes.PENDING,
        outcomes.REVOKED,
        outcomes.EXPIRED,
        outcomes.MALFORMED,
        outcomes.NETWORK_ERROR,
        outcomes.LOCAL_ERROR,
        outcomes.NEEDS_SUBSCRIPTION,
        outcomes.RELAY_IDENTITY_CONFLICT,
        outcomes.RELAY_ROTATION_UNSUPPORTED,
        outcomes.RELAY_UNAVAILABLE,
        outcomes.RELAY_REJECTED,
    }


def test_guidance_is_complete_and_neutral() -> None:
    assert set(outcomes.GUIDANCE) == outcomes.CODES
    assert outcomes.GUIDANCE[outcomes.APPROVED] is None
    for code in outcomes.CODES - {outcomes.APPROVED}:
        assert outcomes.GUIDANCE[code]
    assert (
        "sol private link"
        not in " ".join(value or "" for value in outcomes.GUIDANCE.values()).lower()
    )


def test_handoff_token_sets_are_disjoint() -> None:
    assert set(outcomes.TOKEN_TO_CODE).isdisjoint(outcomes.OUT_OF_DOMAIN_TOKENS)


def test_token_map_targets_valid_codes() -> None:
    assert set(outcomes.TOKEN_TO_CODE.values()) <= outcomes.CODES


@pytest.mark.parametrize(
    ("token", "code"),
    [
        ("nonce_invalid", outcomes.MALFORMED),
        ("tls_verification_failed", outcomes.NETWORK_ERROR),
        ("consent_timeout", outcomes.EXPIRED),
        ("relay_unreachable", outcomes.NETWORK_ERROR),
    ],
)
def test_ambiguous_tokens_map_as_specified(token: str, code: str) -> None:
    assert outcomes.outcome_from_token(token).code == code


def test_out_of_domain_tokens_fail_loudly() -> None:
    with pytest.raises(ValueError, match="not a handoff outcome"):
        outcomes.outcome_from_token("already_enabled")


def test_unknown_token_defaults_to_local_error_and_logs(caplog) -> None:
    caplog.set_level(logging.ERROR)

    outcome = outcomes.outcome_from_token("new_unmapped_token")

    assert outcome.code == outcomes.LOCAL_ERROR
    assert outcome.detail == "new_unmapped_token"
    assert "unmapped handoff outcome token" in caplog.text


@pytest.mark.parametrize(
    ("status", "reason", "expected_code", "expected_guidance"),
    [
        (
            409,
            outcomes.RELAY_REASON_ALREADY_REGISTERED,
            outcomes.RELAY_IDENTITY_CONFLICT,
            "this solstone is already set up under a different identity. "
            "reach out to support to reset it, then try again.",
        ),
        (
            409,
            outcomes.RELAY_REASON_CA_MISMATCH,
            outcomes.RELAY_ROTATION_UNSUPPORTED,
            "this solstone's security key changed and can't be re-registered "
            "automatically yet. reach out to support.",
        ),
        (
            503,
            "not provisioned",
            outcomes.RELAY_UNAVAILABLE,
            "the private network service isn't available right now. "
            "try again in a bit.",
        ),
        (
            503,
            None,
            outcomes.RELAY_UNAVAILABLE,
            "the private network service isn't available right now. "
            "try again in a bit.",
        ),
        (
            400,
            None,
            outcomes.RELAY_REJECTED,
            "the relay couldn't finish setting up your private network (error 400).",
        ),
        (
            413,
            None,
            outcomes.RELAY_REJECTED,
            "the relay couldn't finish setting up your private network (error 413).",
        ),
        (
            409,
            "some other 409 reason",
            outcomes.RELAY_REJECTED,
            "the relay couldn't finish setting up your private network (error 409).",
        ),
        (
            502,
            None,
            outcomes.RELAY_REJECTED,
            "the relay couldn't finish setting up your private network (error 502).",
        ),
    ],
)
def test_relay_rejection_outcome_maps_status_and_reason(
    status: int, reason, expected_code: str, expected_guidance: str
) -> None:
    outcome = outcomes.relay_rejection_outcome(status=status, reason=reason)
    assert outcome.code == expected_code
    assert outcome.guidance == expected_guidance
    assert outcome.detail is not None
    assert str(status) in outcome.detail
    if reason is not None:
        assert reason in outcome.detail


def test_relay_rejection_409s_are_distinguishable() -> None:
    a = outcomes.relay_rejection_outcome(
        status=409, reason=outcomes.RELAY_REASON_ALREADY_REGISTERED
    )
    b = outcomes.relay_rejection_outcome(
        status=409, reason=outcomes.RELAY_REASON_CA_MISMATCH
    )
    assert a.code != b.code
    assert a.guidance != b.guidance
    assert a.detail != b.detail
    assert a.guidance != outcomes.GUIDANCE[outcomes.NETWORK_ERROR]
