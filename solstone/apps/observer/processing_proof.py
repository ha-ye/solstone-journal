# SPDX-License-Identifier: AGPL-3.0-only
# Copyright (c) 2026 sol pbc

"""Read-only proof that observer raw media was terminally processed."""

from __future__ import annotations

import json
from pathlib import Path

from solstone.observe.processing_record import (
    HANDLER_DESCRIBE,
    HANDLER_TRANSCRIBE,
    SCHEMA,
    STATE_ANALYZED,
    STATE_EMPTY,
)
from solstone.think.media import AUDIO_EXTENSIONS, VIDEO_EXTENSIONS

MAX_FIRST_ROW_BYTES = 64 * 1024
TERMINAL_STATES = frozenset({STATE_ANALYZED, STATE_EMPTY})


def has_terminal_processing_proof(recorded_path: Path, recorded_size: object) -> bool:
    """Return True when a same-stem sidecar proves raw media was consumed."""
    suffix = recorded_path.suffix.lower()
    if suffix in AUDIO_EXTENSIONS:
        expected_handler = HANDLER_TRANSCRIBE
    elif suffix in VIDEO_EXTENSIONS:
        expected_handler = HANDLER_DESCRIBE
    else:
        return False

    if isinstance(recorded_size, bool) or not isinstance(recorded_size, int):
        return False

    sidecar_path = recorded_path.with_suffix(".jsonl")
    if not sidecar_path.is_file():
        return False

    try:
        with sidecar_path.open("rb") as sidecar_file:
            first_window = sidecar_file.read(MAX_FIRST_ROW_BYTES)
    except OSError:
        return False

    if b"\n" not in first_window:
        return False

    first_line = first_window.split(b"\n", 1)[0]
    try:
        row = json.loads(first_line.decode("utf-8"))
    except UnicodeDecodeError:
        return False
    except json.JSONDecodeError:
        return False

    if not isinstance(row, dict):
        return False

    record = row.get("_solstone_processing")
    if not isinstance(record, dict):
        return False
    if record.get("schema") != SCHEMA:
        return False
    if record.get("state") not in TERMINAL_STATES:
        return False
    if record.get("handler") != expected_handler:
        return False

    input_size = record.get("input_size")
    return (
        not isinstance(input_size, bool)
        and isinstance(input_size, int)
        and input_size == recorded_size
    )
