# SPDX-License-Identifier: AGPL-3.0-only
# Copyright (c) 2026 sol pbc

from __future__ import annotations

import logging

import pytest

from solstone.think.services import cli, outcomes


def test_outcome_codes_are_machine_distinct() -> None:
    assert len(outcomes.CODES) == 7
    assert outcomes.CODES == {
        outcomes.APPROVED,
        outcomes.PENDING,
        outcomes.REVOKED,
        outcomes.EXPIRED,
        outcomes.MALFORMED,
        outcomes.NETWORK_ERROR,
        outcomes.LOCAL_ERROR,
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


def test_cli_tokens_are_all_accounted_for() -> None:
    accounted = set(outcomes.TOKEN_TO_CODE) | set(outcomes.OUT_OF_DOMAIN_TOKENS)
    assert set(cli.ERROR_MESSAGES) == accounted
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
