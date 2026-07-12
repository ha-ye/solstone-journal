# SPDX-License-Identifier: AGPL-3.0-only
# Copyright (c) 2026 sol pbc

from __future__ import annotations

import asyncio
import errno
import json
import os
import subprocess
import sys
import threading
import time

import pytest

from solstone.think.providers.local_admission import (
    LocalAdmissionTimeout,
    LocalSlotLease,
    acquire_local_slot,
    acquire_local_slot_async,
    record_local_inference,
)


def _isolated_journal(monkeypatch, tmp_path):
    import solstone.think.utils as think_utils

    monkeypatch.setenv("SOLSTONE_JOURNAL", str(tmp_path))
    think_utils._journal_path_cache = None


def _admission_root(tmp_path):
    return tmp_path / "health" / "local-inference-admission"


def _wait_for(predicate, timeout_s: float = 1.0) -> None:
    deadline = time.monotonic() + timeout_s
    while not predicate():
        if time.monotonic() >= deadline:
            raise AssertionError("condition was not met before timeout")
        time.sleep(0.005)


def _wait_for_ticket_count(tmp_path, count: int) -> None:
    root = _admission_root(tmp_path)
    _wait_for(lambda: len(list(root.glob("wait-*.ticket"))) >= count)


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


def test_sync_cancel_event_drops_ticket_and_releases_queue(monkeypatch, tmp_path):
    _isolated_journal(monkeypatch, tmp_path)

    from solstone.think.providers import local_admission

    holder = acquire_local_slot(1, 0.1)
    cancel_event = threading.Event()
    errors: list[BaseException] = []

    def wait_for_slot() -> None:
        try:
            acquire_local_slot(1, 2.0, cancel_event=cancel_event)
        except BaseException as exc:
            errors.append(exc)

    thread = threading.Thread(target=wait_for_slot)
    thread.start()
    _wait_for_ticket_count(tmp_path, 1)

    cancel_event.set()
    thread.join(timeout=1.0)
    holder.release()

    assert not thread.is_alive()
    assert len(errors) == 1
    assert isinstance(errors[0], local_admission.LocalAdmissionCancelled)
    assert not list(_admission_root(tmp_path).glob("wait-*.ticket"))
    with acquire_local_slot(1, 0.1) as permit:
        assert permit.slot_index == 0


def test_sync_cancel_event_after_acquire_releases_permit(monkeypatch, tmp_path):
    _isolated_journal(monkeypatch, tmp_path)

    from solstone.think.providers import local_admission

    cancel_event = threading.Event()
    real_try_acquire = local_admission._try_acquire

    def cancel_after_acquire(capacity, started, root):
        permit = real_try_acquire(capacity, started, root)
        if permit is not None:
            cancel_event.set()
        return permit

    monkeypatch.setattr(local_admission, "_try_acquire", cancel_after_acquire)

    with pytest.raises(local_admission.LocalAdmissionCancelled):
        acquire_local_slot(1, 0.1, cancel_event=cancel_event)

    monkeypatch.setattr(local_admission, "_try_acquire", real_try_acquire)
    with acquire_local_slot(1, 0.1) as permit:
        assert permit.slot_index == 0


def test_lease_close_cancels_pending_reacquire_without_leaking(
    monkeypatch,
    tmp_path,
):
    _isolated_journal(monkeypatch, tmp_path)

    from solstone.think.providers import local_admission

    initial = acquire_local_slot(1, 0.1)
    lease = LocalSlotLease(
        capacity=1,
        deadline=time.monotonic() + 2.0,
        permit=initial,
    )
    lease.yield_slot()
    holder = acquire_local_slot(1, 0.1)
    errors: list[BaseException] = []

    def reacquire() -> None:
        try:
            lease.reacquire()
        except BaseException as exc:
            errors.append(exc)

    thread = threading.Thread(target=reacquire)
    thread.start()
    _wait_for_ticket_count(tmp_path, 1)

    lease.close()
    thread.join(timeout=1.0)
    holder.release()

    assert not thread.is_alive()
    assert len(errors) == 1
    assert isinstance(errors[0], local_admission.LocalAdmissionCancelled)
    assert not list(_admission_root(tmp_path).glob("wait-*.ticket"))
    with acquire_local_slot(1, 0.1) as permit:
        assert permit.slot_index == 0


def test_waiters_are_admitted_in_ticket_order(monkeypatch, tmp_path):
    _isolated_journal(monkeypatch, tmp_path)
    root = _admission_root(tmp_path)
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


def test_yielded_parent_reacquire_respects_existing_fifo_waiter(
    monkeypatch,
    tmp_path,
):
    _isolated_journal(monkeypatch, tmp_path)

    initial = acquire_local_slot(1, 0.1)
    lease = LocalSlotLease(
        capacity=1,
        deadline=time.monotonic() + 2.0,
        permit=initial,
    )
    order: list[str] = []
    waiter_entered = threading.Event()
    release_waiter = threading.Event()
    parent_reacquired = threading.Event()

    def waiter() -> None:
        with acquire_local_slot(1, 2.0):
            order.append("waiter")
            waiter_entered.set()
            assert release_waiter.wait(1.0)

    waiter_thread = threading.Thread(target=waiter)
    waiter_thread.start()
    _wait_for_ticket_count(tmp_path, 1)

    lease.yield_slot()
    assert waiter_entered.wait(1.0)

    def parent() -> None:
        lease.reacquire()
        order.append("parent")
        parent_reacquired.set()

    parent_thread = threading.Thread(target=parent)
    parent_thread.start()
    _wait_for_ticket_count(tmp_path, 1)

    assert order == ["waiter"]
    release_waiter.set()
    assert parent_reacquired.wait(1.0)
    parent_thread.join(timeout=1.0)
    waiter_thread.join(timeout=1.0)
    lease.close()

    assert not parent_thread.is_alive()
    assert not waiter_thread.is_alive()
    assert order == ["waiter", "parent"]


def test_cross_process_nested_acquire_succeeds_while_parent_yielded(
    monkeypatch,
    tmp_path,
):
    _isolated_journal(monkeypatch, tmp_path)

    initial = acquire_local_slot(1, 0.1)
    lease = LocalSlotLease(
        capacity=1,
        deadline=time.monotonic() + 2.0,
        permit=initial,
    )
    env = {**os.environ, "SOLSTONE_JOURNAL": str(tmp_path)}
    script = """
from solstone.think.providers.local_admission import acquire_local_slot
with acquire_local_slot(1, 1.0) as permit:
    assert permit.slot_index == 0
"""

    try:
        lease.yield_slot()
        completed = subprocess.run(
            [sys.executable, "-c", script],
            env=env,
            text=True,
            capture_output=True,
            timeout=2.0,
            check=False,
        )
        assert completed.returncode == 0, completed.stderr
        lease.reacquire()
    finally:
        lease.close()

    with acquire_local_slot(1, 0.1) as permit:
        assert permit.slot_index == 0


def test_capacity_two_two_parents_run_cross_process_nested_acquires(
    monkeypatch,
    tmp_path,
):
    _isolated_journal(monkeypatch, tmp_path)

    env = {**os.environ, "SOLSTONE_JOURNAL": str(tmp_path)}
    script = """
from solstone.think.providers.local_admission import acquire_local_slot
with acquire_local_slot(2, 1.0):
    pass
"""
    barrier = threading.Barrier(2)
    results: list[int] = []
    errors: list[BaseException] = []
    lock = threading.Lock()

    def parent() -> None:
        lease: LocalSlotLease | None = None
        try:
            permit = acquire_local_slot(2, 1.0)
            lease = LocalSlotLease(
                capacity=2,
                deadline=time.monotonic() + 3.0,
                permit=permit,
            )
            barrier.wait(timeout=1.0)
            lease.yield_slot()
            completed = subprocess.run(
                [sys.executable, "-c", script],
                env=env,
                text=True,
                capture_output=True,
                timeout=2.0,
                check=False,
            )
            with lock:
                results.append(completed.returncode)
            if completed.returncode != 0:
                raise AssertionError(completed.stderr)
            lease.reacquire()
        except BaseException as exc:
            with lock:
                errors.append(exc)
        finally:
            if lease is not None:
                lease.close()

    threads = [threading.Thread(target=parent) for _ in range(2)]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join(timeout=4.0)

    assert all(not thread.is_alive() for thread in threads)
    assert errors == []
    assert results == [0, 0]
    first = acquire_local_slot(2, 0.1)
    try:
        second = acquire_local_slot(2, 0.1)
        try:
            assert {first.slot_index, second.slot_index} == {0, 1}
        finally:
            second.release()
    finally:
        first.release()


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
