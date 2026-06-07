# SPDX-License-Identifier: AGPL-3.0-only
# Copyright (c) 2026 sol pbc

from __future__ import annotations

import multiprocessing
import os
import traceback
from pathlib import Path
from queue import Empty
from typing import Any

import pytest


def _append_pattern_worker(
    journal_path: str,
    barrier: Any,
    errors: Any,
    slug: str,
) -> None:
    os.environ["SOLSTONE_JOURNAL"] = journal_path
    try:
        barrier.wait(timeout=5)

        from solstone.think.skills import locked_modify_patterns

        def mutate(rows: list[dict[str, Any]]) -> list[dict[str, Any]]:
            next_rows = list(rows)
            next_rows.append({"slug": slug})
            return next_rows

        locked_modify_patterns(mutate)
    except BaseException:
        errors.put(traceback.format_exc())
        raise


def _drain_errors(errors: Any) -> list[str]:
    found = []
    while True:
        try:
            found.append(errors.get_nowait())
        except Empty:
            return found


def _join_processes(processes: list[Any], errors: Any) -> None:
    for process in processes:
        process.start()
    for process in processes:
        process.join(timeout=10)
    for process in processes:
        if process.is_alive():
            process.terminate()
            process.join(timeout=2)

    error_text = "\n".join(_drain_errors(errors))
    assert all(not process.is_alive() for process in processes), error_text
    assert all(process.exitcode == 0 for process in processes), error_text


def _use_journal(monkeypatch: pytest.MonkeyPatch, journal: Path) -> None:
    monkeypatch.setenv("SOLSTONE_JOURNAL", str(journal))

    import solstone.think.utils as think_utils

    think_utils._journal_path_cache = None


def test_locked_modify_patterns_serializes_processes(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    ctx = multiprocessing.get_context("spawn")
    journal = tmp_path / "skills-processes"
    barrier = ctx.Barrier(2)
    errors = ctx.Queue()
    processes = [
        ctx.Process(
            target=_append_pattern_worker,
            args=(str(journal), barrier, errors, "alpha-skill"),
        ),
        ctx.Process(
            target=_append_pattern_worker,
            args=(str(journal), barrier, errors, "beta-skill"),
        ),
    ]

    _join_processes(processes, errors)

    _use_journal(monkeypatch, journal)
    from solstone.think.skills import load_patterns

    rows = load_patterns()
    assert sorted(row["slug"] for row in rows) == ["alpha-skill", "beta-skill"]


def test_locked_modify_patterns_raises_lock_timeout(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    journal = tmp_path / "skills-timeout"
    _use_journal(monkeypatch, journal)

    from solstone.think import skills as think_skills
    from solstone.think.journal_io import LockTimeout
    from solstone.think.journal_io import hold_lock as real_hold_lock

    def short_hold_lock(path: Path):
        return real_hold_lock(path, timeout=0.2, poll_interval=0.01)

    monkeypatch.setattr(think_skills, "hold_lock", short_hold_lock)
    path = think_skills.patterns_path()

    with real_hold_lock(path, timeout=30):
        with pytest.raises(LockTimeout):
            think_skills.locked_modify_patterns(lambda rows: rows)
