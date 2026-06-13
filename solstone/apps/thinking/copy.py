# SPDX-License-Identifier: AGPL-3.0-only
# Copyright (c) 2026 sol pbc

"""Owner-facing copy constants for the Thinking app."""

from __future__ import annotations

from typing import Any

HEADING = "thinking"
SUBHEADING = "Choose how sol thinks: scout-provided Gemini, your own cloud key, or a local model."
ACTIVE_LANE_LABELS = {
    "scout": "Scout",
    "byo": "BYO cloud",
    "local": "Local",
    "advanced": "Advanced split",
}
LANES = [
    {
        "id": "scout",
        "label": "Scout",
        "description": "Gemini key provided through solstone scout.",
    },
    {
        "id": "byo",
        "label": "BYO cloud",
        "description": "Use your own Claude, Gemini, or GPT key.",
    },
    {
        "id": "local",
        "label": "Local",
        "description": "Use the model running on this machine.",
    },
]
PROVIDER_LABELS = {
    "anthropic": "Claude",
    "google": "Gemini",
    "openai": "GPT",
    "local": "Local",
}
KEY_LABELS = {
    "ANTHROPIC_API_KEY": "Claude key",
    "GOOGLE_API_KEY": "Gemini key",
    "OPENAI_API_KEY": "GPT key",
}
STATE_LABELS = {
    "active": "active",
    "available": "available",
    "unavailable": "not ready",
    "advanced": "split",
    "loading": "loading...",
    "saved": "saved",
    "validating": "validating...",
    "failed": "couldn't finish",
}
ACTION_LABELS = {
    "switch": "Use This Lane",
    "save_key": "Save Key",
    "clear_key": "Clear Key",
    "validate": "Validate",
    "install": "Install",
    "refresh": "Refresh",
}


def thinking_copy_payload() -> dict[str, Any]:
    """Return copy constants for templates and browser code."""

    return {
        "heading": HEADING,
        "subheading": SUBHEADING,
        "active_lane_labels": dict(ACTIVE_LANE_LABELS),
        "lanes": [dict(lane) for lane in LANES],
        "provider_labels": dict(PROVIDER_LABELS),
        "key_labels": dict(KEY_LABELS),
        "state_labels": dict(STATE_LABELS),
        "action_labels": dict(ACTION_LABELS),
    }


def thinking_copy_values() -> list[str]:
    """Return all verbatim copy values, flattening nested constants."""

    values: list[str] = []

    def visit(value: Any) -> None:
        if isinstance(value, str):
            values.append(value)
        elif isinstance(value, dict):
            for item in value.values():
                visit(item)
        elif isinstance(value, list):
            for item in value:
                visit(item)

    visit(thinking_copy_payload())
    return values
