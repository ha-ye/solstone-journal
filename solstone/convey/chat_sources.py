# SPDX-License-Identifier: AGPL-3.0-only
# Copyright (c) 2026 sol pbc

from __future__ import annotations

import calendar
import re
from typing import Any

_SOL_REF_RE = re.compile(r"\[([^\]]+)\]\((sol://[^\s)]+)\)|(sol://[^\s)\]]+)")
_DAY_RE = re.compile(r"^\d{8}$")


def parse_sol_sources(text: str) -> list[dict[str, Any]]:
    sources: list[dict[str, Any]] = []
    seen: set[str] = set()

    for match in _SOL_REF_RE.finditer(text):
        if match.group(2):
            ref = match.group(2)
            label = match.group(1)
        else:
            ref = match.group(3).rstrip(".,;:!?")
            label = _derive_label(ref)

        if ref in seen:
            continue
        seen.add(ref)
        sources.append({"ref": ref, "label": label, "url": resolve_sol_ref(ref)})

    return sources


def resolve_sol_ref(ref: str) -> str | None:
    if not ref.startswith("sol://"):
        return None

    parts = ref.removeprefix("sol://").split("/")
    if len(parts) == 4 and parts[0] == "facets" and _DAY_RE.match(parts[3]):
        _prefix, facet, kind, day = parts
        if kind == "news":
            return f"/app/news/{facet}/{day}"
        if kind == "reflections":
            # Reflections week_view(day) is not facet-scoped; facet cannot be honored.
            return f"/app/reflections/{day}"
        return None

    if parts and _DAY_RE.match(parts[0]):
        return f"/app/timeline/{parts[0]}"

    return None


def _derive_label(ref: str) -> str:
    if not ref.startswith("sol://"):
        return ref

    parts = ref.removeprefix("sol://").split("/")
    if parts and _DAY_RE.match(parts[0]):
        day = parts[0]
        month = int(day[4:6])
        if 1 <= month <= 12:
            return f"{calendar.month_abbr[month]} {int(day[6:8])}"
        return ref

    if len(parts) == 4 and parts[0] == "facets" and _DAY_RE.match(parts[3]):
        _prefix, facet, kind, _day = parts
        return f"{facet} · {kind}"

    return ref
