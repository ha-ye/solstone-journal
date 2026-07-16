# SPDX-License-Identifier: AGPL-3.0-only
# Copyright (c) 2026 sol pbc

from __future__ import annotations

from collections.abc import Mapping
from typing import Any


def build_day_grid_payload(
    counts: Mapping[str, int],
    watermark: str | None,
    *,
    coverage: Mapping[str, str] | None = None,
) -> dict[str, Any]:
    days: dict[str, int] = {}
    pending: dict[str, int] = {}

    for day, raw_count in sorted(counts.items()):
        count = int(raw_count)
        if watermark is not None and day <= watermark:
            days[day] = count
        else:
            pending[day] = count

    if coverage is not None:
        grid_coverage = dict(coverage)
    else:
        all_days = sorted([*days, *pending])
        grid_coverage = (
            {"start": all_days[0], "end": all_days[-1]} if all_days else None
        )
    return {"coverage": grid_coverage, "days": days, "pending": pending}
