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

DAY = "20240101"
STREAM = "test"
SEGMENT = "143022_300"
SOURCE_ID = "merge_source"
TARGET_ID = "merge_target"


def _segment_dir(journal: Path) -> Path:
    return journal / "chronicle" / DAY / STREAM / SEGMENT


def _merge_rewrite_worker(
    journal_path: str,
    barrier: Any,
    append_done: Any,
    errors: Any,
) -> None:
    os.environ["SOLSTONE_JOURNAL"] = journal_path
    try:
        from solstone.think.entities import merge as merge_mod

        plan = merge_mod._plan_segment_rewrites(SOURCE_ID, TARGET_ID)
        barrier.wait(timeout=5)
        if not append_done.wait(timeout=5):
            raise TimeoutError("append worker did not finish")
        time.sleep(0.05)
        merge_mod._apply_segment_plan(plan["operations"], SOURCE_ID, TARGET_ID)
    except BaseException:
        errors.put(traceback.format_exc())
        raise


def _append_worker(
    journal_path: str,
    seg_dir_path: str,
    barrier: Any,
    append_done: Any,
    errors: Any,
) -> None:
    os.environ["SOLSTONE_JOURNAL"] = journal_path
    try:
        from solstone.apps.speakers.attribution import append_speaker_correction

        barrier.wait(timeout=5)
        append_speaker_correction(
            Path(seg_dir_path),
            {
                "sentence_id": 2,
                "original_speaker": "other_source",
                "corrected_speaker": "other_target",
                "original_method": "user_corrected",
                "timestamp": 1,
            },
        )
        append_done.set()
    except BaseException:
        errors.put(traceback.format_exc())
        append_done.set()
        raise


def _drain_errors(errors: Any) -> list[str]:
    found = []
    while True:
        try:
            found.append(errors.get_nowait())
        except Empty:
            return found


def test_entity_merge_corrections_rewrite_preserves_concurrent_append(
    tmp_path: Path,
) -> None:
    ctx = multiprocessing.get_context("spawn")
    journal = tmp_path / "journal"
    seg_dir = _segment_dir(journal)
    talents_dir = seg_dir / "talents"
    talents_dir.mkdir(parents=True)
    corrections_path = talents_dir / "speaker_corrections.json"
    corrections_path.write_text(
        json.dumps(
            {
                "corrections": [
                    {
                        "sentence_id": 1,
                        "original_speaker": SOURCE_ID,
                        "corrected_speaker": SOURCE_ID,
                        "original_method": "acoustic",
                        "timestamp": 0,
                    }
                ]
            },
            indent=2,
        ),
        encoding="utf-8",
    )

    barrier = ctx.Barrier(2)
    append_done = ctx.Event()
    errors = ctx.Queue()
    processes = [
        ctx.Process(
            target=_merge_rewrite_worker,
            args=(str(journal), barrier, append_done, errors),
        ),
        ctx.Process(
            target=_append_worker,
            args=(str(journal), str(seg_dir), barrier, append_done, errors),
        ),
    ]

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

    data = json.loads(corrections_path.read_text(encoding="utf-8"))
    by_sentence = {entry["sentence_id"]: entry for entry in data["corrections"]}
    assert by_sentence[1]["original_speaker"] == TARGET_ID
    assert by_sentence[1]["corrected_speaker"] == TARGET_ID
    assert by_sentence[2]["original_speaker"] == "other_source"
    assert by_sentence[2]["corrected_speaker"] == "other_target"
