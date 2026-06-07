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

DAY = "20240101"
STREAM = "test"
SEGMENT = "143022_300"


def _correction_worker(
    journal_path: str,
    seg_dir_path: str,
    barrier: Any,
    errors: Any,
    sentence_id: int,
) -> None:
    os.environ["SOLSTONE_JOURNAL"] = journal_path
    try:
        from solstone.apps.speakers.attribution import append_speaker_correction

        barrier.wait(timeout=5)
        append_speaker_correction(
            Path(seg_dir_path),
            {
                "sentence_id": sentence_id,
                "original_speaker": f"speaker_{sentence_id}",
                "corrected_speaker": f"corrected_{sentence_id}",
                "original_method": "acoustic",
                "timestamp": 0,
            },
        )
    except BaseException:
        errors.put(traceback.format_exc())
        raise


def _fresh_segment(journal: Path) -> Path:
    return journal / "chronicle" / DAY / STREAM / SEGMENT


def _drain_errors(errors: Any) -> list[str]:
    found = []
    while True:
        try:
            found.append(errors.get_nowait())
        except Empty:
            return found


def test_speaker_corrections_locked_appends_survive_concurrent_writers(
    tmp_path: Path,
) -> None:
    ctx = multiprocessing.get_context("spawn")
    journal = tmp_path / "journal"
    seg_dir = _fresh_segment(journal)
    sentence_ids = tuple(range(1, 7))
    barrier = ctx.Barrier(len(sentence_ids))
    errors = ctx.Queue()

    processes = [
        ctx.Process(
            target=_correction_worker,
            args=(str(journal), str(seg_dir), barrier, errors, sentence_id),
        )
        for sentence_id in sentence_ids
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

    corrections_path = seg_dir / "talents" / "speaker_corrections.json"
    data = json.loads(corrections_path.read_text(encoding="utf-8"))
    actual_sentence_ids = {entry["sentence_id"] for entry in data["corrections"]}
    assert actual_sentence_ids == set(sentence_ids)


def test_speaker_corrections_preserve_legacy_byte_shape(tmp_path: Path) -> None:
    from solstone.apps.speakers.attribution import append_speaker_correction

    seg_dir = tmp_path / "segment"
    corrections_path = seg_dir / "talents" / "speaker_corrections.json"
    first = {
        "sentence_id": 1,
        "original_speaker": "alice",
        "corrected_speaker": "bob",
        "original_method": "acoustic",
        "timestamp": 0,
    }
    second = {
        "sentence_id": 2,
        "original_speaker": "carol",
        "corrected_speaker": "dave",
        "original_method": "structural",
        "timestamp": 1,
    }

    append_speaker_correction(seg_dir, first)
    assert corrections_path.read_bytes() == json.dumps(
        {"corrections": [first]}, indent=2
    ).encode("utf-8")

    append_speaker_correction(seg_dir, second)
    assert corrections_path.read_bytes() == json.dumps(
        {"corrections": [first, second]}, indent=2
    ).encode("utf-8")
