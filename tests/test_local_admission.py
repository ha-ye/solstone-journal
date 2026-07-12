# SPDX-License-Identifier: AGPL-3.0-only
# Copyright (c) 2026 sol pbc

from __future__ import annotations

import asyncio
import errno
import json
import threading
import time

import pytest

from solstone.think.providers.local_admission import (
    LocalAdmissionTimeout,
    acquire_local_slot,
    acquire_local_slot_async,
    record_local_inference,
)


def _isolated_journal(monkeypatch, tmp_path):
    import solstone.think.utils as think_utils

    monkeypatch.setenv("SOLSTONE_JOURNAL", str(tmp_path))
    think_utils._journal_path_cache = None


def test_cross_thread_admission_never_exceeds_capacity(monkeypatch, tmp_path):
    _isolated_journal(monkeypatch, tmp_path)
    active = 0
    peak = 0
    lock = threading.Lock()

    def work() -> None:
        nonlocal active, peak
        with acquire_local_slot(2, 2):
            with lock:
                active += 1
                peak = max(peak, active)
            time.sleep(0.04)
            with lock:
                active -= 1

    threads = [threading.Thread(target=work) for _ in range(8)]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join()

    assert peak == 2


def test_failure_releases_permit(monkeypatch, tmp_path):
    _isolated_journal(monkeypatch, tmp_path)

    with pytest.raises(RuntimeError, match="boom"):
        with acquire_local_slot(1, 0.5):
            raise RuntimeError("boom")

    with acquire_local_slot(1, 0.1) as permit:
        assert permit.slot_index == 0


def test_sync_queue_timeout(monkeypatch, tmp_path):
    _isolated_journal(monkeypatch, tmp_path)
    first = acquire_local_slot(1, 0.1)
    try:
        with pytest.raises(LocalAdmissionTimeout):
            acquire_local_slot(1, 0.03)
    finally:
        first.release()


def test_exclusive_waits_for_single_slot_holder(monkeypatch, tmp_path):
    _isolated_journal(monkeypatch, tmp_path)
    holder = acquire_local_slot(2, 0.1)
    acquired: list[int] = []

    def wait_exclusive() -> None:
        with acquire_local_slot(2, 1.0, exclusive=True) as permit:
            acquired.append(permit.slot_index)

    thread = threading.Thread(target=wait_exclusive)
    thread.start()
    time.sleep(0.05)
    assert acquired == []

    holder.release()
    thread.join(timeout=1.0)

    assert not thread.is_alive()
    assert acquired == [0]


def test_exclusive_capacity_one_degenerates_to_single_slot(monkeypatch, tmp_path):
    _isolated_journal(monkeypatch, tmp_path)

    with acquire_local_slot(1, 0.1, exclusive=True) as permit:
        assert permit.slot_index == 0
        assert permit.capacity == 1


def test_exclusive_timeout_does_not_strand_partial_locks(monkeypatch, tmp_path):
    _isolated_journal(monkeypatch, tmp_path)
    slot_zero = acquire_local_slot(2, 0.1)
    slot_one = acquire_local_slot(2, 0.1)
    slot_zero.release()
    try:
        with pytest.raises(LocalAdmissionTimeout):
            acquire_local_slot(2, 0.03, exclusive=True)

        with acquire_local_slot(2, 0.1) as permit:
            assert permit.slot_index == 0
    finally:
        slot_one.release()


def test_exclusive_flock_error_releases_partial_slot_set(monkeypatch, tmp_path):
    _isolated_journal(monkeypatch, tmp_path)

    from solstone.think.providers import local_admission

    real_flock = local_admission.fcntl.flock

    def fail_second_slot(lock_file, operation):
        if (
            str(lock_file.name).endswith("slot-1.lock")
            and operation & local_admission.fcntl.LOCK_EX
            and operation & local_admission.fcntl.LOCK_NB
        ):
            raise OSError(errno.EIO, "simulated flock failure")
        return real_flock(lock_file, operation)

    monkeypatch.setattr(local_admission.fcntl, "flock", fail_second_slot)

    with pytest.raises(OSError):
        acquire_local_slot(2, 0.1, exclusive=True)

    with acquire_local_slot(1, 0.1) as permit:
        assert permit.slot_index == 0


def test_exclusive_failure_releases_all_slots(monkeypatch, tmp_path):
    _isolated_journal(monkeypatch, tmp_path)

    with pytest.raises(RuntimeError, match="boom"):
        with acquire_local_slot(2, 0.5, exclusive=True):
            raise RuntimeError("boom")

    first = acquire_local_slot(2, 0.1)
    try:
        second = acquire_local_slot(2, 0.1)
        try:
            assert {first.slot_index, second.slot_index} == {0, 1}
        finally:
            second.release()
    finally:
        first.release()


def test_async_queued_cancellation_does_not_leak(monkeypatch, tmp_path):
    _isolated_journal(monkeypatch, tmp_path)

    async def exercise() -> None:
        first = await acquire_local_slot_async(1, 0.5)
        queued = asyncio.create_task(acquire_local_slot_async(1, 1.0))
        await asyncio.sleep(0.05)
        queued.cancel()
        with pytest.raises(asyncio.CancelledError):
            await queued
        assert not list(
            (tmp_path / "health" / "local-inference-admission").glob("wait-*.ticket")
        )
        first.release()
        second = await acquire_local_slot_async(1, 0.1)
        second.release()

    asyncio.run(exercise())


def test_waiters_are_admitted_in_ticket_order(monkeypatch, tmp_path):
    _isolated_journal(monkeypatch, tmp_path)
    root = tmp_path / "health" / "local-inference-admission"
    first = acquire_local_slot(1, 1)
    order: list[int] = []

    def wait(index: int) -> None:
        with acquire_local_slot(1, 2):
            order.append(index)
            time.sleep(0.01)

    threads = []
    for index in range(5):
        thread = threading.Thread(target=wait, args=(index,))
        thread.start()
        threads.append(thread)
        deadline = time.monotonic() + 1
        while len(list(root.glob("wait-*.ticket"))) < index + 1:
            assert time.monotonic() < deadline
            time.sleep(0.005)

    first.release()
    for thread in threads:
        thread.join()

    assert order == list(range(5))


def test_stale_ticket_from_exited_owner_is_pruned(monkeypatch, tmp_path):
    _isolated_journal(monkeypatch, tmp_path)
    root = tmp_path / "health" / "local-inference-admission"
    root.mkdir(parents=True)
    stale = root / "wait-00000000000000000000-1-stale.ticket"
    stale.write_text("", encoding="utf-8")

    with acquire_local_slot(1, 0.2):
        assert not stale.exists()


def test_telemetry_is_durable_and_content_free(monkeypatch, tmp_path):
    _isolated_journal(monkeypatch, tmp_path)
    record_local_inference(
        {
            "timestamp": 1.0,
            "request_id": "abc",
            "provider": "local",
            "model": "local/qwen3.5-4b",
            "queue_wait_ms": 12.5,
            "outcome": "success",
        }
    )

    path = tmp_path / "health" / "local-inference" / time.strftime("%Y%m%d.jsonl")
    row = json.loads(path.read_text(encoding="utf-8"))
    assert row["request_id"] == "abc"
    assert "prompt" not in row
    assert "output" not in row
