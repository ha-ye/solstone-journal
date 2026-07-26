# SPDX-License-Identifier: AGPL-3.0-only
# Copyright (c) 2026 sol pbc

from __future__ import annotations

import os
import threading
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from typing import Any

import pytest

from solstone.think.sandbox_profile import (
    probe_contract,
    probe_durability,
    probe_records,
    probe_replay,
)
from solstone.think.sandbox_profile.probe_slot import ProbeSlot, acquire_probe_slot
from solstone.think.sandbox_profile.probe_writer import (
    ProbeAttemptWriter,
    begin_probe_attempt,
)
from tests.sandbox_profile import (
    ATTEMPT_ID,
    FIXED_TS,
    OTHER_ATTEMPT_ID,
    RUN_ID,
    start_record,
    write_attempt_dir,
    write_ledger,
)


def _proof() -> str:
    return probe_contract.CAPABILITY_ORDER[0]


def _begin(
    journal: Path,
    *,
    selected: tuple[str, ...] | None = None,
) -> tuple[ProbeSlot, ProbeAttemptWriter]:
    selected = selected or (_proof(),)
    slot = acquire_probe_slot(journal, run_id=RUN_ID)
    writer = begin_probe_attempt(
        slot,
        selected=selected,
        execution_order=selected,
        attempt_id=ATTEMPT_ID,
        started_at=FIXED_TS,
    )
    return slot, writer


def _assert_probe_error(excinfo, code: str) -> None:
    assert excinfo.value.code == code
    assert excinfo.value.__cause__ is None


def test_acquire_releases_lock_when_replay_escapes_memory_error(
    monkeypatch,
    tmp_path,
) -> None:
    journal = tmp_path / "journal"
    sentinel = MemoryError("replay sentinel")
    original_replay = probe_replay.replay_probe_ledger

    def fail_replay(_journal: Path) -> probe_records.ProbeReplay:
        raise sentinel

    monkeypatch.setattr(probe_replay, "replay_probe_ledger", fail_replay)
    with pytest.raises(MemoryError) as excinfo:
        acquire_probe_slot(journal, run_id=RUN_ID)
    assert excinfo.value is sentinel

    monkeypatch.setattr(probe_replay, "replay_probe_ledger", original_replay)
    slot = acquire_probe_slot(journal, run_id=RUN_ID)
    try:
        assert slot.owned is True
    finally:
        slot.release()


def test_complete_terminal_survives_reported_fsync_error_but_slot_is_poisoned(
    monkeypatch,
    tmp_path,
) -> None:
    journal = tmp_path / "journal"
    proof = _proof()
    slot, writer = _begin(journal)
    try:
        writer.dispatch_contact(proof, lambda: None)
        writer.write_proof_terminal(
            proof=proof,
            state=probe_contract.PROOF_STATE_FAILED,
            checks=(),
            reason=probe_contract.PROOF_SPECIFIC_REASONS[proof][0],
            duration_ms=1,
            finished_at=FIXED_TS,
        )

        def fsync_then_report_failure(fd: int) -> None:
            os.fsync(fd)
            raise OSError("reported after durable bytes")

        monkeypatch.setattr(probe_durability, "_fsync_file", fsync_then_report_failure)
        with pytest.raises(probe_records.ProbeOperationError) as write_error:
            writer.write_attempt_terminal(finished_at=FIXED_TS)
        _assert_probe_error(
            write_error, probe_contract.STABLE_ERROR_RECORD_WRITE_FAILED
        )

        replay = probe_replay.replay_probe_ledger(journal)
        assert replay.attempt_count == 1
        assert (
            replay.attempts[0].terminal.state == probe_contract.ATTEMPT_STATE_DEGRADED
        )
        assert (
            replay.attempts[0].terminal.terminal_reason
            == probe_contract.ATTEMPT_TERMINAL_REASON_PROOF_FAILED
        )

        with pytest.raises(probe_records.ProbeOperationError) as poisoned:
            writer.write_attempt_terminal(finished_at=FIXED_TS)
        _assert_probe_error(poisoned, probe_contract.STABLE_ERROR_RECORD_WRITE_FAILED)
    finally:
        slot.release()


@pytest.mark.parametrize(
    ("crash_point", "expected_error"),
    [
        ("before_start_barrier", None),
        ("during_start_barrier", probe_contract.STABLE_ERROR_STALE_ATTEMPT),
        ("after_start_barrier", probe_contract.STABLE_ERROR_STALE_ATTEMPT),
    ],
)
def test_crash_states_around_start_forward_barrier(
    tmp_path,
    crash_point: str,
    expected_error: str | None,
) -> None:
    journal = tmp_path / crash_point
    if crash_point == "before_start_barrier":
        slot = acquire_probe_slot(journal, run_id=RUN_ID)
        slot.release()
    elif crash_point == "during_start_barrier":
        write_attempt_dir(journal)
    else:
        write_attempt_dir(journal)
        write_ledger(journal, [start_record()])

    if expected_error is None:
        slot = acquire_probe_slot(journal, run_id=RUN_ID)
        try:
            assert slot.owned is True
        finally:
            slot.release()
        return

    with pytest.raises(probe_records.ProbeOperationError) as excinfo:
        acquire_probe_slot(journal, run_id=RUN_ID)
    _assert_probe_error(excinfo, expected_error)


def test_contact_waits_for_start_fsync_and_revalidates_held_ledger_identity(
    monkeypatch,
    tmp_path,
) -> None:
    journal = tmp_path / "journal"
    proof = _proof()
    fsync_entered = threading.Event()
    finish_fsync = threading.Event()
    original_fsync = probe_durability._fsync_file

    def blocked_fsync(fd: int) -> None:
        fsync_entered.set()
        assert finish_fsync.wait(timeout=2)
        original_fsync(fd)

    def begin_attempt() -> ProbeAttemptWriter:
        slot = acquire_probe_slot(journal, run_id=RUN_ID)
        return begin_probe_attempt(
            slot,
            selected=(proof,),
            execution_order=(proof,),
            attempt_id=ATTEMPT_ID,
            started_at=FIXED_TS,
        )

    monkeypatch.setattr(probe_durability, "_fsync_file", blocked_fsync)
    with ThreadPoolExecutor(max_workers=1) as executor:
        begin_future = executor.submit(begin_attempt)
        try:
            assert fsync_entered.wait(timeout=2)
            assert not begin_future.done()

            ledger_path = probe_contract.probe_ledger_path(journal)
            ledger_path.unlink()
            ledger_path.write_bytes(b"")
        finally:
            finish_fsync.set()
        writer = begin_future.result(timeout=2)
        with pytest.raises(probe_records.ProbeOperationError) as contact:
            writer.dispatch_contact(proof, lambda: None)
        _assert_probe_error(contact, probe_contract.STABLE_ERROR_STALE_ATTEMPT)
        writer.slot.release()


@pytest.mark.parametrize(
    "terminal_class",
    ["cleanup_unverified", "cancelled", "internal_error"],
)
def test_nonretryable_terminals_block_same_slot_begin_and_fresh_acquire(
    tmp_path,
    terminal_class: str,
) -> None:
    journal = tmp_path / terminal_class
    proof = _proof()
    slot, writer = _begin(journal)
    try:
        if terminal_class == "cancelled":
            writer.write_cancelled_attempt(
                proof=proof,
                state=probe_contract.PROOF_STATE_NOT_RUN,
                checks=(),
                duration_ms=None,
                finished_at=FIXED_TS,
            )
        else:
            reason = (
                probe_contract.REASON_CLEANUP_UNVERIFIED
                if terminal_class == "cleanup_unverified"
                else probe_contract.REASON_INTERNAL_ERROR
            )
            writer.dispatch_contact(proof, lambda: None)
            writer.write_proof_terminal(
                proof=proof,
                state=probe_contract.PROOF_STATE_FAILED,
                checks=(),
                reason=reason,
                duration_ms=1,
                finished_at=FIXED_TS,
            )
            writer.write_attempt_terminal(finished_at=FIXED_TS)

        with pytest.raises(probe_records.ProbeOperationError) as same_slot:
            begin_probe_attempt(
                slot,
                selected=(proof,),
                execution_order=(proof,),
                attempt_id=OTHER_ATTEMPT_ID,
                started_at=FIXED_TS,
            )
        _assert_probe_error(same_slot, probe_contract.STABLE_ERROR_INTERNAL_ERROR)
    finally:
        slot.release()

    with pytest.raises(probe_records.ProbeOperationError) as fresh:
        acquire_probe_slot(journal, run_id=RUN_ID)
    _assert_probe_error(fresh, probe_contract.STABLE_ERROR_STALE_ATTEMPT)


@pytest.mark.parametrize("exc_type", [MemoryError, KeyboardInterrupt, SystemExit])
def test_decode_seam_does_not_convert_escaping_exception_classes(
    monkeypatch,
    tmp_path,
    exc_type: type[BaseException],
) -> None:
    journal = tmp_path / "journal"
    sentinel = exc_type("decode sentinel")
    path = probe_contract.probe_ledger_path(journal)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("{}\n", encoding="utf-8")

    class RaisingDecoder:
        def __init__(self, *_args: Any, **_kwargs: Any) -> None:
            return None

        def raw_decode(self, _text: str) -> tuple[dict[str, object], int]:
            raise sentinel

    monkeypatch.setattr(probe_replay.json, "JSONDecoder", RaisingDecoder)

    with pytest.raises(exc_type) as excinfo:
        probe_replay.replay_probe_ledger(journal)
    assert excinfo.value is sentinel


@pytest.mark.parametrize("exc_type", [MemoryError, KeyboardInterrupt, SystemExit])
def test_durability_seam_does_not_convert_escaping_exception_classes(
    monkeypatch,
    tmp_path,
    exc_type: type[BaseException],
) -> None:
    sentinel = exc_type("durability sentinel")

    def fail_fsync(_fd: int) -> None:
        raise sentinel

    monkeypatch.setattr(probe_durability, "_fsync_file", fail_fsync)
    fd = os.open(os.devnull, os.O_WRONLY)
    try:
        with pytest.raises(exc_type) as excinfo:
            data = probe_durability.encode_jsonl_record(start_record())
            probe_durability.append_jsonl_strict(fd, tmp_path, data)
    finally:
        os.close(fd)
    assert excinfo.value is sentinel


def test_deep_json_recursion_redacts_raw_exception_text(
    tmp_path,
    caplog,
) -> None:
    journal = tmp_path / "journal"
    path = probe_contract.probe_ledger_path(journal)
    path.parent.mkdir(parents=True, exist_ok=True)
    depth = 10_000
    path.write_text("[" * depth + "0" + "]" * depth + "\n", encoding="utf-8")

    with pytest.raises(probe_records.ProbeOperationError) as excinfo:
        probe_replay.replay_probe_ledger(journal)

    exc = excinfo.value
    assert exc.code == probe_contract.STABLE_ERROR_STALE_ATTEMPT
    assert str(exc) == probe_contract.STABLE_ERROR_STALE_ATTEMPT
    assert exc.__cause__ is None
    for forbidden in ("RecursionError", "maximum recursion"):
        assert forbidden not in str(exc)
        assert forbidden not in repr(exc)
        assert forbidden not in caplog.text
