# SPDX-License-Identifier: AGPL-3.0-only
# Copyright (c) 2026 sol pbc

"""Tests for transcripts owner-facing copy discipline."""

from __future__ import annotations

import re
from pathlib import Path

from solstone.apps.transcripts.copy import (
    SPEAKER_LABEL_SOURCE_AMBIGUOUS_MESSAGE,
    SPEAKER_LABELS_UNAVAILABLE_MESSAGE,
    transcripts_copy_payload,
    transcripts_copy_values,
)


def test_no_literal_copy_in_templates():
    """Templates reference copy constants; Python protocol keys are out of scope."""

    root = Path("solstone/apps/transcripts")

    hits: list[tuple[Path, str]] = []
    for path in root.rglob("*.html"):
        text = path.read_text(encoding="utf-8")
        for value in transcripts_copy_values():
            literal_patterns = (
                re.compile(rf">\s*{re.escape(value)}\s*<"),
                re.compile(rf"(?<!=)['\"`]{re.escape(value)}['\"`]"),
            )
            if any(pattern.search(text) for pattern in literal_patterns):
                hits.append((path, value))

    assert hits == []


def test_copy_payload_reflects_tr_constants_only():
    payload = transcripts_copy_payload()

    assert payload["TR_SPEAKER_HEDGE_PROBABLE"] == "probably {name}"
    assert payload["TR_SPEAKER_HEDGE_MAYBE"] == "maybe {name}?"
    assert payload["TR_SPEAKER_UNKNOWN_CHIP"] == "unknown voice"
    assert payload["TR_SPEAKER_PROPAGATION_OFFER"] == (
        "{count} more statements may need this change"
    )
    assert payload["TR_SPEAKER_ALREADY_CORRECT"] == "already set"
    assert SPEAKER_LABELS_UNAVAILABLE_MESSAGE not in payload.values()
    assert SPEAKER_LABEL_SOURCE_AMBIGUOUS_MESSAGE not in payload.values()
    assert all(name.startswith("TR_") for name in payload)


def test_all_copy_constants_referenced_by_render_surface():
    html = "\n".join(
        path.read_text(encoding="utf-8")
        for path in Path("solstone/apps/transcripts").rglob("*.html")
    )

    missing = [name for name in transcripts_copy_payload() if name not in html]

    assert missing == []


def test_copy_values_flatten_payload_values():
    payload = transcripts_copy_payload()

    assert sorted(transcripts_copy_values()) == sorted(
        value for value in payload.values() if isinstance(value, str)
    )


def test_copy_avoids_surveillance_verbs():
    banned = re.compile(r"\b(capture|watch|record|monitor|track|collect)\b", re.I)

    assert [value for value in transcripts_copy_values() if banned.search(value)] == []
