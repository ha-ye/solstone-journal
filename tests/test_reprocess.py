# SPDX-License-Identifier: AGPL-3.0-only
# Copyright (c) 2026 sol pbc

from __future__ import annotations

import importlib
import os
from datetime import date, timedelta
from pathlib import Path
from unittest.mock import Mock

import pytest

from solstone.think import reprocess
from tests.helpers.module_mocks import capturing_thread_constructor, module_mock

DAY = "20250115"
SEGMENT = "120000_300"
UNREACHABLE = "supervisor not reachable - start it (journal start), then retry\n"


def _invoke_reprocess(monkeypatch, capsys, journal: Path, *argv: str):
    monkeypatch.setenv("SOLSTONE_JOURNAL", str(journal))
    monkeypatch.setattr("sys.argv", ["journal reprocess", *argv])

    exit_code = 0
    try:
        reprocess.main()
    except SystemExit as exc:
        if isinstance(exc.code, int):
            exit_code = exc.code
        elif exc.code is None:
            exit_code = 0
        else:
            exit_code = 1

    captured = capsys.readouterr()
    return exit_code, captured.out, captured.err


def _seed_segment(journal: Path, day: str = DAY) -> Path:
    segment_dir = journal / "chronicle" / day / "default" / SEGMENT
    segment_dir.mkdir(parents=True)
    return segment_dir


def _touch_marker(journal: Path, day: str, name: str, ns: int) -> Path:
    marker = journal / "chronicle" / day / "health" / name
    marker.parent.mkdir(parents=True, exist_ok=True)
    marker.touch()
    os.utime(marker, ns=(ns, ns))
    return marker


class _ActiveTaskStub:
    def __init__(self, cmd):
        self.cmd = cmd
        self.start_time = 100.0


class _SupervisorRequestHarness:
    def __init__(self, monkeypatch, *, active_cmd=None, fail_day: str | None = None):
        self.mod = importlib.import_module("solstone.think.supervisor")
        self.queue = self.mod.TaskQueue(on_queue_change=None)
        self.fail_day = fail_day
        active_cmd = active_cmd or ["journal", "think", "-v", "--day", "20250114"]
        partition = self.mod.TaskQueue.get_command_name(active_cmd)
        self.queue._active["active-ref"] = _ActiveTaskStub(active_cmd)
        self.queue._running[partition] = {
            "ref": "active-ref",
            "thread": None,
            "scheduler_name": None,
        }
        self.submit_calls = []
        original_submit = self.queue.submit

        def submit_spy(cmd, ref=None, day=None, scheduler_name=None):
            self.submit_calls.append(
                {
                    "cmd": cmd,
                    "ref": ref,
                    "day": day,
                    "scheduler_name": scheduler_name,
                }
            )
            return original_submit(cmd, ref=ref, day=day, scheduler_name=scheduler_name)

        self.spawned = []
        monkeypatch.setattr(
            self.mod,
            "threading",
            module_mock(
                self.mod.threading,
                Thread=capturing_thread_constructor(
                    self.spawned,
                    capture=lambda thread: thread._args,
                ),
            ),
        )
        monkeypatch.setattr(self.queue, "submit", submit_spy)
        monkeypatch.setattr(self.mod, "_task_queue", self.queue)
        monkeypatch.setattr(self.mod, "_supervisor_callosum", Mock())

    def callosum_send(self, tract, event, **fields):
        if fields.get("day") == self.fail_day:
            return False
        self.mod._handle_task_request({"tract": tract, "event": event, **fields})
        return True


def test_process_now_pending_day_sends_drain_and_preserves_marker(
    tmp_path, monkeypatch, capsys
):
    journal = tmp_path / "journal"
    _seed_segment(journal)
    stream = _touch_marker(journal, DAY, "stream.updated", 2_000_000_000)
    before = stream.stat().st_mtime_ns
    send = Mock(return_value=True)
    monkeypatch.setattr(reprocess, "callosum_send", send)

    code, out, err = _invoke_reprocess(monkeypatch, capsys, journal, DAY)

    assert code == 0
    assert out == f"reprocess (process-now) submitted for {DAY}\n"
    assert err == ""
    send.assert_called_once_with("supervisor", "drain", day=DAY)
    assert stream.stat().st_mtime_ns == before
    assert not (stream.parent / "daily.updated").exists()


def test_process_now_complete_day_is_noop_and_preserves_markers(
    tmp_path, monkeypatch, capsys
):
    journal = tmp_path / "journal"
    _seed_segment(journal)
    stream = _touch_marker(journal, DAY, "stream.updated", 1_000_000_000)
    daily = _touch_marker(journal, DAY, "daily.updated", 2_000_000_000)
    before = (stream.stat().st_mtime_ns, daily.stat().st_mtime_ns)
    send = Mock(return_value=True)
    monkeypatch.setattr(reprocess, "callosum_send", send)

    code, out, err = _invoke_reprocess(monkeypatch, capsys, journal, DAY)

    assert code == 0
    assert (
        out
        == f"day {DAY} already complete; use --from-scratch to force a full re-run\n"
    )
    assert err == ""
    send.assert_not_called()
    assert (stream.stat().st_mtime_ns, daily.stat().st_mtime_ns) == before


def test_from_scratch_sends_request_and_preserves_marker(tmp_path, monkeypatch, capsys):
    journal = tmp_path / "journal"
    _seed_segment(journal)
    stream = _touch_marker(journal, DAY, "stream.updated", 1_000_000_000)
    _touch_marker(journal, DAY, "daily.updated", 2_000_000_000)
    before = stream.stat().st_mtime_ns
    send = Mock(return_value=True)
    monkeypatch.setattr(reprocess, "callosum_send", send)

    code, out, err = _invoke_reprocess(
        monkeypatch, capsys, journal, DAY, "--from-scratch"
    )

    assert code == 0
    assert out == f"reprocess (from-scratch) submitted for {DAY}\n"
    assert err == ""
    send.assert_called_once_with(
        "supervisor",
        "request",
        cmd=["journal", "think", "-v", "--day", DAY, "--from-scratch"],
        day=DAY,
        queue_if_active_cmd_differs=True,
    )
    assert stream.stat().st_mtime_ns == before


def test_mark_updated_pending_day_touches_marker_and_sends_drain(
    tmp_path, monkeypatch, capsys
):
    journal = tmp_path / "journal"
    _seed_segment(journal)
    stream = _touch_marker(journal, DAY, "stream.updated", 1_000_000_000)
    before = stream.stat().st_mtime_ns
    send = Mock(return_value=True)
    monkeypatch.setattr(reprocess, "callosum_send", send)

    code, out, err = _invoke_reprocess(
        monkeypatch, capsys, journal, DAY, "--mark-updated"
    )

    assert code == 0
    assert out == f"reprocess (mark-updated) submitted for {DAY}\n"
    assert err == ""
    send.assert_called_once_with("supervisor", "drain", day=DAY)
    assert stream.stat().st_mtime_ns > before


def test_mark_updated_complete_day_still_touches_marker_and_sends_drain(
    tmp_path, monkeypatch, capsys
):
    journal = tmp_path / "journal"
    _seed_segment(journal)
    stream = _touch_marker(journal, DAY, "stream.updated", 1_000_000_000)
    daily = _touch_marker(journal, DAY, "daily.updated", 2_000_000_000)
    before = stream.stat().st_mtime_ns
    send = Mock(return_value=True)
    monkeypatch.setattr(reprocess, "callosum_send", send)

    code, out, err = _invoke_reprocess(
        monkeypatch, capsys, journal, DAY, "--mark-updated"
    )

    assert code == 0
    assert out == f"reprocess (mark-updated) submitted for {DAY}\n"
    assert err == ""
    send.assert_called_once_with("supervisor", "drain", day=DAY)
    assert stream.stat().st_mtime_ns > before
    assert daily.exists()


def test_mark_updated_and_from_scratch_are_mutually_exclusive(
    tmp_path, monkeypatch, capsys
):
    journal = tmp_path / "journal"
    _seed_segment(journal)
    send = Mock(return_value=True)
    monkeypatch.setattr(reprocess, "callosum_send", send)

    code, out, err = _invoke_reprocess(
        monkeypatch,
        capsys,
        journal,
        DAY,
        "--mark-updated",
        "--from-scratch",
    )

    assert code == 2
    assert out == ""
    assert "not allowed with" in err
    send.assert_not_called()


def test_mark_updated_today_exits_without_send_or_marker_touch(
    tmp_path, monkeypatch, capsys
):
    day = date.today().strftime("%Y%m%d")
    journal = tmp_path / "journal"
    _seed_segment(journal, day)
    stream = _touch_marker(journal, day, "stream.updated", 1_000_000_000)
    before = stream.stat().st_mtime_ns
    send = Mock(return_value=True)
    monkeypatch.setattr(reprocess, "callosum_send", send)

    code, out, err = _invoke_reprocess(
        monkeypatch, capsys, journal, day, "--mark-updated"
    )

    assert code == 1
    assert out == ""
    assert err == "reprocess is past-only (cannot reprocess today or a future day)\n"
    send.assert_not_called()
    assert stream.stat().st_mtime_ns == before


def test_mark_updated_missing_day_exits_without_send_or_marker_touch(
    tmp_path, monkeypatch, capsys
):
    journal = tmp_path / "journal"
    marker = journal / "chronicle" / DAY / "health" / "stream.updated"
    send = Mock(return_value=True)
    monkeypatch.setattr(reprocess, "callosum_send", send)

    code, out, err = _invoke_reprocess(
        monkeypatch, capsys, journal, DAY, "--mark-updated"
    )

    assert code == 1
    assert out == ""
    assert err == f"no data for day {DAY}\n"
    send.assert_not_called()
    assert not marker.exists()


def test_mark_updated_unreachable_touches_marker_and_exits_nonzero(
    tmp_path, monkeypatch, capsys
):
    journal = tmp_path / "journal"
    _seed_segment(journal)
    stream = _touch_marker(journal, DAY, "stream.updated", 1_000_000_000)
    before = stream.stat().st_mtime_ns
    send = Mock(return_value=False)
    monkeypatch.setattr(reprocess, "callosum_send", send)

    code, out, err = _invoke_reprocess(
        monkeypatch, capsys, journal, DAY, "--mark-updated"
    )

    assert code == 1
    assert out == ""
    assert err == UNREACHABLE
    send.assert_called_once_with("supervisor", "drain", day=DAY)
    assert stream.stat().st_mtime_ns > before


@pytest.mark.parametrize("day", ["2025011", "20250230"])
def test_malformed_day_exits_without_send(tmp_path, monkeypatch, capsys, day):
    journal = tmp_path / "journal"
    send = Mock(return_value=True)
    monkeypatch.setattr(reprocess, "callosum_send", send)

    code, out, err = _invoke_reprocess(monkeypatch, capsys, journal, day)

    assert code == 1
    assert out == ""
    assert err == "expected day in YYYYMMDD format\n"
    send.assert_not_called()


def test_missing_day_exits_without_send_or_materializing_day(
    tmp_path, monkeypatch, capsys
):
    journal = tmp_path / "journal"
    day_dir = journal / "chronicle" / DAY
    send = Mock(return_value=True)
    monkeypatch.setattr(reprocess, "callosum_send", send)

    code, out, err = _invoke_reprocess(monkeypatch, capsys, journal, DAY)

    assert code == 1
    assert out == ""
    assert err == f"no data for day {DAY}\n"
    send.assert_not_called()
    assert not day_dir.exists()


def test_empty_day_exits_without_send(tmp_path, monkeypatch, capsys):
    journal = tmp_path / "journal"
    (journal / "chronicle" / DAY / "health").mkdir(parents=True)
    send = Mock(return_value=True)
    monkeypatch.setattr(reprocess, "callosum_send", send)

    code, out, err = _invoke_reprocess(monkeypatch, capsys, journal, DAY)

    assert code == 1
    assert out == ""
    assert err == f"no data for day {DAY}\n"
    send.assert_not_called()


@pytest.mark.parametrize(
    "day",
    [
        date.today().strftime("%Y%m%d"),
        (date.today() + timedelta(days=1)).strftime("%Y%m%d"),
    ],
)
def test_today_and_future_exit_without_send_or_marker_touch(
    tmp_path, monkeypatch, capsys, day
):
    journal = tmp_path / "journal"
    _seed_segment(journal, day)
    stream = _touch_marker(journal, day, "stream.updated", 1_000_000_000)
    before = stream.stat().st_mtime_ns
    send = Mock(return_value=True)
    monkeypatch.setattr(reprocess, "callosum_send", send)

    code, out, err = _invoke_reprocess(monkeypatch, capsys, journal, day)

    assert code == 1
    assert out == ""
    assert err == "reprocess is past-only (cannot reprocess today or a future day)\n"
    send.assert_not_called()
    assert stream.stat().st_mtime_ns == before


def test_supervisor_unreachable_exits_nonzero(tmp_path, monkeypatch, capsys):
    journal = tmp_path / "journal"
    _seed_segment(journal)
    _touch_marker(journal, DAY, "stream.updated", 2_000_000_000)
    send = Mock(return_value=False)
    monkeypatch.setattr(reprocess, "callosum_send", send)

    code, out, err = _invoke_reprocess(monkeypatch, capsys, journal, DAY)

    assert code == 1
    assert out == ""
    assert err == UNREACHABLE
    send.assert_called_once_with("supervisor", "drain", day=DAY)


@pytest.mark.parametrize("day", ["2025011", "20250230"])
def test_reprocess_day_malformed_day_returns_code(tmp_path, monkeypatch, day):
    monkeypatch.setenv("SOLSTONE_JOURNAL", str(tmp_path / "journal"))
    send = Mock(return_value=True)
    monkeypatch.setattr(reprocess, "callosum_send", send)

    outcome = reprocess.reprocess_day(day, reprocess.FLAVOR_PROCESS_NOW)

    assert outcome.code is reprocess.ReprocessCode.MALFORMED_DAY
    send.assert_not_called()


@pytest.mark.parametrize(
    "day",
    [
        date.today().strftime("%Y%m%d"),
        (date.today() + timedelta(days=1)).strftime("%Y%m%d"),
    ],
)
def test_reprocess_day_today_and_future_return_past_only(tmp_path, monkeypatch, day):
    monkeypatch.setenv("SOLSTONE_JOURNAL", str(tmp_path / "journal"))
    send = Mock(return_value=True)
    monkeypatch.setattr(reprocess, "callosum_send", send)

    outcome = reprocess.reprocess_day(day, reprocess.FLAVOR_PROCESS_NOW)

    assert outcome.code is reprocess.ReprocessCode.PAST_ONLY
    send.assert_not_called()


def test_reprocess_day_missing_day_returns_no_data(tmp_path, monkeypatch):
    monkeypatch.setenv("SOLSTONE_JOURNAL", str(tmp_path / "journal"))
    send = Mock(return_value=True)
    monkeypatch.setattr(reprocess, "callosum_send", send)

    outcome = reprocess.reprocess_day(DAY, reprocess.FLAVOR_PROCESS_NOW)

    assert outcome.code is reprocess.ReprocessCode.NO_DATA
    send.assert_not_called()


def test_reprocess_day_empty_day_returns_no_data(tmp_path, monkeypatch):
    journal = tmp_path / "journal"
    (journal / "chronicle" / DAY / "health").mkdir(parents=True)
    monkeypatch.setenv("SOLSTONE_JOURNAL", str(journal))
    send = Mock(return_value=True)
    monkeypatch.setattr(reprocess, "callosum_send", send)

    outcome = reprocess.reprocess_day(DAY, reprocess.FLAVOR_PROCESS_NOW)

    assert outcome.code is reprocess.ReprocessCode.NO_DATA
    send.assert_not_called()


def test_reprocess_day_process_now_submitted(tmp_path, monkeypatch):
    journal = tmp_path / "journal"
    _seed_segment(journal)
    _touch_marker(journal, DAY, "stream.updated", 2_000_000_000)
    monkeypatch.setenv("SOLSTONE_JOURNAL", str(journal))
    send = Mock(return_value=True)
    monkeypatch.setattr(reprocess, "callosum_send", send)

    outcome = reprocess.reprocess_day(DAY, reprocess.FLAVOR_PROCESS_NOW)

    assert outcome.code is reprocess.ReprocessCode.PROCESS_NOW_SUBMITTED
    send.assert_called_once_with("supervisor", "drain", day=DAY)


def test_reprocess_day_from_scratch_submitted_before_complete_check(
    tmp_path, monkeypatch
):
    journal = tmp_path / "journal"
    _seed_segment(journal)
    _touch_marker(journal, DAY, "stream.updated", 1_000_000_000)
    _touch_marker(journal, DAY, "daily.updated", 2_000_000_000)
    monkeypatch.setenv("SOLSTONE_JOURNAL", str(journal))
    send = Mock(return_value=True)
    monkeypatch.setattr(reprocess, "callosum_send", send)

    outcome = reprocess.reprocess_day(DAY, reprocess.FLAVOR_FROM_SCRATCH)

    assert outcome.code is reprocess.ReprocessCode.FROM_SCRATCH_SUBMITTED
    send.assert_called_once_with(
        "supervisor",
        "request",
        cmd=["journal", "think", "-v", "--day", DAY, "--from-scratch"],
        day=DAY,
        queue_if_active_cmd_differs=True,
    )


def test_reprocess_day_mark_updated_submitted(tmp_path, monkeypatch):
    journal = tmp_path / "journal"
    _seed_segment(journal)
    stream = _touch_marker(journal, DAY, "stream.updated", 1_000_000_000)
    before = stream.stat().st_mtime_ns
    monkeypatch.setenv("SOLSTONE_JOURNAL", str(journal))
    send = Mock(return_value=True)
    monkeypatch.setattr(reprocess, "callosum_send", send)

    outcome = reprocess.reprocess_day(DAY, reprocess.FLAVOR_MARK_UPDATED)

    assert outcome.code is reprocess.ReprocessCode.MARK_UPDATED_SUBMITTED
    send.assert_called_once_with("supervisor", "drain", day=DAY)
    assert stream.stat().st_mtime_ns > before


def test_reprocess_day_already_complete_returns_noop(tmp_path, monkeypatch):
    journal = tmp_path / "journal"
    _seed_segment(journal)
    _touch_marker(journal, DAY, "stream.updated", 1_000_000_000)
    _touch_marker(journal, DAY, "daily.updated", 2_000_000_000)
    monkeypatch.setenv("SOLSTONE_JOURNAL", str(journal))
    send = Mock(return_value=True)
    monkeypatch.setattr(reprocess, "callosum_send", send)

    outcome = reprocess.reprocess_day(DAY, reprocess.FLAVOR_PROCESS_NOW)

    assert outcome.code is reprocess.ReprocessCode.ALREADY_COMPLETE
    send.assert_not_called()


def test_reprocess_day_process_now_unreachable(tmp_path, monkeypatch):
    journal = tmp_path / "journal"
    _seed_segment(journal)
    _touch_marker(journal, DAY, "stream.updated", 2_000_000_000)
    monkeypatch.setenv("SOLSTONE_JOURNAL", str(journal))
    send = Mock(return_value=False)
    monkeypatch.setattr(reprocess, "callosum_send", send)

    outcome = reprocess.reprocess_day(DAY, reprocess.FLAVOR_PROCESS_NOW)

    assert outcome.code is reprocess.ReprocessCode.UNREACHABLE
    send.assert_called_once_with("supervisor", "drain", day=DAY)


def test_reprocess_day_mark_updated_unreachable(tmp_path, monkeypatch):
    journal = tmp_path / "journal"
    _seed_segment(journal)
    stream = _touch_marker(journal, DAY, "stream.updated", 1_000_000_000)
    before = stream.stat().st_mtime_ns
    monkeypatch.setenv("SOLSTONE_JOURNAL", str(journal))
    send = Mock(return_value=False)
    monkeypatch.setattr(reprocess, "callosum_send", send)

    outcome = reprocess.reprocess_day(DAY, reprocess.FLAVOR_MARK_UPDATED)

    assert outcome.code is reprocess.ReprocessCode.UNREACHABLE
    send.assert_called_once_with("supervisor", "drain", day=DAY)
    assert stream.stat().st_mtime_ns > before


def test_reprocess_day_from_scratch_unreachable(tmp_path, monkeypatch):
    journal = tmp_path / "journal"
    _seed_segment(journal)
    _touch_marker(journal, DAY, "stream.updated", 2_000_000_000)
    monkeypatch.setenv("SOLSTONE_JOURNAL", str(journal))
    send = Mock(return_value=False)
    monkeypatch.setattr(reprocess, "callosum_send", send)

    outcome = reprocess.reprocess_day(DAY, reprocess.FLAVOR_FROM_SCRATCH)

    assert outcome.code is reprocess.ReprocessCode.UNREACHABLE
    send.assert_called_once_with(
        "supervisor",
        "request",
        cmd=["journal", "think", "-v", "--day", DAY, "--from-scratch"],
        day=DAY,
        queue_if_active_cmd_differs=True,
    )


def test_range_from_scratch_without_yes_prints_plan_and_sends_nothing(
    tmp_path, monkeypatch, capsys
):
    journal = tmp_path / "journal"
    _seed_segment(journal, "20250115")
    _seed_segment(journal, "20250116")
    send = Mock(return_value=True)
    monkeypatch.setattr(reprocess, "callosum_send", send)

    code, out, err = _invoke_reprocess(
        monkeypatch,
        capsys,
        journal,
        "20250115",
        "--from-scratch",
        "--through",
        "20250116",
    )

    assert code == 0
    assert out == (
        "from-scratch reprocess plan:\n"
        "2 days with data (0 segments) will be queued. Progress will be visible "
        "in journal top or journal health. Queued days do not survive a supervisor "
        "restart.\n"
        "These days run one at a time and can take hours; today's own journal "
        "processing waits until the whole range finishes.\n"
        "re-run with --yes to proceed\n"
    )
    assert err == ""
    send.assert_not_called()


def test_range_from_scratch_queues_oldest_first_through_supervisor(
    tmp_path, monkeypatch, capsys
):
    journal = tmp_path / "journal"
    days = ["20250115", "20250116", "20250117"]
    for day in days:
        _seed_segment(journal, day)
    harness = _SupervisorRequestHarness(monkeypatch)
    monkeypatch.setattr(reprocess, "callosum_send", harness.callosum_send)

    code, out, err = _invoke_reprocess(
        monkeypatch,
        capsys,
        journal,
        days[0],
        "--from-scratch",
        "--through",
        days[-1],
        "--yes",
    )

    assert code == 0
    assert out == (
        "queued from-scratch reprocess for 3 days (0 segments)\n"
        "progress is visible in journal top or journal health\n"
        "queued days do not survive a supervisor restart\n"
    )
    assert err == ""
    assert [call["day"] for call in harness.submit_calls] == days
    assert [call["cmd"] for call in harness.submit_calls] == [
        ["journal", "think", "-v", "--day", day, "--from-scratch"] for day in days
    ]
    partition = harness.mod.TaskQueue.get_command_name(harness.submit_calls[0]["cmd"])
    assert [entry["cmd"] for entry in harness.queue._queues[partition]] == [
        ["journal", "think", "-v", "--day", day, "--from-scratch"] for day in days
    ]
    assert harness.spawned == []


def test_range_from_scratch_skips_no_data_days_through_supervisor(
    tmp_path, monkeypatch, capsys
):
    journal = tmp_path / "journal"
    _seed_segment(journal, "20250115")
    _seed_segment(journal, "20250117")
    harness = _SupervisorRequestHarness(monkeypatch)
    monkeypatch.setattr(reprocess, "callosum_send", harness.callosum_send)

    code, out, err = _invoke_reprocess(
        monkeypatch,
        capsys,
        journal,
        "20250115",
        "--from-scratch",
        "--through",
        "20250117",
        "--yes",
    )

    assert code == 0
    assert out == (
        "queued from-scratch reprocess for 2 days (0 segments)\n"
        "progress is visible in journal top or journal health\n"
        "queued days do not survive a supervisor restart\n"
    )
    assert err == ""
    assert [call["day"] for call in harness.submit_calls] == ["20250115", "20250117"]
    partition = harness.mod.TaskQueue.get_command_name(harness.submit_calls[0]["cmd"])
    assert len(harness.queue._queues[partition]) == 2
    assert harness.spawned == []


def test_range_from_scratch_empty_segment_counts_day_with_zero_segments(
    tmp_path, monkeypatch, capsys
):
    journal = tmp_path / "journal"
    _seed_segment(journal, DAY)
    harness = _SupervisorRequestHarness(monkeypatch)
    monkeypatch.setattr(reprocess, "callosum_send", harness.callosum_send)

    code, out, err = _invoke_reprocess(
        monkeypatch,
        capsys,
        journal,
        DAY,
        "--from-scratch",
        "--through",
        DAY,
        "--yes",
    )

    assert code == 0
    assert out == (
        "queued from-scratch reprocess for 1 days (0 segments)\n"
        "progress is visible in journal top or journal health\n"
        "queued days do not survive a supervisor restart\n"
    )
    assert err == ""
    assert [call["day"] for call in harness.submit_calls] == [DAY]
    assert harness.spawned == []


@pytest.mark.parametrize("with_yes", [False, True])
def test_range_from_scratch_zero_data_exits_nonzero_without_plan_or_send(
    tmp_path, monkeypatch, capsys, with_yes
):
    journal = tmp_path / "journal"
    send = Mock(return_value=True)
    monkeypatch.setattr(reprocess, "callosum_send", send)
    argv = [DAY, "--from-scratch", "--through", "20250116"]
    if with_yes:
        argv.append("--yes")

    code, out, err = _invoke_reprocess(monkeypatch, capsys, journal, *argv)

    assert code == 1
    assert out == ""
    assert err == f"no data for days {DAY} through 20250116\n"
    send.assert_not_called()


@pytest.mark.parametrize(
    ("argv", "expected_err"),
    [
        ([DAY, "--through", "20250116"], "--through requires --from-scratch\n"),
        (
            [DAY, "--mark-updated", "--through", "20250116"],
            "--through requires --from-scratch\n",
        ),
        (
            [DAY, "--from-scratch", "--through", "2025011"],
            "expected day in YYYYMMDD format\n",
        ),
        (
            [
                DAY,
                "--from-scratch",
                "--through",
                date.today().strftime("%Y%m%d"),
            ],
            "reprocess is past-only (cannot reprocess today or a future day)\n",
        ),
        (
            [DAY, "--from-scratch", "--through", "20250114"],
            "--through must be on or after the start day\n",
        ),
    ],
)
def test_range_from_scratch_through_validation_failures_queue_nothing(
    tmp_path, monkeypatch, capsys, argv, expected_err
):
    journal = tmp_path / "journal"
    send = Mock(return_value=True)
    monkeypatch.setattr(reprocess, "callosum_send", send)

    code, out, err = _invoke_reprocess(monkeypatch, capsys, journal, *argv)

    assert code == 1
    assert out == ""
    assert err == expected_err
    send.assert_not_called()


def test_range_from_scratch_partial_send_failure_reports_day_sets(
    tmp_path, monkeypatch, capsys
):
    journal = tmp_path / "journal"
    days = ["20250115", "20250116", "20250117"]
    for day in days:
        _seed_segment(journal, day)
    harness = _SupervisorRequestHarness(monkeypatch, fail_day=days[0])
    monkeypatch.setattr(reprocess, "callosum_send", harness.callosum_send)

    code, out, err = _invoke_reprocess(
        monkeypatch,
        capsys,
        journal,
        days[0],
        "--from-scratch",
        "--through",
        days[-1],
        "--yes",
    )

    assert code == 1
    assert out == ""
    assert err == (
        "failed to queue day 1 of 3 (20250115): supervisor not reachable - start "
        "it (journal start), then retry\n"
        "queued day set: none\n"
        "not-queued day set: 20250115, 20250116, 20250117\n"
    )
    assert harness.submit_calls == []
    assert harness.queue._queues == {}
    assert harness.spawned == []
