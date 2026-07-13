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
        "description": "sol pbc runs the model on confidential GPUs.",
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
CONFIDENTIAL_TRUST_HEADING = "confidential processing"
CONFIDENTIAL_TRUST_SUB = "operated by sol pbc"
CONFIDENTIAL_LANE_EGRESS = "when it's on, the thinking leaves your device — text, images, and (with the audio switch on, its default) your recordings for transcription. your journal itself never leaves."
CONFIDENTIAL_SETUP_EGRESS_AUDIO_ON = "what leaves your device: the text and images sol needs a model to work through, and your audio recordings for transcription. your journal itself never leaves."
CONFIDENTIAL_SETUP_EGRESS_AUDIO_OFF = "what leaves your device: the text and images sol needs a model to work through. your recordings stay on your device — speech becomes text there."
CONFIDENTIAL_TRUST_CLAIMS = (
    "no content is retained · no human reviews it · nothing is used to train"
)
CONFIDENTIAL_TRUST_FAIL_CLOSED = "your journal must verify the service before anything is sent — if it can't verify, it doesn't send."
CONFIDENTIAL_TRUST_SUBSTRATE = "sol pbc runs the model itself on confidential GPUs in Microsoft Azure. the hardware boundary keeps the cloud host excluded from what's processed — no third-party AI provider is in the path."
CONFIDENTIAL_EARLY_ACCESS = "confidential processing is coming — scouts get it first."
CONFIDENTIAL_AUDIO_LABEL = "transcribe audio on the service"
CONFIDENTIAL_AUDIO_ON = "your recordings are transcribed on the service — sent over the verified channel, processed, and not kept. on while confidential processing is in use."
CONFIDENTIAL_AUDIO_OFF = (
    "speech becomes text on your device. your recordings don't leave."
)
CONFIDENTIAL_AUDIO_NOTE = (
    "turn it off any time — it takes effect on the next recording."
)
CONFIDENTIAL_AUDIO_DEFERRAL = "transcription is waiting — nothing is sent until your journal verifies the service. recordings stay on your device and transcribe once the check passes."
CONFIDENTIAL_AUDIO = {
    "label": CONFIDENTIAL_AUDIO_LABEL,
    "on": CONFIDENTIAL_AUDIO_ON,
    "off": CONFIDENTIAL_AUDIO_OFF,
    "note": CONFIDENTIAL_AUDIO_NOTE,
    "deferral": CONFIDENTIAL_AUDIO_DEFERRAL,
}
CONFIDENTIAL_TRUST_BEATS = {
    "heading": CONFIDENTIAL_TRUST_HEADING,
    "sub": CONFIDENTIAL_TRUST_SUB,
    "egress_audio_on": CONFIDENTIAL_SETUP_EGRESS_AUDIO_ON,
    "egress_audio_off": CONFIDENTIAL_SETUP_EGRESS_AUDIO_OFF,
    "claims": CONFIDENTIAL_TRUST_CLAIMS,
    "attestation": CONFIDENTIAL_TRUST_FAIL_CLOSED,
    "substrate": CONFIDENTIAL_TRUST_SUBSTRATE,
}
CONFIDENTIAL_LANE_DETAIL = {
    "heading": CONFIDENTIAL_TRUST_HEADING,
    "sub": CONFIDENTIAL_TRUST_SUB,
    "mechanism": CONFIDENTIAL_TRUST_SUBSTRATE,
    "egress": CONFIDENTIAL_LANE_EGRESS,
    "claims": CONFIDENTIAL_TRUST_CLAIMS,
    "attestation": CONFIDENTIAL_TRUST_FAIL_CLOSED,
    "early_access": CONFIDENTIAL_EARLY_ACCESS,
}
CONFIDENTIAL_SETUP = {
    "trust_beats": dict(CONFIDENTIAL_TRUST_BEATS),
}
CONFIDENTIAL_ATTESTATION_STATES = {
    "off": "",
    "verifying": "checking the hardware…",
    "verified": "{legs} · {substrate} · checked {checked}",
    "failed": "couldn't verify the service — sol isn't sending.",
    "stale": "your journal needs to re-check the service before sending.",
    "unreachable": "can't reach confidential processing right now — sol isn't sending.",
}
CONFIDENTIAL_OPERATION_STATES = {
    "starting": "opening your browser to confirm…",
    "waiting": "finish turning it on in your browser",
    "early_access": CONFIDENTIAL_EARLY_ACCESS,
    "repair_needed": "couldn't verify the service — sol isn't sending.",
}
CONFIDENTIAL_ACTIONS = {
    "off": "turn on confidential processing →",
    "enabled": "turn off",
    "recheck": "check again",
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
    "confidential_checking": {
        "label": "sol is waiting on",
        "value": "confidential processing",
        "detail": CONFIDENTIAL_ATTESTATION_STATES["verifying"],
    },
    "confidential_verified": {
        "label": "sol is thinking with",
        "value": "confidential processing",
        "detail": CONFIDENTIAL_ATTESTATION_STATES["verified"],
    },
    "confidential_blocked": {
        "label": "sol is holding",
        "value": "confidential processing",
        "detail": "{message}",
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
            "setup": {
                "trust_beats": dict(CONFIDENTIAL_SETUP["trust_beats"]),
            },
            "audio": dict(CONFIDENTIAL_AUDIO),
            "attestation_states": dict(CONFIDENTIAL_ATTESTATION_STATES),
            "operation_states": dict(CONFIDENTIAL_OPERATION_STATES),
            "actions": dict(CONFIDENTIAL_ACTIONS),
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
