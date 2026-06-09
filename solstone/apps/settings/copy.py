# SPDX-License-Identifier: AGPL-3.0-only
# Copyright (c) 2026 sol pbc

"""Locked copy for convey settings CLI and restart-aware settings UI flows."""

from __future__ import annotations

CONVEY_REFUSE_NO_PASSWORD_NETWORK = "error: enabling network access requires a password. set one first with: journal password set"
CONVEY_REFUSE_NO_PASSWORD_TRUST = "error: disabling localhost trust requires a password (otherwise no client could authenticate). set one first with: journal password set"
CONVEY_NETWORK_ENABLE_PROGRESS = "enabling network access. restarting convey…"
CONVEY_NETWORK_ENABLE_DONE = (
    "network access enabled. convey is now reachable at: {host_url}"
)
CONVEY_NETWORK_DISABLE_PROGRESS = "restricting to localhost only. restarting convey…"
CONVEY_NETWORK_DISABLE_DONE = (
    "network access disabled. convey is now reachable only at: http://localhost:{port}"
)
CONVEY_RESTART_TIMEOUT = (
    "warning: restart did not complete in 15 seconds. check status with: sol status"
)
CONVEY_MOVED_NETWORK_ENABLE = (
    "moved to `journal settings convey network-access enable` — run that instead."
)
CONVEY_MOVED_NETWORK_DISABLE = (
    "moved to `journal settings convey network-access disable` — run that instead."
)
CONVEY_NETWORK_ACCESS_CONFIG_REJECTED = (
    "network access is no longer changed through settings config. run journal "
    "settings convey network-access enable or journal settings convey network-access "
    "disable from this machine, or use the local settings security toggle."
)
CONVEY_NETWORK_LOCAL_ONLY_REASON = (
    "network access can only be changed from this machine."
)
CONVEY_NETWORK_ACCESS_LABEL = "network access"
CONVEY_NETWORK_ACCESS_HINT = (
    "lets devices on your network reach convey. changing this restarts convey."
)
FACET_DETAIL_SUCCESS_HEADING = "{title} is ready"
FACET_DETAIL_VALUE_FRAMING = (
    "{title} gathers the people, places, and things that share this context. "
    "as you tag them, they'll show up here and in your journal's filtered views."
)
FACET_DETAIL_PRIMARY_CTA = "tag people, places, and things to {title}"
FACET_DETAIL_SECONDARY_CTA = "create another facet"
FACET_DETAIL_TERTIARY_ESCAPE = "back to settings"

__all__ = [
    "CONVEY_MOVED_NETWORK_DISABLE",
    "CONVEY_MOVED_NETWORK_ENABLE",
    "CONVEY_NETWORK_ACCESS_CONFIG_REJECTED",
    "CONVEY_NETWORK_ACCESS_HINT",
    "CONVEY_NETWORK_ACCESS_LABEL",
    "CONVEY_NETWORK_DISABLE_DONE",
    "CONVEY_NETWORK_DISABLE_PROGRESS",
    "CONVEY_NETWORK_ENABLE_DONE",
    "CONVEY_NETWORK_ENABLE_PROGRESS",
    "CONVEY_NETWORK_LOCAL_ONLY_REASON",
    "CONVEY_REFUSE_NO_PASSWORD_NETWORK",
    "CONVEY_REFUSE_NO_PASSWORD_TRUST",
    "CONVEY_RESTART_TIMEOUT",
    "FACET_DETAIL_PRIMARY_CTA",
    "FACET_DETAIL_SECONDARY_CTA",
    "FACET_DETAIL_SUCCESS_HEADING",
    "FACET_DETAIL_TERTIARY_ESCAPE",
    "FACET_DETAIL_VALUE_FRAMING",
]
