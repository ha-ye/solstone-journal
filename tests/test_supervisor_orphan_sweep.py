# SPDX-License-Identifier: AGPL-3.0-only
# Copyright (c) 2026 sol pbc

import os
import signal
import subprocess
import sys
from pathlib import Path

import pytest

from solstone.think import supervisor
from solstone.think.journal_io.lease import (
    acquire_file_lease,
    read_file_lease_fd,
    set_file_lease_offset_token,
)
from solstone.think.providers.mlx_server import MLX_SERVER_PROCESS_NAME

TEST_JOURNAL = Path("/journal/test")


def _write_fd_holder_script(tmp_path: Path) -> Path:
    script = tmp_path / "fd_holder.py"
    script.write_text(
        "\n".join(
            [
                "import os",
                "import subprocess",
                "import sys",
                "import time",
                "",
                "fd = int(sys.argv[1])",
                "role = sys.argv[2]",
                "if role == 'child':",
                "    grand = subprocess.Popen(",
                "        [sys.executable, __file__, str(fd), 'grand'],",
                "        stdin=subprocess.DEVNULL,",
                "        stdout=subprocess.DEVNULL,",
                "        stderr=subprocess.DEVNULL,",
                "        pass_fds=(fd,),",
                "    )",
                "    print(grand.pid, flush=True)",
                "else:",
                "    print(os.getpid(), flush=True)",
                "while True:",
                "    time.sleep(1)",
            ]
        )
        + "\n",
        encoding="utf-8",
    )
    return script


def _spawn_fd_holder(script: Path, fd: int, role: str) -> subprocess.Popen:
    return subprocess.Popen(
        [sys.executable, str(script), str(fd), role],
        stdin=subprocess.DEVNULL,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
        pass_fds=(fd,),
    )


def _cleanup_processes(*pids: int) -> None:
    for pid in pids:
        try:
            proc = supervisor.psutil.Process(pid)
        except supervisor.psutil.NoSuchProcess:
            continue
        try:
            proc.terminate()
        except supervisor.psutil.Error:
            pass
    for pid in pids:
        try:
            proc = supervisor.psutil.Process(pid)
            proc.wait(timeout=2)
        except (supervisor.psutil.NoSuchProcess, supervisor.psutil.TimeoutExpired):
            try:
                supervisor.psutil.Process(pid).kill()
            except supervisor.psutil.Error:
                pass


class _FakeProcess:
    def __init__(
        self,
        *,
        pid: int,
        name: str = "journal:sense",
        ppid: int = 1,
        username: str = "jer",
        name_error: Exception | None = None,
        ppid_error: Exception | None = None,
        username_error: Exception | None = None,
    ):
        self.pid = pid
        self._name = name
        self._ppid = ppid
        self._username = username
        self._name_error = name_error
        self._ppid_error = ppid_error
        self._username_error = username_error

    def name(self) -> str:
        if self._name_error:
            raise self._name_error
        return self._name

    def ppid(self) -> int:
        if self._ppid_error:
            raise self._ppid_error
        return self._ppid

    def username(self) -> str:
        if self._username_error:
            raise self._username_error
        return self._username


class TestOrphanSweep:
    def _patch_common(self, monkeypatch, procs):
        kills = []
        monkeypatch.setattr(supervisor.sys, "platform", "linux")
        monkeypatch.setattr(supervisor.getpass, "getuser", lambda: "jer")
        monkeypatch.setattr(supervisor.psutil, "process_iter", lambda _attrs: procs)
        monkeypatch.setattr(supervisor, "_candidate_journal", lambda proc: TEST_JOURNAL)
        monkeypatch.setattr(
            supervisor.os, "kill", lambda pid, sig: kills.append((pid, sig))
        )
        return kills

    @pytest.mark.parametrize(
        "proctitle",
        [
            "journal:sense",
            "journal:cortex",
            "journal:convey",
            "journal:spl",
            "journal:think",
            "journal:heartbeat",
            "journal:identity",
            "journal:providers",
            "journal:facet-candidates",
            "llama-server",
            MLX_SERVER_PROCESS_NAME,
        ],
    )
    def test_sweepable_orphan_proctitles_are_sigtermed(self, monkeypatch, proctitle):
        procs = [_FakeProcess(pid=111, name=proctitle)]
        kills = self._patch_common(monkeypatch, procs)
        monkeypatch.setattr(supervisor.psutil, "pid_exists", lambda _pid: False)

        assert supervisor._sweep_orphaned_sol_processes(journal=TEST_JOURNAL) == 1
        assert kills == [(111, signal.SIGTERM)]

    @pytest.mark.parametrize(
        "proctitle",
        ["sol:call", "solstone:convey", "journal", "python"],
    )
    def test_non_sweepable_orphan_proctitles_are_ignored(self, monkeypatch, proctitle):
        procs = [_FakeProcess(pid=111, name=proctitle)]
        kills = self._patch_common(monkeypatch, procs)

        assert supervisor._sweep_orphaned_sol_processes(journal=TEST_JOURNAL) == 0
        assert kills == []

    def test_non_matching_processes_are_ignored(self, monkeypatch):
        monkeypatch.setattr(supervisor.os, "getpid", lambda: 555)
        procs = [
            _FakeProcess(pid=111, username="other"),
            _FakeProcess(pid=112, name="llama-server", username="other"),
            _FakeProcess(pid=222, ppid=2),
            _FakeProcess(pid=223, name="llama-server", ppid=2),
            _FakeProcess(pid=333, name="python"),
            _FakeProcess(pid=444, name="solstone:convey"),
            _FakeProcess(pid=555),
        ]
        kills = self._patch_common(monkeypatch, procs)

        assert supervisor._sweep_orphaned_sol_processes(journal=TEST_JOURNAL) == 0
        assert kills == []

    def test_survivors_after_grace_are_sigkilled(self, monkeypatch):
        procs = [_FakeProcess(pid=111), _FakeProcess(pid=222)]
        kills = self._patch_common(monkeypatch, procs)
        monkeypatch.setattr(supervisor.psutil, "pid_exists", lambda pid: pid == 222)
        monkeypatch.setattr(supervisor.time, "sleep", lambda _seconds: None)

        assert (
            supervisor._sweep_orphaned_sol_processes(
                journal=TEST_JOURNAL,
                grace=0.0,
            )
            == 2
        )
        assert kills == [
            (111, signal.SIGTERM),
            (222, signal.SIGTERM),
            (222, signal.SIGKILL),
        ]

    def test_process_access_errors_are_swallowed(self, monkeypatch):
        procs = [
            _FakeProcess(pid=111, name_error=supervisor.psutil.NoSuchProcess(pid=111)),
            _FakeProcess(
                pid=222,
                username_error=supervisor.psutil.AccessDenied(pid=222),
            ),
            _FakeProcess(pid=333),
        ]
        kills = self._patch_common(monkeypatch, procs)
        monkeypatch.setattr(supervisor.psutil, "pid_exists", lambda _pid: False)

        assert supervisor._sweep_orphaned_sol_processes(journal=TEST_JOURNAL) == 1
        assert kills == [(333, signal.SIGTERM)]

    @pytest.mark.parametrize("platform", ["linux", "darwin", "freebsd"])
    def test_runs_on_all_platforms(self, monkeypatch, platform):
        procs = [_FakeProcess(pid=111)]
        kills = self._patch_common(monkeypatch, procs)
        monkeypatch.setattr(supervisor.sys, "platform", platform)
        monkeypatch.setattr(supervisor.psutil, "pid_exists", lambda _pid: False)

        assert supervisor._sweep_orphaned_sol_processes(journal=TEST_JOURNAL) == 1
        assert kills == [(111, signal.SIGTERM)]

    def test_candidate_in_different_journal_is_skipped(self, monkeypatch):
        procs = [_FakeProcess(pid=111), _FakeProcess(pid=222, name="llama-server")]
        kills = self._patch_common(monkeypatch, procs)
        monkeypatch.setattr(
            supervisor,
            "_candidate_journal",
            lambda proc: Path("/journal/other"),
        )

        assert supervisor._sweep_orphaned_sol_processes(journal=TEST_JOURNAL) == 0
        assert kills == []

    @pytest.mark.parametrize(
        "reason",
        ["access_denied", "missing_key", "malformed_value"],
    )
    def test_unknown_journal_candidate_is_skipped(self, monkeypatch, reason):
        procs = [_FakeProcess(pid=111)]
        kills = self._patch_common(monkeypatch, procs)
        monkeypatch.setattr(supervisor, "_candidate_journal", lambda proc: None)

        assert supervisor._sweep_orphaned_sol_processes(journal=TEST_JOURNAL) == 0
        assert kills == []

    def test_same_journal_candidate_is_swept(self, monkeypatch):
        procs = [_FakeProcess(pid=111)]
        kills = self._patch_common(monkeypatch, procs)
        monkeypatch.setattr(
            supervisor,
            "_candidate_journal",
            lambda proc: TEST_JOURNAL,
        )
        monkeypatch.setattr(supervisor.psutil, "pid_exists", lambda _pid: False)

        assert supervisor._sweep_orphaned_sol_processes(journal=TEST_JOURNAL) == 1
        assert kills == [(111, signal.SIGTERM)]

    def test_fd_holding_child_grandchild_reaped_before_replacement_and_survivor_blocks(
        self, tmp_path, monkeypatch
    ):
        journal = tmp_path / "journal"
        lock_path = journal / "health" / "speakers-analyze" / "install-generation.lock"
        script = _write_fd_holder_script(tmp_path)
        current_user = supervisor.getpass.getuser()
        monkeypatch.setattr(supervisor.sys, "platform", "linux")
        monkeypatch.setattr(supervisor.getpass, "getuser", lambda: current_user)
        monkeypatch.setattr(
            supervisor, "_candidate_journal", lambda proc: journal.resolve()
        )

        lease = acquire_file_lease(lock_path, attempts=1)
        assert lease is not None
        set_file_lease_offset_token(lease, 73, lock_path)
        owner_fd = read_file_lease_fd(lease, lock_path)
        child = _spawn_fd_holder(script, owner_fd, "child")
        assert child.stdout is not None
        grand_pid = int(child.stdout.readline().strip())
        os.close(owner_fd)
        lease._fd = None
        spawned_pids = {child.pid, grand_pid}
        inventory = [
            _FakeProcess(pid=child.pid, name="journal:sense", username=current_user),
            _FakeProcess(pid=grand_pid, name="journal:think", username=current_user),
        ]
        assert {proc.pid for proc in inventory}.issubset(spawned_pids)
        monkeypatch.setattr(supervisor.psutil, "process_iter", lambda _attrs: inventory)
        try:
            assert acquire_file_lease(lock_path, attempts=1) is None
            assert supervisor._sweep_orphaned_sol_processes(journal=journal) == 2
            _cleanup_processes(child.pid, grand_pid)
            replacement = acquire_file_lease(lock_path, attempts=1)
            assert replacement is not None
            replacement.release()
        finally:
            _cleanup_processes(child.pid, grand_pid)

        survivor_lease = acquire_file_lease(lock_path, attempts=1)
        assert survivor_lease is not None
        set_file_lease_offset_token(survivor_lease, 74, lock_path)
        survivor_fd = read_file_lease_fd(survivor_lease, lock_path)
        survivor = _spawn_fd_holder(script, survivor_fd, "survivor")
        assert survivor.stdout is not None
        survivor_pid = int(survivor.stdout.readline().strip())
        os.close(survivor_fd)
        survivor_lease._fd = None
        monkeypatch.setattr(supervisor.psutil, "process_iter", lambda _attrs: [])
        try:
            assert supervisor._sweep_orphaned_sol_processes(journal=journal) == 0
            assert acquire_file_lease(lock_path, attempts=1) is None
        finally:
            _cleanup_processes(survivor.pid, survivor_pid)

        replacement = acquire_file_lease(lock_path, attempts=1)
        assert replacement is not None
        replacement.release()
