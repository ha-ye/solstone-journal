# SPDX-License-Identifier: AGPL-3.0-only
# Copyright (c) 2026 sol pbc

from __future__ import annotations

import pytest

from solstone.think.sandbox_profile import probe_contract, probe_records
from tests.sandbox_profile import ATTEMPT_ID, FIXED_TS, RUN_ID, proof_record


def test_canonical_uuid_rejects_noncanonical_text() -> None:
    with pytest.raises(probe_records.ProbeRecordValidationError):
        probe_records.validate_canonical_uuid(RUN_ID.upper())


def test_selected_must_be_nonempty_duplicate_free_canonical_subsequence() -> None:
    assert probe_records.validate_selected([probe_contract.CAPABILITY_ORDER[0]]) == (
        probe_contract.CAPABILITY_ORDER[0],
    )
    with pytest.raises(probe_records.ProbeRecordValidationError):
        probe_records.validate_selected([])
    with pytest.raises(probe_records.ProbeRecordValidationError):
        probe_records.validate_selected(
            [probe_contract.CAPABILITY_ORDER[1], probe_contract.CAPABILITY_ORDER[0]]
        )
    with pytest.raises(probe_records.ProbeRecordValidationError):
        probe_records.validate_selected(
            [probe_contract.CAPABILITY_ORDER[0], probe_contract.CAPABILITY_ORDER[0]]
        )


def test_execution_order_must_be_duplicate_free_permutation() -> None:
    selected = probe_contract.CAPABILITY_ORDER[:2]
    assert probe_records.validate_execution_order(
        list(reversed(selected)), selected
    ) == (
        selected[1],
        selected[0],
    )
    with pytest.raises(probe_records.ProbeRecordValidationError):
        probe_records.validate_execution_order([selected[0], selected[0]], selected)
    with pytest.raises(probe_records.ProbeRecordValidationError):
        probe_records.validate_execution_order([selected[0]], selected)


def test_duration_rejects_bool_before_int() -> None:
    with pytest.raises(probe_records.ProbeRecordValidationError):
        probe_records.validate_non_negative_int(True)


def test_proof_terminal_semantics_for_passed_failed_and_not_run() -> None:
    proof = probe_contract.CAPABILITY_ORDER[0]
    passed = probe_records.validate_proof_terminal_payload(proof_record(proof=proof))
    assert passed.state == probe_contract.PROOF_STATE_PASSED

    failed = probe_records.validate_proof_terminal_payload(
        proof_record(
            proof=proof,
            state=probe_contract.PROOF_STATE_FAILED,
            checks=probe_contract.PROOF_CHECKS[proof][:1],
            reason=probe_contract.PROOF_SPECIFIC_REASONS[proof][0],
        )
    )
    assert failed.cleanup_state == probe_contract.DECLARED_CLEANUP_STATES[proof]

    not_run = probe_records.validate_proof_terminal_payload(
        proof_record(
            proof=proof,
            state=probe_contract.PROOF_STATE_NOT_RUN,
            reason=probe_contract.REASON_DEPENDENCY_FAILED,
            duration_ms=None,
        )
    )
    assert not_run.cleanup_state == probe_contract.CLEANUP_STATE_VERIFIED


def test_dependency_failed_is_valid_only_for_not_run() -> None:
    proof = probe_contract.CAPABILITY_ORDER[0]
    with pytest.raises(probe_records.ProbeRecordValidationError):
        probe_records.build_proof_terminal_record(
            run_id=RUN_ID,
            attempt_id=ATTEMPT_ID,
            proof=proof,
            state=probe_contract.PROOF_STATE_FAILED,
            checks=(),
            reason=probe_contract.REASON_DEPENDENCY_FAILED,
            duration_ms=1,
            finished_at=FIXED_TS,
        )


def test_operation_error_carries_only_closed_fields() -> None:
    exc = probe_records.ProbeOperationError(
        probe_contract.STABLE_ERROR_STALE_ATTEMPT,
        attempt_id=ATTEMPT_ID,
        record_kind=probe_contract.RECORD_KIND_PROOF_TERMINAL,
        proof=probe_contract.CAPABILITY_ORDER[0],
    )
    assert str(exc) == probe_contract.STABLE_ERROR_STALE_ATTEMPT
    assert exc.__cause__ is None
    with pytest.raises(ValueError):
        probe_records.ProbeOperationError(
            probe_contract.STABLE_ERROR_STALE_ATTEMPT,
            proof="free-text",
        )
