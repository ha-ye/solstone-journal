# SPDX-License-Identifier: AGPL-3.0-only
# Copyright (c) 2026 sol pbc

from __future__ import annotations

from collections.abc import Mapping
from typing import Any


def build_date_nav_index(day_counts: Mapping[str, int | float]) -> dict[str, Any]:
    months: dict[str, int | float] = {}
    days: list[str] = []

    for day, total in sorted(day_counts.items()):
        if total <= 0:
            continue
        days.append(day)
        month = day[:6]
        months[month] = months.get(month, 0) + total

    coverage = {"start": days[0], "end": days[-1]} if days else None
    return {"coverage": coverage, "months": months}
