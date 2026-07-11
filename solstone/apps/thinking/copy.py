# SPDX-License-Identifier: AGPL-3.0-only
# Copyright (c) 2026 sol pbc

"""Owner-facing copy constants for the Thinking app."""

from __future__ import annotations

from typing import Any

HEADING = "thinking"
LANES = [
    {
        "id": "local",
        "label": "Local",
        "sub": "on your device",
        "description": (
            "a model runs right on this computer — nothing leaves for sol to think."
        ),
    },
    {
        "id": "confidential",
        "label": "Confidential processing",
        "sub": "operated by sol pbc",
        "tag": "not open yet",
        "description": "coming — scouts get it first.",
    },
    {
        "id": "byo",
        "label": "your own AI engine",
        "sub": "your key, or your own endpoint",
        "description": (
            "bring a provider key — Claude, Gemini, or GPT — or point sol at your "
            "own endpoint. it stays in your journal; sol pbc is never in the path."
        ),
    },
]
CONFIDENTIAL_LANE_DETAIL = {
    "heading": "confidential processing",
    "sub": "operated by sol pbc · not open yet",
    "mechanism": (
        "let sol "
        "think off your device — on confidential hardware sol pbc runs "
        "that keeps nothing."
    ),
    "egress": (
        "when it opens, sol will send only the thinking off your device — never "
        "your journal, which stays here on this computer. it runs on confidential "
        "hardware sol pbc operates."
    ),
    "claims": "no content is retained · no human reviews it · nothing is used to train",
    "attestation": "your journal checks the hardware before it sends anything.",
    "early_access": (
        "scouts get confidential processing first. it isn't open yet — when it "
        "is, the scout program is the way in."
    ),
}
ACTIVE_LANE_LABELS = {
    "none": "not thinking yet",
    "local": "local",
    "confidential": "confidential processing",
    "byo": "your own AI engine",
    "advanced": "advanced split",
}
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
GLANCE = {
    "lane_label": "sol is thinking with",
    "local": {
        "value": "a model on your device",
        "detail": "runs right on this computer — nothing leaves for sol to think",
    },
    "byo_key": {
        "value": "your own key · {provider}",
        "detail": "a key you added — stays in your journal, never shared",
    },
    "byo_endpoint": {
        "value": "your own endpoint",
        "detail": "sol thinks at the endpoint you set — your server, your rules",
    },
    "byo_scout": {
        "value": "scout · we cover it",
        "detail": (
            "covered through the scout program while you're in alpha — stays in "
            "your journal"
        ),
    },
    "none": {
        "value": "not thinking yet",
        "detail": (
            "sol is keeping your journal — but it can't answer you until you "
            "choose how it thinks below."
        ),
    },
}
BYO_SETUP = {
    "intro": (
        "bring your own AI engine. sol pbc is never in the path — it stays in "
        "your journal."
    ),
    "chooser_key": "a key",
    "chooser_endpoint": "your own endpoint",
    "key_heading": "pick your provider",
    "key_sub": (
        "all three work the same in solstone. choose the one you have a key for."
    ),
    "get_key": "get a key ↗",
    "paste_title": "paste your {provider} key",
    "key_hint": (
        "it stays in your journal — sol pbc never sets it up or sees it. paste "
        "it once; sol uses it from here."
    ),
    "terms": (
        "your questions are processed by {provider}, stored only briefly for "
        "processing, and never used for training."
    ),
    "terms_link": "terms ↗",
    "endpoint_heading": "point sol at your own endpoint",
    "endpoint_sub": "any OpenAI-compatible endpoint — your server, your rules.",
    "endpoint_honesty": (
        "sol checks the endpoint works before it relies on it. if it can't "
        "reach it, sol tells you — it never quietly falls back to anyone else."
    ),
    "scout_heading": "in the scout program?",
    "scout_sub": (
        "be an early tester for solstone — we'll cover your thinking, using Gemini."
    ),
    "scout_terms_link": "scout program terms ↗",
    "scout_provenance": (
        "covered through the scout program — the key stays in your journal."
    ),
}
LANE_SWITCH = {
    "heading": "switch how sol thinks?",
    "current_label": "now",
    "target_label": "switch to",
    "confirm": "switch",
    "cancel": "keep using {current}",
    "to_local_note": (
        "sol will think right on this computer. your {current} setup stays saved "
        "— switch back anytime."
    ),
    "to_byo_note": "sol will think with your own engine. {setup} is still here.",
    "setup_key": "a saved key",
    "setup_endpoint": "your endpoint",
    "setup_scout": "scout",
}
LOCAL_INSTALL = {
    "phases": {
        "resolving": "resolving",
        "downloading": "downloading",
        "verifying": "verifying",
        "installing": "installing",
    },
    "pill_inflight": "setting up",
    "pill_failed": STATE_LABELS["failed"],
    "retry": "try again",
    "install": "install local model",
    "notice_inflight": "local thinking will stay in your journal once setup finishes.",
}
CONFIDENTIAL_MORE_LABEL = "how it works →"
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
        "confidential": {
            "lane_detail": dict(CONFIDENTIAL_LANE_DETAIL),
            "more_label": CONFIDENTIAL_MORE_LABEL,
        },
        "glance": dict(GLANCE),
        "byo_setup": dict(BYO_SETUP),
        "lane_switch": dict(LANE_SWITCH),
        "local_install": {
            **LOCAL_INSTALL,
            "phases": dict(LOCAL_INSTALL["phases"]),
        },
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
