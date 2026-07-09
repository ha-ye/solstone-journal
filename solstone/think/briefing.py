# SPDX-License-Identifier: AGPL-3.0-only
# Copyright (c) 2026 sol pbc

"""Shared morning briefing loaders and renderers."""

from __future__ import annotations

import json
import logging
from typing import Any

from solstone.think.talent import morning_briefing_path

logger = logging.getLogger(__name__)

BRIEFING_ABSENT_TEXT = "Nothing to report."
SECTION_KEYS = (
    "your_day",
    "yesterday",
    "needs_attention",
    "forward_look",
    "reading",
)
REQUIRED_ROOT_KEYS = ("metadata", *SECTION_KEYS)
SECTION_HEADINGS = {
    "your_day": "Your Day",
    "yesterday": "Yesterday",
    "needs_attention": "Needs Attention",
    "forward_look": "Forward Look",
    "reading": "Reading",
}


def load_briefing(day: str) -> dict | None:
    """Load a day's JSON morning briefing if it has the required root shape."""
    path = morning_briefing_path(day)
    if not path.exists():
        return None

    try:
        with path.open(encoding="utf-8") as handle:
            briefing = json.load(handle)
    except Exception:
        logger.warning("failed to load morning briefing JSON %s", path, exc_info=True)
        return None

    if not isinstance(briefing, dict):
        return None
    if any(key not in briefing for key in REQUIRED_ROOT_KEYS):
        return None
    return briefing


def render_briefing_sections(briefing: dict) -> dict[str, str]:
    """Render non-empty briefing sections as markdown bullet bodies."""
    sections: dict[str, str] = {}

    your_day_lines = []
    for item in _dict_items(briefing.get("your_day")):
        text = _clean(item.get("text"))
        if not text:
            continue
        time = _clean(item.get("time"))
        if time:
            your_day_lines.append(f"- **{time}** \u2014 {text}")
        else:
            your_day_lines.append(f"- {text}")
    if your_day_lines:
        sections["your_day"] = "\n".join(your_day_lines)

    yesterday_lines = [f"- {text}" for text in _string_items(briefing.get("yesterday"))]
    if yesterday_lines:
        sections["yesterday"] = "\n".join(yesterday_lines)

    needs_lines = []
    for item in briefing_needs_items(briefing):
        text = _clean(item.get("text"))
        if text:
            needs_lines.append(f"- {text}")
    if needs_lines:
        sections["needs_attention"] = "\n".join(needs_lines)

    forward_lines = [
        f"- {text}" for text in _string_items(briefing.get("forward_look"))
    ]
    if forward_lines:
        sections["forward_look"] = "\n".join(forward_lines)

    reading_lines = []
    for item in _dict_items(briefing.get("reading")):
        facet = _clean(item.get("facet"))
        summary = _clean(item.get("summary"))
        if facet and summary:
            reading_lines.append(f"- **{facet}** \u2014 {summary}")
        elif facet:
            reading_lines.append(f"- **{facet}**")
        elif summary:
            reading_lines.append(f"- {summary}")
    if reading_lines:
        sections["reading"] = "\n".join(reading_lines)

    return sections


def render_briefing_markdown(briefing: dict) -> str:
    """Render a full markdown projection of a morning briefing."""
    sections = render_briefing_sections(briefing)
    metadata = (
        briefing.get("metadata") if isinstance(briefing.get("metadata"), dict) else {}
    )
    preamble = _clean(metadata.get("coverage_preamble"))

    lines: list[str] = []
    if preamble:
        lines.extend(f"> {line}" if line else ">" for line in preamble.splitlines())

    for key in SECTION_KEYS:
        if lines:
            lines.append("")
        lines.append(f"## {SECTION_HEADINGS[key]}")
        lines.append("")
        lines.append(sections.get(key) or BRIEFING_ABSENT_TEXT)

    return "\n".join(lines).strip()


def briefing_needs_items(briefing: dict) -> list[dict]:
    """Return raw needs_attention item objects from a briefing."""
    return _dict_items(briefing.get("needs_attention"))


def briefing_meeting_count(briefing: dict) -> int:
    """Count Your Day items with a non-empty time."""
    return sum(
        1 for item in _dict_items(briefing.get("your_day")) if _clean(item.get("time"))
    )


def _clean(value: object) -> str:
    return str(value or "").strip()


def _dict_items(value: Any) -> list[dict]:
    if not isinstance(value, list):
        return []
    return [item for item in value if isinstance(item, dict)]


def _string_items(value: Any) -> list[str]:
    if not isinstance(value, list):
        return []
    return [text for text in (_clean(item) for item in value) if text]
