# SPDX-License-Identifier: AGPL-3.0-only
# Copyright (c) 2026 sol pbc

"""App maintenance task discovery and execution.

Maint tasks are one-time scripts that live in apps/{app}/maint/*.py.
Each task is a standalone CLI with a main() function.

State tracking:
- Completed tasks create <journal>/maint/{app}/{task}.jsonl
- The state file contains execution events (exec, line, exit)
- If file exists with exit_code: 0, task is considered complete

Discovery:
- Scans apps/*/maint/*.py for task scripts
- Skips files starting with underscore
- Tasks run in sorted order by task name (app as tiebreaker)

Execution:
- Tasks run as subprocesses
- stdout/stderr captured to state file
- Exit code determines success (0) or failure (non-zero)
"""

from __future__ import annotations

import ast
import json
import logging
import queue
import signal
import subprocess
import sys
import threading
import time
import uuid
from dataclasses import dataclass
from pathlib import Path
from typing import Optional

from solstone.think.utils import now_ms

logger = logging.getLogger(__name__)


@dataclass
class MaintTask:
    """A discovered maintenance task."""

    app: str
    name: str
    script_path: Path
    description: str = ""
    retry_on_next_start: bool = False
    blocks_supervisor_start: bool = False

    @property
    def qualified_name(self) -> str:
        """Return app:task qualified name."""
        return f"{self.app}:{self.name}"


@dataclass(frozen=True)
class MaintTaskResult:
    """Result of a maintenance task run."""

    task: MaintTask
    success: bool
    exit_code: int
    state_file: Path


def discover_tasks() -> list[MaintTask]:
    """Discover all maint tasks from apps/*/maint/*.py.

    Returns:
        List of MaintTask sorted by (app, name)
    """
    tasks = []
    apps_dir = Path(__file__).parent.parent / "apps"

    if not apps_dir.exists():
        return tasks

    for app_dir in sorted(apps_dir.iterdir()):
        if not app_dir.is_dir() or app_dir.name.startswith("_"):
            continue

        maint_dir = app_dir / "maint"
        if not maint_dir.is_dir():
            continue

        for script in sorted(maint_dir.glob("*.py")):
            if script.name.startswith("_"):
                continue

            # Extract description from module docstring and opt-ins without import.
            description = ""
            retry_on_next_start = False
            blocks_supervisor_start = False
            try:
                content = script.read_text()
                # Find first docstring (handles files with license headers)
                for quote in ['"""', "'''"]:
                    start = content.find(quote)
                    if start >= 0:
                        end = content.find(quote, start + 3)
                        if end > start:
                            description = (
                                content[start + 3 : end].strip().split("\n")[0]
                            )
                            break
                opt_ins = _parse_task_opt_ins(content, filename=str(script))
                retry_on_next_start = opt_ins["retry_on_next_start"]
                blocks_supervisor_start = opt_ins["blocks_supervisor_start"]
            except Exception:
                pass

            tasks.append(
                MaintTask(
                    app=app_dir.name,
                    name=script.stem,
                    script_path=script,
                    description=description,
                    retry_on_next_start=retry_on_next_start,
                    blocks_supervisor_start=blocks_supervisor_start,
                )
            )

    tasks.sort(key=lambda t: (t.name, t.app))
    return tasks


def _parse_task_opt_ins(source: str, *, filename: str) -> dict[str, bool]:
    tree = ast.parse(source, filename=filename)
    opt_ins = {
        "retry_on_next_start": False,
        "blocks_supervisor_start": False,
    }
    names = {
        "MAINT_RETRY_ON_NEXT_START": "retry_on_next_start",
        "MAINT_BLOCKS_SUPERVISOR_START": "blocks_supervisor_start",
    }
    for node in tree.body:
        target: ast.expr | None = None
        value: ast.expr | None = None
        if isinstance(node, ast.Assign) and len(node.targets) == 1:
            target = node.targets[0]
            value = node.value
        elif isinstance(node, ast.AnnAssign):
            target = node.target
            value = node.value
        if not isinstance(target, ast.Name) or target.id not in names:
            continue
        if not isinstance(value, ast.Constant) or not isinstance(value.value, bool):
            continue
        opt_ins[names[target.id]] = value.value
    return opt_ins


def get_state_file(journal: Path, app: str, task: str) -> Path:
    """Get path to task state file."""
    return journal / "maint" / app / f"{task}.jsonl"


def _parse_state_file(state_file: Path) -> dict:
    """Parse a JSONL state file and return metadata.

    Returns:
        Dict with keys: duration_ms (int|None), line_count (int), ran_ts (int|None)
    """
    default = {"duration_ms": None, "line_count": 0, "ran_ts": None}
    if not state_file.exists():
        return default

    try:
        latest = _latest_attempt_events(state_file)
        if not latest:
            return default

        duration_ms = None
        line_count = 0
        exec_ts = None
        exit_ts = None
        for event in latest:
            event_type = event.get("event")
            if event_type == "exec" and exec_ts is None:
                exec_ts = event.get("ts")
            elif event_type == "line":
                line_count += 1
            elif event_type == "exit":
                exit_ts = event.get("ts")
                if isinstance(event.get("duration_ms"), int):
                    duration_ms = event["duration_ms"]

        return {
            "duration_ms": duration_ms,
            "line_count": line_count,
            "ran_ts": exit_ts if exit_ts is not None else exec_ts,
        }
    except OSError as e:
        logger.warning(f"Error parsing state file {state_file}: {e}")
        return default


def get_task_status(
    journal: Path, app: str, task: str
) -> tuple[str, Optional[int], Optional[int]]:
    """Check task status from state file.

    Returns:
        Tuple of (status, exit_code, ran_ts) where ran_ts is the most relevant
        event timestamp in epoch milliseconds, and status is:
        - "pending": No state file exists
        - "in_progress": Started but no exit event yet
        - "success": Completed with exit code 0
        - "failed": Completed with non-zero exit code
    """
    state_file = get_state_file(journal, app, task)

    if not state_file.exists():
        return "pending", None, None

    try:
        events = _latest_attempt_events(state_file)
        exec_ts = None
        last_event = None
        for event in events:
            if event.get("event") == "exec" and exec_ts is None:
                exec_ts = event.get("ts")
            last_event = event

        if last_event and last_event.get("event") == "exit":
            ts = last_event.get("ts")
            exit_code = last_event.get("exit_code", -1)
            if exit_code == 0:
                return "success", 0, ts
            return "failed", exit_code, ts

        if exec_ts is not None:
            return "in_progress", None, exec_ts
    except OSError as e:
        logger.warning(f"Error reading state file {state_file}: {e}")

    # File exists but no valid exit event - treat as in-progress.
    return "in_progress", None, None


def _read_events(state_file: Path) -> list[dict]:
    events: list[dict] = []
    with open(state_file, "r") as f:
        for raw_line in f:
            line = raw_line.strip()
            if not line:
                continue
            try:
                event = json.loads(line)
            except json.JSONDecodeError:
                continue
            if isinstance(event, dict):
                events.append(event)
    return events


def _attempt_blocks(events: list[dict]) -> list[list[dict]]:
    blocks: list[list[dict]] = []
    current: list[dict] = []
    current_attempt_id: str | None = None
    for event in events:
        event_type = event.get("event")
        if event_type == "exec":
            if current:
                blocks.append(current)
            current = [event]
            attempt_id = event.get("attempt_id")
            current_attempt_id = attempt_id if isinstance(attempt_id, str) else None
            continue
        if not current:
            current = [event]
            continue
        event_attempt_id = event.get("attempt_id")
        if (
            current_attempt_id is not None
            and isinstance(event_attempt_id, str)
            and event_attempt_id != current_attempt_id
        ):
            blocks.append(current)
            current = [event]
            current_attempt_id = event_attempt_id
            continue
        current.append(event)
    if current:
        blocks.append(current)
    return blocks


def _latest_attempt_events(state_file: Path) -> list[dict]:
    blocks = _attempt_blocks(_read_events(state_file))
    return blocks[-1] if blocks else []


def _write_event(f, event: dict) -> None:
    """Write a JSONL event to file."""
    f.write(json.dumps(event) + "\n")
    f.flush()


def _terminate_with_grace(proc, qualified_name: str) -> int:
    """Terminate a stalled task, escalating to SIGKILL if needed."""
    proc.terminate()
    try:
        return proc.wait(timeout=5)
    except subprocess.TimeoutExpired:
        proc.kill()
        try:
            return proc.wait(timeout=5)
        except subprocess.TimeoutExpired:
            logger.error("Maint task unkillable: %s", qualified_name)
            return -signal.SIGKILL


def run_task(
    journal: Path,
    task: MaintTask,
    emit_fn=None,
    *,
    stall_warn_interval_sec: float = 30.0,
    stall_hard_cap_sec: float = 120.0,
) -> tuple[bool, int]:
    """Run a maintenance task.

    Args:
        journal: Path to journal root
        task: MaintTask to run
        emit_fn: Optional function to emit Callosum events
        stall_warn_interval_sec: Seconds of output silence before warning
        stall_hard_cap_sec: Seconds of output silence before terminating

    Returns:
        Tuple of (success, exit_code)
    """
    maint_dir = journal / "maint" / task.app
    maint_dir.mkdir(parents=True, exist_ok=True)

    state_file = get_state_file(journal, task.app, task.name)
    start_time = time.time()
    start_ts = int(start_time * 1000)
    attempt_id = uuid.uuid4().hex

    # Build command to run the task
    cmd = [sys.executable, "-m", f"solstone.apps.{task.app}.maint.{task.name}"]

    logger.info(f"Running maint task: {task.qualified_name}")

    # Emit start event
    if emit_fn:
        emit_fn(
            "convey",
            "maint_start",
            app=task.app,
            task=task.name,
            description=task.description,
        )

    try:
        with open(state_file, "a") as f:
            # Write exec event
            _write_event(
                f,
                {
                    "event": "exec",
                    "attempt_id": attempt_id,
                    "ts": start_ts,
                    "app": task.app,
                    "task": task.name,
                    "cmd": cmd,
                },
            )

            proc = subprocess.Popen(
                cmd,
                stdout=subprocess.PIPE,
                stderr=subprocess.STDOUT,
                text=True,
                bufsize=1,
            )

            poll_interval = min(0.5, stall_warn_interval_sec / 4)
            last_output_ts = start_time
            next_warn_ts = start_time + stall_warn_interval_sec
            output_queue: queue.Queue[str | None] = queue.Queue()
            reader_thread = None
            reader_done = proc.stdout is None

            if proc.stdout:

                def read_stdout() -> None:
                    try:
                        for line in proc.stdout:
                            output_queue.put(line)
                    except (ValueError, OSError):
                        pass
                    output_queue.put(None)

                reader_thread = threading.Thread(target=read_stdout, daemon=True)
                reader_thread.start()

            exit_code = None
            stalled = False
            empty_after_exit = 0
            poll_fn = getattr(proc, "poll", None)

            while True:
                process_exited = False
                if poll_fn is not None:
                    process_exited = poll_fn() is not None

                if reader_done:
                    if process_exited or poll_fn is None:
                        exit_code = proc.wait()
                        break
                elif process_exited and reader_thread is None:
                    exit_code = proc.wait()
                    break

                if not process_exited:
                    now = time.time()
                    idle_sec = now - last_output_ts
                    if idle_sec >= stall_hard_cap_sec:
                        logger.error(
                            "Maint task stalled past hard cap: %s",
                            task.qualified_name,
                        )
                        exit_code = _terminate_with_grace(proc, task.qualified_name)
                        stalled = True
                        break
                    if now >= next_warn_ts:
                        logger.warning(
                            "Maint task stalled: %s (no output for %.1fs)",
                            task.qualified_name,
                            idle_sec,
                        )
                        next_warn_ts += stall_warn_interval_sec

                if reader_thread is None or reader_done:
                    time.sleep(poll_interval)
                    continue

                try:
                    line = output_queue.get(timeout=poll_interval)
                except queue.Empty:
                    if process_exited:
                        empty_after_exit += 1
                        if empty_after_exit >= 2:
                            exit_code = proc.wait()
                            break
                    else:
                        empty_after_exit = 0
                    continue

                empty_after_exit = 0
                if line is None:
                    reader_done = True
                    continue

                last_output_ts = time.time()
                next_warn_ts = last_output_ts + stall_warn_interval_sec
                line = line.rstrip("\n")
                _write_event(
                    f,
                    {
                        "event": "line",
                        "attempt_id": attempt_id,
                        "ts": now_ms(),
                        "line": line,
                    },
                )
                print(f"  {line}")

            if reader_thread is not None:
                reader_thread.join(timeout=1.0)

            duration_ms = int((time.time() - start_time) * 1000)

            if stalled:
                _write_event(
                    f,
                    {
                        "event": "exit",
                        "attempt_id": attempt_id,
                        "ts": now_ms(),
                        "exit_code": exit_code,
                        "duration_ms": duration_ms,
                        "error": "stalled",
                    },
                )
            else:
                if exit_code is None:
                    exit_code = proc.wait()
                _write_event(
                    f,
                    {
                        "event": "exit",
                        "attempt_id": attempt_id,
                        "ts": now_ms(),
                        "exit_code": exit_code,
                        "duration_ms": duration_ms,
                    },
                )

        if stalled:
            if emit_fn:
                emit_fn(
                    "convey",
                    "maint_complete",
                    app=task.app,
                    task=task.name,
                    exit_code=exit_code,
                    success=False,
                    error="stalled",
                )
            logger.warning(
                f"Maint task stalled: {task.qualified_name} (exit code {exit_code})"
            )
            return False, exit_code

        success = exit_code == 0

        # Emit completion event
        if emit_fn:
            emit_fn(
                "convey",
                "maint_complete",
                app=task.app,
                task=task.name,
                exit_code=exit_code,
                duration_ms=duration_ms,
                success=success,
            )

        if success:
            logger.info(
                f"Completed maint task: {task.qualified_name} ({duration_ms}ms)"
            )
        else:
            logger.warning(
                f"Maint task failed: {task.qualified_name} (exit code {exit_code})"
            )

        return success, exit_code

    except Exception as e:
        logger.error(f"Error running maint task {task.qualified_name}: {e}")

        # Try to write error to state file
        try:
            with open(state_file, "a") as f:
                _write_event(
                    f,
                    {
                        "event": "exit",
                        "attempt_id": attempt_id,
                        "ts": now_ms(),
                        "exit_code": -1,
                        "error": str(e),
                    },
                )
        except Exception:
            pass

        if emit_fn:
            emit_fn(
                "convey",
                "maint_complete",
                app=task.app,
                task=task.name,
                exit_code=-1,
                success=False,
                error=str(e),
            )

        return False, -1


def run_pending_tasks(journal: Path, emit_fn=None) -> list[MaintTaskResult]:
    """Run all pending maintenance tasks.

    Args:
        journal: Path to journal root
        emit_fn: Optional function to emit Callosum events

    Returns:
        Per-task run results, in execution order.
    """
    tasks = discover_tasks()
    pending = []

    for task in tasks:
        status, _, _ = get_task_status(journal, task.app, task.name)
        if status == "pending" or (status == "failed" and task.retry_on_next_start):
            pending.append(task)

    if not pending:
        return []

    logger.info(f"Found {len(pending)} pending maintenance task(s)")

    results: list[MaintTaskResult] = []

    for task in pending:
        success, _ = run_task(journal, task, emit_fn)
        _status, exit_code, _ran_ts = get_task_status(journal, task.app, task.name)
        results.append(
            MaintTaskResult(
                task=task,
                success=success,
                exit_code=exit_code if exit_code is not None else -1,
                state_file=get_state_file(journal, task.app, task.name),
            )
        )

    return results


def list_tasks(journal: Path) -> list[dict]:
    """List all tasks with their status.

    Returns:
        List of dicts with task info and status
    """
    tasks = discover_tasks()
    result = []

    for task in tasks:
        status, exit_code, _ = get_task_status(journal, task.app, task.name)
        state_file = get_state_file(journal, task.app, task.name)
        if status != "pending":
            meta = _parse_state_file(state_file)
        else:
            meta = {"duration_ms": None, "line_count": 0, "ran_ts": None}

        result.append(
            {
                "app": task.app,
                "name": task.name,
                "qualified_name": task.qualified_name,
                "description": task.description,
                "status": status,
                "exit_code": exit_code,
                "ran_ts": meta["ran_ts"],
                "state_file": str(state_file) if status != "pending" else None,
                "duration_ms": meta["duration_ms"],
                "line_count": meta["line_count"],
            }
        )

    return result


def get_task_by_name(name: str) -> Optional[MaintTask]:
    """Get a task by qualified name (app:task) or just task name.

    Args:
        name: Task name, either "app:task" or just "task"

    Returns:
        MaintTask if found, None otherwise
    """
    tasks = discover_tasks()

    # Try qualified name first
    if ":" in name:
        for task in tasks:
            if task.qualified_name == name:
                return task
    else:
        # Try just task name (must be unique)
        matches = [t for t in tasks if t.name == name]
        if len(matches) == 1:
            return matches[0]
        elif len(matches) > 1:
            logger.warning(
                f"Ambiguous task name '{name}', found in: "
                f"{', '.join(t.app for t in matches)}"
            )

    return None
