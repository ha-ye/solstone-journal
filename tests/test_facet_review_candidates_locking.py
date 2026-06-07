# SPDX-License-Identifier: AGPL-3.0-only
# Copyright (c) 2026 sol pbc

from __future__ import annotations

import json
import multiprocessing
import os
import traceback
from pathlib import Path
from queue import Empty
from typing import Any

import pytest

from solstone.think.facet_review_candidates import (
    facet_review_candidates_path,
    load_candidates,
    save_candidates,
)


def _record_candidate_worker(
    journal_path: str,
    barrier: Any,
    errors: Any,
    index: int,
) -> None:
    os.environ["SOLSTONE_JOURNAL"] = journal_path
    try:
        barrier.wait(timeout=5)

        from solstone.think.facet_review_candidates import record_facet_candidate

        record_facet_candidate(
            name=f"Candidate {index}",
            name_key=f"candidate {index}",
            count=index + 1,
            window_days=14,
            samples=[
                {
                    "day": "20260602",
                    "stream": "archon",
                    "segment": f"09000{index}_300",
                }
            ],
            day="20260602",
        )
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


def test_facet_review_candidate_atomic_failure_preserves_existing_bytes(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("SOLSTONE_JOURNAL", str(tmp_path))
    path = facet_review_candidates_path()
    seed = {"name": "Café", "name_key": "café", "status": "open"}
    original = json.dumps(seed, ensure_ascii=False) + "\n"
    path.write_text(original, encoding="utf-8")

    def fail_replace(_src: str, _dst: str) -> None:
        raise OSError("simulated replace failure")

    monkeypatch.setattr("solstone.think.journal_io.atomic.os.replace", fail_replace)

    with pytest.raises(OSError):
        save_candidates([{"name": "Changed", "name_key": "changed"}])

    assert path.read_text(encoding="utf-8") == original
    assert list(path.parent.glob(".tmp_*")) == []


def test_facet_review_candidate_locked_modify_survives_multiprocess_writers(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    ctx = multiprocessing.get_context("spawn")
    journal = tmp_path / "journal"
    barrier = ctx.Barrier(4)
    errors = ctx.Queue()
    processes = [
        ctx.Process(
            target=_record_candidate_worker,
            args=(str(journal), barrier, errors, index),
        )
        for index in range(4)
    ]

    _join_processes(processes, errors)

    monkeypatch.setenv("SOLSTONE_JOURNAL", str(journal))
    rows = load_candidates()
    assert sorted(row["name_key"] for row in rows) == [
        "candidate 0",
        "candidate 1",
        "candidate 2",
        "candidate 3",
    ]
