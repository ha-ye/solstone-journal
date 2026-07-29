#!/usr/bin/env python3
# SPDX-License-Identifier: AGPL-3.0-only
# Copyright (c) 2026 sol pbc

"""Unified process spawning and lifecycle management utilities.

All subprocess output is automatically logged to:
    journal/chronicle/{YYYYMMDD}/health/{ref}_{process_name}.log

Where process_name is derived from cmd[0] basename, and ref is a unique correlation ID.

Symlinks provide stable access paths:
    journal/chronicle/{YYYYMMDD}/health/{process_name}.log (day-level symlink)
    journal/health/{process_name}.log (journal-level symlink)

Logs automatically roll over at midnight for long-running processes.
"""

from __future__ import annotations

import logging
import os
import signal
import subprocess
import threading
import time
from collections import namedtuple
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path

import psutil

from solstone.think.callosum import CallosumConnection
from solstone.think.utils import CHRONICLE_DIR, get_journal, now_ms

logger = logging.getLogger(__name__)
# Default wall-clock budget (30m) for a task run with no explicit cap.
DEFAULT_TASK_MAX_RUNTIME = 1800
KILL_REAP_GRACE_S = 0.5
DESCENDANT_POLL_INTERVAL_S = 0.05
DescendantRef = namedtuple("DescendantRef", ["pid", "pgid"])

_SIGNAL_RACE_EXCEPTIONS = (
    ProcessLookupError,
    OSError,
    psutil.NoSuchProcess,
    psutil.AccessDenied,
)

_GENERATION_ENV_KEY = "SOL_SPEAKERS_ANALYZE_INSTALL_GENERATION_ID"
_GENERATION_FD_ENV_KEY = "SOL_SPEAKERS_ANALYZE_INSTALL_GENERATION_FD"
_GENERATION_TOKEN_ENV_KEY = "SOL_SPEAKERS_ANALYZE_INSTALL_GENERATION_TOKEN"
_GENERATION_FD_MIN = 3
_GENERATION_FD_MAX = 1_048_576


class ProcessTreeNotReaped(subprocess.TimeoutExpired):
    """Raised when a bounded process-tree termination cannot prove cleanup."""

    reason: str
    survivors: list[DescendantRef]

    def __init__(
        self,
        cmd: list[str],
        timeout: float,
        *,
        reason: str,
        survivors: list[DescendantRef] | None = None,
    ):
        super().__init__(cmd, timeout)
        self.reason = reason
        self.survivors = list(survivors or [])

    def __str__(self) -> str:
        base = super().__str__()
        if not self.survivors:
            return f"{base}; process tree not reaped: reason={self.reason}"
        survivor_text = ", ".join(_format_descendant(ref) for ref in self.survivors)
        return (
            f"{base}; process tree not reaped: reason={self.reason}; "
            f"survivors=[{survivor_text}]"
        )


def _format_descendant(ref: DescendantRef) -> str:
    if ref.pgid is None:
        return f"pid={ref.pid}"
    return f"pid={ref.pid} pgid={ref.pgid}"


def snapshot_descendants(pid: int) -> list[DescendantRef]:
    """Return recursive descendant pid/pgid refs for a process."""
    try:
        children = psutil.Process(pid).children(recursive=True)
    except psutil.NoSuchProcess:
        return []
    except (psutil.AccessDenied, psutil.Error, OSError):
        raise

    descendants = []
    for child in children:
        try:
            pgid = os.getpgid(child.pid)
        except (ProcessLookupError, OSError):
            pgid = None
        descendants.append(DescendantRef(child.pid, pgid))
    return descendants


def _safe_signal_id(
    value: int | None,
    *,
    own_pid: int,
    own_pgid: int,
    kind: str,
    process_name: str,
    sig: signal.Signals,
) -> bool:
    if value is None:
        return False
    if value <= 1 or value == own_pid or value == own_pgid:
        logger.warning(
            "%s terminate: refusing unsafe %s signal target id=%s sig=%s "
            "own_pid=%s own_pgid=%s",
            process_name,
            kind,
            value,
            sig.name,
            own_pid,
            own_pgid,
        )
        return False
    return True


def _signal_pid(
    pid: int | None,
    sig: signal.Signals,
    *,
    own_pid: int,
    own_pgid: int,
    process_name: str,
) -> None:
    if not _safe_signal_id(
        pid,
        own_pid=own_pid,
        own_pgid=own_pgid,
        kind="pid",
        process_name=process_name,
        sig=sig,
    ):
        return
    try:
        os.kill(pid, sig)
    except _SIGNAL_RACE_EXCEPTIONS:
        pass


def _signal_pgid(
    pgid: int | None,
    sig: signal.Signals,
    *,
    own_pid: int,
    own_pgid: int,
    process_name: str,
) -> None:
    if not _safe_signal_id(
        pgid,
        own_pid=own_pid,
        own_pgid=own_pgid,
        kind="pgid",
        process_name=process_name,
        sig=sig,
    ):
        return
    try:
        os.killpg(pgid, sig)
    except _SIGNAL_RACE_EXCEPTIONS:
        pass


def _signal_parent_process(
    process: subprocess.Popen,
    sig: signal.Signals,
    *,
    own_pid: int,
    own_pgid: int,
    process_name: str,
) -> None:
    if not _safe_signal_id(
        process.pid,
        own_pid=own_pid,
        own_pgid=own_pgid,
        kind="pid",
        process_name=process_name,
        sig=sig,
    ):
        return
    try:
        if sig == signal.SIGTERM:
            process.terminate()
        else:
            process.kill()
    except _SIGNAL_RACE_EXCEPTIONS:
        pass


def _signal_descendants(
    descendants: list[DescendantRef],
    sig: signal.Signals,
    *,
    own_pid: int,
    own_pgid: int,
    process_name: str,
) -> None:
    for descendant in descendants:
        _signal_pid(
            descendant.pid,
            sig,
            own_pid=own_pid,
            own_pgid=own_pgid,
            process_name=process_name,
        )

    signaled_pgids = set()
    for descendant in descendants:
        if descendant.pgid in signaled_pgids:
            continue
        signaled_pgids.add(descendant.pgid)
        _signal_pgid(
            descendant.pgid,
            sig,
            own_pid=own_pid,
            own_pgid=own_pgid,
            process_name=process_name,
        )


def _descendant_alive(descendant: DescendantRef) -> bool:
    try:
        return psutil.Process(descendant.pid).status() != psutil.STATUS_ZOMBIE
    except psutil.NoSuchProcess:
        return False
    except (psutil.AccessDenied, OSError):
        return True


def _alive_descendants(descendants: list[DescendantRef]) -> list[DescendantRef]:
    return [descendant for descendant in descendants if _descendant_alive(descendant)]


def _poll_descendants_until_gone(
    descendants: list[DescendantRef], deadline: float
) -> list[DescendantRef]:
    if not descendants:
        return []

    survivors = _alive_descendants(descendants)
    while survivors:
        now = time.monotonic()
        if now >= deadline:
            return survivors
        time.sleep(min(DESCENDANT_POLL_INTERVAL_S, deadline - now))
        survivors = _alive_descendants(descendants)
    return []


def _log_descendant_survivors(
    process_name: str, reason: str, survivors: list[DescendantRef]
) -> None:
    for survivor in survivors:
        logger.warning(
            "%s terminate: descendant alive after signaling reason=%s pid=%s pgid=%s",
            process_name,
            reason,
            survivor.pid,
            survivor.pgid,
        )


def _get_journal_path() -> Path:
    """Return the journal path (auto-creates if needed)."""
    return Path(get_journal())


def _current_day() -> str:
    """Get current day in YYYYMMDD format."""
    return datetime.now().strftime("%Y%m%d")


def _day_health_log_path(journal_root: Path, day: str, ref: str, name: str) -> Path:
    """Build path to day health log.

    Returns: journal/chronicle/{day}/health/{ref}_{name}.log
    """
    return journal_root / CHRONICLE_DIR / day / "health" / f"{ref}_{name}.log"


def _atomic_symlink(link_path: Path, target: str) -> None:
    """Create or update symlink atomically.

    Args:
        link_path: Path where symlink should be created
        target: Target path (can be relative or absolute)
    """
    link_path.parent.mkdir(parents=True, exist_ok=True)
    tmp_link = link_path.with_suffix(f".tmp{os.getpid()}_{threading.get_ident()}")
    try:
        tmp_link.symlink_to(target)
        tmp_link.replace(link_path)
    finally:
        # Clean up temp file if it still exists
        if tmp_link.exists() or tmp_link.is_symlink():
            tmp_link.unlink(missing_ok=True)


def _format_log_line(prefix: str, stream: str, line: str) -> str:
    """Format log line with ISO timestamp and labels.

    Args:
        prefix: Process identifier (e.g., "observer" or "describe:file.webm")
        stream: "stdout" or "stderr"
        line: Output line from process

    Returns:
        Formatted line: "2024-11-01T10:30:45 [prefix:stream] line\\n"
    """
    timestamp = datetime.now().isoformat(timespec="seconds")
    clean_line = line.rstrip("\n")
    return f"{timestamp} [{prefix}:{stream}] {clean_line}\n"


class DailyLogWriter:
    """Thread-safe log writer that automatically rolls over at midnight.

    When ``day`` is provided, the writer is pinned to that day directory
    and midnight rollover is disabled (batch processing of historical days).

    Writes to: journal/chronicle/{YYYYMMDD}/health/{ref}_{name}.log

    Creates and maintains symlinks:
    - journal/chronicle/{YYYYMMDD}/health/{name}.log -> {ref}_{name}.log (day-level)
    - journal/health/{name}.log -> chronicle/{YYYYMMDD}/health/{ref}_{name}.log (journal-level)

    When the day changes, automatically closes old file, opens new file, and updates symlinks.
    The journal root is resolved once at construction time and pinned for the
    lifetime of the writer.
    """

    def __init__(self, ref: str, name: str, day: str | None = None):
        self._ref = ref
        self._name = name
        self._journal_root: Path = _get_journal_path()
        self._pinned = day is not None
        self._lock = threading.Lock()
        self._current_day = day or _current_day()
        self._fh = self._open_log()
        self._update_symlinks()

    def _open_log(self, day: str | None = None):
        """Open the log file for ``day`` (defaults to the current day)."""
        day = day or self._current_day
        log_path = _day_health_log_path(self._journal_root, day, self._ref, self._name)
        log_path.parent.mkdir(parents=True, exist_ok=True)
        return log_path.open("a", encoding="utf-8")

    def _update_symlinks(self) -> None:
        """Update day-level and journal-level symlinks to point to current log."""
        journal = self._journal_root
        day_health = journal / CHRONICLE_DIR / self._current_day / "health"
        log_filename = f"{self._ref}_{self._name}.log"

        # Day-level symlink: chronicle/{YYYYMMDD}/health/{name}.log -> {ref}_{name}.log
        day_symlink = day_health / f"{self._name}.log"
        _atomic_symlink(day_symlink, log_filename)

        # Journal-level symlink: health/{name}.log -> ../chronicle/{YYYYMMDD}/health/{ref}_{name}.log
        # Relative from journal/health/ to journal/chronicle/{YYYYMMDD}/health/
        journal_symlink = journal / "health" / f"{self._name}.log"
        relative_target = (
            f"../{CHRONICLE_DIR}/{self._current_day}/health/{log_filename}"
        )
        _atomic_symlink(journal_symlink, relative_target)

    def write(self, message: str) -> None:
        """Write message to log, handling day rollover."""
        with self._lock:
            if not self._pinned:
                # Check for day change
                day_now = _current_day()
                if day_now != self._current_day:
                    # Open the new day's log BEFORE touching the old handle.
                    # A failed open must leave the old (open) handle and the
                    # tracked day untouched, so the next write re-attempts the
                    # rollover — nothing here may propagate out of write() and
                    # kill the drain thread.
                    try:
                        new_fh = self._open_log(day_now)
                    except OSError:
                        new_fh = None
                    if new_fh is not None:
                        old_fh = self._fh
                        self._fh = new_fh
                        self._current_day = day_now
                        # Best-effort after the swap: neither the symlink
                        # refresh nor closing the old handle may raise out.
                        try:
                            self._update_symlinks()
                        except OSError:
                            pass
                        try:
                            if not old_fh.closed:
                                old_fh.close()
                        except OSError:
                            pass

            # Write and flush — swallow disk-full so output threads survive
            try:
                self._fh.write(message)
                self._fh.flush()
            except OSError:
                pass

    def close(self) -> None:
        """Close log file."""
        with self._lock:
            if not self._fh.closed:
                self._fh.close()

    @property
    def path(self) -> Path:
        """Get current log file path."""
        return _day_health_log_path(
            self._journal_root, self._current_day, self._ref, self._name
        )


def _command_partition(cmd: Sequence[str]) -> str:
    """Return the queue/log partition name for a managed-process cmd.

    Think tasks partition by bare mode name
    (daily/segment/flush/activity/weekly/cadence);
    everything else uses sol/journal subcommand or process basename.
    """
    if cmd and cmd[0] in ("sol", "journal") and len(cmd) > 1:
        name = cmd[1]
        if name == "think":
            for flag, mode in [
                ("--activity", "activity"),
                ("--flush", "flush"),
                ("--segments", "segment"),
                ("--weekly", "weekly"),
                ("--cadence", "cadence"),
                ("--segment", "segment"),
            ]:
                if flag in cmd:
                    name = mode
                    break
            else:
                name = "daily"
        elif name == "maintenance":
            if len(cmd) >= 4 and cmd[2] == "run":
                name = f"maintenance:{cmd[3]}"
    else:
        name = Path(cmd[0]).name if cmd else "unknown"
    return name


def _parse_generation_fd(value: object) -> int:
    try:
        fd = int(str(value))
    except (TypeError, ValueError) as exc:
        raise RuntimeError("invalid speakers-analyze generation fd") from exc
    if fd < _GENERATION_FD_MIN or fd > _GENERATION_FD_MAX:
        raise RuntimeError("invalid speakers-analyze generation fd")
    try:
        os.fstat(fd)
    except OSError as exc:
        raise RuntimeError("invalid speakers-analyze generation fd") from exc
    return fd


def _generation_pass_fds(effective_env: Mapping[str, object]) -> tuple[int, ...]:
    if not effective_env.get(_GENERATION_ENV_KEY):
        return ()
    if os.name != "posix":
        return ()
    fd_value = effective_env.get(_GENERATION_FD_ENV_KEY)
    if fd_value is None:
        raise RuntimeError("missing speakers-analyze generation fd")
    if effective_env.get(_GENERATION_TOKEN_ENV_KEY) is None:
        raise RuntimeError("missing speakers-analyze generation token")
    return (_parse_generation_fd(fd_value),)


@dataclass
class ManagedProcess:
    """Subprocess wrapper with automatic output logging and lifecycle management.

        All output is automatically logged to:
            journal/chronicle/{YYYYMMDD}/health/{ref}_{name}.log

    Where name is derived from cmd[0] basename, and ref is a unique correlation ID.

        Symlinks are automatically created and maintained:
            journal/chronicle/{YYYYMMDD}/health/{name}.log -> {ref}_{name}.log (day-level)
            journal/health/{name}.log -> chronicle/{YYYYMMDD}/health/{ref}_{name}.log (journal-level)

    Logs roll over automatically at midnight for long-running processes.

    Process lifecycle events are broadcast via Callosum logs tract.
    """

    process: subprocess.Popen
    name: str
    log_writer: DailyLogWriter
    cmd: list[str]
    _threads: list[threading.Thread]
    ref: str
    _start_time: float
    _callosum: CallosumConnection | None
    _owns_callosum: bool = True

    @property
    def start_time(self) -> float:
        """Epoch timestamp when this process was spawned."""
        return self._start_time

    @classmethod
    def spawn(
        cls,
        cmd: list[str],
        *,
        env: dict | None = None,
        ref: str | None = None,
        callosum: CallosumConnection | None = None,
        day: str | None = None,
    ) -> "ManagedProcess":
        """Spawn process with automatic output logging to daily health directory.

        Args:
            cmd: Command and arguments
            env: Optional environment variables (inherits parent env if not provided)
            ref: Optional correlation ID (auto-generated if not provided)
            callosum: Optional shared CallosumConnection (creates new one if not provided)
            day: Optional day override (YYYYMMDD). When provided, logs are placed
                in that day's health directory instead of today's.

        Returns:
            ManagedProcess instance

        Raises:
            RuntimeError: If process fails to spawn

        Example:
            managed = ManagedProcess.spawn(["observer", "-v"])
            # Logs to: {JOURNAL}/{YYYYMMDD}/health/{ref}_observer.log
            # Symlinks: {YYYYMMDD}/health/observer.log (day-level)
            #           health/observer.log (journal-level)

            # With explicit correlation ID:
            managed = ManagedProcess.spawn(
                ["journal", "indexer", "--rescan"],
                ref="1730476800000",
            )
            # Logs to: {JOURNAL}/{YYYYMMDD}/health/1730476800000_indexer.log
        """
        name = _command_partition(cmd)

        # Generate correlation ID (use provided ref, else timestamp)
        ref = ref if ref else str(now_ms())
        start_time = time.time()

        # Use provided callosum or create new one
        owns_callosum = callosum is None
        if owns_callosum:
            callosum = CallosumConnection()
            callosum.start()

        log_writer = DailyLogWriter(ref, name, day=day)

        logger.info(f"Starting {name}: {' '.join(cmd)}")

        try:
            effective_env = env if env is not None else os.environ
            pass_fds = _generation_pass_fds(effective_env)
            proc = subprocess.Popen(
                cmd,
                stdin=subprocess.DEVNULL,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                text=True,
                bufsize=1,
                env=env,
                pass_fds=pass_fds,
                process_group=0,
            )
        except Exception as exc:
            log_writer.close()
            if owns_callosum and callosum:
                callosum.stop()
            raise RuntimeError(f"Failed to spawn {name}: {exc}") from exc

        logger.info(f"Started {name} with PID {proc.pid}")

        # Emit exec event
        if callosum:
            callosum.emit(
                "logs",
                "exec",
                ref=ref,
                name=name,
                pid=proc.pid,
                cmd=list(cmd),
                log_path=str(log_writer.path),
            )

        # Start output streaming threads
        def stream_output(pipe, stream_label: str):
            if pipe is None:
                return
            with pipe:
                for line in pipe:
                    formatted = _format_log_line(name, stream_label, line)
                    log_writer.write(formatted)

                    # Emit line event
                    if callosum:
                        callosum.emit(
                            "logs",
                            "line",
                            ref=ref,
                            name=name,
                            pid=proc.pid,
                            stream=stream_label,
                            line=line.rstrip("\n"),
                        )

        threads = [
            threading.Thread(
                target=stream_output,
                args=(proc.stdout, "stdout"),
                daemon=True,
            ),
            threading.Thread(
                target=stream_output,
                args=(proc.stderr, "stderr"),
                daemon=True,
            ),
        ]
        for thread in threads:
            thread.start()

        return cls(
            process=proc,
            name=name,
            log_writer=log_writer,
            cmd=list(cmd),
            _threads=threads,
            ref=ref,
            _start_time=start_time,
            _callosum=callosum,
            _owns_callosum=owns_callosum,
        )

    def wait(self, timeout: float | None = None) -> int:
        """Wait for process completion, return exit code.

        Args:
            timeout: Optional timeout in seconds

        Returns:
            Exit code

        Raises:
            subprocess.TimeoutExpired: If timeout exceeded
        """
        return self.process.wait(timeout=timeout)

    def poll(self) -> int | None:
        """Check if process has terminated.

        Returns:
            Exit code if terminated, None if still running
        """
        return self.process.poll()

    def is_running(self) -> bool:
        """Check if process is still running."""
        return self.process.poll() is None

    def terminate(self, timeout: float = 15) -> int:
        """Terminate the managed process and its snapshotted process tree.

        Descendants are snapshotted before signaling so children that outlive
        and are reparented away from the managed process can still be targeted.
        SIGTERM is sent to the managed parent, parent process group, descendant
        pids, and descendant process groups. If any live process remains after
        the bounded graceful wait, SIGKILL is sent to the remaining tree targets
        and reaping is bounded by `KILL_REAP_GRACE_S`.

        Args:
            timeout: Seconds to wait after SIGTERM before SIGKILL (default: 15).

        Returns:
            Parent exit code when the parent and all snapshotted descendants
            are reaped or gone.

        Raises:
            subprocess.TimeoutExpired: The original parent wait timeout is
                re-raised after bounded SIGKILL escalation if the parent did not
                reap after SIGTERM.
            ProcessTreeNotReaped: Raised when the parent reaped but descendant
                cleanup could not be proven or descendants survived SIGKILL.
        """
        logger.debug(f"Terminating {self.name} (PID {self.pid})...")
        own_pid = os.getpid()
        own_pgid = os.getpgrp()
        try:
            parent_pgid = os.getpgid(self.process.pid)
        except (ProcessLookupError, OSError):
            parent_pgid = None

        try:
            descendants = snapshot_descendants(self.process.pid)
            snapshot_error = None
        except (psutil.AccessDenied, psutil.Error, OSError) as exc:
            descendants = []
            snapshot_error = exc
            logger.warning(
                "%s terminate: descendant snapshot uncertain pid=%s pgid=%s error=%s",
                self.name,
                self.process.pid,
                parent_pgid,
                exc.__class__.__name__,
            )

        deadline = time.monotonic() + timeout
        parent_timeout = None
        parent_alive = False
        exit_code = None

        _signal_parent_process(
            self.process,
            signal.SIGTERM,
            own_pid=own_pid,
            own_pgid=own_pgid,
            process_name=self.name,
        )
        _signal_pgid(
            parent_pgid,
            signal.SIGTERM,
            own_pid=own_pid,
            own_pgid=own_pgid,
            process_name=self.name,
        )
        _signal_descendants(
            descendants,
            signal.SIGTERM,
            own_pid=own_pid,
            own_pgid=own_pgid,
            process_name=self.name,
        )

        try:
            exit_code = self.process.wait(timeout=timeout)
            logger.debug(f"{self.name} terminated gracefully with code {exit_code}")
        except subprocess.TimeoutExpired as exc:
            parent_timeout = exc
            parent_alive = True
            logger.warning(
                "%s terminate: parent did not reap after %ss; "
                "force killing pid=%s pgid=%s",
                self.name,
                timeout,
                self.process.pid,
                parent_pgid,
            )

        graceful_survivors = []
        if not parent_alive and snapshot_error is None and descendants:
            graceful_survivors = _poll_descendants_until_gone(descendants, deadline)

        if parent_alive or graceful_survivors:
            if parent_alive:
                _signal_parent_process(
                    self.process,
                    signal.SIGKILL,
                    own_pid=own_pid,
                    own_pgid=own_pgid,
                    process_name=self.name,
                )
                _signal_pgid(
                    parent_pgid,
                    signal.SIGKILL,
                    own_pid=own_pid,
                    own_pgid=own_pgid,
                    process_name=self.name,
                )

            _signal_descendants(
                descendants,
                signal.SIGKILL,
                own_pid=own_pid,
                own_pgid=own_pgid,
                process_name=self.name,
            )

            if parent_alive:
                try:
                    self.process.wait(timeout=KILL_REAP_GRACE_S)
                    logger.debug(
                        f"{self.name} killed with code {self.process.returncode}"
                    )
                except subprocess.TimeoutExpired:
                    logger.warning(
                        "%s remained unreaped %.1fs after SIGKILL "
                        "pid=%s pgid=%s "
                        "(likely D-state; supervisor exiting; SIGKILL guarantees "
                        "eventual death)",
                        self.name,
                        KILL_REAP_GRACE_S,
                        self.process.pid,
                        parent_pgid,
                    )

            kill_survivors = []
            if descendants:
                kill_survivors = _poll_descendants_until_gone(
                    descendants,
                    time.monotonic() + KILL_REAP_GRACE_S,
                )

            if kill_survivors:
                _log_descendant_survivors(
                    self.name,
                    "parent_timeout" if parent_alive else "survived_sigkill",
                    kill_survivors,
                )

            if parent_alive:
                raise parent_timeout

            if kill_survivors:
                raise ProcessTreeNotReaped(
                    self.cmd,
                    timeout,
                    reason="survived_sigkill",
                    survivors=kill_survivors,
                )

        if snapshot_error is not None:
            logger.warning(
                "%s terminate: cleanup not proven pid=%s pgid=%s "
                "reason=cleanup_unproven",
                self.name,
                self.process.pid,
                parent_pgid,
            )
            raise ProcessTreeNotReaped(
                self.cmd,
                timeout,
                reason="cleanup_unproven",
            )

        if exit_code is None:
            exit_code = self.process.returncode
        return exit_code

    def cleanup(self) -> None:
        """Wait for output threads to finish and close log file.

        Call this after process exits to clean up resources.
        Each step is isolated so one failure doesn't block the rest.
        """
        for thread in self._threads:
            try:
                thread.join(timeout=1)
            except Exception:
                pass

        try:
            self.log_writer.close()
        except Exception:
            pass

        # Emit exit event
        if self._callosum:
            try:
                duration_ms = int((time.time() - self._start_time) * 1000)
                self._callosum.emit(
                    "logs",
                    "exit",
                    ref=self.ref,
                    name=self.name,
                    pid=self.pid,
                    exit_code=self.returncode,
                    duration_ms=duration_ms,
                    cmd=self.cmd,
                    log_path=str(self.log_writer.path),
                )
            except Exception:
                pass
            # Only stop callosum if we created it (not shared)
            if self._owns_callosum:
                try:
                    self._callosum.stop()
                except Exception:
                    pass

    @property
    def pid(self) -> int:
        """Process ID."""
        return self.process.pid

    @property
    def returncode(self) -> int | None:
        """Return code if process has exited, None otherwise."""
        return self.process.returncode


def run_task(
    cmd: list[str],
    *,
    timeout: float | None = None,
    env: dict | None = None,
    ref: str | None = None,
    callosum: CallosumConnection | None = None,
    day: str | None = None,
) -> tuple[bool, int, Path, bool]:
    """Run a task to completion with automatic logging (blocking).

    Spawns process, waits for completion, cleans up resources.
    Output is automatically logged to: journal/{YYYYMMDD}/health/{ref}_{name}.log
    where name is derived from cmd[0] basename.

    Args:
        cmd: Command and arguments
        timeout: Optional timeout in seconds
        env: Optional environment variables
        ref: Optional correlation ID (auto-generated if not provided)
        callosum: Optional shared CallosumConnection (creates new one if not provided)
        day: Optional day override (YYYYMMDD). When provided, logs are placed
            in that day's health directory instead of today's.

    Returns:
        (success, exit_code, log_path, timed_out) tuple. success is True
        only when the process exited 0 AND did not exceed the wall-clock
        ``timeout``; a timeout is always a failure regardless of the
        post-termination exit code. log_path points to the process output
        log file, and timed_out is True only when the wall-clock ``timeout``
        was exceeded.

    Example:
        success, code, log, timed_out = run_task(
            ["sol", "generate", "20241101", "-f", "flow"],
            timeout=300,
        )
        # Logs to: {JOURNAL}/{YYYYMMDD}/health/{ref}_generate.log

        # With explicit correlation ID:
        success, code, log, timed_out = run_task(
            ["journal", "indexer", "--rescan"],
            ref="1730476800000",
        )
        # Logs to: {JOURNAL}/{YYYYMMDD}/health/1730476800000_indexer.log
    """
    managed = ManagedProcess.spawn(cmd, env=env, ref=ref, callosum=callosum, day=day)
    log_path = managed.log_writer.path
    timed_out = False
    try:
        exit_code = managed.wait(timeout=timeout)
    except subprocess.TimeoutExpired:
        logger.error(f"{managed.name} timed out after {timeout}s, terminating...")
        timed_out = True
        exit_code = managed.terminate()
    finally:
        managed.cleanup()

    if exit_code != 0:
        logger.warning(f"{managed.name} exited with code {exit_code}")

    return ((exit_code == 0) and not timed_out, exit_code, log_path, timed_out)
