# SPDX-License-Identifier: AGPL-3.0-only
# Copyright (c) 2026 sol pbc

"""Scout lane state mapping for the Thinking app."""

from __future__ import annotations

from typing import Any

from solstone.apps.thinking import copy as thinking_copy
from solstone.think.services import operations
from solstone.think.services import status as service_status

RESTING_FROM_STATUS = {
    "enabled": thinking_copy.SCOUT_STATE_ON,
    "pending": thinking_copy.SCOUT_STATE_REQUESTED,
    "manual_key": thinking_copy.SCOUT_STATE_MANUAL_KEY_PRESENT,
    "disabled": thinking_copy.SCOUT_STATE_OFF,
}
PHASE_TO_PRODUCT = {
    "starting": thinking_copy.SCOUT_OP_STARTING,
    "waiting": thinking_copy.SCOUT_OP_WAITING,
    "enabled": thinking_copy.SCOUT_STATE_INVITED,
    "pending": thinking_copy.SCOUT_STATE_REQUESTED,
    "revoked": thinking_copy.SCOUT_STATE_ENDED,
    "error": thinking_copy.SCOUT_STATE_REPAIR_NEEDED,
}


def resting_state() -> str:
    status = service_status.scout_status()
    return RESTING_FROM_STATUS[str(status["state"])]


def resting_guidance(state: str) -> str | None:
    return thinking_copy.SCOUT_RESTING_GUIDANCE.get(state)


def actions_for_state(state: str) -> dict[str, bool]:
    return {
        "enable": state == thinking_copy.SCOUT_STATE_OFF,
        "refresh": state
        in {thinking_copy.SCOUT_STATE_REQUESTED, thinking_copy.SCOUT_STATE_ON},
        "disable": state
        in {thinking_copy.SCOUT_STATE_REQUESTED, thinking_copy.SCOUT_STATE_ON},
    }


def provenance_payload() -> dict[str, Any]:
    return service_status.scout_provenance_view()


def remap_operation(raw: dict[str, Any] | None) -> dict[str, Any] | None:
    if raw is None:
        return None
    payload = dict(raw)
    phase = str(payload.get("phase") or "")
    payload["phase"] = PHASE_TO_PRODUCT.get(phase, phase)
    return payload


def status_payload() -> dict[str, Any]:
    state = resting_state()
    return {
        "service": "scout",
        "state": state,
        "guidance": resting_guidance(state),
        "provenance": provenance_payload(),
        "actions": actions_for_state(state),
        "operation": remap_operation(operations.operation_for_service("scout")),
    }
