# SPDX-License-Identifier: AGPL-3.0-only
# Copyright (c) 2026 sol pbc

from __future__ import annotations

import re

from solstone.think.services import outcomes, status

BLOCKED_COPY_RE = re.compile(
    r"sign(?:ed)?\s+in|signing\s+in|log(?:ged)?\s+in|your\s+account|"
    r"account\s+settings|linked|authenticate",
    re.IGNORECASE,
)


def test_handoff_guidance_avoids_blocked_brand_terms() -> None:
    strings = [
        *(value or "" for value in outcomes.GUIDANCE.values()),
        status.SCOUT_MANUAL_KEY_GUIDANCE,
        status.SCOUT_PENDING_GUIDANCE,
        status.SCOUT_DISABLED_GUIDANCE,
        status.SPL_NOT_ENABLED_GUIDANCE,
        status.SPL_INCONSISTENT_GUIDANCE,
    ]

    assert all(not BLOCKED_COPY_RE.search(value) for value in strings)
    assert all("sol private link" not in value.lower() for value in strings)
