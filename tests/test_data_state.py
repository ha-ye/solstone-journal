# SPDX-License-Identifier: AGPL-3.0-only
# Copyright (c) 2026 sol pbc

import os
import time

from solstone.observe.processing_record import STATE_EMPTY, STATE_FAILED
from solstone.think.data_state import (
    DataState,
    derive_modality_state,
    read_processing_record,
)


def test_read_processing_record_returns_header_record() -> None:
    record = {"state": STATE_EMPTY}

    assert read_processing_record([{"_solstone_processing": record}]) is record


def test_read_processing_record_rejects_missing_or_invalid_header() -> None:
    assert read_processing_record([]) is None
    assert read_processing_record(["header"]) is None
    assert read_processing_record([{"_solstone_processing": STATE_EMPTY}]) is None
    assert (
        read_processing_record([{"raw": "screen.webm"}, {"_solstone_processing": {}}])
        is None
    )


def test_derive_chunks_win_beats_processing_record(tmp_path) -> None:
    segment = tmp_path / "090000_300"
    segment.mkdir()

    state = derive_modality_state(
        segment,
        "screen",
        has_chunks=True,
        has_jsonl=True,
        has_raw=True,
        record={"state": STATE_EMPTY},
    )

    assert state == DataState.ANALYZED.value


def test_derive_empty_record_beats_failed_marker(tmp_path) -> None:
    segment = tmp_path / "090000_300"
    segment.mkdir()
    (segment / ".analyze_failed_screen").write_text("{}\n", encoding="utf-8")

    state = derive_modality_state(
        segment,
        "screen",
        has_chunks=False,
        has_jsonl=True,
        has_raw=True,
        record={"state": STATE_EMPTY},
    )

    assert state == DataState.EMPTY.value


def test_derive_empty_record_beats_stale_marker(tmp_path) -> None:
    segment = tmp_path / "090000_300"
    segment.mkdir()
    marker = segment / ".analyzing_screen"
    marker.write_text(
        '{"started_at": "2026-05-20T09:00:00Z", "modality": "screen"}\n',
        encoding="utf-8",
    )
    old_time = time.time() - 2000
    os.utime(marker, (old_time, old_time))

    state = derive_modality_state(
        segment,
        "screen",
        has_chunks=False,
        has_jsonl=True,
        has_raw=True,
        record={"state": STATE_EMPTY},
    )

    assert state == DataState.EMPTY.value
    assert marker.exists()
    assert not (segment / ".analyze_failed_screen").exists()


def test_derive_empty_record_beats_corrupt_marker(tmp_path) -> None:
    segment = tmp_path / "090000_300"
    segment.mkdir()
    marker = segment / ".analyzing_screen"
    marker.write_text("{not json", encoding="utf-8")

    state = derive_modality_state(
        segment,
        "screen",
        has_chunks=False,
        has_jsonl=True,
        has_raw=True,
        record={"state": STATE_EMPTY},
    )

    assert state == DataState.EMPTY.value
    assert marker.exists()
    assert not (segment / ".analyze_failed_screen").exists()


def test_derive_failed_record_maps_to_failed(tmp_path) -> None:
    segment = tmp_path / "090000_300"
    segment.mkdir()

    state = derive_modality_state(
        segment,
        "audio",
        has_chunks=False,
        has_jsonl=True,
        has_raw=True,
        record={"state": STATE_FAILED},
    )

    assert state == DataState.FAILED.value


def test_derive_ignores_missing_or_unknown_record_state(tmp_path) -> None:
    segment = tmp_path / "090000_300"
    segment.mkdir()

    for record in ({}, {"state": "surprise"}):
        assert (
            derive_modality_state(
                segment,
                "audio",
                has_chunks=False,
                has_jsonl=True,
                has_raw=False,
                record=record,
            )
            == DataState.PENDING.value
        )


def test_derive_no_record_preserves_legacy_result(tmp_path) -> None:
    segment = tmp_path / "090000_300"
    segment.mkdir()

    assert (
        derive_modality_state(
            segment,
            "audio",
            has_chunks=False,
            has_jsonl=True,
            has_raw=False,
        )
        == DataState.PENDING.value
    )
