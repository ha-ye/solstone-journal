# SPDX-License-Identifier: AGPL-3.0-only
# Copyright (c) 2026 sol pbc

from __future__ import annotations

import json
import multiprocessing
import os
import time
import traceback
from pathlib import Path
from queue import Empty
from typing import Any


def _update_worker(
    journal_path: str,
    barrier: Any,
    errors: Any,
    section: str,
    data: dict[str, Any],
    delay: float,
) -> None:
    os.environ["SOLSTONE_JOURNAL"] = journal_path
    try:
        barrier.wait(timeout=5)
        if delay:
            time.sleep(delay)

        from solstone.think.awareness import update_state

        update_state(section, data)
    except BaseException:
        errors.put(traceback.format_exc())
        raise


def _record_import_worker(
    journal_path: str,
    barrier: Any,
    errors: Any,
) -> None:
    os.environ["SOLSTONE_JOURNAL"] = journal_path
    try:
        barrier.wait(timeout=5)

        from solstone.think.awareness import record_import

        record_import("src", source_display=None)
    except BaseException:
        errors.put(traceback.format_exc())
        raise


def _seed_current(journal: Path, state: dict[str, Any]) -> Path:
    awareness_dir = journal / "awareness"
    awareness_dir.mkdir(parents=True)
    current_path = awareness_dir / "current.json"
    current_path.write_text(json.dumps(state, indent=2) + "\n", encoding="utf-8")
    return current_path


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


def _run_section_case(
    tmp_path: Path,
    name: str,
    alpha_delay: float,
    beta_delay: float,
) -> None:
    ctx = multiprocessing.get_context("spawn")
    journal = tmp_path / name
    current_path = _seed_current(journal, {"seed": {"k": "v"}})
    barrier = ctx.Barrier(2)
    errors = ctx.Queue()

    processes = [
        ctx.Process(
            target=_update_worker,
            args=(str(journal), barrier, errors, "alpha", {"a": 1}, alpha_delay),
        ),
        ctx.Process(
            target=_update_worker,
            args=(str(journal), barrier, errors, "beta", {"b": 2}, beta_delay),
        ),
    ]

    _join_processes(processes, errors)

    state = json.loads(current_path.read_text(encoding="utf-8"))
    assert state["seed"] == {"k": "v"}
    assert state["alpha"] == {"a": 1}
    assert state["beta"] == {"b": 2}


def test_awareness_update_state_locked_merge_survives_alpha_then_beta(
    tmp_path: Path,
) -> None:
    _run_section_case(tmp_path, "alpha_first", alpha_delay=0.0, beta_delay=0.2)


def test_awareness_update_state_locked_merge_survives_beta_then_alpha(
    tmp_path: Path,
) -> None:
    _run_section_case(tmp_path, "beta_first", alpha_delay=0.2, beta_delay=0.0)


def test_awareness_update_state_locked_merge_survives_simultaneous(
    tmp_path: Path,
) -> None:
    _run_section_case(tmp_path, "simultaneous", alpha_delay=0.0, beta_delay=0.0)


def test_awareness_record_import_locked_increment(tmp_path: Path) -> None:
    ctx = multiprocessing.get_context("spawn")
    journal = tmp_path / "imports"
    current_path = _seed_current(journal, {})
    barrier = ctx.Barrier(2)
    errors = ctx.Queue()
    processes = [
        ctx.Process(target=_record_import_worker, args=(str(journal), barrier, errors)),
        ctx.Process(target=_record_import_worker, args=(str(journal), barrier, errors)),
    ]

    _join_processes(processes, errors)

    state = json.loads(current_path.read_text(encoding="utf-8"))
    imports = state["imports"]
    assert imports["import_count"] == 2
    assert imports["sources_used"] == ["src"]
