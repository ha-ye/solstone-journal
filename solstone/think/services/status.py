# SPDX-License-Identifier: AGPL-3.0-only
# Copyright (c) 2026 sol pbc

"""Read-only, secret-free optional service status helpers."""

from __future__ import annotations

from solstone.think.link.paths import load_service_token
from solstone.think.link.window import read_posture
from solstone.think.services.constants import SERVICE_SPL

STATE_ENABLED = "enabled"
STATE_MANUAL_KEY = "manual_key"
STATE_PENDING = "pending"
STATE_DISABLED = "disabled"
STATE_NOT_ENABLED = "not_enabled"
STATE_INCONSISTENT = "inconsistent"

SPL_NOT_ENABLED_GUIDANCE = "spl is not enabled."
SPL_INCONSISTENT_GUIDANCE = "spl is in an inconsistent state; re-enable to repair."


def _response(service: str, state: str, guidance: str | None) -> dict[str, str | None]:
    return {"service": service, "state": state, "guidance": guidance}


def spl_status() -> dict[str, str | None]:
    posture = read_posture()
    token_present = load_service_token() is not None
    if posture == SERVICE_SPL and token_present:
        return _response(SERVICE_SPL, STATE_ENABLED, None)
    if posture == SERVICE_SPL:
        return _response(SERVICE_SPL, STATE_INCONSISTENT, SPL_INCONSISTENT_GUIDANCE)
    return _response(SERVICE_SPL, STATE_NOT_ENABLED, SPL_NOT_ENABLED_GUIDANCE)
