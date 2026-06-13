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
    "STT_AUTO_SWITCH_NOTICE",
    "STT_FORCE_LOCAL_HINT",
    "STT_NO_KEY_RECOVERY",
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
    for name in NEW_STT_COPY:
        assert name in install_copy.__all__
        assert getattr(install_copy, name)

    assert "{ram_gb}" in install_copy.STT_LOCAL_REQUIREMENTS_TEMPLATE
    assert "{available_gb}" in install_copy.STT_DETECTED_MEMORY_TEMPLATE
    assert "{ram_gb}" in install_copy.STT_EXPLICIT_LOCAL_LOW_TEMPLATE


def test_new_stt_install_copy_avoids_banned_owner_terms() -> None:
    combined = "\n".join(getattr(install_copy, name) for name in NEW_STT_COPY)

    for term in BANNED_OWNER_TERMS:
        assert re.search(rf"\b{term}\b", combined, re.IGNORECASE) is None
