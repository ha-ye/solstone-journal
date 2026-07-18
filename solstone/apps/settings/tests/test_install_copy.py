# SPDX-License-Identifier: AGPL-3.0-only
# Copyright (c) 2026 sol pbc

from __future__ import annotations

import re

from solstone.apps.settings import install_copy

NEW_STT_COPY = (
    "STT_LOCAL_REQUIREMENTS_TEMPLATE",
    "STT_LOCAL_UNSUPPORTED",
    "STT_DETECTED_MEMORY_TEMPLATE",
    "STT_DETECTED_MEMORY_UNKNOWN",
    "STT_NO_LOCAL_STT_RECOVERY",
    "STT_EXPLICIT_LOCAL_LOW_TEMPLATE",
)
BANNED_OWNER_TERMS = (
    "capture",
    "watch",
    "record",
    "monitor",
    "track",
    "collect",
)


def test_new_stt_install_copy_is_exported_and_populated() -> None:
    expected_values = {
        "STT_LOCAL_REQUIREMENTS_TEMPLATE": (
            "local transcription needs about {ram_gb} GB of free memory for the "
            "on-device model (transcription, speaker labels, and overlap detection)."
        ),
        "STT_LOCAL_UNSUPPORTED": (
            "local transcription is not available on this platform."
        ),
        "STT_DETECTED_MEMORY_TEMPLATE": (
            "{available_gb} GB of free memory detected on this machine."
        ),
        "STT_DETECTED_MEMORY_UNKNOWN": (
            "free memory on this machine could not be detected."
        ),
        "STT_NO_LOCAL_STT_RECOVERY": (
            "free up memory on this machine or use a supported platform to transcribe "
            "locally. with confidential processing enabled, transcription runs on the "
            "service instead."
        ),
        "STT_EXPLICIT_LOCAL_LOW_TEMPLATE": (
            "free memory is below {ram_gb} GB. local transcription can still run, but "
            "this machine may be slow or unstable while it does."
        ),
    }

    for name in NEW_STT_COPY:
        assert name in install_copy.__all__
        assert getattr(install_copy, name) == expected_values[name]

    assert "{ram_gb}" in install_copy.STT_LOCAL_REQUIREMENTS_TEMPLATE
    assert "{available_gb}" in install_copy.STT_DETECTED_MEMORY_TEMPLATE
    assert "{ram_gb}" in install_copy.STT_EXPLICIT_LOCAL_LOW_TEMPLATE


def test_new_stt_install_copy_avoids_banned_owner_terms() -> None:
    combined = "\n".join(getattr(install_copy, name) for name in NEW_STT_COPY)

    for term in BANNED_OWNER_TERMS:
        assert re.search(rf"\b{term}\b", combined, re.IGNORECASE) is None
