# SPDX-License-Identifier: AGPL-3.0-only
# Copyright (c) 2026 sol pbc

"""Lucide icon helpers for Convey rendering."""

from __future__ import annotations

import functools
import json
from pathlib import Path

APP_LUCIDE_MAP: dict[str, str] = {
    "home": "house",
    "sol": "bot",
    "chat": "message-circle",
    "activities": "calendar-days",
    "transcripts": "scroll-text",
    "observer": "antenna",
    "search": "search",
    "import": "import",
    "curation": "wand-sparkles",
    "backup": "history",
    "entities": "contact",
    "health": "stethoscope",
    "network": "network",
    "news": "newspaper",
    "reflections": "moon",
    "settings": "settings",
    "speakers": "mic-vocal",
    "stats": "chart-column",
    "support": "life-buoy",
    "thinking": "brain",
    "timeline": "calendar-range",
    "tokens": "coins",
}


@functools.lru_cache(maxsize=1)
def _lucide_icons() -> dict[str, str]:
    path = Path(__file__).parent / "static" / "icons" / "lucide.json"
    with path.open(encoding="utf-8") as handle:
        return json.load(handle)


@functools.lru_cache(maxsize=1)
def _lucide_tags() -> dict[str, list[str]]:
    path = Path(__file__).parent / "static" / "icons" / "lucide-tags.json"
    with path.open(encoding="utf-8") as handle:
        return json.load(handle)


@functools.lru_cache(maxsize=1)
def _emoji_lucide_map() -> dict[str, str]:
    path = Path(__file__).parent / "static" / "icons" / "emoji-lucide.json"
    with path.open(encoding="utf-8") as handle:
        return json.load(handle)


def lucide_svg(name: str) -> str | None:
    """Return raw SVG markup for a Lucide icon name."""
    return _lucide_icons().get(name)


def is_lucide_icon(name: str) -> bool:
    """Return whether name is a vendored Lucide icon."""
    return bool(name) and lucide_svg(name) is not None


def emoji_to_lucide(emoji: str, default: str | None = None) -> str | None:
    """Translate an emoji to a Lucide icon name."""
    if not isinstance(emoji, str):
        raise TypeError("emoji must be a string")
    if not emoji:
        return default

    emoji_map = _emoji_lucide_map()

    raw_hit = emoji_map.get(emoji)
    if raw_hit is not None:
        return raw_hit

    stripped = "".join(
        ch for ch in emoji if ch != "\ufe0f" and not (0x1F3FB <= ord(ch) <= 0x1F3FF)
    )
    stripped_hit = emoji_map.get(stripped)
    if stripped_hit is not None:
        return stripped_hit

    if "\u200d" in stripped:
        leading_base = stripped.split("\u200d", 1)[0]
        base_hit = emoji_map.get(leading_base)
        if base_hit is not None:
            return base_hit

    return default


def lucide_svg_for_emoji(emoji: str) -> str | None:
    """Return raw SVG markup for an emoji's mapped Lucide icon."""
    icon_name = emoji_to_lucide(emoji)
    if icon_name is None:
        return None
    return lucide_svg(icon_name)


def resolve_facet_icon_svg(icon: str | None, emoji: str) -> str | None:
    """Resolve a facet icon override, falling back to the emoji mapping."""
    if icon:
        svg = lucide_svg(icon)
        if svg is not None:
            return svg
    return lucide_svg_for_emoji(emoji)


def search_lucide_icons(query: str, limit: int = 80) -> list[dict[str, str]]:
    """Search vendored Lucide icons by name first, then tags."""
    q = (query or "").strip().lower()
    names = sorted(_lucide_icons())
    if q:
        name_matches = [name for name in names if q in name]
        tag_matches = [
            name
            for name in names
            if q not in name and any(q in tag for tag in _lucide_tags().get(name, []))
        ]
        chosen = (name_matches + tag_matches)[:limit]
    else:
        chosen = names[:limit]

    results: list[dict[str, str]] = []
    for name in chosen:
        svg = lucide_svg(name)
        if svg is not None:
            results.append({"name": name, "svg": svg})
    return results
