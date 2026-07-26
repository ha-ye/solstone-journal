# SPDX-License-Identifier: AGPL-3.0-only
# Copyright (c) 2026 sol pbc

from __future__ import annotations

import os
import signal
import subprocess
import sys
import threading
import time
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

import pytest

from solstone.think.sandbox_profile import (
    probe_contract,
    probe_durability,
    probe_records,
)
from solstone.think.sandbox_profile.probe_slot import acquire_probe_slot
from solstone.think.sandbox_profile.probe_writer import begin_probe_attempt
from tests._repo_inventory import assert_inventory_unchanged, repository_inventory
from tests.sandbox_profile import ATTEMPT_ID, FIXED_TS, RUN_ID


def _proof() -> str:
    return probe_contract.CAPABILITY_ORDER[0]


def _wait_for_file(path: Path, *, timeout: float = 5.0) -> None:
    deadline = time.monotonic() + timeout
    while not path.exists() and time.monotonic() < deadline:
        time.sleep(0.02)
    assert path.exists()


def _holder_env() -> dict[str, str]:
    env = os.environ.copy()
    env.setdefault("PYTHONUNBUFFERED", "1")
    return env


def _spawn_slot_holder(journal: Path, ready: Path, release: Path) -> subprocess.Popen:
    holder_code = f"""
import pathlib
import time
from solstone.think.sandbox_profile.probe_slot import acquire_probe_slot

slot = acquire_probe_slot(pathlib.Path({str(journal)!r}), run_id={RUN_ID!r})
pathlib.Path({str(ready)!r}).write_text("ready", encoding="utf-8")
while not pathlib.Path({str(release)!r}).exists():
    time.sleep(0.02)
slot.release()
"""
    return subprocess.Popen(
        [sys.executable, "-c", holder_code],
        cwd=Path.cwd(),
        env=_holder_env(),
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
    )


def _release_holder(holder: subprocess.Popen, release: Path) -> None:
    release.write_text("release", encoding="utf-8")
    stdout, stderr = holder.communicate(timeout=10)
    assert holder.returncode == 0, (stdout, stderr)


def _kill_holder(holder: subprocess.Popen) -> None:
    if holder.poll() is None:
        holder.kill()
        holder.wait(timeout=5)


def test_live_subprocess_owner_blocks_second_probe_slot(tmp_path) -> None:
    journal = tmp_path / "journal"
    ready = tmp_path / "ready"
    release = tmp_path / "release"
    holder = _spawn_slot_holder(journal, ready, release)
    try:
        _wait_for_file(ready)

        with pytest.raises(probe_records.ProbeOperationError) as excinfo:
            acquire_probe_slot(journal, run_id=RUN_ID)

        assert excinfo.value.code == probe_contract.STABLE_ERROR_PROBE_ACTIVE
        _release_holder(holder, release)
    finally:
        _kill_holder(holder)


def test_sigkill_releases_probe_slot_without_dead_owner_cleanup(tmp_path) -> None:
    journal = tmp_path / "journal"
    ready = tmp_path / "ready"
    release = tmp_path / "release"
    holder = _spawn_slot_holder(journal, ready, release)
    try:
        _wait_for_file(ready)

        os.kill(holder.pid, signal.SIGKILL)
        stdout, stderr = holder.communicate(timeout=10)
        assert holder.returncode == -signal.SIGKILL, (stdout, stderr)

        slot = acquire_probe_slot(journal, run_id=RUN_ID)
        try:
            assert slot.owned is True
        finally:
            slot.release()
    finally:
        _kill_holder(holder)


def test_release_and_reacquire_wait_for_blocked_append(
    monkeypatch,
    tmp_path,
) -> None:
    journal = tmp_path / "journal"
    proof = _proof()
    entered_write = threading.Event()
    finish_write = threading.Event()
    release_started = threading.Event()
    release_finished = threading.Event()
    reacquired = threading.Event()
    original_write = probe_durability._write_once

    def blocked_write(fd: int, data: bytes) -> int:
        entered_write.set()
        assert finish_write.wait(timeout=2)
        return original_write(fd, data)

    with acquire_probe_slot(journal, run_id=RUN_ID) as slot:
        writer = begin_probe_attempt(
            slot,
            selected=(proof,),
            execution_order=(proof,),
            attempt_id=ATTEMPT_ID,
            started_at=FIXED_TS,
        )
        writer.dispatch_contact(proof, lambda: None)
        writer.write_proof_terminal(
            proof=proof,
            state=probe_contract.PROOF_STATE_FAILED,
            checks=(),
            reason=probe_contract.PROOF_SPECIFIC_REASONS[proof][0],
            duration_ms=1,
            finished_at=FIXED_TS,
        )
        monkeypatch.setattr(probe_durability, "_write_once", blocked_write)

        def write_terminal() -> None:
            writer.write_attempt_terminal(finished_at=FIXED_TS)

        def release_then_reacquire() -> None:
            release_started.set()
            slot.release()
            release_finished.set()
            fresh = acquire_probe_slot(journal, run_id=RUN_ID)
            try:
                reacquired.set()
            finally:
                fresh.release()

        with ThreadPoolExecutor(max_workers=2) as executor:
            terminal_future = executor.submit(write_terminal)
            owner_future = None
            try:
                assert entered_write.wait(timeout=2)

                owner_future = executor.submit(release_then_reacquire)
                assert release_started.wait(timeout=2)
                time.sleep(0.05)
                assert not release_finished.is_set()
                assert not reacquired.is_set()
            finally:
                finish_write.set()
            terminal_future.result(timeout=2)
            if owner_future is not None:
                owner_future.result(timeout=2)

    assert release_finished.is_set()
    assert reacquired.is_set()


def test_old_lock_owner_detects_replaced_lock_after_new_owner_acquires(
    tmp_path,
) -> None:
    journal = tmp_path / "journal"
    proof = _proof()
    old_slot = acquire_probe_slot(journal, run_id=RUN_ID)
    lock_path = probe_contract.probe_lock_path(journal)
    lock_path.unlink()
    lock_path.write_bytes(b"")
    new_slot = acquire_probe_slot(journal, run_id=RUN_ID)
    try:
        before = repository_inventory(journal)
        with pytest.raises(probe_records.ProbeOperationError) as excinfo:
            begin_probe_attempt(
                old_slot,
                selected=(proof,),
                execution_order=(proof,),
                attempt_id=ATTEMPT_ID,
                started_at=FIXED_TS,
            )
        after = repository_inventory(journal)
    finally:
        new_slot.release()
        old_slot.release()

    assert excinfo.value.code == probe_contract.STABLE_ERROR_STALE_ATTEMPT
    assert_inventory_unchanged(before, after)


def test_release_and_cancellation_wait_for_in_flight_contact(tmp_path) -> None:
    proof = _proof()

    release_journal = tmp_path / "release"
    contact_entered = threading.Event()
    finish_contact = threading.Event()
    release_finished = threading.Event()
    with acquire_probe_slot(release_journal, run_id=RUN_ID) as slot:
        writer = begin_probe_attempt(
            slot,
            selected=(proof,),
            execution_order=(proof,),
            attempt_id=ATTEMPT_ID,
            started_at=FIXED_TS,
        )

        def contact_operation() -> None:
            contact_entered.set()
            assert finish_contact.wait(timeout=2)

        contact_thread = threading.Thread(
            target=lambda: writer.dispatch_contact(proof, contact_operation)
        )
        release_thread = threading.Thread(
            target=lambda: (slot.release(), release_finished.set())
        )
        contact_thread.start()
        release_thread_started = False
        try:
            assert contact_entered.wait(timeout=2)
            release_thread.start()
            release_thread_started = True
            time.sleep(0.05)
            assert not release_finished.is_set()
        finally:
            finish_contact.set()
            contact_thread.join(timeout=2)
            if release_thread_started:
                release_thread.join(timeout=2)

    assert not contact_thread.is_alive()
    assert not release_thread.is_alive()
    assert release_finished.is_set()

    cancel_journal = tmp_path / "cancel"
    selected = probe_contract.CAPABILITY_ORDER[:2]
    contact_entered.clear()
    finish_contact.clear()
    cancel_finished = threading.Event()
    with acquire_probe_slot(cancel_journal, run_id=RUN_ID) as slot:
        writer = begin_probe_attempt(
            slot,
            selected=selected,
            execution_order=selected,
            attempt_id=ATTEMPT_ID,
            started_at=FIXED_TS,
        )
        contact_thread = threading.Thread(
            target=lambda: writer.dispatch_contact(selected[0], contact_operation)
        )

        def cancel_attempt() -> None:
            writer.write_cancelled_attempt(
                proof=selected[0],
                state=probe_contract.PROOF_STATE_FAILED,
                checks=probe_contract.PROOF_CHECKS[selected[0]][:1],
                duration_ms=1,
                finished_at=FIXED_TS,
            )
            cancel_finished.set()

        contact_thread.start()
        with ThreadPoolExecutor(max_workers=1) as executor:
            cancel_future = None
            try:
                assert contact_entered.wait(timeout=2)
                cancel_future = executor.submit(cancel_attempt)
                time.sleep(0.05)
                assert not cancel_finished.is_set()
            finally:
                finish_contact.set()
                contact_thread.join(timeout=2)
            if cancel_future is not None:
                cancel_future.result(timeout=2)

    assert not contact_thread.is_alive()
    assert cancel_finished.is_set()
