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


def _pipeline_worker(
    journal_path: str,
    seg_dir_path: str,
    barrier: Any,
    errors: Any,
    delay: float,
) -> None:
    os.environ["SOLSTONE_JOURNAL"] = journal_path
    try:
        barrier.wait(timeout=5)
        if delay:
            time.sleep(delay)

        from solstone.apps.speakers.attribution import save_speaker_labels

        save_speaker_labels(
            Path(seg_dir_path),
            [
                {
                    "sentence_id": 1,
                    "speaker": "X",
                    "confidence": "high",
                    "method": "acoustic",
                },
                {
                    "sentence_id": 2,
                    "speaker": "pipeline_overwrite",
                    "confidence": "high",
                    "method": "acoustic",
                },
            ],
            {
                "owner_centroid_last_refreshed_at": "2026-03-15T12:00:00Z",
                "voiceprint_versions": {"X": 1},
            },
        )
    except BaseException:
        errors.put(traceback.format_exc())
        raise


def _patch_worker(
    journal_path: str,
    seg_dir_path: str,
    barrier: Any,
    errors: Any,
    delay: float,
) -> None:
    os.environ["SOLSTONE_JOURNAL"] = journal_path
    try:
        barrier.wait(timeout=5)
        if delay:
            time.sleep(delay)

        from solstone.apps.speakers.attribution import apply_label_patches

        apply_label_patches(
            Path(seg_dir_path),
            {
                2: {
                    "speaker": "Y",
                    "confidence": "high",
                    "method": "user_corrected",
                }
            },
            allow_insert=False,
        )
    except BaseException:
        errors.put(traceback.format_exc())
        raise


def _seed_labels(journal: Path) -> Path:
    seg_dir = journal / "chronicle" / DAY / STREAM / SEGMENT
    talents_dir = seg_dir / "talents"
    talents_dir.mkdir(parents=True)
    (talents_dir / "speaker_labels.json").write_text(
        json.dumps(
            {
                "labels": [
                    {
                        "sentence_id": 1,
                        "speaker": "old_one",
                        "confidence": "high",
                        "method": "acoustic",
                    },
                    {
                        "sentence_id": 2,
                        "speaker": "old_two",
                        "confidence": "high",
                        "method": "acoustic",
                    },
                ],
                "owner_centroid_last_refreshed_at": None,
                "voiceprint_versions": {},
            }
        ),
        encoding="utf-8",
    )
    return seg_dir


def _drain_errors(errors: Any) -> list[str]:
    found = []
    while True:
        try:
            found.append(errors.get_nowait())
        except Empty:
            return found


def _run_case(
    tmp_path: Path,
    name: str,
    pipeline_delay: float,
    patch_delay: float,
) -> None:
    ctx = multiprocessing.get_context("spawn")
    journal = tmp_path / name
    seg_dir = _seed_labels(journal)
    barrier = ctx.Barrier(2)
    errors = ctx.Queue()

    processes = [
        ctx.Process(
            target=_pipeline_worker,
            args=(str(journal), str(seg_dir), barrier, errors, pipeline_delay),
        ),
        ctx.Process(
            target=_patch_worker,
            args=(str(journal), str(seg_dir), barrier, errors, patch_delay),
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

    labels_path = seg_dir / "talents" / "speaker_labels.json"
    data = json.loads(labels_path.read_text(encoding="utf-8"))
    by_sid = {int(label["sentence_id"]): label for label in data["labels"]}
    assert by_sid[1]["speaker"] == "X"
    assert by_sid[2]["speaker"] == "Y"
    assert by_sid[2]["method"] == "user_corrected"


def test_speaker_labels_locked_merge_survives_pipeline_then_user(
    tmp_path: Path,
) -> None:
    _run_case(tmp_path, "pipeline_first", pipeline_delay=0.0, patch_delay=0.2)


def test_speaker_labels_locked_merge_survives_user_then_pipeline(
    tmp_path: Path,
) -> None:
    _run_case(tmp_path, "user_first", pipeline_delay=0.2, patch_delay=0.0)
