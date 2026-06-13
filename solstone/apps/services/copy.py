# SPDX-License-Identifier: AGPL-3.0-only
# Copyright (c) 2026 sol pbc

"""Owner-facing copy constants for the services app."""

from __future__ import annotations

from typing import Any

from solstone.think.services import portal_client

SERVICES_HEADING = "your services"
SERVICES_UMBRELLA = "solstone runs on your machine. these services are optional — turn them on when they help, turn them off whenever you want. nothing here is required to use solstone."
SERVICES_COMING_SOON_LABEL = "coming soon"

SCOUT_DESCRIPTION = "join solstone scout — we'll set you up with a Gemini key on your machine and bring you into the alpha cohort"
SCOUT_MANAGE_AFFORDANCE = "manage on the web →"
SPL_DESCRIPTION = "reach your journal from your other devices, privately"
SPL_MANAGE_AFFORDANCE = "manage in link →"
SPB_DESCRIPTION = (
    "keep an encrypted copy of your journal somewhere safe — only you can read it"
)
SPB_MANAGE_AFFORDANCE = "manage in backup →"
SPN_DESCRIPTION = (
    "let sol reach you on your other devices when there's something worth a look"
)
SCOUT_LABEL = "solstone scout"
SPL_LABEL = "solstone private link"
SPB_LABEL = "solstone backup"
SPN_LABEL = "solstone private notifications"
SERVICES_PROMISE = "your journal is always private, only yours."

STATE_LABELS = {
    "disabled": "off",
    "not_enabled": "off",
    "manual_key": "manual key",
    "pending": "pending",
    "enabled": "enabled",
    "inconsistent": "needs repair",
    "coming_soon": SERVICES_COMING_SOON_LABEL,
    "starting": "starting",
    "waiting": "waiting",
    "revoked": "off",
    "error": "couldn't finish",
    "busy": "busy",
    "browser_open_failed": "couldn't open your browser — open this link yourself",
    "load_failed": "couldn't load service status",
}

ACTION_LABELS = {
    "enable": "enable",
    "disable": "disable",
    "refresh": "refresh",
    "retry": "retry",
    "open_link": "open link →",
}


def services_copy_payload() -> dict[str, Any]:
    """Return copy constants for templates and browser code."""

    return {
        "heading": SERVICES_HEADING,
        "umbrella": SERVICES_UMBRELLA,
        "coming_soon_label": SERVICES_COMING_SOON_LABEL,
        "promise": SERVICES_PROMISE,
        "state_labels": dict(STATE_LABELS),
        "action_labels": dict(ACTION_LABELS),
        "services": [
            {
                "id": "scout",
                "label": SCOUT_LABEL,
                "description": SCOUT_DESCRIPTION,
                "manage_affordance": SCOUT_MANAGE_AFFORDANCE,
                "manage_href": portal_client.portal_base_url(),
                "coming_soon": False,
            },
            {
                "id": "spl",
                "label": SPL_LABEL,
                "description": SPL_DESCRIPTION,
                "manage_affordance": SPL_MANAGE_AFFORDANCE,
                "manage_href": "/app/link",
                "coming_soon": False,
            },
            {
                "id": "spb",
                "label": SPB_LABEL,
                "description": SPB_DESCRIPTION,
                "manage_affordance": SPB_MANAGE_AFFORDANCE,
                "manage_href": "/app/backup",
                "coming_soon": False,
            },
            {
                "id": "spn",
                "label": SPN_LABEL,
                "description": SPN_DESCRIPTION,
                "manage_affordance": "",
                "manage_href": None,
                "coming_soon": True,
            },
        ],
    }


def services_copy_values() -> list[str]:
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

    visit(services_copy_payload())
    return values
