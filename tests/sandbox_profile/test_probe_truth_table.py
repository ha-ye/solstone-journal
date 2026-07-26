# SPDX-License-Identifier: AGPL-3.0-only
# Copyright (c) 2026 sol pbc

from __future__ import annotations

from solstone.think.sandbox_profile import probe_contract, probe_records
from tests.sandbox_profile import proof_record


def _proof(**overrides):
    payload = proof_record(**overrides)
    return probe_records.validate_proof_terminal_payload(payload)


def test_cleanup_unverified_precedes_every_other_reason() -> None:
    proof = probe_contract.CAPABILITY_ORDER[0]
    records = [
        _proof(
            proof=proof,
            state=probe_contract.PROOF_STATE_FAILED,
            reason=probe_contract.REASON_CLEANUP_UNVERIFIED,
        ),
        _proof(
            proof=proof,
            state=probe_contract.PROOF_STATE_FAILED,
            reason=probe_contract.REASON_CANCELLED,
        ),
    ]

    assert probe_records.derive_attempt_terminal(records) == (
        probe_contract.ATTEMPT_STATE_DEGRADED,
        probe_contract.REASON_CLEANUP_UNVERIFIED,
    )


def test_valid_cancelled_suffix_maps_to_cancelled() -> None:
    proof = probe_contract.CAPABILITY_ORDER[0]
    records = [
        _proof(
            proof=proof,
            state=probe_contract.PROOF_STATE_FAILED,
            reason=probe_contract.REASON_CANCELLED,
        ),
        _proof(
            proof=proof,
            state=probe_contract.PROOF_STATE_NOT_RUN,
            reason=probe_contract.REASON_CANCELLED,
            duration_ms=None,
        ),
    ]

    assert probe_records.derive_attempt_terminal(records) == (
        probe_contract.ATTEMPT_STATE_CANCELLED,
        probe_contract.REASON_CANCELLED,
    )


def test_internal_error_precedes_generic_proof_failure() -> None:
    proof = probe_contract.CAPABILITY_ORDER[0]
    records = [
        _proof(
            proof=proof,
            state=probe_contract.PROOF_STATE_FAILED,
            reason=probe_contract.REASON_INTERNAL_ERROR,
        ),
        _proof(
            proof=proof,
            state=probe_contract.PROOF_STATE_FAILED,
            reason=probe_contract.PROOF_SPECIFIC_REASONS[proof][0],
        ),
    ]

    assert probe_records.derive_attempt_terminal(records) == (
        probe_contract.ATTEMPT_STATE_ERROR,
        probe_contract.REASON_INTERNAL_ERROR,
    )


def test_generic_failed_row_maps_to_proof_failed() -> None:
    proof = probe_contract.CAPABILITY_ORDER[0]
    records = [
        _proof(
            proof=proof,
            state=probe_contract.PROOF_STATE_FAILED,
            reason=probe_contract.PROOF_SPECIFIC_REASONS[proof][0],
        )
    ]

    assert probe_records.derive_attempt_terminal(records) == (
        probe_contract.ATTEMPT_STATE_DEGRADED,
        probe_contract.ATTEMPT_TERMINAL_REASON_PROOF_FAILED,
    )


def test_all_passed_maps_to_ok_null() -> None:
    records = [_proof(proof=probe_contract.CAPABILITY_ORDER[0])]

    assert probe_records.derive_attempt_terminal(records) == (
        probe_contract.ATTEMPT_STATE_OK,
        None,
    )


def test_record_write_failed_is_not_a_record_reason_or_state() -> None:
    flat_values = set(probe_contract.RECORD_TYPES)
    flat_values.update(probe_contract.PROOF_TERMINAL_STATES)
    flat_values.update(probe_contract.ATTEMPT_TERMINAL_STATES)
    flat_values.update(probe_contract.PROOF_REASON_POOL)
    flat_values.update(probe_contract.COMMON_REASONS)
    flat_values.update(probe_contract.ATTEMPT_TERMINAL_REASONS)

    assert probe_contract.STABLE_ERROR_RECORD_WRITE_FAILED not in flat_values
