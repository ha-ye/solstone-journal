# SPDX-License-Identifier: AGPL-3.0-only
# Copyright (c) 2026 sol pbc

from __future__ import annotations

import os

import pytest

from solstone.think.sandbox_profile import (
    probe_contract,
    probe_durability,
    probe_records,
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


def test_append_order_is_write_file_fsync_then_directory_fsync(monkeypatch, tmp_path):
    events: list[str] = []
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

    fd = os.open(os.devnull, os.O_WRONLY)
    try:
        data = probe_durability.encode_jsonl_record(start_record())
        probe_durability.append_jsonl_strict(fd, tmp_path, data)
    finally:
        os.close(fd)

    assert events == ["write", "file_fsync", "dir_fsync"]


@pytest.mark.parametrize(
    "seam",
    [
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

    fd = os.open(os.devnull, os.O_WRONLY)
    try:
        with pytest.raises(probe_records.ProbeOperationError) as excinfo:
            data = probe_durability.encode_jsonl_record(start_record())
            probe_durability.append_jsonl_strict(fd, tmp_path, data)
    finally:
        os.close(fd)

    assert excinfo.value.code == probe_contract.STABLE_ERROR_RECORD_WRITE_FAILED
    assert "secret" not in str(excinfo.value)
    assert excinfo.value.__cause__ is None


def test_append_short_write_surfaces_record_write_failed(monkeypatch, tmp_path) -> None:
    monkeypatch.setattr(probe_durability, "_write_once", lambda _handle, _data: 0)

    fd = os.open(os.devnull, os.O_WRONLY)
    try:
        with pytest.raises(probe_records.ProbeOperationError) as excinfo:
            data = probe_durability.encode_jsonl_record(start_record())
            probe_durability.append_jsonl_strict(fd, tmp_path, data)
    finally:
        os.close(fd)

    assert excinfo.value.code == probe_contract.STABLE_ERROR_RECORD_WRITE_FAILED


def test_attempt_parent_fsync_failure_surfaces_record_write_failed(
    monkeypatch, tmp_path
) -> None:
    def fail_directory_fsync(_path):
        raise OSError("secret")

    monkeypatch.setattr(probe_durability, "_fsync_directory", fail_directory_fsync)

    with acquire_probe_slot(tmp_path / "journal", run_id=RUN_ID) as slot:
        proof = probe_contract.CAPABILITY_ORDER[0]
        with pytest.raises(probe_records.ProbeOperationError) as excinfo:
            begin_probe_attempt(
                slot,
                selected=(proof,),
                execution_order=(proof,),
                attempt_id=ATTEMPT_ID,
                started_at=FIXED_TS,
            )

    assert excinfo.value.code == probe_contract.STABLE_ERROR_RECORD_WRITE_FAILED
    assert "secret" not in str(excinfo.value)


def test_attempt_directory_collision_fails_closed_to_stale(tmp_path) -> None:
    journal = tmp_path / "journal"
    proof = probe_contract.CAPABILITY_ORDER[0]

    with acquire_probe_slot(journal, run_id=RUN_ID) as slot:
        attempt_path = probe_contract.probe_attempts_parent_path(journal) / ATTEMPT_ID
        attempt_path.mkdir(parents=True)
        with pytest.raises(probe_records.ProbeOperationError) as excinfo:
            begin_probe_attempt(
                slot,
                selected=(proof,),
                execution_order=(proof,),
                attempt_id=ATTEMPT_ID,
                started_at=FIXED_TS,
            )

    assert excinfo.value.code == probe_contract.STABLE_ERROR_STALE_ATTEMPT


def test_start_record_write_failure_poisons_slot_before_retry(
    monkeypatch, tmp_path
) -> None:
    journal = tmp_path / "journal"
    proof = probe_contract.CAPABILITY_ORDER[0]
    write_calls = 0

    def fail_write(_fd, _data):
        nonlocal write_calls
        write_calls += 1
        raise OSError("secret")

    with acquire_probe_slot(journal, run_id=RUN_ID) as slot:
        monkeypatch.setattr(probe_durability, "_write_once", fail_write)
        with pytest.raises(probe_records.ProbeOperationError) as first:
            begin_probe_attempt(
                slot,
                selected=(proof,),
                execution_order=(proof,),
                attempt_id=ATTEMPT_ID,
                started_at=FIXED_TS,
            )
        assert first.value.code == probe_contract.STABLE_ERROR_RECORD_WRITE_FAILED
        assert write_calls == 1

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

    assert second.value.code == probe_contract.STABLE_ERROR_INTERNAL_ERROR
    assert write_calls == 1
    assert not (
        probe_contract.probe_attempts_parent_path(journal) / OTHER_ATTEMPT_ID
    ).exists()
    assert_inventory_unchanged(before, after)


def test_attempt_directory_collision_poisons_slot_before_retry(tmp_path) -> None:
    journal = tmp_path / "journal"
    proof = probe_contract.CAPABILITY_ORDER[0]

    with acquire_probe_slot(journal, run_id=RUN_ID) as slot:
        attempt_path = probe_contract.probe_attempts_parent_path(journal) / ATTEMPT_ID
        attempt_path.mkdir(parents=True)
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

    assert second.value.code == probe_contract.STABLE_ERROR_INTERNAL_ERROR
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
        writer.dispatch_contact(proof, lambda: None)

        def fail_write(_fd, _data):
            raise OSError("secret")

        monkeypatch.setattr(probe_durability, "_write_once", fail_write)
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
            writer.dispatch_contact(proof, lambda: None)
        assert blocked.value.code == probe_contract.STABLE_ERROR_RECORD_WRITE_FAILED
