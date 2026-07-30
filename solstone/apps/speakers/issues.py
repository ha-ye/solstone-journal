# SPDX-License-Identifier: AGPL-3.0-only
# Copyright (c) 2026 sol pbc

"""Speaker scan and suggestion issue payload helpers."""

from __future__ import annotations

from typing import Any

from solstone.convey.reasons import SPEAKER_DISCOVERY_FAILED

SPEAKER_DISCOVERY_INVALID_EMBEDDINGS = "speaker_discovery_invalid_embeddings"
SPEAKER_DISCOVERY_INVALID_EMBEDDINGS_MESSAGE = (
    "i skipped some voice samples because they were not usable."
)
SPEAKER_DISCOVERY_OWNER_VOICE_UNAVAILABLE = (
    "speaker_discovery_owner_voice_unavailable"
)
SPEAKER_DISCOVERY_OWNER_VOICE_UNAVAILABLE_MESSAGE = (
    "i need your voice set up before looking for new voices."
)
SPEAKER_SUGGESTION_GENERATOR_FAILED = "speaker_suggestion_generator_failed"
SPEAKER_SUGGESTION_GENERATOR_FAILED_MESSAGE = (
    "i couldn't finish part of the speaker suggestions."
)


def scan_issue(reason_code: str, message: str, *, count: int) -> dict[str, Any]:
    """Build the fixed scan issue shape."""
    return {"reason_code": reason_code, "message": message, "count": int(count)}


def invalid_embeddings_issue(count: int) -> dict[str, Any]:
    return scan_issue(
        SPEAKER_DISCOVERY_INVALID_EMBEDDINGS,
        SPEAKER_DISCOVERY_INVALID_EMBEDDINGS_MESSAGE,
        count=count,
    )


def owner_voice_unavailable_issue() -> dict[str, Any]:
    return scan_issue(
        SPEAKER_DISCOVERY_OWNER_VOICE_UNAVAILABLE,
        SPEAKER_DISCOVERY_OWNER_VOICE_UNAVAILABLE_MESSAGE,
        count=0,
    )


def suggestion_issue(
    reason_code: str,
    message: str,
    *,
    generator: str,
) -> dict[str, str]:
    """Build the fixed suggestion issue shape."""
    return {"reason_code": reason_code, "generator": generator, "message": message}


def speaker_discovery_failed_suggestion_issue(*, generator: str) -> dict[str, str]:
    return suggestion_issue(
        SPEAKER_DISCOVERY_FAILED.code,
        SPEAKER_DISCOVERY_FAILED.message,
        generator=generator,
    )


def speaker_suggestion_generator_failed_issue(*, generator: str) -> dict[str, str]:
    return suggestion_issue(
        SPEAKER_SUGGESTION_GENERATOR_FAILED,
        SPEAKER_SUGGESTION_GENERATOR_FAILED_MESSAGE,
        generator=generator,
    )
