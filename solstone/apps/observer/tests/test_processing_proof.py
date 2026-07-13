# SPDX-License-Identifier: AGPL-3.0-only
# Copyright (c) 2026 sol pbc

"""Tests for observer processing-proof validation."""

from __future__ import annotations

import json
from pathlib import Path

from solstone.apps.observer.processing_proof import has_terminal_processing_proof
from solstone.observe.processing_record import (
    HANDLER_TRANSCRIBE,
    SCHEMA,
    STATE_ANALYZED,
    STATE_EMPTY,
)


def _processing_record(
    *,
    input_size: object = 17,
    state: str = STATE_EMPTY,
) -> dict:
    return {
        "schema": SCHEMA,
        "state": state,
        "handler": HANDLER_TRANSCRIBE,
        "input_size": input_size,
    }


def _processing_row(
    *,
    input_size: object = 17,
    state: str = STATE_EMPTY,
) -> dict:
    return {
        "_solstone_processing": _processing_record(input_size=input_size, state=state)
    }


def _write_sidecar(
    recorded_path: Path,
    *,
    input_size: object = 17,
    state: str = STATE_EMPTY,
) -> None:
    recorded_path.with_suffix(".jsonl").write_text(
        json.dumps(_processing_row(input_size=input_size, state=state)) + "\n",
        encoding="utf-8",
    )


def test_jsonl_recorded_path_cannot_self_reference_as_processing_proof(
    tmp_path,
):
    recorded_path = tmp_path / "audio.jsonl"
    recorded_path.write_text(json.dumps(_processing_row()) + "\n", encoding="utf-8")

    assert not has_terminal_processing_proof(recorded_path, 17)


def test_image_recorded_path_cannot_have_processing_proof(tmp_path):
    recorded_path = tmp_path / "screen.png"
    _write_sidecar(recorded_path)

    assert not has_terminal_processing_proof(recorded_path, 17)


def test_bool_recorded_size_is_not_processing_proof(tmp_path):
    recorded_path = tmp_path / "audio.flac"
    _write_sidecar(recorded_path)

    assert not has_terminal_processing_proof(recorded_path, True)


def test_string_recorded_size_is_not_processing_proof(tmp_path):
    recorded_path = tmp_path / "audio.flac"
    _write_sidecar(recorded_path)

    assert not has_terminal_processing_proof(recorded_path, "17")


def test_bool_input_size_is_not_processing_proof(tmp_path):
    recorded_path = tmp_path / "audio.flac"
    _write_sidecar(recorded_path, input_size=True)

    assert not has_terminal_processing_proof(recorded_path, 1)


def test_analyzed_terminal_state_is_processing_proof(tmp_path):
    recorded_path = tmp_path / "audio.flac"
    _write_sidecar(recorded_path, state=STATE_ANALYZED)

    assert has_terminal_processing_proof(recorded_path, 17)


def test_non_dict_first_row_is_not_processing_proof(tmp_path):
    recorded_path = tmp_path / "audio.flac"
    recorded_path.with_suffix(".jsonl").write_text("[]\n", encoding="utf-8")

    assert not has_terminal_processing_proof(recorded_path, 17)


def test_non_dict_processing_record_is_not_processing_proof(tmp_path):
    recorded_path = tmp_path / "audio.flac"
    recorded_path.with_suffix(".jsonl").write_text(
        json.dumps({"_solstone_processing": "not a record"}) + "\n",
        encoding="utf-8",
    )

    assert not has_terminal_processing_proof(recorded_path, 17)


def test_first_window_without_newline_is_not_processing_proof(tmp_path):
    recorded_path = tmp_path / "audio.flac"
    recorded_path.with_suffix(".jsonl").write_text(
        json.dumps(_processing_row()),
        encoding="utf-8",
    )

    assert not has_terminal_processing_proof(recorded_path, 17)


def test_non_utf8_first_line_is_not_processing_proof(tmp_path):
    recorded_path = tmp_path / "audio.flac"
    recorded_path.with_suffix(".jsonl").write_bytes(b"\xff\xfe\x00\n")

    assert not has_terminal_processing_proof(recorded_path, 17)
