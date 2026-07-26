# SPDX-License-Identifier: AGPL-3.0-only
# Copyright (c) 2026 sol pbc

from __future__ import annotations

import json
import os
import threading
from pathlib import Path

import pytest

from solstone.think.sandbox_profile import (
    probe_contract,
    probe_durability,
    probe_records,
    probe_slot,
)
from solstone.think.sandbox_profile.probe_slot import acquire_probe_slot
from solstone.think.sandbox_profile.probe_writer import (
    ProbeAttemptWriter,
    begin_probe_attempt,
)
from tests._repo_inventory import assert_inventory_unchanged, repository_inventory
from tests.sandbox_profile import (
    ATTEMPT_ID,
    FIXED_TS,
    OTHER_ATTEMPT_ID,
    RUN_ID,
    THIRD_ATTEMPT_ID,
    complete_attempt_records,
    write_attempt_dir,
    write_ledger,
)


def _attempt_id(index: int) -> str:
    return f"00000000-0000-4000-8000-{index:012d}"


def _proof() -> str:
    return probe_contract.CAPABILITY_ORDER[0]


def _begin(
    slot: probe_slot.ProbeSlot,
    *,
    attempt_id: str = ATTEMPT_ID,
) -> ProbeAttemptWriter:
    proof = _proof()
    return begin_probe_attempt(
        slot,
        selected=(proof,),
        execution_order=(proof,),
        attempt_id=attempt_id,
        started_at=FIXED_TS,
    )


def _write_passed_proof(writer: ProbeAttemptWriter) -> None:
    proof = _proof()
    writer.dispatch_contact(proof, lambda: None)
    writer.write_proof_terminal(
        proof=proof,
        state=probe_contract.PROOF_STATE_PASSED,
        checks=probe_contract.PROOF_CHECKS[proof],
        reason=None,
        duration_ms=1,
        finished_at=FIXED_TS,
    )


def _complete_attempt(writer: ProbeAttemptWriter) -> None:
    _write_passed_proof(writer)
    writer.write_attempt_terminal(finished_at=FIXED_TS)


def _assert_probe_error(excinfo, code: str) -> None:
    assert excinfo.value.code == code
    assert excinfo.value.__cause__ is None


def _ledger_rows(journal: Path) -> list[dict[str, object]]:
    path = probe_contract.probe_ledger_path(journal)
    return [
        json.loads(line)
        for line in path.read_text(encoding="utf-8").splitlines()
        if line
    ]


def test_begin_validation_failure_spends_slot_without_mutation(tmp_path) -> None:
    journal = tmp_path / "journal"
    with acquire_probe_slot(journal, run_id=RUN_ID) as slot:
        before = repository_inventory(journal)
        with pytest.raises(probe_records.ProbeOperationError) as first:
            begin_probe_attempt(
                slot,
                selected=(),
                execution_order=(),
                attempt_id=ATTEMPT_ID,
                started_at=FIXED_TS,
            )
        after_first = repository_inventory(journal)

        with pytest.raises(probe_records.ProbeOperationError) as second:
            _begin(slot, attempt_id=OTHER_ATTEMPT_ID)
        after_second = repository_inventory(journal)

    _assert_probe_error(first, probe_contract.STABLE_ERROR_INTERNAL_ERROR)
    _assert_probe_error(second, probe_contract.STABLE_ERROR_INTERNAL_ERROR)
    assert_inventory_unchanged(before, after_first)
    assert_inventory_unchanged(before, after_second)
    assert not probe_contract.probe_attempts_parent_path(journal).exists()


def test_begin_after_release_is_internal_without_mutation(tmp_path) -> None:
    journal = tmp_path / "journal"
    slot = acquire_probe_slot(journal, run_id=RUN_ID)
    slot.release()
    before = repository_inventory(journal)

    with pytest.raises(probe_records.ProbeOperationError) as excinfo:
        _begin(slot)
    after = repository_inventory(journal)

    _assert_probe_error(excinfo, probe_contract.STABLE_ERROR_INTERNAL_ERROR)
    assert_inventory_unchanged(before, after)
    assert not probe_contract.probe_attempts_parent_path(journal).exists()


def test_append_after_release_is_internal_without_mutation(tmp_path) -> None:
    journal = tmp_path / "journal"
    proof = _proof()
    slot = acquire_probe_slot(journal, run_id=RUN_ID)
    writer = _begin(slot)
    writer.dispatch_contact(proof, lambda: None)
    slot.release()
    before = repository_inventory(journal)

    with pytest.raises(probe_records.ProbeOperationError) as excinfo:
        writer.write_proof_terminal(
            proof=proof,
            state=probe_contract.PROOF_STATE_PASSED,
            checks=probe_contract.PROOF_CHECKS[proof],
            reason=None,
            duration_ms=1,
            finished_at=FIXED_TS,
        )
    after = repository_inventory(journal)

    _assert_probe_error(excinfo, probe_contract.STABLE_ERROR_INTERNAL_ERROR)
    assert_inventory_unchanged(before, after)


def test_unlocked_append_after_release_raises_stable_error_without_mutation(
    tmp_path,
) -> None:
    journal = tmp_path / "journal"
    slot = acquire_probe_slot(journal, run_id=RUN_ID)
    slot.release()
    before = repository_inventory(journal)

    with slot.operation_guard():
        with pytest.raises(probe_records.ProbeOperationError) as excinfo:
            slot.append_encoded_record_unlocked(
                b"{}\n",
                record_type=probe_contract.RECORD_TYPE_ATTEMPT_STARTED,
                attempt_id=ATTEMPT_ID,
            )
    after = repository_inventory(journal)

    _assert_probe_error(excinfo, probe_contract.STABLE_ERROR_PROBE_ACTIVE)
    assert_inventory_unchanged(before, after)


def test_second_begin_after_active_and_terminal_are_internal_without_mutation(
    tmp_path,
) -> None:
    journal = tmp_path / "journal"
    with acquire_probe_slot(journal, run_id=RUN_ID) as slot:
        writer = _begin(slot)
        before_active_retry = repository_inventory(journal)
        with pytest.raises(probe_records.ProbeOperationError) as active_retry:
            _begin(slot, attempt_id=OTHER_ATTEMPT_ID)
        after_active_retry = repository_inventory(journal)

        _complete_attempt(writer)
        before_terminal_retry = repository_inventory(journal)
        with pytest.raises(probe_records.ProbeOperationError) as terminal_retry:
            _begin(slot, attempt_id=THIRD_ATTEMPT_ID)
        after_terminal_retry = repository_inventory(journal)

    _assert_probe_error(active_retry, probe_contract.STABLE_ERROR_INTERNAL_ERROR)
    _assert_probe_error(terminal_retry, probe_contract.STABLE_ERROR_INTERNAL_ERROR)
    assert_inventory_unchanged(before_active_retry, after_active_retry)
    assert_inventory_unchanged(before_terminal_retry, after_terminal_retry)
    assert not (
        probe_contract.probe_attempts_parent_path(journal) / OTHER_ATTEMPT_ID
    ).exists()
    assert not (
        probe_contract.probe_attempts_parent_path(journal) / THIRD_ATTEMPT_ID
    ).exists()


def test_second_begin_from_foreign_thread_while_active_is_internal_without_mutation(
    tmp_path,
) -> None:
    journal = tmp_path / "journal"
    with acquire_probe_slot(journal, run_id=RUN_ID) as slot:
        _begin(slot)
        before = repository_inventory(journal)
        result: list[str] = []

        def try_second_begin() -> None:
            try:
                _begin(slot, attempt_id=OTHER_ATTEMPT_ID)
            except probe_records.ProbeOperationError as exc:
                result.append(exc.code)

        thread = threading.Thread(target=try_second_begin)
        thread.start()
        thread.join(timeout=2)
        after = repository_inventory(journal)

    assert not thread.is_alive()
    assert result == [probe_contract.STABLE_ERROR_INTERNAL_ERROR]
    assert_inventory_unchanged(before, after)
    assert not (
        probe_contract.probe_attempts_parent_path(journal) / OTHER_ATTEMPT_ID
    ).exists()


def test_nested_begin_reentrancy_fails_without_deadlock_or_mutation(tmp_path) -> None:
    journal = tmp_path / "journal"
    with acquire_probe_slot(journal, run_id=RUN_ID) as slot:
        before = repository_inventory(journal)
        with slot.operation_guard():
            with pytest.raises(probe_records.ProbeOperationError) as excinfo:
                _begin(slot)
        after = repository_inventory(journal)

        writer = _begin(slot)

    _assert_probe_error(excinfo, probe_contract.STABLE_ERROR_INTERNAL_ERROR)
    assert_inventory_unchanged(before, after)
    assert writer.start.attempt_id == ATTEMPT_ID


def test_start_prospective_overflow_spends_without_directory_or_append(
    monkeypatch, tmp_path
) -> None:
    journal = tmp_path / "journal"
    oversized = b"x" * (probe_contract.MAX_LEDGER_BYTES + 1)

    with acquire_probe_slot(journal, run_id=RUN_ID) as slot:
        before = repository_inventory(journal)
        monkeypatch.setattr(
            probe_durability,
            "encode_jsonl_record",
            lambda _record: oversized,
        )
        with pytest.raises(probe_records.ProbeOperationError) as first:
            _begin(slot)
        after_first = repository_inventory(journal)

        with pytest.raises(probe_records.ProbeOperationError) as second:
            _begin(slot, attempt_id=OTHER_ATTEMPT_ID)
        after_second = repository_inventory(journal)

    _assert_probe_error(first, probe_contract.STABLE_ERROR_ATTEMPT_LIMIT_REACHED)
    _assert_probe_error(second, probe_contract.STABLE_ERROR_INTERNAL_ERROR)
    assert_inventory_unchanged(before, after_first)
    assert_inventory_unchanged(before, after_second)
    assert probe_contract.probe_ledger_path(journal).read_bytes() == b""
    assert not probe_contract.probe_attempts_parent_path(journal).exists()


def test_attempt_count_allows_sixty_fourth_and_refuses_existing_sixty_four(
    tmp_path,
) -> None:
    proof = _proof()
    journal_63 = tmp_path / "journal-63"
    records_63: list[dict[str, object]] = []
    for index in range(probe_contract.MAX_ATTEMPTS - 1):
        attempt_id = _attempt_id(index)
        write_attempt_dir(journal_63, attempt_id)
        records_63.extend(complete_attempt_records(attempt_id=attempt_id))
    write_ledger(journal_63, records_63)

    with acquire_probe_slot(journal_63, run_id=RUN_ID) as slot:
        writer = begin_probe_attempt(
            slot,
            selected=(proof,),
            execution_order=(proof,),
            attempt_id=_attempt_id(probe_contract.MAX_ATTEMPTS - 1),
            started_at=FIXED_TS,
        )
        assert writer.start.attempt_id == _attempt_id(probe_contract.MAX_ATTEMPTS - 1)

    journal_64 = tmp_path / "journal-64"
    records_64: list[dict[str, object]] = []
    for index in range(probe_contract.MAX_ATTEMPTS):
        attempt_id = _attempt_id(index)
        write_attempt_dir(journal_64, attempt_id)
        records_64.extend(complete_attempt_records(attempt_id=attempt_id))
    ledger = write_ledger(journal_64, records_64)
    before = ledger.read_bytes()

    with pytest.raises(probe_records.ProbeOperationError) as excinfo:
        acquire_probe_slot(journal_64, run_id=RUN_ID)

    _assert_probe_error(excinfo, probe_contract.STABLE_ERROR_ATTEMPT_LIMIT_REACHED)
    assert ledger.read_bytes() == before
    assert not (
        probe_contract.probe_attempts_parent_path(journal_64)
        / _attempt_id(probe_contract.MAX_ATTEMPTS)
    ).exists()


@pytest.mark.parametrize("path_kind", ["ledger", "lock"])
@pytest.mark.parametrize("node_kind", ["symlink", "fifo"])
def test_nonregular_ledger_and_lock_paths_fail_stale_before_acquire(
    tmp_path,
    path_kind: str,
    node_kind: str,
) -> None:
    journal = tmp_path / f"{path_kind}-{node_kind}"
    path = (
        probe_contract.probe_ledger_path(journal)
        if path_kind == "ledger"
        else probe_contract.probe_lock_path(journal)
    )
    path.parent.mkdir(parents=True, exist_ok=True)
    if node_kind == "symlink":
        target = tmp_path / f"{path_kind}-target"
        target.write_text("", encoding="utf-8")
        path.symlink_to(target)
    else:
        os.mkfifo(path)

    with pytest.raises(probe_records.ProbeOperationError) as excinfo:
        acquire_probe_slot(journal, run_id=RUN_ID)

    _assert_probe_error(excinfo, probe_contract.STABLE_ERROR_STALE_ATTEMPT)
    assert not probe_contract.probe_attempts_parent_path(journal).exists()


@pytest.mark.parametrize("path_kind", ["ledger", "lock"])
def test_canonical_path_replacement_poisons_before_begin(
    tmp_path,
    path_kind: str,
) -> None:
    journal = tmp_path / f"replace-{path_kind}"
    with acquire_probe_slot(journal, run_id=RUN_ID) as slot:
        path = (
            probe_contract.probe_ledger_path(journal)
            if path_kind == "ledger"
            else probe_contract.probe_lock_path(journal)
        )
        path.unlink()
        path.write_bytes(b"")
        before = repository_inventory(journal)

        with pytest.raises(probe_records.ProbeOperationError) as first:
            _begin(slot)
        after_first = repository_inventory(journal)

        with pytest.raises(probe_records.ProbeOperationError) as second:
            _begin(slot, attempt_id=OTHER_ATTEMPT_ID)

    _assert_probe_error(first, probe_contract.STABLE_ERROR_STALE_ATTEMPT)
    _assert_probe_error(second, probe_contract.STABLE_ERROR_INTERNAL_ERROR)
    assert_inventory_unchanged(before, after_first)
    assert not (
        probe_contract.probe_attempts_parent_path(journal) / ATTEMPT_ID
    ).exists()


def test_external_same_inode_ledger_growth_poisons_before_begin(tmp_path) -> None:
    journal = tmp_path / "journal"
    with acquire_probe_slot(journal, run_id=RUN_ID) as slot:
        probe_contract.probe_ledger_path(journal).write_bytes(b"x")
        before = repository_inventory(journal)

        with pytest.raises(probe_records.ProbeOperationError) as excinfo:
            _begin(slot)
        after = repository_inventory(journal)

    _assert_probe_error(excinfo, probe_contract.STABLE_ERROR_STALE_ATTEMPT)
    assert_inventory_unchanged(before, after)
    assert not probe_contract.probe_attempts_parent_path(journal).exists()


def test_start_exact_ledger_bound_is_admitted(monkeypatch, tmp_path) -> None:
    journal = tmp_path / "journal"
    data = b"x" * probe_contract.MAX_LEDGER_BYTES
    with acquire_probe_slot(journal, run_id=RUN_ID) as slot:
        monkeypatch.setattr(
            probe_durability,
            "encode_jsonl_record",
            lambda _record: data,
        )
        writer = _begin(slot)

    assert writer.start.attempt_id == ATTEMPT_ID
    assert probe_contract.probe_ledger_path(journal).stat().st_size == len(data)
    assert (probe_contract.probe_attempts_parent_path(journal) / ATTEMPT_ID).is_dir()


def test_proof_and_terminal_prospective_boundaries(monkeypatch, tmp_path) -> None:
    proof = _proof()
    original_encode = probe_durability.encode_jsonl_record

    proof_journal = tmp_path / "proof"
    with acquire_probe_slot(proof_journal, run_id=RUN_ID) as slot:
        writer = _begin(slot)
        writer.dispatch_contact(proof, lambda: None)
        remaining = probe_contract.MAX_LEDGER_BYTES - slot._ledger_tracked_size
        monkeypatch.setattr(
            probe_durability,
            "encode_jsonl_record",
            lambda _record: b"x" * remaining,
        )
        writer.write_proof_terminal(
            proof=proof,
            state=probe_contract.PROOF_STATE_PASSED,
            checks=probe_contract.PROOF_CHECKS[proof],
            reason=None,
            duration_ms=1,
            finished_at=FIXED_TS,
        )
        assert slot._ledger_tracked_size == probe_contract.MAX_LEDGER_BYTES

    monkeypatch.setattr(probe_durability, "encode_jsonl_record", original_encode)
    over_journal = tmp_path / "proof-over"
    with acquire_probe_slot(over_journal, run_id=RUN_ID) as slot:
        writer = _begin(slot)
        writer.dispatch_contact(proof, lambda: None)
        before = probe_contract.probe_ledger_path(over_journal).stat().st_size
        too_large = probe_contract.MAX_LEDGER_BYTES - slot._ledger_tracked_size + 1
        monkeypatch.setattr(
            probe_durability,
            "encode_jsonl_record",
            lambda _record: b"x" * too_large,
        )
        with pytest.raises(probe_records.ProbeOperationError) as excinfo:
            writer.write_proof_terminal(
                proof=proof,
                state=probe_contract.PROOF_STATE_PASSED,
                checks=probe_contract.PROOF_CHECKS[proof],
                reason=None,
                duration_ms=1,
                finished_at=FIXED_TS,
            )
        after = probe_contract.probe_ledger_path(over_journal).stat().st_size

    _assert_probe_error(excinfo, probe_contract.STABLE_ERROR_ATTEMPT_LIMIT_REACHED)
    assert after == before

    monkeypatch.setattr(probe_durability, "encode_jsonl_record", original_encode)
    terminal_journal = tmp_path / "terminal"
    with acquire_probe_slot(terminal_journal, run_id=RUN_ID) as slot:
        writer = _begin(slot)
        _write_passed_proof(writer)
        remaining = probe_contract.MAX_LEDGER_BYTES - slot._ledger_tracked_size
        monkeypatch.setattr(
            probe_durability,
            "encode_jsonl_record",
            lambda _record: b"x" * remaining,
        )
        writer.write_attempt_terminal(finished_at=FIXED_TS)
        assert slot._ledger_tracked_size == probe_contract.MAX_LEDGER_BYTES


def test_writer_replacement_cannot_escape_poisoned_slot(monkeypatch, tmp_path) -> None:
    journal = tmp_path / "journal"
    proof = _proof()
    with acquire_probe_slot(journal, run_id=RUN_ID) as slot:
        writer = _begin(slot)
        writer.dispatch_contact(proof, lambda: None)

        def fail_write(_fd, _data):
            raise OSError("secret")

        monkeypatch.setattr(probe_durability, "_write_once", fail_write)
        with pytest.raises(probe_records.ProbeOperationError) as first:
            writer.write_proof_terminal(
                proof=proof,
                state=probe_contract.PROOF_STATE_PASSED,
                checks=probe_contract.PROOF_CHECKS[proof],
                reason=None,
                duration_ms=1,
                finished_at=FIXED_TS,
            )
        replacement = ProbeAttemptWriter(
            slot=slot,
            start=writer.start,
            attempt_dir=writer.attempt_dir,
        )
        with pytest.raises(probe_records.ProbeOperationError) as second:
            replacement.write_proof_terminal(
                proof=proof,
                state=probe_contract.PROOF_STATE_PASSED,
                checks=probe_contract.PROOF_CHECKS[proof],
                reason=None,
                duration_ms=1,
                finished_at=FIXED_TS,
            )

    _assert_probe_error(first, probe_contract.STABLE_ERROR_RECORD_WRITE_FAILED)
    _assert_probe_error(second, probe_contract.STABLE_ERROR_RECORD_WRITE_FAILED)


def test_cancelled_attempt_before_any_proof_writes_not_run_suffix_and_terminal(
    tmp_path,
) -> None:
    selected = probe_contract.CAPABILITY_ORDER[:2]
    journal = tmp_path / "journal"
    with acquire_probe_slot(journal, run_id=RUN_ID) as slot:
        writer = begin_probe_attempt(
            slot,
            selected=selected,
            execution_order=selected,
            attempt_id=ATTEMPT_ID,
            started_at=FIXED_TS,
        )
        writer.write_cancelled_attempt(
            proof=selected[0],
            state=probe_contract.PROOF_STATE_NOT_RUN,
            checks=(),
            duration_ms=None,
            finished_at=FIXED_TS,
        )
        with pytest.raises(probe_records.ProbeOperationError) as second_begin:
            begin_probe_attempt(
                slot,
                selected=(selected[0],),
                execution_order=(selected[0],),
                attempt_id=OTHER_ATTEMPT_ID,
                started_at=FIXED_TS,
            )

    rows = _ledger_rows(journal)
    assert [row["type"] for row in rows] == [
        probe_contract.RECORD_TYPE_ATTEMPT_STARTED,
        probe_contract.RECORD_TYPE_PROOF_TERMINAL,
        probe_contract.RECORD_TYPE_PROOF_TERMINAL,
        probe_contract.RECORD_TYPE_ATTEMPT_TERMINAL,
    ]
    assert rows[1]["state"] == probe_contract.PROOF_STATE_NOT_RUN
    assert rows[1]["reason"] == probe_contract.REASON_CANCELLED
    assert rows[2]["state"] == probe_contract.PROOF_STATE_NOT_RUN
    assert rows[2]["checks"] == []
    assert rows[2]["reason"] == probe_contract.REASON_CANCELLED
    assert rows[-1]["state"] == probe_contract.ATTEMPT_STATE_CANCELLED
    assert rows[-1]["terminal_reason"] == probe_contract.REASON_CANCELLED
    _assert_probe_error(second_begin, probe_contract.STABLE_ERROR_INTERNAL_ERROR)

    with pytest.raises(probe_records.ProbeOperationError) as reacquire:
        acquire_probe_slot(journal, run_id=RUN_ID)
    _assert_probe_error(reacquire, probe_contract.STABLE_ERROR_STALE_ATTEMPT)


def test_cancelled_attempt_after_contact_in_flight_writes_failed_first_row(
    tmp_path,
) -> None:
    selected = probe_contract.CAPABILITY_ORDER[:2]
    journal = tmp_path / "journal"
    contact_started = threading.Event()
    release_contact = threading.Event()
    cancel_result: list[str] = []

    with acquire_probe_slot(journal, run_id=RUN_ID) as slot:
        writer = begin_probe_attempt(
            slot,
            selected=selected,
            execution_order=selected,
            attempt_id=ATTEMPT_ID,
            started_at=FIXED_TS,
        )

        def contact_operation() -> None:
            contact_started.set()
            assert release_contact.wait(timeout=2)

        contact_thread = threading.Thread(
            target=lambda: writer.dispatch_contact(selected[0], contact_operation)
        )
        contact_thread.start()
        assert contact_started.wait(timeout=2)

        def cancel() -> None:
            writer.write_cancelled_attempt(
                proof=selected[0],
                state=probe_contract.PROOF_STATE_FAILED,
                checks=probe_contract.PROOF_CHECKS[selected[0]][:1],
                duration_ms=1,
                finished_at=FIXED_TS,
            )
            cancel_result.append("done")

        cancel_thread = threading.Thread(target=cancel)
        cancel_thread.start()
        release_contact.set()
        contact_thread.join(timeout=2)
        cancel_thread.join(timeout=2)

    assert not contact_thread.is_alive()
    assert not cancel_thread.is_alive()
    assert cancel_result == ["done"]
    rows = _ledger_rows(journal)
    assert rows[1]["state"] == probe_contract.PROOF_STATE_FAILED
    assert rows[1]["checks"] == [probe_contract.PROOF_CHECKS[selected[0]][0]]
    assert rows[1]["reason"] == probe_contract.REASON_CANCELLED
    assert rows[2]["state"] == probe_contract.PROOF_STATE_NOT_RUN
    assert rows[2]["reason"] == probe_contract.REASON_CANCELLED
    assert rows[-1]["state"] == probe_contract.ATTEMPT_STATE_CANCELLED


def test_malformed_first_cancellation_is_sticky_and_poisons_internal(
    tmp_path,
) -> None:
    proof = _proof()
    journal = tmp_path / "journal"
    with acquire_probe_slot(journal, run_id=RUN_ID) as slot:
        writer = _begin(slot)
        before_rows = _ledger_rows(journal)
        with pytest.raises(probe_records.ProbeOperationError) as malformed:
            writer.write_cancelled_attempt(
                proof=proof,
                state=probe_contract.PROOF_STATE_FAILED,
                checks=(),
                duration_ms=1,
                finished_at=FIXED_TS,
            )
        with pytest.raises(probe_records.ProbeOperationError) as contact:
            writer.dispatch_contact(proof, lambda: None)

    _assert_probe_error(malformed, probe_contract.STABLE_ERROR_INTERNAL_ERROR)
    _assert_probe_error(contact, probe_contract.STABLE_ERROR_INTERNAL_ERROR)
    assert _ledger_rows(journal) == before_rows


def test_failed_first_cancellation_append_is_sticky_and_poisons_exact_code(
    monkeypatch,
    tmp_path,
) -> None:
    proof = _proof()
    journal = tmp_path / "journal"
    with acquire_probe_slot(journal, run_id=RUN_ID) as slot:
        writer = _begin(slot)
        writer.dispatch_contact(proof, lambda: None)
        before_rows = _ledger_rows(journal)

        def fail_write(_fd, _data):
            raise OSError("secret")

        monkeypatch.setattr(probe_durability, "_write_once", fail_write)
        with pytest.raises(probe_records.ProbeOperationError) as failed_append:
            writer.write_cancelled_attempt(
                proof=proof,
                state=probe_contract.PROOF_STATE_FAILED,
                checks=probe_contract.PROOF_CHECKS[proof][:1],
                duration_ms=1,
                finished_at=FIXED_TS,
            )
        with pytest.raises(probe_records.ProbeOperationError) as contact:
            writer.dispatch_contact(proof, lambda: None)

    _assert_probe_error(failed_append, probe_contract.STABLE_ERROR_RECORD_WRITE_FAILED)
    _assert_probe_error(contact, probe_contract.STABLE_ERROR_RECORD_WRITE_FAILED)
    assert _ledger_rows(journal) == before_rows


def test_dispatch_contact_return_and_exception_consume_authorization(
    tmp_path,
) -> None:
    proof = _proof()

    return_journal = tmp_path / "return"
    with acquire_probe_slot(return_journal, run_id=RUN_ID) as slot:
        writer = _begin(slot)
        result = object()

        assert writer.dispatch_contact(proof, lambda: result) is result
        with pytest.raises(probe_records.ProbeOperationError) as second_return:
            writer.dispatch_contact(proof, lambda: object())

    _assert_probe_error(second_return, probe_contract.STABLE_ERROR_INTERNAL_ERROR)

    exception_journal = tmp_path / "exception"
    expected = RuntimeError("caller failure")

    def fail_contact() -> None:
        raise expected

    with acquire_probe_slot(exception_journal, run_id=RUN_ID) as slot:
        writer = _begin(slot)
        with pytest.raises(RuntimeError) as excinfo:
            writer.dispatch_contact(proof, fail_contact)
        with pytest.raises(probe_records.ProbeOperationError) as second_exception:
            writer.dispatch_contact(proof, lambda: None)

    assert excinfo.value is expected
    _assert_probe_error(second_exception, probe_contract.STABLE_ERROR_INTERNAL_ERROR)


def test_dispatch_contact_reentrant_callback_returns_internal_error(
    tmp_path,
) -> None:
    proof = _proof()
    with acquire_probe_slot(tmp_path / "journal", run_id=RUN_ID) as slot:
        writer = _begin(slot)

        def reenter() -> None:
            writer.dispatch_contact(proof, lambda: None)

        with pytest.raises(probe_records.ProbeOperationError) as excinfo:
            writer.dispatch_contact(proof, reenter)
        with pytest.raises(probe_records.ProbeOperationError) as second:
            writer.dispatch_contact(proof, lambda: None)

    _assert_probe_error(excinfo, probe_contract.STABLE_ERROR_INTERNAL_ERROR)
    _assert_probe_error(second, probe_contract.STABLE_ERROR_INTERNAL_ERROR)


def test_proof_terminal_requires_matching_contact_consumption(tmp_path) -> None:
    proof = _proof()

    passed_journal = tmp_path / "passed"
    with acquire_probe_slot(passed_journal, run_id=RUN_ID) as slot:
        writer = _begin(slot)
        with pytest.raises(probe_records.ProbeOperationError) as missing_contact:
            writer.write_proof_terminal(
                proof=proof,
                state=probe_contract.PROOF_STATE_PASSED,
                checks=probe_contract.PROOF_CHECKS[proof],
                reason=None,
                duration_ms=1,
                finished_at=FIXED_TS,
            )

    _assert_probe_error(missing_contact, probe_contract.STABLE_ERROR_INTERNAL_ERROR)

    not_run_journal = tmp_path / "not-run"
    with acquire_probe_slot(not_run_journal, run_id=RUN_ID) as slot:
        writer = _begin(slot)
        writer.dispatch_contact(proof, lambda: None)
        with pytest.raises(probe_records.ProbeOperationError) as consumed_not_run:
            writer.write_proof_terminal(
                proof=proof,
                state=probe_contract.PROOF_STATE_NOT_RUN,
                checks=(),
                reason=probe_contract.REASON_CANCELLED,
                duration_ms=None,
                finished_at=FIXED_TS,
            )

    _assert_probe_error(consumed_not_run, probe_contract.STABLE_ERROR_INTERNAL_ERROR)

    clean_not_run_journal = tmp_path / "clean-not-run"
    with acquire_probe_slot(clean_not_run_journal, run_id=RUN_ID) as slot:
        writer = _begin(slot)
        writer.write_proof_terminal(
            proof=proof,
            state=probe_contract.PROOF_STATE_NOT_RUN,
            checks=(),
            reason=probe_contract.REASON_CANCELLED,
            duration_ms=None,
            finished_at=FIXED_TS,
        )
        writer.write_attempt_terminal(finished_at=FIXED_TS)
