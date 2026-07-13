# SPDX-License-Identifier: AGPL-3.0-only
# Copyright (c) 2026 sol pbc

"""Transcription configuration helpers."""

from __future__ import annotations

import logging
from collections.abc import Mapping
from typing import Any

logger = logging.getLogger(__name__)


def confidential_audio_enabled(
    transcribe_config: Mapping[str, Any] | None = None,
) -> bool:
    """Return the effective confidential-audio setting.

    Missing config means enabled. Invalid persisted values fail closed toward local
    audio processing.
    """
    if transcribe_config is None:
        from solstone.think.utils import get_config

        raw_config = get_config().get("transcribe", {})
        transcribe_config = raw_config if isinstance(raw_config, Mapping) else {}

    if "confidential_audio" not in transcribe_config:
        return True

    value = transcribe_config.get("confidential_audio")
    if isinstance(value, bool):
        return value

    logger.warning(
        "Invalid transcribe.confidential_audio value %r; treating as disabled",
        value,
    )
    return False


__all__ = ["confidential_audio_enabled"]
