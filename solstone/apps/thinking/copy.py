# SPDX-License-Identifier: AGPL-3.0-only
# Copyright (c) 2026 sol pbc

"""Owner-facing copy constants for the Thinking app."""

from __future__ import annotations

from typing import Any

HEADING = "thinking"
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
        "description": "be an early tester for solstone — we'll cover your thinking, using Gemini.",
    },
    {
        "id": "byo",
        "label": "BYO cloud",
        "description": "your key from Claude, Gemini, or GPT — your billing, your control. stays in your journal.",
    },
    {
        "id": "local",
        "label": "Local",
        "description": "a model runs right in your journal — your data never leaves.",
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
    "check": "Check",
}
SCOUT_STATE_OFF = "off"
SCOUT_STATE_REQUESTED = "requested"
SCOUT_STATE_INVITED = "invited"
SCOUT_STATE_ON = "on"
SCOUT_STATE_ENDED = "ended"
SCOUT_STATE_MANUAL_KEY_PRESENT = "manual_key_present"
SCOUT_STATE_REPAIR_NEEDED = "repair_needed"
SCOUT_OP_STARTING = "starting"
SCOUT_OP_WAITING = "waiting"
SCOUT_STATE_LABELS = {
    SCOUT_STATE_OFF: "off",
    SCOUT_STATE_REQUESTED: "requested",
    SCOUT_STATE_INVITED: "invited",
    SCOUT_STATE_ON: "on",
    SCOUT_STATE_ENDED: "ended",
    SCOUT_STATE_MANUAL_KEY_PRESENT: "BYO key",
    SCOUT_STATE_REPAIR_NEEDED: "repair needed",
}
SCOUT_RESTING_GUIDANCE = {
    SCOUT_STATE_OFF: "Scout is off.",
    SCOUT_STATE_REQUESTED: "Scout is waiting for approval.",
    SCOUT_STATE_INVITED: "Scout is ready.",
    SCOUT_STATE_ON: "Scout is on — sol can think.",
    SCOUT_STATE_ENDED: "Scout has ended.",
    SCOUT_STATE_MANUAL_KEY_PRESENT: "A Gemini key you manage is already set.",
}
SCOUT_MANUAL_KEY_BLOCK_COPY = "a Gemini key you manage is already set — clear it in your own key first, then turn on scout."
SCOUT_CONSENT_CTA = "continue to approve →"


def thinking_copy_payload() -> dict[str, Any]:
    """Return copy constants for templates and browser code."""

    return {
        "heading": HEADING,
        "active_lane_labels": dict(ACTIVE_LANE_LABELS),
        "lanes": [dict(lane) for lane in LANES],
        "provider_labels": dict(PROVIDER_LABELS),
        "key_labels": dict(KEY_LABELS),
        "state_labels": dict(STATE_LABELS),
        "action_labels": dict(ACTION_LABELS),
        "scout": {
            "state_labels": dict(SCOUT_STATE_LABELS),
            "resting_guidance": dict(SCOUT_RESTING_GUIDANCE),
            "manual_key_block": SCOUT_MANUAL_KEY_BLOCK_COPY,
            "consent_cta": SCOUT_CONSENT_CTA,
        },
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
