# SPDX-License-Identifier: AGPL-3.0-only
# Copyright (c) 2026 sol pbc

from __future__ import annotations

import re

from solstone.apps.thinking import install_copy

LOCAL_COPY = (
    "LOCAL_REQUIREMENTS_TEMPLATE",
    "LOCAL_DETECTED_MEMORY_TEMPLATE",
    "LOCAL_DETECTED_MEMORY_UNKNOWN",
    "LOCAL_PATHS_FRAMING",
    "LOCAL_EXPERIMENTAL_NOTE",
    "LOCAL_RECOVERY_NO_HOSTED_KEY",
    "LOCAL_RECOVERY_HOSTED_KEY_SET",
    "LOCAL_MEMORY_WARNING_LOW_TEMPLATE",
    "LOCAL_MEMORY_WARNING_UNKNOWN",
    "LOCAL_MLX_MEMORY_WARNING_UNKNOWN",
)
BANNED_OWNER_TERMS = (
    "capture",
    "watch",
    "record",
    "monitor",
    "track",
    "collect",
)


def test_local_install_copy_is_exported_and_populated() -> None:
    for name in LOCAL_COPY:
        assert name in install_copy.__all__
        assert getattr(install_copy, name)

    assert "{ram_gb}" in install_copy.LOCAL_REQUIREMENTS_TEMPLATE
    assert "{download_size}" in install_copy.LOCAL_REQUIREMENTS_TEMPLATE
    assert "{available_gb}" in install_copy.LOCAL_DETECTED_MEMORY_TEMPLATE
    assert "{ram_gb}" in install_copy.LOCAL_MEMORY_WARNING_LOW_TEMPLATE


def test_local_install_copy_avoids_banned_owner_terms() -> None:
    combined = "\n".join(getattr(install_copy, name) for name in LOCAL_COPY)

    for term in BANNED_OWNER_TERMS:
        assert re.search(rf"\b{term}\b", combined, re.IGNORECASE) is None
