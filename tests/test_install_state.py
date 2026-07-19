# SPDX-License-Identifier: AGPL-3.0-only
# Copyright (c) 2026 sol pbc

from __future__ import annotations

import json
import multiprocessing
import os
from pathlib import Path
from typing import get_args

import pytest

from solstone.think.providers import install_state
from solstone.think.providers.install_state import (
    IN_FLIGHT_STATES,
    TERMINAL_STATES,
    InstallState,
    InstallStatusConflictError,
    InstallStatusMalformedError,
    begin_install_attempt,
    bump_progress,
    canonical_fingerprint,
    fingerprint_sha256,
    make_idle_status,
    provider_status_path,
    read_install_status,
    record_interrupted_install,
    transition_state,
    write_install_status,
)


def _set_journal(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("SOLSTONE_JOURNAL", str(tmp_path))


def _target(model: str = "model") -> dict:
    return {
        "provider": "local",
        "model": model,
        "requirements": [
            {"role": "model", "path": "b"},
            {"path": "a", "role": "runtime"},
        ],
    }


def _transition_worker(journal: str, barrier, queue) -> None:
    os.environ["SOLSTONE_JOURNAL"] = journal
    from solstone.think.providers.install_state import (
        InstallStatusConflictError,
        read_install_status,
        transition_state,
        write_install_status,
    )

    status = read_install_status(name="local")
    barrier.wait(timeout=10)
    try:
        write_install_status(transition_state(status, new_state="downloading"))
    except InstallStatusConflictError:
        queue.put("conflict")
    else:
        queue.put("written")


def test_install_state_literal_membership_and_partitions() -> None:
    assert get_args(InstallState) == (
        "idle",
        "resolving",
        "downloading",
        "verifying",
        "installing",
        "installed",
        "failed",
    )
    assert IN_FLIGHT_STATES | TERMINAL_STATES == set(get_args(InstallState))
    assert IN_FLIGHT_STATES & TERMINAL_STATES == set()


def test_missing_record_is_synthetic_idle(tmp_path, monkeypatch) -> None:
    _set_journal(tmp_path, monkeypatch)

    status = read_install_status(name="local")

    assert status["provider"] == "local"
    assert status["install_state"] == "idle"
    assert status["revision"] == 0
    assert not provider_status_path("local").exists()


def test_malformed_record_raises(tmp_path, monkeypatch) -> None:
    _set_journal(tmp_path, monkeypatch)
    path = provider_status_path("local")
    path.parent.mkdir(parents=True)
    path.write_text("{not-json", encoding="utf-8")

    with pytest.raises(InstallStatusMalformedError):
        read_install_status(name="local")


def test_status_record_mode_and_provider_independence(tmp_path, monkeypatch) -> None:
    _set_journal(tmp_path, monkeypatch)

    local = begin_install_attempt("local", _target("local"))
    parakeet = begin_install_attempt(
        "parakeet", {"provider": "parakeet", "model": "parakeet"}
    )

    assert local["provider"] == "local"
    assert parakeet["provider"] == "parakeet"
    assert read_install_status(name="local")["attempt_id"] == local["attempt_id"]
    assert read_install_status(name="parakeet")["attempt_id"] == parakeet["attempt_id"]
    assert (provider_status_path("local").stat().st_mode & 0o777) == 0o600
    assert (provider_status_path("parakeet").stat().st_mode & 0o777) == 0o600


def test_canonical_fingerprint_is_stable_and_sorted() -> None:
    left = canonical_fingerprint(_target())
    right = canonical_fingerprint(
        {
            "requirements": [
                {"path": "a", "role": "runtime"},
                {"role": "model", "path": "b"},
            ],
            "model": "model",
            "provider": "local",
        }
    )

    assert left == right
    assert fingerprint_sha256(left) == fingerprint_sha256(right)
    assert " " not in left


def test_transition_graph_allows_phase_cycles_for_same_attempt(
    tmp_path, monkeypatch
) -> None:
    _set_journal(tmp_path, monkeypatch)
    status = begin_install_attempt("local", _target(), initial_state="downloading")
    verifying = write_install_status(transition_state(status, new_state="verifying"))
    downloading = write_install_status(
        transition_state(verifying, new_state="downloading")
    )

    assert downloading["install_state"] == "downloading"
    assert downloading["attempt_id"] == status["attempt_id"]
    assert downloading["revision"] == verifying["revision"] + 1


def test_first_terminal_wins_and_late_progress_is_ignored(
    tmp_path, monkeypatch
) -> None:
    _set_journal(tmp_path, monkeypatch)
    status = begin_install_attempt("local", _target(), initial_state="downloading")
    installed = write_install_status(transition_state(status, new_state="installed"))

    late_progress = write_install_status(bump_progress(status, received=1, total=2))
    late_failed = write_install_status(
        transition_state(status, new_state="failed", error="too late")
    )

    assert late_progress == installed
    assert late_failed == installed
    assert read_install_status(name="local")["install_state"] == "installed"


def test_stale_revision_rejected(tmp_path, monkeypatch) -> None:
    _set_journal(tmp_path, monkeypatch)
    status = begin_install_attempt("local", _target(), initial_state="downloading")
    current = write_install_status(transition_state(status, new_state="verifying"))

    with pytest.raises(InstallStatusConflictError):
        write_install_status(transition_state(status, new_state="installing"))

    assert read_install_status(name="local") == current


def test_different_attempt_cannot_start_while_in_flight(tmp_path, monkeypatch) -> None:
    _set_journal(tmp_path, monkeypatch)
    current = begin_install_attempt("local", _target(), initial_state="downloading")
    other = make_idle_status("local")
    other["revision"] = current["revision"]
    other["attempt_id"] = "other-attempt"
    other["install_state"] = "resolving"

    with pytest.raises(InstallStatusConflictError):
        write_install_status(other)


def test_progress_coalescing(monkeypatch, tmp_path) -> None:
    _set_journal(tmp_path, monkeypatch)
    clock = [0.0]
    monkeypatch.setattr(install_state.time, "monotonic", lambda: clock[0])
    status = begin_install_attempt("local", _target(), initial_state="downloading")

    first = write_install_status(bump_progress(status, received=1, total=10))
    clock[0] = 0.2
    skipped = write_install_status(bump_progress(first, received=2, total=10))
    clock[0] = 1.2
    written = write_install_status(bump_progress(skipped, received=3, total=10))

    assert first["progress_bytes_received"] == 1
    assert skipped == first
    assert written["revision"] == first["revision"] + 1
    assert written["progress_bytes_received"] == 3


def test_record_interrupted_install_marks_matching_attempt_failed(
    tmp_path, monkeypatch
) -> None:
    _set_journal(tmp_path, monkeypatch)
    status = begin_install_attempt("local", _target(), initial_state="downloading")

    failed = record_interrupted_install(
        "local",
        attempt_id=str(status["attempt_id"]),
        target_fingerprint_sha256=status["target_fingerprint_sha256"],
    )

    assert failed["install_state"] == "failed"
    assert failed["error_code"] == "install_interrupted"


def test_migration_api_removes_legacy_status_fields(tmp_path, monkeypatch) -> None:
    _set_journal(tmp_path, monkeypatch)
    config_path = tmp_path / "config" / "journal.json"
    config_path.parent.mkdir(parents=True)
    config_path.write_text(
        json.dumps(
            {
                "providers": {
                    "bundled": {
                        "local": {
                            "install_state": "failed",
                            "install_error": "old",
                            "model_id": "kept",
                        }
                    }
                }
            }
        )
        + "\n",
        encoding="utf-8",
    )

    result = install_state.migrate_legacy_provider_install_state()

    data = json.loads(config_path.read_text(encoding="utf-8"))
    assert result == {"removed": 2}
    assert data["providers"]["bundled"]["local"] == {"model_id": "kept"}


def test_two_process_stale_transition_one_writer_wins(tmp_path, monkeypatch) -> None:
    _set_journal(tmp_path, monkeypatch)
    ctx = multiprocessing.get_context("spawn")
    barrier = ctx.Barrier(2)
    queue = ctx.Queue()
    processes = [
        ctx.Process(target=_transition_worker, args=(str(tmp_path), barrier, queue))
        for _ in range(2)
    ]

    for process in processes:
        process.start()
    for process in processes:
        process.join(timeout=10)
    for process in processes:
        if process.is_alive():
            process.terminate()
            process.join(timeout=5)
    assert [process.exitcode for process in processes] == [0, 0]

    results = sorted(queue.get(timeout=1) for _ in processes)
    assert results == ["conflict", "written"]
    assert read_install_status(name="local")["install_state"] == "downloading"
