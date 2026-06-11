# SPDX-License-Identifier: AGPL-3.0-only
# Copyright (c) 2026 sol pbc

import importlib
import itertools
import json
from types import SimpleNamespace
from unittest import mock


def test_task_queue_defers_submit_when_not_ready(monkeypatch):
    mod = importlib.import_module("solstone.think.supervisor")
    queue = mod.TaskQueue(on_queue_change=None, ready=False)

    started = []

    def fake_thread_start(self):
        started.append(self._args)

    monkeypatch.setattr(mod.threading.Thread, "start", fake_thread_start)

    ref = queue.submit(
        ["journal", "indexer", "--rescan"], ref="pending-ref", day="20260418"
    )

    assert ref == "pending-ref"
    assert started == []
    assert queue._pending == [
        {
            "refs": ["pending-ref"],
            "cmd": ["journal", "indexer", "--rescan"],
            "day": "20260418",
            "scheduler_name": None,
        }
    ]
    assert "outbound_approval" not in json.dumps(queue._pending)
    assert queue.collect_queue_counts() == {"pending": 1}


def test_task_queue_set_ready_drains_in_submission_order(monkeypatch):
    mod = importlib.import_module("solstone.think.supervisor")
    queue = mod.TaskQueue(on_queue_change=None, ready=False)

    started = []

    def fake_thread_start(self):
        started.append(self._args)

    monkeypatch.setattr(mod.threading.Thread, "start", fake_thread_start)

    queue.submit(["journal", "indexer", "--rescan"], ref="ref-1")
    queue.submit(["sol", "insight", "20260418"], ref="ref-2")
    queue.submit(["journal", "heartbeat"], ref="ref-3")

    queue.set_ready()

    assert [args[0] for args in started] == [["ref-1"], ["ref-2"], ["ref-3"]]
    assert [args[1] for args in started] == [
        ["journal", "indexer", "--rescan"],
        ["sol", "insight", "20260418"],
        ["journal", "heartbeat"],
    ]
    assert queue._pending == []


def test_task_queue_set_ready_dedupes_same_cmd_in_pending(monkeypatch):
    mod = importlib.import_module("solstone.think.supervisor")
    queue = mod.TaskQueue(on_queue_change=None, ready=False)

    started = []

    def fake_thread_start(self):
        started.append(self._args)

    monkeypatch.setattr(mod.threading.Thread, "start", fake_thread_start)

    queue.submit(["journal", "indexer", "--rescan"], ref="ref-1")
    queue.submit(["journal", "indexer", "--rescan"], ref="ref-2")

    queue.set_ready()

    assert len(started) == 1
    assert started[0][0] == ["ref-1"]
    assert queue._queues["indexer"] == [
        {
            "refs": ["ref-2"],
            "cmd": ["journal", "indexer", "--rescan"],
            "day": None,
            "scheduler_name": None,
        }
    ]
    assert "outbound_approval" not in json.dumps(queue._queues["indexer"])


def test_task_queue_ready_true_default_dispatches_immediately(monkeypatch):
    mod = importlib.import_module("solstone.think.supervisor")
    queue = mod.TaskQueue(on_queue_change=None)

    started = []

    def fake_thread_start(self):
        started.append(self._args)

    monkeypatch.setattr(mod.threading.Thread, "start", fake_thread_start)

    ref = queue.submit(["journal", "indexer", "--rescan"], ref="ready-ref")

    assert ref == "ready-ref"
    assert len(started) == 1
    assert started[0][0] == ["ready-ref"]
    assert queue._pending == []


def test_wait_for_convey_ready_success(caplog):
    mod = importlib.import_module("solstone.think.supervisor")
    caplog.set_level("INFO")
    convey_mp = SimpleNamespace(process=SimpleNamespace(poll=lambda: None))

    with mock.patch(
        "solstone.think.supervisor.is_solstone_up",
        side_effect=[False, False, True],
    ) as probe:
        assert mod.wait_for_convey_ready(convey_mp, timeout=1.0, interval=0.001) is True

    assert probe.call_count == 3
    assert "Convey ready after" in caplog.text


def test_wait_for_convey_ready_timeout(caplog):
    mod = importlib.import_module("solstone.think.supervisor")
    caplog.set_level("ERROR")
    convey_mp = SimpleNamespace(process=SimpleNamespace(poll=lambda: None))
    ticks = itertools.chain([0.0, 0.0, 0.1, 0.2, 0.3], itertools.repeat(0.35))

    with mock.patch("solstone.think.supervisor.is_solstone_up", return_value=False):
        with mock.patch(
            "solstone.think.supervisor.read_service_port", return_value=5015
        ):
            with mock.patch("solstone.think.supervisor.time.sleep", return_value=None):
                with mock.patch(
                    "solstone.think.supervisor.time.monotonic",
                    side_effect=lambda: next(ticks),
                ):
                    assert (
                        mod.wait_for_convey_ready(
                            convey_mp,
                            timeout=0.3,
                            interval=0.05,
                        )
                        is False
                    )

    assert "Convey not ready after" in caplog.text


def test_wait_for_convey_ready_convey_died(caplog):
    mod = importlib.import_module("solstone.think.supervisor")
    caplog.set_level("ERROR")
    convey_mp = SimpleNamespace(process=SimpleNamespace(poll=lambda: -11))

    with mock.patch("solstone.think.supervisor.is_solstone_up") as probe:
        assert (
            mod.wait_for_convey_ready(convey_mp, timeout=1.0, interval=0.001) is False
        )

    probe.assert_not_called()
    assert "Convey process exited during startup" in caplog.text


# require_solstone branch tests (down/tempfail/up/skip) live with the function in
# tests/test_think_utils.py::TestSolstoneGuard — they test a utils helper, not
# supervisor startup, and were a duplicate set here.
