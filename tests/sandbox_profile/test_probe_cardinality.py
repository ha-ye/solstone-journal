# SPDX-License-Identifier: AGPL-3.0-only
# Copyright (c) 2026 sol pbc

from __future__ import annotations

import pytest

from solstone.think.sandbox_profile import probe_contract, probe_records
from solstone.think.sandbox_profile.probe_replay import replay_probe_ledger
from solstone.think.sandbox_profile.probe_slot import acquire_probe_slot
from solstone.think.sandbox_profile.probe_writer import begin_probe_attempt
from tests.sandbox_profile import (
    ATTEMPT_ID,
    FIXED_TS,
    OTHER_ATTEMPT_ID,
    RUN_ID,
    THIRD_ATTEMPT_ID,
    complete_attempt_records,
    start_record,
    write_attempt_dir,
    write_ledger,
)


def _assert_stale(journal) -> None:
    with pytest.raises(probe_records.ProbeOperationError) as excinfo:
        replay_probe_ledger(journal)
    assert excinfo.value.code == probe_contract.STABLE_ERROR_STALE_ATTEMPT


def test_exact_start_proofs_terminal_cardinality_passes(tmp_path) -> None:
    journal = tmp_path / "journal"
    selected = probe_contract.CAPABILITY_ORDER[:2]
    write_attempt_dir(journal)
    write_ledger(journal, complete_attempt_records(selected=selected))

    replay = replay_probe_ledger(journal)

    assert replay.attempt_count == 1
    assert [proof.proof for proof in replay.attempts[0].proofs] == list(selected)


def test_missing_proof_terminal_is_stale(tmp_path) -> None:
    journal = tmp_path / "journal"
    selected = probe_contract.CAPABILITY_ORDER[:2]
    records = complete_attempt_records(selected=selected)
    write_attempt_dir(journal)
    write_ledger(journal, [records[0], records[1], records[-1]])

    _assert_stale(journal)


def test_wrong_proof_order_is_stale(tmp_path) -> None:
    journal = tmp_path / "journal"
    selected = probe_contract.CAPABILITY_ORDER[:2]
    records = complete_attempt_records(selected=selected)
    write_attempt_dir(journal)
    write_ledger(journal, [records[0], records[2], records[1], records[-1]])

    _assert_stale(journal)


def test_duplicate_attempt_id_is_stale(tmp_path) -> None:
    journal = tmp_path / "journal"
    first = complete_attempt_records()
    second = complete_attempt_records()
    write_attempt_dir(journal, ATTEMPT_ID)
    write_ledger(journal, first + second)

    _assert_stale(journal)


def test_record_after_nonretry_terminal_is_stale(tmp_path) -> None:
    journal = tmp_path / "journal"
    proof = probe_contract.CAPABILITY_ORDER[0]
    cancelled = complete_attempt_records(
        proof_overrides={
            proof: {
                "state": probe_contract.PROOF_STATE_FAILED,
                "reason": probe_contract.REASON_CANCELLED,
            }
        }
    )
    next_start = start_record(attempt_id=OTHER_ATTEMPT_ID)
    write_attempt_dir(journal, ATTEMPT_ID)
    write_attempt_dir(journal, OTHER_ATTEMPT_ID)
    write_ledger(journal, cancelled + [next_start])

    _assert_stale(journal)


def test_writer_enforces_contact_and_proof_order(tmp_path) -> None:
    journal = tmp_path / "journal"
    selected = probe_contract.CAPABILITY_ORDER[:2]
    with acquire_probe_slot(journal, run_id=RUN_ID) as slot:
        writer = begin_probe_attempt(
            slot,
            selected=selected,
            execution_order=selected,
            attempt_id=THIRD_ATTEMPT_ID,
            started_at=FIXED_TS,
        )
        writer.assert_contact_allowed(selected[0])
        with pytest.raises(probe_records.ProbeOperationError) as excinfo:
            writer.assert_contact_allowed(selected[1])
        assert excinfo.value.code == probe_contract.STABLE_ERROR_INTERNAL_ERROR
        with pytest.raises(probe_records.ProbeOperationError):
            writer.write_attempt_terminal()

        with pytest.raises(probe_records.ProbeOperationError):
            writer.write_proof_terminal(
                proof=selected[1],
                state=probe_contract.PROOF_STATE_PASSED,
                checks=probe_contract.PROOF_CHECKS[selected[1]],
                reason=None,
                duration_ms=1,
                finished_at=FIXED_TS,
            )
