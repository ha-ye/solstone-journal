# SPDX-License-Identifier: AGPL-3.0-only
# Copyright (c) 2026 sol pbc

"""Owner-facing copy constants for the transcripts app."""

from __future__ import annotations

from typing import Any

TR_SPEAKER_CHANGE_LABEL = "change speaker"
TR_SPEAKER_ASSIGN_LABEL = "add speaker"
TR_SPEAKER_PICKER_TITLE = "choose speaker"
TR_SPEAKER_PICKER_SEARCH_PLACEHOLDER = "find a person"
TR_SPEAKER_PICKER_OWNER = "this is me"
TR_SPEAKER_PICKER_EMPTY = "no known voices yet"
TR_SPEAKER_SOMEONE_ELSE = "someone else…"
TR_SPEAKER_PICKER_NO_RESULTS = "no matching people"
TR_SPEAKER_UNKNOWN_CHIP = "unknown voice"
TR_SPEAKER_HEDGE_PROBABLE = "probably {name}"
TR_SPEAKER_HEDGE_MAYBE = "maybe {name}?"
TR_SPEAKER_CONFIDENCE_HIGH = "high confidence"
TR_SPEAKER_CONFIDENCE_UNKNOWN = "confidence unavailable"
TR_SPEAKER_MARGIN_OWNER = "close owner match"
TR_SPEAKER_MARGIN_ACOUSTIC = "close voice match"
TR_SPEAKER_ACTION_UNAVAILABLE = "speaker change unavailable"
TR_SPEAKER_NO_EMBEDDING = "voice sample unavailable"
TR_SPEAKER_CORRECT_RETRY = "retry speaker change"
TR_SPEAKER_CORRECT_BUSY = "speaker files are busy"
TR_SPEAKER_OWNER_TOO_CLOSE = "that voice is too close to yours to save there"
TR_SPEAKER_OWNER_IDENTITY_REQUIRED = "set your identity before tagging yourself"
TR_SPEAKER_ALREADY_CORRECT = "already set"
TR_SPEAKER_PROPAGATION_OFFER = "{count} more statements may need this change"
TR_SPEAKER_PROPAGATION_APPLY = "apply changes"
TR_SPEAKER_PROPAGATION_DISMISS = "dismiss"
TR_SPEAKER_PROPAGATION_APPLIED = "changes applied"

# API/read-path prose; keep un-prefixed so transcripts_copy_payload() does not
# expose it to templates.
SPEAKER_LABELS_UNAVAILABLE_MESSAGE = "speaker labels are unavailable for this segment"
SPEAKER_LABEL_SOURCE_AMBIGUOUS_MESSAGE = (
    "speaker label source is ambiguous for this segment"
)


def transcripts_copy_payload() -> dict[str, Any]:
    """Return copy constants for templates and browser code."""
    return {
        name: value
        for name, value in globals().items()
        if name.startswith("TR_") and name.isupper()
    }


def transcripts_copy_values() -> list[str]:
    """Return all verbatim copy values, flattening list constants."""
    values: list[str] = []
    for value in transcripts_copy_payload().values():
        if isinstance(value, str):
            values.append(value)
        elif isinstance(value, list):
            values.extend(item for item in value if isinstance(item, str))
    return values
