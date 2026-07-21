# SPDX-License-Identifier: AGPL-3.0-only
# Copyright (c) 2026 sol pbc

from __future__ import annotations

import os
import subprocess
import sys
import time
from pathlib import Path

import pytest

from solstone.think.providers.install_lease import (
    acquire_install_lease,
    lease_path,
    probe_install_lease_free,
    probe_install_lease_state,
    prune_unowned_lease_file,
)


def _env(journal: Path) -> dict[str, str]:
    env = os.environ.copy()
    env["SOLSTONE_JOURNAL"] = str(journal)
    return env


def test_lock_file_existence_alone_is_not_busy(tmp_path, monkeypatch) -> None:
    monkeypatch.setenv("SOLSTONE_JOURNAL", str(tmp_path))
    path = lease_path("local")
    path.parent.mkdir(parents=True)
    path.write_text("", encoding="utf-8")

    assert probe_install_lease_free("local") is True
    lease = acquire_install_lease("local")
    assert lease is not None
    lease.release()


def test_context_release_paths(tmp_path, monkeypatch) -> None:
    monkeypatch.setenv("SOLSTONE_JOURNAL", str(tmp_path))

    with acquire_install_lease("local") as lease:
        assert lease is not None
        assert probe_install_lease_free("local") is False

    assert probe_install_lease_free("local") is True


def test_probe_install_lease_state_missing_does_not_create_path(
    tmp_path, monkeypatch
) -> None:
    monkeypatch.setenv("SOLSTONE_JOURNAL", str(tmp_path))
    path = lease_path("local")

    assert probe_install_lease_state("local") == "missing"
    assert not path.exists()
    assert not path.parent.exists()


def test_probe_install_lease_state_free_existing_file(tmp_path, monkeypatch) -> None:
    monkeypatch.setenv("SOLSTONE_JOURNAL", str(tmp_path))
    path = lease_path("local")
    path.parent.mkdir(parents=True)
    path.write_text("", encoding="utf-8")

    assert probe_install_lease_state("local") == "free"
    assert path.exists()


def test_probe_install_lease_state_held_existing_file(tmp_path, monkeypatch) -> None:
    monkeypatch.setenv("SOLSTONE_JOURNAL", str(tmp_path))

    lease = acquire_install_lease("local")
    assert lease is not None
    try:
        assert probe_install_lease_state("local") == "held"
    finally:
        lease.release()

    assert probe_install_lease_state("local") == "free"


def test_probe_install_lease_state_propagates_unexpected_oserror(
    tmp_path, monkeypatch
) -> None:
    monkeypatch.setenv("SOLSTONE_JOURNAL", str(tmp_path))
    path = lease_path("local")
    path.parent.mkdir(parents=True)
    path.write_text("", encoding="utf-8")

    from solstone.think.providers import install_lease

    def fail_flock(_fd: int, _operation: int) -> None:
        raise OSError("unexpected flock failure")

    monkeypatch.setattr(install_lease.fcntl, "flock", fail_flock)

    with pytest.raises(OSError, match="unexpected flock failure"):
        probe_install_lease_state("local")


def test_prune_unowned_lease_file(tmp_path, monkeypatch) -> None:
    monkeypatch.setenv("SOLSTONE_JOURNAL", str(tmp_path))
    path = lease_path("local")
    path.parent.mkdir(parents=True)
    path.write_text("", encoding="utf-8")

    assert prune_unowned_lease_file("local") is True
    assert not path.exists()


def test_two_process_holder_and_contender(tmp_path) -> None:
    ready = tmp_path / "ready"
    release = tmp_path / "release"
    holder_code = f"""
import pathlib
import time
from solstone.think.providers.install_lease import acquire_install_lease
lease = acquire_install_lease("local")
assert lease is not None
pathlib.Path({str(ready)!r}).write_text("ready")
while not pathlib.Path({str(release)!r}).exists():
    time.sleep(0.05)
lease.release()
"""
    contender_code = """
from solstone.think.providers.install_lease import acquire_install_lease
lease = acquire_install_lease("local")
if lease is None:
    print("busy", flush=True)
else:
    print("free", flush=True)
    lease.release()
"""
    holder = subprocess.Popen(
        [sys.executable, "-c", holder_code],
        cwd=Path.cwd(),
        env=_env(tmp_path),
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
    )
    try:
        deadline = time.monotonic() + 10
        while not ready.exists() and time.monotonic() < deadline:
            time.sleep(0.05)
        assert ready.exists()

        busy = subprocess.run(
            [sys.executable, "-c", contender_code],
            cwd=Path.cwd(),
            env=_env(tmp_path),
            capture_output=True,
            text=True,
            check=True,
        )
        assert busy.stdout.strip() == "busy"

        release.write_text("release", encoding="utf-8")
        stdout, stderr = holder.communicate(timeout=10)
        assert holder.returncode == 0, (stdout, stderr)

        free = subprocess.run(
            [sys.executable, "-c", contender_code],
            cwd=Path.cwd(),
            env=_env(tmp_path),
            capture_output=True,
            text=True,
            check=True,
        )
        assert free.stdout.strip() == "free"
    finally:
        if holder.poll() is None:
            holder.terminate()
            holder.wait(timeout=5)


def test_interrupted_classification_free_vs_busy(tmp_path, monkeypatch) -> None:
    monkeypatch.setenv("SOLSTONE_JOURNAL", str(tmp_path))

    lease = acquire_install_lease("parakeet")
    assert lease is not None
    try:
        assert probe_install_lease_free("parakeet") is False
    finally:
        lease.release()

    assert probe_install_lease_free("parakeet") is True
