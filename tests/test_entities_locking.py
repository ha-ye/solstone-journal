# SPDX-License-Identifier: AGPL-3.0-only
# Copyright (c) 2026 sol pbc

from __future__ import annotations

import multiprocessing
import os
import traceback
from pathlib import Path
from queue import Empty
from typing import Any


def _observation_worker(
    journal_path: str,
    barrier: Any,
    errors: Any,
    index: int,
) -> None:
    os.environ["SOLSTONE_JOURNAL"] = journal_path
    try:
        from solstone.think.entities.observations import add_observation

        barrier.wait(timeout=5)
        add_observation("work", "Alice", f"obs-{index}", source_day="20250101")
    except BaseException:
        errors.put(traceback.format_exc())
        raise


def _detected_entity_worker(
    journal_path: str,
    barrier: Any,
    errors: Any,
    index: int,
) -> None:
    os.environ["SOLSTONE_JOURNAL"] = journal_path
    try:
        from solstone.think.entities.saving import save_detected_entity

        barrier.wait(timeout=5)
        save_detected_entity("work", "20250101", "Person", f"E{index}", "d")
    except BaseException:
        errors.put(traceback.format_exc())
        raise


def _candidate_worker(
    journal_path: str,
    barrier: Any,
    errors: Any,
    index: int,
) -> None:
    os.environ["SOLSTONE_JOURNAL"] = journal_path
    try:
        from solstone.think.entities.review_candidates import locked_modify_candidates

        def mutate(rows: list[dict[str, Any]]) -> list[dict[str, Any]]:
            next_rows = list(rows)
            next_rows.append(
                {
                    "facet": "work",
                    "source_slug": f"s{index}",
                    "target_slug": "target",
                }
            )
            return next_rows

        barrier.wait(timeout=5)
        locked_modify_candidates(mutate)
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


def _run_workers(tmp_path: Path, monkeypatch: Any, name: str, target: Any) -> Path:
    ctx = multiprocessing.get_context("spawn")
    journal = tmp_path / name
    monkeypatch.setenv("SOLSTONE_JOURNAL", str(journal))
    barrier = ctx.Barrier(4)
    errors = ctx.Queue()
    processes = [
        ctx.Process(target=target, args=(str(journal), barrier, errors, i))
        for i in range(4)
    ]

    _join_processes(processes, errors)
    return journal


def test_add_observation_serializes_process_writers(
    tmp_path: Path,
    monkeypatch: Any,
) -> None:
    _run_workers(tmp_path, monkeypatch, "observations", _observation_worker)

    from solstone.think.entities.observations import load_observations

    observations = load_observations("work", "Alice")

    assert sorted(obs["content"] for obs in observations) == [
        "obs-0",
        "obs-1",
        "obs-2",
        "obs-3",
    ]


def test_save_detected_entity_serializes_process_writers(
    tmp_path: Path,
    monkeypatch: Any,
) -> None:
    _run_workers(tmp_path, monkeypatch, "detected", _detected_entity_worker)

    from solstone.think.entities.loading import load_entities

    entities = load_entities("work", "20250101")

    assert sorted(entity["name"] for entity in entities) == ["E0", "E1", "E2", "E3"]


def test_locked_modify_candidates_serializes_process_writers(
    tmp_path: Path,
    monkeypatch: Any,
) -> None:
    _run_workers(tmp_path, monkeypatch, "candidates", _candidate_worker)

    from solstone.think.entities.review_candidates import load_candidates

    rows = load_candidates()

    assert sorted(row["source_slug"] for row in rows) == ["s0", "s1", "s2", "s3"]
