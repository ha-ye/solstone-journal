# SPDX-License-Identifier: AGPL-3.0-only
# Copyright (c) 2026 sol pbc

"""Read-only, secret-free optional service status helpers."""

from __future__ import annotations

from solstone.think.link.paths import load_service_token
from solstone.think.link.window import read_posture
from solstone.think.services import scout
from solstone.think.services.constants import SERVICE_SCOUT, SERVICE_SPL

STATE_ENABLED = "enabled"
STATE_MANUAL_KEY = "manual_key"
STATE_PENDING = "pending"
STATE_DISABLED = "disabled"
STATE_NOT_ENABLED = "not_enabled"
STATE_INCONSISTENT = "inconsistent"

SCOUT_MANUAL_KEY_GUIDANCE = (
    "A manually-managed Gemini key is in use; scout is not managing it."
)
SCOUT_PENDING_GUIDANCE = "Scout approval is pending review."
SCOUT_DISABLED_GUIDANCE = "Scout is not enabled."
SPL_NOT_ENABLED_GUIDANCE = "spl is not enabled."
SPL_INCONSISTENT_GUIDANCE = "spl is in an inconsistent state; re-enable to repair."


def _response(service: str, state: str, guidance: str | None) -> dict[str, str | None]:
    return {"service": service, "state": state, "guidance": guidance}


def scout_status() -> dict[str, str | None]:
    if scout.is_scout_enabled():
        return _response(SERVICE_SCOUT, STATE_ENABLED, None)
    if scout.is_manual_key_present():
        return _response(SERVICE_SCOUT, STATE_MANUAL_KEY, SCOUT_MANUAL_KEY_GUIDANCE)

    provenance = scout.scout_provenance()
    if isinstance(provenance, dict) and provenance.get("state") == STATE_PENDING:
        return _response(SERVICE_SCOUT, STATE_PENDING, SCOUT_PENDING_GUIDANCE)

    return _response(SERVICE_SCOUT, STATE_DISABLED, SCOUT_DISABLED_GUIDANCE)


def spl_status() -> dict[str, str | None]:
    posture = read_posture()
    token_present = load_service_token() is not None
    if posture == SERVICE_SPL and token_present:
        return _response(SERVICE_SPL, STATE_ENABLED, None)
    if posture == SERVICE_SPL:
        return _response(SERVICE_SPL, STATE_INCONSISTENT, SPL_INCONSISTENT_GUIDANCE)
    return _response(SERVICE_SPL, STATE_NOT_ENABLED, SPL_NOT_ENABLED_GUIDANCE)
