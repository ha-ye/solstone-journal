# SPDX-License-Identifier: AGPL-3.0-only
# Copyright (c) 2026 sol pbc

"""Tests for handle_shutdown's reap pass."""

import os
import signal
import threading
from unittest.mock import MagicMock

import pytest

import solstone.think.supervisor as supervisor


class FakeManaged:
    def __init__(self, name, exits_after_terminate=True):
        self.name = name
        self.process = MagicMock()
        self.process.pid = 12345
        self._running = True
        self._exits_after_terminate = exits_after_terminate
        self.process.terminate.side_effect = self._on_terminate
        self.process.kill.side_effect = self._on_kill

    def is_running(self):
        return self._running

    def _on_terminate(self):
        if self._exits_after_terminate:
            self._running = False

    def _on_kill(self):
        self._running = False


def test_reap_terminates_and_kills_survivor(monkeypatch):
    well_behaved = FakeManaged("well", exits_after_terminate=True)
    stuck = FakeManaged("stuck", exits_after_terminate=False)
    monkeypatch.setattr(supervisor, "_managed_procs", [well_behaved, stuck])
    monkeypatch.setattr(supervisor, "shutdown_requested", False)
    monkeypatch.setattr(supervisor, "HANDLE_SHUTDOWN_REAP_S", 0.0)

    with pytest.raises(KeyboardInterrupt):
        supervisor.handle_shutdown(15, None)

    assert well_behaved.process.terminate.called
    assert stuck.process.terminate.called
    assert not well_behaved.process.kill.called
    assert stuck.process.kill.called


def test_reap_idempotent_on_second_call(monkeypatch):
    proc = FakeManaged("svc", exits_after_terminate=True)
    monkeypatch.setattr(supervisor, "_managed_procs", [proc])
    monkeypatch.setattr(supervisor, "shutdown_requested", False)
    monkeypatch.setattr(supervisor, "HANDLE_SHUTDOWN_REAP_S", 0.0)

    with pytest.raises(KeyboardInterrupt):
        supervisor.handle_shutdown(15, None)
    assert proc.process.terminate.call_count == 1

    supervisor.handle_shutdown(15, None)

    assert proc.process.terminate.call_count == 1


def test_reap_empty_managed_procs(monkeypatch):
    monkeypatch.setattr(supervisor, "_managed_procs", [])
    monkeypatch.setattr(supervisor, "shutdown_requested", False)

    with pytest.raises(KeyboardInterrupt):
        supervisor.handle_shutdown(15, None)


def test_reap_swallows_oserror_on_kill(monkeypatch, caplog):
    bad = FakeManaged("bad", exits_after_terminate=False)
    bad.process.kill.side_effect = OSError("permission denied")
    monkeypatch.setattr(supervisor, "_managed_procs", [bad])
    monkeypatch.setattr(supervisor, "shutdown_requested", False)
    monkeypatch.setattr(supervisor, "HANDLE_SHUTDOWN_REAP_S", 0.0)
    caplog.set_level("ERROR")

    with pytest.raises(KeyboardInterrupt):
        supervisor.handle_shutdown(15, None)

    assert "shutdown: kill failed for bad" in caplog.text


def test_parent_watcher_waits_for_pipe_eof_without_ppid_poll(monkeypatch):
    read_fd, write_fd = os.pipe()
    getppid = MagicMock()
    monkeypatch.setattr(supervisor.os, "getppid", getppid)
    os.close(write_fd)
    try:
        assert supervisor.wait_until_parent_gone(read_fd) == "eof"
    finally:
        os.close(read_fd)
    getppid.assert_not_called()


@pytest.mark.parametrize("fd_kind", ["devnull", "bad"])
def test_parent_watcher_non_pipe_and_bad_fd_fall_back_to_ppid(fd_kind, monkeypatch):
    fd = os.open(os.devnull, os.O_RDONLY) if fd_kind == "devnull" else -1
    pids = iter([999, 1])
    monkeypatch.setattr(supervisor.os, "getppid", lambda: next(pids))
    monkeypatch.setattr(supervisor.time, "sleep", lambda _seconds: None)
    monkeypatch.setattr(
        supervisor.os,
        "read",
        lambda *_args, **_kwargs: pytest.fail("non-pipe fd should not be read"),
    )
    try:
        assert supervisor.wait_until_parent_gone(fd) == "orphaned"
    finally:
        if fd_kind == "devnull":
            os.close(fd)


def test_parent_watcher_shutdown_deadline_sends_sigterm_once_kills_children():
    sent_event = threading.Event()
    managed = FakeManaged("stuck", exits_after_terminate=False)
    kill = MagicMock()
    exit_now = MagicMock()

    supervisor.enforce_parent_death_shutdown_deadline(
        "eof",
        ceiling=0,
        managed_procs=[managed],
        sent_event=sent_event,
        kill=kill,
        exit_now=exit_now,
        sleep=lambda _seconds: None,
    )

    kill.assert_called_once_with(os.getpid(), signal.SIGTERM)
    assert managed.process.kill.called
    exit_now.assert_called_once_with(1)


def test_parent_watcher_shutdown_deadline_does_not_resend_sigterm():
    sent_event = threading.Event()
    sent_event.set()
    managed = FakeManaged("stuck", exits_after_terminate=False)
    kill = MagicMock()
    exit_now = MagicMock()

    supervisor.enforce_parent_death_shutdown_deadline(
        "eof",
        ceiling=0,
        managed_procs=[managed],
        sent_event=sent_event,
        kill=kill,
        exit_now=exit_now,
        sleep=lambda _seconds: None,
    )

    kill.assert_not_called()
    assert managed.process.kill.called
    exit_now.assert_called_once_with(1)


def test_app_supervised_graceful_budget_stays_under_hard_ceiling():
    assert (
        supervisor.app_supervised_graceful_budget_s()
        < supervisor.APP_SUPERVISED_SHUTDOWN_CEILING_S
    )


def test_stop_process_applies_timeout_cap(monkeypatch):
    managed = MagicMock()
    managed.name = "spl"
    supervisor._SERVICE_STATE.clear()
    supervisor._SERVICE_STATE["spl"] = {
        "restart": True,
        "shutdown_timeout": 15,
    }
    calls: list[tuple[float, str]] = []
    monkeypatch.setattr(
        supervisor,
        "_terminate_managed",
        lambda _managed, timeout, *, reason: calls.append((timeout, reason)),
    )

    supervisor._stop_process(managed, timeout_cap=2.0)

    assert calls == [(2.0, "shutdown")]
    managed.cleanup.assert_called_once_with()


def test_stop_process_defaults_to_service_timeout(monkeypatch):
    managed = MagicMock()
    managed.name = "spl"
    supervisor._SERVICE_STATE.clear()
    supervisor._SERVICE_STATE["spl"] = {
        "restart": True,
        "shutdown_timeout": 15,
    }
    calls: list[tuple[float, str]] = []
    monkeypatch.setattr(
        supervisor,
        "_terminate_managed",
        lambda _managed, timeout, *, reason: calls.append((timeout, reason)),
    )

    supervisor._stop_process(managed)

    assert calls == [(15, "shutdown")]
    managed.cleanup.assert_called_once_with()
