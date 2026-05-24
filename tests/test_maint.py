# SPDX-License-Identifier: AGPL-3.0-only
# Copyright (c) 2026 sol pbc

"""Tests for the maint (maintenance task) system."""

import json
import logging
import signal
import subprocess
import sys
import threading
from pathlib import Path

import pytest

from solstone.think.maint import (
    MaintTask,
    get_state_file,
    get_task_status,
    list_tasks,
    run_task,
)


@pytest.fixture
def temp_journal(tmp_path):
    """Create a temporary journal directory."""
    journal = tmp_path / "journal"
    journal.mkdir()
    return journal


class TestMaintTask:
    """Tests for MaintTask dataclass."""

    def test_qualified_name(self):
        task = MaintTask(app="chat", name="fix_metadata", script_path=Path("/dummy"))
        assert task.qualified_name == "chat:fix_metadata"


class TestStatusTracking:
    """Tests for task status tracking."""

    def test_get_state_file_path(self, temp_journal):
        """Test state file path generation."""
        path = get_state_file(temp_journal, "chat", "fix_metadata")
        assert path == temp_journal / "maint" / "chat" / "fix_metadata.jsonl"

    def test_pending_status_no_file(self, temp_journal):
        """Test that missing state file means pending."""
        status, exit_code, ran_ts = get_task_status(
            temp_journal, "chat", "fix_metadata"
        )
        assert status == "pending"
        assert exit_code is None
        assert ran_ts is None

    def test_success_status(self, temp_journal):
        """Test that exit code 0 means success."""
        state_dir = temp_journal / "maint" / "chat"
        state_dir.mkdir(parents=True)
        state_file = state_dir / "fix_metadata.jsonl"
        state_file.write_text(
            '{"event": "exec", "ts": 1000}\n'
            '{"event": "line", "line": "done"}\n'
            '{"event": "exit", "ts": 3000, "exit_code": 0}\n'
        )

        status, exit_code, ran_ts = get_task_status(
            temp_journal, "chat", "fix_metadata"
        )
        assert status == "success"
        assert exit_code == 0
        assert ran_ts is not None

    def test_failed_status(self, temp_journal):
        """Test that non-zero exit code means failed."""
        state_dir = temp_journal / "maint" / "chat"
        state_dir.mkdir(parents=True)
        state_file = state_dir / "fix_metadata.jsonl"
        state_file.write_text(
            '{"event": "exec", "ts": 1000}\n'
            '{"event": "exit", "ts": 2000, "exit_code": 1}\n'
        )

        status, exit_code, ran_ts = get_task_status(
            temp_journal, "chat", "fix_metadata"
        )
        assert status == "failed"
        assert exit_code == 1
        assert ran_ts == 2000

    def test_in_progress_status_no_exit_event(self, temp_journal):
        """Test that file without exit event is treated as in-progress."""
        state_dir = temp_journal / "maint" / "chat"
        state_dir.mkdir(parents=True)
        state_file = state_dir / "fix_metadata.jsonl"
        state_file.write_text('{"event": "exec", "ts": 1000}\n')

        status, exit_code, ran_ts = get_task_status(
            temp_journal, "chat", "fix_metadata"
        )
        assert status == "in_progress"
        assert exit_code is None
        assert ran_ts == 1000


class TestListTasks:
    """Tests for listing tasks with status metadata."""

    def test_list_tasks_includes_ran_ts_and_state_file(self, temp_journal, monkeypatch):
        tasks = [
            MaintTask(app="chat", name="done", script_path=Path("/dummy/done.py")),
            MaintTask(app="chat", name="failed", script_path=Path("/dummy/failed.py")),
            MaintTask(
                app="chat", name="pending", script_path=Path("/dummy/pending.py")
            ),
        ]

        def mock_discover_tasks():
            return tasks

        monkeypatch.setattr("solstone.think.maint.discover_tasks", mock_discover_tasks)

        # Success task with timestamp
        success_file = get_state_file(temp_journal, "chat", "done")
        success_file.parent.mkdir(parents=True, exist_ok=True)
        success_file.write_text(
            '{"event": "exec", "ts": 1000}\n'
            '{"event": "exit", "ts": 5000, "exit_code": 0}\n'
        )

        # Failed task with timestamp
        failed_file = get_state_file(temp_journal, "chat", "failed")
        failed_file.write_text(
            '{"event": "exec", "ts": 2000}\n'
            '{"event": "exit", "ts": 6000, "exit_code": 2}\n'
        )

        listed = list_tasks(temp_journal)
        by_name = {task["name"]: task for task in listed}

        done_task = by_name["done"]
        assert done_task["status"] == "success"
        assert done_task["ran_ts"] == 5000
        assert done_task["state_file"] == str(success_file)

        failed_task = by_name["failed"]
        assert failed_task["status"] == "failed"
        assert failed_task["ran_ts"] == 6000
        assert failed_task["state_file"] == str(failed_file)

        pending_task = by_name["pending"]
        assert pending_task["status"] == "pending"
        assert pending_task["ran_ts"] is None
        assert pending_task["state_file"] is None

    def test_list_tasks_includes_duration_and_line_count(
        self, temp_journal, monkeypatch
    ):
        """Test that list_tasks returns duration_ms and line_count."""
        tasks = [
            MaintTask(app="chat", name="done", script_path=Path("/dummy/done.py")),
            MaintTask(
                app="chat", name="pending", script_path=Path("/dummy/pending.py")
            ),
        ]

        def mock_discover_tasks():
            return tasks

        monkeypatch.setattr("solstone.think.maint.discover_tasks", mock_discover_tasks)

        # Success task with line events and duration
        success_file = get_state_file(temp_journal, "chat", "done")
        success_file.parent.mkdir(parents=True, exist_ok=True)
        success_file.write_text(
            '{"event": "exec", "ts": 1000}\n'
            '{"event": "line", "ts": 1500, "line": "step 1"}\n'
            '{"event": "line", "ts": 2000, "line": "step 2"}\n'
            '{"event": "line", "ts": 2500, "line": "step 3"}\n'
            '{"event": "exit", "ts": 3000, "exit_code": 0, "duration_ms": 2000}\n'
        )

        listed = list_tasks(temp_journal)
        by_name = {task["name"]: task for task in listed}

        done = by_name["done"]
        assert done["duration_ms"] == 2000
        assert done["line_count"] == 3

        pending = by_name["pending"]
        assert pending["duration_ms"] is None
        assert pending["line_count"] == 0

    def test_list_tasks_in_progress_status(self, temp_journal, monkeypatch):
        """Test that list_tasks returns in_progress for tasks without exit event."""
        tasks = [
            MaintTask(
                app="chat", name="running", script_path=Path("/dummy/running.py")
            ),
        ]

        def mock_discover_tasks():
            return tasks

        monkeypatch.setattr("solstone.think.maint.discover_tasks", mock_discover_tasks)

        # In-progress task: exec event but no exit event
        state_file = get_state_file(temp_journal, "chat", "running")
        state_file.parent.mkdir(parents=True, exist_ok=True)
        state_file.write_text(
            '{"event": "exec", "ts": 1000}\n'
            '{"event": "line", "ts": 1500, "line": "working..."}\n'
        )

        listed = list_tasks(temp_journal)
        assert len(listed) == 1
        t = listed[0]
        assert t["status"] == "in_progress"
        assert t["ran_ts"] == 1000
        assert t["exit_code"] is None
        assert t["duration_ms"] is None
        assert t["line_count"] == 1


class TestFormatDuration:
    """Tests for duration formatting."""

    def test_milliseconds(self):
        from solstone.convey.maint_cli import _format_duration

        assert _format_duration(0) == "0ms"
        assert _format_duration(500) == "500ms"
        assert _format_duration(999) == "999ms"

    def test_seconds(self):
        from solstone.convey.maint_cli import _format_duration

        assert _format_duration(1000) == "1s"
        assert _format_duration(2500) == "2s"
        assert _format_duration(59999) == "59s"

    def test_minutes(self):
        from solstone.convey.maint_cli import _format_duration

        assert _format_duration(60000) == "1m 0s"
        assert _format_duration(143000) == "2m 23s"


class TestRunTask:
    """Tests for running individual tasks."""

    def _task(self, name: str) -> MaintTask:
        return MaintTask(
            app="test_app",
            name=name,
            script_path=Path(f"/dummy/{name}.py"),
        )

    def _patch_python_subprocess(self, monkeypatch, code: str) -> None:
        real_popen = subprocess.Popen

        def mock_popen(_cmd, **kwargs):
            return real_popen([sys.executable, "-c", code], **kwargs)

        monkeypatch.setattr(subprocess, "Popen", mock_popen)

    def _read_events(self, journal: Path, task: MaintTask) -> list[dict]:
        state_file = get_state_file(journal, task.app, task.name)
        events = [
            json.loads(line) for line in state_file.read_text().strip().split("\n")
        ]
        assert {event["event"] for event in events} <= {"exec", "line", "exit"}
        return events

    def test_run_successful_task(self, temp_journal, monkeypatch):
        """Test running a successful task creates correct state file."""
        import subprocess

        task = MaintTask(
            app="test_app",
            name="success_task",
            script_path=Path("/dummy/success.py"),
        )

        # Mock subprocess to simulate success
        def mock_popen(*args, **kwargs):
            class MockProc:
                stdout = iter(["Processing...\n", "Done!\n"])

                def wait(self):
                    return 0

            return MockProc()

        monkeypatch.setattr(subprocess, "Popen", mock_popen)

        success, exit_code = run_task(temp_journal, task)

        assert success is True
        assert exit_code == 0

        # Check state file was created
        state_file = get_state_file(temp_journal, "test_app", "success_task")
        assert state_file.exists()

        # Verify contents
        lines = state_file.read_text().strip().split("\n")
        events = [json.loads(line) for line in lines]

        assert events[0]["event"] == "exec"
        assert events[0]["app"] == "test_app"
        assert events[0]["task"] == "success_task"

        # Should have line events
        line_events = [e for e in events if e["event"] == "line"]
        assert len(line_events) == 2

        # Last event should be exit with code 0
        assert events[-1]["event"] == "exit"
        assert events[-1]["exit_code"] == 0

    def test_run_failing_task(self, temp_journal, monkeypatch):
        """Test running a failing task records failure."""
        import subprocess

        task = MaintTask(
            app="test_app",
            name="fail_task",
            script_path=Path("/dummy/fail.py"),
        )

        # Mock subprocess to simulate failure
        def mock_popen(*args, **kwargs):
            class MockProc:
                stdout = iter(["About to fail\n"])

                def wait(self):
                    return 1

            return MockProc()

        monkeypatch.setattr(subprocess, "Popen", mock_popen)

        success, exit_code = run_task(temp_journal, task)

        assert success is False
        assert exit_code == 1

        # Check state file was created with failure
        state_file = get_state_file(temp_journal, "test_app", "fail_task")
        assert state_file.exists()

        lines = state_file.read_text().strip().split("\n")
        last_event = json.loads(lines[-1])
        assert last_event["exit_code"] == 1

    def test_run_task_emits_events(self, temp_journal, monkeypatch):
        """Test that run_task calls emit_fn with correct events."""
        import subprocess

        task = MaintTask(
            app="test_app",
            name="emit_task",
            script_path=Path("/dummy/emit.py"),
            description="Test task",
        )

        def mock_popen(*args, **kwargs):
            class MockProc:
                stdout = iter(["Working\n"])

                def wait(self):
                    return 0

            return MockProc()

        monkeypatch.setattr(subprocess, "Popen", mock_popen)

        emitted = []

        def capture_emit(tract, event, **kwargs):
            emitted.append((tract, event, kwargs))

        success, _ = run_task(temp_journal, task, emit_fn=capture_emit)

        assert success is True
        assert len(emitted) == 2

        # Check start event
        assert emitted[0][0] == "convey"
        assert emitted[0][1] == "maint_start"
        assert emitted[0][2]["app"] == "test_app"
        assert emitted[0][2]["task"] == "emit_task"

        # Check complete event
        assert emitted[1][0] == "convey"
        assert emitted[1][1] == "maint_complete"
        assert emitted[1][2]["success"] is True

    def test_stall_warning_logs_named_task(self, temp_journal, monkeypatch, caplog):
        task = self._task("warn_task")
        self._patch_python_subprocess(
            monkeypatch,
            "import time; time.sleep(0.8)",
        )

        with caplog.at_level(logging.WARNING, logger="solstone.think.maint"):
            success, exit_code = run_task(
                temp_journal,
                task,
                stall_warn_interval_sec=0.5,
                stall_hard_cap_sec=4.0,
            )

        assert success is True
        assert exit_code == 0
        assert any(
            "test_app:warn_task" in record.message and "stalled" in record.message
            for record in caplog.records
        )
        events = self._read_events(temp_journal, task)
        assert events[-1]["exit_code"] == 0

    def test_stall_hard_cap_terminates_silent_task(self, temp_journal, monkeypatch):
        task = self._task("silent_stall_task")
        self._patch_python_subprocess(
            monkeypatch,
            "import time; time.sleep(30)",
        )

        success, exit_code = run_task(
            temp_journal,
            task,
            stall_warn_interval_sec=0.2,
            stall_hard_cap_sec=0.6,
        )

        assert success is False
        assert exit_code == -signal.SIGTERM
        events = self._read_events(temp_journal, task)
        last_event = events[-1]
        assert list(last_event.keys()) == [
            "event",
            "ts",
            "exit_code",
            "duration_ms",
            "error",
        ]
        assert last_event["exit_code"] == -signal.SIGTERM
        assert last_event["error"] == "stalled"

    def test_stall_hard_cap_kills_sigterm_ignoring_task(
        self, temp_journal, monkeypatch
    ):
        task = self._task("sigkill_stall_task")
        self._patch_python_subprocess(
            monkeypatch,
            (
                "import signal, time\n"
                "signal.signal(signal.SIGTERM, signal.SIG_IGN)\n"
                "time.sleep(30)\n"
            ),
        )

        success, exit_code = run_task(
            temp_journal,
            task,
            stall_warn_interval_sec=2.0,
            stall_hard_cap_sec=8.0,
        )

        assert success is False
        assert exit_code == -signal.SIGKILL
        events = self._read_events(temp_journal, task)
        last_event = events[-1]
        assert last_event["exit_code"] == -signal.SIGKILL
        assert last_event["error"] == "stalled"

    def test_stalled_task_emits_stalled_complete_event(self, temp_journal, monkeypatch):
        task = self._task("stalled_emit_task")
        self._patch_python_subprocess(
            monkeypatch,
            "import time; time.sleep(30)",
        )
        emitted = []

        def capture_emit(tract, event, **kwargs):
            emitted.append((tract, event, kwargs))

        success, exit_code = run_task(
            temp_journal,
            task,
            emit_fn=capture_emit,
            stall_warn_interval_sec=0.2,
            stall_hard_cap_sec=0.6,
        )

        assert success is False
        assert exit_code == -signal.SIGTERM
        assert emitted[-1] == (
            "convey",
            "maint_complete",
            {
                "app": "test_app",
                "task": "stalled_emit_task",
                "exit_code": -signal.SIGTERM,
                "success": False,
                "error": "stalled",
            },
        )
        events = self._read_events(temp_journal, task)
        assert events[-1]["error"] == "stalled"

    def test_final_stdout_line_is_drained_after_process_exit(
        self, temp_journal, monkeypatch
    ):
        task = self._task("final_line_task")
        self._patch_python_subprocess(
            monkeypatch,
            (
                "import sys\n"
                "sys.stdout.write('first\\n')\n"
                "sys.stdout.flush()\n"
                "sys.stdout.write('final\\n')\n"
                "sys.stdout.flush()\n"
            ),
        )

        success, exit_code = run_task(
            temp_journal,
            task,
            stall_warn_interval_sec=2.0,
            stall_hard_cap_sec=4.0,
        )

        assert success is True
        assert exit_code == 0
        events = self._read_events(temp_journal, task)
        lines = [event["line"] for event in events if event["event"] == "line"]
        assert lines == ["first", "final"]

    def test_output_activity_resets_idle_timer(self, temp_journal, monkeypatch):
        task = self._task("idle_reset_task")
        self._patch_python_subprocess(
            monkeypatch,
            (
                "import sys, time\n"
                "sys.stdout.write('one\\n'); sys.stdout.flush()\n"
                "time.sleep(2.0)\n"
                "sys.stdout.write('two\\n'); sys.stdout.flush()\n"
            ),
        )

        success, exit_code = run_task(
            temp_journal,
            task,
            stall_warn_interval_sec=2.0,
            stall_hard_cap_sec=8.0,
        )

        assert success is True
        assert exit_code == 0
        events = self._read_events(temp_journal, task)
        assert "error" not in events[-1]

    def test_stalled_task_does_not_wedge_followup_run_task(
        self, temp_journal, monkeypatch
    ):
        stall_task = self._task("stalled_followup_first")
        fast_task = self._task("stalled_followup_second")
        real_popen = subprocess.Popen
        programs = iter(
            [
                "import time; time.sleep(30)",
                "print('done')",
            ]
        )

        def mock_popen(_cmd, **kwargs):
            return real_popen([sys.executable, "-c", next(programs)], **kwargs)

        monkeypatch.setattr(subprocess, "Popen", mock_popen)

        success, exit_code = run_task(
            temp_journal,
            stall_task,
            stall_warn_interval_sec=0.2,
            stall_hard_cap_sec=0.6,
        )

        assert success is False
        assert exit_code < 0
        stall_events = self._read_events(temp_journal, stall_task)
        assert stall_events[-1]["error"] == "stalled"

        success, exit_code = run_task(
            temp_journal,
            fast_task,
            stall_warn_interval_sec=0.2,
            stall_hard_cap_sec=0.6,
        )

        assert success is True
        assert exit_code == 0
        fast_events = self._read_events(temp_journal, fast_task)
        assert "error" not in fast_events[-1]

    def test_unkillable_stalled_task_records_sigkill_exit(
        self, temp_journal, monkeypatch, caplog
    ):
        task = self._task("unkillable_task")
        procs = []

        class BlockingStdout:
            def __init__(self):
                self.release = threading.Event()

            def __iter__(self):
                self.release.wait()
                return self

            def __next__(self):
                raise StopIteration

        class MockProc:
            def __init__(self):
                self.stdout = BlockingStdout()
                self.terminated = False
                self.killed = False

            def poll(self):
                return None

            def terminate(self):
                self.terminated = True

            def kill(self):
                self.killed = True

            def wait(self, timeout=None):
                raise subprocess.TimeoutExpired(cmd="fake", timeout=timeout)

        def mock_popen(*args, **kwargs):
            proc = MockProc()
            procs.append(proc)
            return proc

        monkeypatch.setattr(subprocess, "Popen", mock_popen)

        with caplog.at_level(logging.ERROR, logger="solstone.think.maint"):
            success, exit_code = run_task(
                temp_journal,
                task,
                stall_warn_interval_sec=0.2,
                stall_hard_cap_sec=0.6,
            )

        procs[0].stdout.release.set()

        assert success is False
        assert exit_code == -signal.SIGKILL
        assert procs[0].terminated is True
        assert procs[0].killed is True
        assert any(
            "Maint task unkillable: test_app:unkillable_task" in record.message
            for record in caplog.records
        )
        events = self._read_events(temp_journal, task)
        last_event = events[-1]
        assert last_event["exit_code"] == -signal.SIGKILL
        assert last_event["error"] == "stalled"
