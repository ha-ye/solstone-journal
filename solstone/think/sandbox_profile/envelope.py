# SPDX-License-Identifier: AGPL-3.0-only
# Copyright (c) 2026 sol pbc

"""Closed response envelope for ``journal sandbox-profile``."""

from __future__ import annotations

import json
from dataclasses import dataclass, field

from solstone.think.sandbox_profile import manifest

TOP_OK = "ok"
TOP_DEGRADED = "degraded"
TOP_ERROR = "error"
TOP_CLEANUP_FAILED = "cleanup_failed"

EXIT_BY_STATE: dict[str, int] = {
    TOP_OK: 0,
    TOP_DEGRADED: 1,
    TOP_ERROR: 2,
    TOP_CLEANUP_FAILED: 3,
}

CAP_NOT_APPLIED = "not_applied"
CAP_READY = "ready"
CAP_DEGRADED = "degraded"
CAP_CLEANUP_FAILED = "cleanup_failed"

CAPABILITY_STATES = frozenset(
    {CAP_NOT_APPLIED, CAP_READY, CAP_DEGRADED, CAP_CLEANUP_FAILED}
)

RESIDUAL_CODES = frozenset(
    {
        "apply_interrupted",
        "intent_finalize_missing",
        "unmanaged_existing_state",
        "scout_block_missing",
        "scout_key_fingerprint_mismatch",
        "unrelated_manual_key_preserved",
        "spl_identity_missing",
        "spl_token_missing",
        "spl_posture_not_spl",
        "spb_binding_missing",
        "spb_instance_mismatch",
        "spb_backup_config_incomplete",
        "spp_block_missing",
        "spp_credential_fingerprint_mismatch",
        "spp_credential_ownership_conflict",
        "cleanup_still_applied",
        "local_artifact_io_failed",
        "post_commit_failed",
        "missing_expected_artifact",
    }
)

ERROR_CODES = frozenset(
    {
        "sandbox_marker_missing",
        "sandbox_marker_symlink",
        "sandbox_marker_not_regular",
        "sandbox_marker_unparseable",
        "sandbox_marker_non_object",
        "sandbox_marker_wrong_kind",
        "sandbox_marker_wrong_contract_version",
        "sandbox_marker_wrong_profile",
        "sandbox_marker_bad_run_id",
        "sandbox_marker_path_mismatch",
        "intent_missing",
        "intent_malformed",
        "intent_run_mismatch",
        "payload_invalid",
        "spb_instance_mismatch",
        "unknown_capability",
        "unsupported_capability_action",
        "internal_error",
    }
)

GUIDANCE: dict[str, str | None] = {
    "sandbox_marker_missing": "Create a fresh disposable sandbox marker, then retry.",
    "sandbox_marker_symlink": "Replace the marker with a regular JSON file.",
    "sandbox_marker_not_regular": "Replace the marker with a regular JSON file.",
    "sandbox_marker_unparseable": "Rewrite the marker as valid JSON.",
    "sandbox_marker_non_object": "Rewrite the marker as a JSON object.",
    "sandbox_marker_wrong_kind": "Use kind 'solstone-disposable-journal'.",
    "sandbox_marker_wrong_contract_version": "Use the supported sandbox profile contract.",
    "sandbox_marker_wrong_profile": "Use the supported sandbox profile contract.",
    "sandbox_marker_bad_run_id": "Use a canonical UUID run_id.",
    "sandbox_marker_path_mismatch": "Point SOLSTONE_JOURNAL at the marker's canonical journal path.",
    "intent_missing": "Run prepare first.",
    "intent_malformed": "Use the owning run or create a fresh sandbox.",
    "intent_run_mismatch": "Use the owning run or create a fresh sandbox.",
    "payload_invalid": "Send one valid JSON object on stdin for the selected capability.",
    "spb_instance_mismatch": "Send a hosted backup binding for the prepared runtime instance_id.",
    "unknown_capability": "Use one of the supported capabilities.",
    "unsupported_capability_action": "Use one of the supported capabilities for this action.",
    "internal_error": "Inspect logs and retry in a fresh sandbox.",
}


@dataclass(frozen=True, slots=True)
class CapabilityEnvelope:
    name: str
    state: str
    residuals: tuple[str, ...] = ()

    def to_json(self) -> dict[str, object]:
        if self.name not in manifest.CAPABILITY_ORDER:
            raise ValueError(f"unsupported capability name: {self.name!r}")
        if self.state not in CAPABILITY_STATES:
            raise ValueError(f"unsupported capability state: {self.state!r}")
        for residual in self.residuals:
            if residual not in RESIDUAL_CODES:
                raise ValueError(f"unsupported residual code: {residual!r}")
        return {
            "name": self.name,
            "state": self.state,
            "residuals": list(self.residuals),
        }


@dataclass(frozen=True, slots=True)
class ErrorEnvelope:
    code: str
    message: str

    def to_json(self) -> dict[str, str]:
        if self.code not in ERROR_CODES:
            raise ValueError(f"unsupported error code: {self.code!r}")
        return {"code": self.code, "message": self.message}


@dataclass(frozen=True, slots=True)
class Envelope:
    action: str
    profile: str
    run_id: str | None
    state: str
    capabilities: tuple[CapabilityEnvelope, ...]
    next_actions: tuple[str, ...] = ()
    error: ErrorEnvelope | None = None
    contract_version: int = field(default=manifest.CONTRACT_VERSION)

    def to_json(self) -> dict[str, object]:
        if self.state not in EXIT_BY_STATE:
            raise ValueError(f"unsupported top-level state: {self.state!r}")
        return {
            "contract_version": self.contract_version,
            "action": self.action,
            "profile": self.profile,
            "run_id": self.run_id,
            "state": self.state,
            "capabilities": [cap.to_json() for cap in self.capabilities],
            "next_actions": list(self.next_actions),
            "error": None if self.error is None else self.error.to_json(),
        }

    @property
    def exit_code(self) -> int:
        return EXIT_BY_STATE[self.state]


def empty_capabilities() -> tuple[CapabilityEnvelope, ...]:
    return tuple(
        CapabilityEnvelope(name, CAP_NOT_APPLIED) for name in manifest.CAPABILITY_ORDER
    )


def error_envelope(
    *,
    action: str,
    code: str,
    message: str,
    run_id: str | None,
    next_actions: tuple[str, ...] = (),
) -> Envelope:
    if not next_actions:
        guidance = GUIDANCE.get(code)
        next_actions = (guidance,) if guidance else ()
    return Envelope(
        action=action,
        profile=manifest.PROFILE,
        run_id=run_id,
        state=TOP_ERROR,
        capabilities=empty_capabilities(),
        next_actions=next_actions,
        error=ErrorEnvelope(code, message),
    )


def render_json(envelope: Envelope) -> str:
    return json.dumps(envelope.to_json(), indent=2) + "\n"


def summarize_human(envelope: Envelope) -> str:
    payload = envelope.to_json()
    lines = [
        f"action: {payload['action']}",
        f"profile: {payload['profile']}",
        f"run_id: {payload['run_id'] or '<none>'}",
        f"state: {payload['state']}",
    ]
    for capability in payload["capabilities"]:
        if not isinstance(capability, dict):
            continue
        residuals = capability.get("residuals") or []
        suffix = f" residuals={','.join(residuals)}" if residuals else ""
        lines.append(f"- {capability.get('name')}: {capability.get('state')}{suffix}")
    if payload["error"]:
        error = payload["error"]
        if isinstance(error, dict):
            lines.append(f"error: {error.get('code')}: {error.get('message')}")
    next_actions = payload["next_actions"]
    if next_actions:
        lines.append("next_actions:")
        lines.extend(f"- {item}" for item in next_actions)
    return "\n".join(lines) + "\n"
