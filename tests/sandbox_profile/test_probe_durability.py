# SPDX-License-Identifier: AGPL-3.0-only
# Copyright (c) 2026 sol pbc

from __future__ import annotations

import os
from pathlib import Path

import pytest

from solstone.think.sandbox_profile import (
    probe_contract,
    probe_durability,
    probe_records,
    probe_slot,
)
from solstone.think.sandbox_profile.probe_slot import acquire_probe_slot
from solstone.think.sandbox_profile.probe_writer import begin_probe_attempt
from tests._repo_inventory import assert_inventory_unchanged, repository_inventory
from tests.sandbox_profile import (
    ATTEMPT_ID,
    FIXED_TS,
    OTHER_ATTEMPT_ID,
    RUN_ID,
    start_record,
)


class _DummyLease:
    path = Path("lock")

    @property
    def owned(self) -> bool:
        return True

    def release(self) -> None:
        return None


def _slot(journal: Path) -> probe_slot.ProbeSlot:
    return probe_slot.ProbeSlot(
        journal_path=journal,
        run_id=RUN_ID,
        ledger_path=probe_contract.probe_ledger_path(journal),
        lock_path=probe_contract.probe_lock_path(journal),
        attempts_parent_path=probe_contract.probe_attempts_parent_path(journal),
        _lease=_DummyLease(),
    )


def test_append_order_is_write_file_fsync_then_directory_fsync(monkeypatch, tmp_path):
    events: list[str] = []

    def open_append(_path):
        events.append("open")
        return os.open(os.devnull, os.O_WRONLY)

    monkeypatch.setattr(
        probe_durability,
        "_mkdir",
        lambda *_args, **_kwargs: events.append("mkdir"),
    )
    monkeypatch.setattr(
        probe_durability,
        "_open_append",
        open_append,
    )
    monkeypatch.setattr(
        probe_durability,
        "_write_once",
        lambda _handle, data: events.append("write") or len(data),
    )
    monkeypatch.setattr(
        probe_durability,
        "_fsync_file",
        lambda _handle: events.append("file_fsync"),
    )
    monkeypatch.setattr(
        probe_durability,
        "_fsync_directory",
        lambda _path: events.append("dir_fsync"),
    )

    probe_durability.append_jsonl_strict(tmp_path / "ledger.jsonl", start_record())

    assert events == ["mkdir", "open", "write", "file_fsync", "dir_fsync"]


@pytest.mark.parametrize(
    "seam",
    [
        "_mkdir",
        "_open_append",
        "_write_once",
        "_fsync_file",
        "_fsync_directory",
    ],
)
def test_append_faults_surface_record_write_failed(monkeypatch, tmp_path, seam) -> None:
    if seam == "_write_once":
        monkeypatch.setattr(
            probe_durability,
            seam,
            lambda *_args: (_ for _ in ()).throw(OSError("secret")),
        )
    else:
        monkeypatch.setattr(
            probe_durability,
            seam,
            lambda *_args, **_kwargs: (_ for _ in ()).throw(OSError("secret")),
        )

    with pytest.raises(probe_records.ProbeOperationError) as excinfo:
        probe_durability.append_jsonl_strict(tmp_path / "ledger.jsonl", start_record())

    assert excinfo.value.code == probe_contract.STABLE_ERROR_RECORD_WRITE_FAILED
    assert "secret" not in str(excinfo.value)
    assert excinfo.value.__cause__ is None


def test_append_short_write_surfaces_record_write_failed(monkeypatch, tmp_path) -> None:
    monkeypatch.setattr(probe_durability, "_write_once", lambda _handle, _data: 0)

    with pytest.raises(probe_records.ProbeOperationError) as excinfo:
        probe_durability.append_jsonl_strict(tmp_path / "ledger.jsonl", start_record())

    assert excinfo.value.code == probe_contract.STABLE_ERROR_RECORD_WRITE_FAILED


def test_attempt_parent_fsync_failure_surfaces_record_write_failed(
    monkeypatch, tmp_path
) -> None:
    def fail_directory_fsync(_path):
        raise OSError("secret")

    monkeypatch.setattr(probe_durability, "_fsync_directory", fail_directory_fsync)

    with pytest.raises(probe_records.ProbeOperationError) as excinfo:
        probe_slot.create_attempt_directory(_slot(tmp_path / "journal"), ATTEMPT_ID)

    assert excinfo.value.code == probe_contract.STABLE_ERROR_RECORD_WRITE_FAILED
    assert "secret" not in str(excinfo.value)


def test_attempt_directory_collision_fails_closed_to_stale(tmp_path) -> None:
    slot = _slot(tmp_path / "journal")
    probe_slot.create_attempt_directory(slot, ATTEMPT_ID)

    with pytest.raises(probe_records.ProbeOperationError) as excinfo:
        probe_slot.create_attempt_directory(slot, ATTEMPT_ID)

    assert excinfo.value.code == probe_contract.STABLE_ERROR_STALE_ATTEMPT


def test_start_record_write_failure_poisons_slot_before_retry(
    monkeypatch, tmp_path
) -> None:
    journal = tmp_path / "journal"
    proof = probe_contract.CAPABILITY_ORDER[0]
    append_calls = 0

    def fail_append(_path, _record):
        nonlocal append_calls
        append_calls += 1
        probe_records.raise_probe_error(probe_contract.STABLE_ERROR_RECORD_WRITE_FAILED)

    with acquire_probe_slot(journal, run_id=RUN_ID) as slot:
        monkeypatch.setattr(probe_durability, "append_jsonl_strict", fail_append)
        with pytest.raises(probe_records.ProbeOperationError) as first:
            begin_probe_attempt(
                slot,
                selected=(proof,),
                execution_order=(proof,),
                attempt_id=ATTEMPT_ID,
                started_at=FIXED_TS,
            )
        assert first.value.code == probe_contract.STABLE_ERROR_RECORD_WRITE_FAILED
        assert append_calls == 1

        before = repository_inventory(journal)
        with pytest.raises(probe_records.ProbeOperationError) as second:
            begin_probe_attempt(
                slot,
                selected=(proof,),
                execution_order=(proof,),
                attempt_id=OTHER_ATTEMPT_ID,
                started_at=FIXED_TS,
            )
        after = repository_inventory(journal)

    assert second.value.code == probe_contract.STABLE_ERROR_RECORD_WRITE_FAILED
    assert append_calls == 1
    assert not (
        probe_contract.probe_attempts_parent_path(journal) / OTHER_ATTEMPT_ID
    ).exists()
    assert_inventory_unchanged(before, after)


def test_attempt_directory_collision_poisons_slot_before_retry(tmp_path) -> None:
    journal = tmp_path / "journal"
    proof = probe_contract.CAPABILITY_ORDER[0]

    with acquire_probe_slot(journal, run_id=RUN_ID) as slot:
        probe_slot.create_attempt_directory(slot, ATTEMPT_ID)
        with pytest.raises(probe_records.ProbeOperationError) as first:
            begin_probe_attempt(
                slot,
                selected=(proof,),
                execution_order=(proof,),
                attempt_id=ATTEMPT_ID,
                started_at=FIXED_TS,
            )
        assert first.value.code == probe_contract.STABLE_ERROR_STALE_ATTEMPT

        with pytest.raises(probe_records.ProbeOperationError) as second:
            begin_probe_attempt(
                slot,
                selected=(proof,),
                execution_order=(proof,),
                attempt_id=OTHER_ATTEMPT_ID,
                started_at=FIXED_TS,
            )

    assert second.value.code == probe_contract.STABLE_ERROR_STALE_ATTEMPT
    assert not (
        probe_contract.probe_attempts_parent_path(journal) / OTHER_ATTEMPT_ID
    ).exists()


def test_writer_record_write_failure_poisons_later_contact(
    monkeypatch, tmp_path
) -> None:
    journal = tmp_path / "journal"
    proof = probe_contract.CAPABILITY_ORDER[0]
    with acquire_probe_slot(journal, run_id=RUN_ID) as slot:
        writer = begin_probe_attempt(
            slot,
            selected=(proof,),
            execution_order=(proof,),
            attempt_id=ATTEMPT_ID,
            started_at=FIXED_TS,
        )

        def fail_append(_path, _record):
            probe_records.raise_probe_error(
                probe_contract.STABLE_ERROR_RECORD_WRITE_FAILED
            )

        monkeypatch.setattr(probe_durability, "append_jsonl_strict", fail_append)
        with pytest.raises(probe_records.ProbeOperationError) as excinfo:
            writer.write_proof_terminal(
                proof=proof,
                state=probe_contract.PROOF_STATE_PASSED,
                checks=probe_contract.PROOF_CHECKS[proof],
                reason=None,
                duration_ms=1,
                finished_at=FIXED_TS,
            )
        assert excinfo.value.code == probe_contract.STABLE_ERROR_RECORD_WRITE_FAILED

        with pytest.raises(probe_records.ProbeOperationError) as blocked:
            writer.assert_contact_allowed(proof)
        assert blocked.value.code == probe_contract.STABLE_ERROR_RECORD_WRITE_FAILED
