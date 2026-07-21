# SPDX-License-Identifier: AGPL-3.0-only
# Copyright (c) 2026 sol pbc

"""Owner-facing active-brain health projection."""

from __future__ import annotations

import logging
import uuid
from datetime import datetime, timezone
from pathlib import Path
from typing import Literal, TypedDict

from solstone.think.callosum import callosum_send
from solstone.think.providers.brain_state import (
    BRAIN_EVIDENCE_REASON_CODES,
    COMPONENT_ORDER,
    BrainAggregateState,
    BrainEvidenceComponent,
    BrainStateRecord,
    inspect_brain_state,
)

logger = logging.getLogger(__name__)

BrainSurface = Literal["thinking", "health", "home", "support", "cli"]
BrainActionKind = Literal[
    "none",
    "open_thinking",
    "open_local_setup",
    "check_or_view",
]

HEADLINES: dict[BrainAggregateState, str] = {
    "ready": "sol can think",
    "checking": "checking how sol thinks",
    "blocked": "sol needs a way to think",
    "unhealthy": "sol's thinking needs attention",
    "unknown": "thinking status unavailable",
}

LOCAL_RUNTIME_REASON_CODES = frozenset(
    {
        "gpu_unavailable",
        "local_runtime_not_ready",
        "local_artifact_not_ready",
        "local_server_unhealthy",
        "local_runtime_state_invalid",
        "local_runtime_state_unavailable",
        "local_runtime_state_stale",
        "local_runtime_fingerprint_mismatch",
    }
)


class BrainAction(TypedDict, total=False):
    label: str
    href: str
    refresh: bool
    command: str


class BrainIdentity(TypedDict):
    lane: str | None
    provider: str | None
    model: str | None


class BrainEvidenceView(TypedDict):
    observed_at: str | None
    age_seconds: int | None
    age_text: str | None


class BrainComponentSnapshot(TypedDict):
    status: str | None
    reason_code: str | None
    reason_text: str
    observed_at: str | None


class BrainComponentsSnapshot(TypedDict):
    generate: BrainComponentSnapshot
    cogitate: BrainComponentSnapshot


class BrainSnapshot(TypedDict):
    state: BrainAggregateState
    headline: str
    reason_code: str | None
    reason_text: str
    failing_component: str | None
    action: BrainAction | None
    identity: BrainIdentity
    evidence: BrainEvidenceView
    components: BrainComponentsSnapshot
    progressing: bool


def _utc(value: datetime) -> datetime:
    if value.tzinfo is None:
        return value.replace(tzinfo=timezone.utc)
    return value.astimezone(timezone.utc)


def _parse_timestamp(value: str | None) -> datetime | None:
    if not value:
        return None
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError:
        return None
    return _utc(parsed)


def brain_age(now: datetime, observed_at: str | None) -> tuple[int | None, str | None]:
    observed = _parse_timestamp(observed_at)
    if observed is None:
        return None, None
    seconds = max(0, int((_utc(now) - observed).total_seconds()))
    if seconds < 60:
        return seconds, f"{seconds}s"
    minutes = seconds // 60
    if minutes < 60:
        return seconds, f"{minutes}m"
    hours = minutes // 60
    if hours < 48:
        return seconds, f"{hours}h"
    return seconds, f"{hours // 24}d"


def brain_reason_text(reason_code: str | None) -> str:
    if reason_code is None:
        return "ok"
    if reason_code == "thinking_engine_not_chosen":
        return "no thinking engine chosen"
    if reason_code == "configuration_invalid":
        return "configuration invalid"
    if reason_code == "stale_expected_fingerprint":
        return "stale expected fingerprint"
    if reason_code == "lost_fence":
        return "refresh fence lost"
    if reason_code == "busy":
        return "check already running"
    return reason_code.replace("_", " ")


def _component_reason(component: BrainEvidenceComponent | None) -> str | None:
    if component is None:
        return None
    reason = component.get("reason_code")
    return reason if isinstance(reason, str) else None


def _component_snapshot(
    component: BrainEvidenceComponent | None,
) -> BrainComponentSnapshot:
    if component is None:
        return {
            "status": None,
            "reason_code": None,
            "reason_text": "unknown",
            "observed_at": None,
        }
    reason = _component_reason(component)
    return {
        "status": component.get("status"),
        "reason_code": reason,
        "reason_text": brain_reason_text(reason),
        "observed_at": component.get("observed_at"),
    }


def _components(record: BrainStateRecord | None) -> BrainComponentsSnapshot:
    evidence = record["evidence"] if record is not None else {}
    return {
        "generate": _component_snapshot(evidence.get("generate")),
        "cogitate": _component_snapshot(evidence.get("cogitate")),
    }


def _evidence_view(
    record: BrainStateRecord | None,
) -> tuple[str | None, str | None]:
    if record is None:
        return None, None
    ready_component: BrainEvidenceComponent | None = None
    for component_name in COMPONENT_ORDER:
        component = record["evidence"].get(component_name)
        if component is None:
            continue
        if component["status"] != "ok":
            return component_name, component.get("observed_at")
        if ready_component is None:
            ready_component = component
    if ready_component is not None:
        return None, ready_component.get("observed_at")
    return None, None


def _component_for_reason(reason_code: str | None) -> str | None:
    if reason_code is None:
        return None
    for component_name in COMPONENT_ORDER:
        if reason_code in BRAIN_EVIDENCE_REASON_CODES[component_name]:
            return component_name
    return None


def _is_progressing(
    reason_code: str | None,
    *,
    runtime_transition_in_progress: bool,
) -> bool:
    return reason_code == "brain_check_in_progress" or (
        reason_code == "local_runtime_not_ready" and runtime_transition_in_progress
    )


def _bundled_runtime_issue(
    *,
    active_lane: str | None,
    reason_code: str | None,
    failing_component: str | None,
) -> bool:
    if active_lane != "bundled":
        return False
    if reason_code in LOCAL_RUNTIME_REASON_CODES:
        return True
    return reason_code == "probe_internal_error" and failing_component == (
        "lane_prerequisites"
    )


def _action_kind(snapshot: BrainSnapshot) -> BrainActionKind:
    state = snapshot["state"]
    if state in {"ready", "checking"}:
        return "none"
    if state == "blocked" and snapshot["progressing"]:
        return "none"
    if state in {"blocked", "unhealthy"}:
        if _bundled_runtime_issue(
            active_lane=snapshot["identity"]["lane"],
            reason_code=snapshot["reason_code"],
            failing_component=snapshot["failing_component"],
        ):
            return "open_local_setup"
        return "open_thinking"
    if state == "unknown" and snapshot["reason_code"] == "configuration_invalid":
        return "open_thinking"
    if state == "unknown":
        return "check_or_view"
    return "none"


def _resolve_action(kind: BrainActionKind, surface: BrainSurface) -> BrainAction | None:
    if kind == "none":
        return None
    if kind == "open_thinking":
        return {"label": "open thinking", "href": "/app/thinking/#main"}
    if kind == "open_local_setup":
        return {"label": "open local setup", "href": "/app/thinking/#local-setup"}
    if surface in {"thinking", "health"}:
        return {"label": "check again", "refresh": True}
    if surface in {"home", "support"}:
        return {"label": "view health", "href": "/app/health/#brain"}
    return {"label": "check again", "command": "journal brain refresh"}


def build_brain_snapshot(
    now: datetime,
    *,
    surface: BrainSurface,
    journal_path: Path | None = None,
) -> BrainSnapshot:
    now = _utc(now)
    inspection = inspect_brain_state(now, journal_path=journal_path)
    projection = inspection["projection"]
    state = projection["aggregate_state"]
    reason_code = projection["reason_code"]
    failing_component, observed_at = _evidence_view(inspection["record"])
    if failing_component is None:
        failing_component = _component_for_reason(reason_code)
    age_seconds, age_text = brain_age(now, observed_at)
    progressing = _is_progressing(
        reason_code,
        runtime_transition_in_progress=projection["runtime_transition_in_progress"],
    )
    snapshot: BrainSnapshot = {
        "state": state,
        "headline": HEADLINES[state],
        "reason_code": reason_code,
        "reason_text": brain_reason_text(reason_code),
        "failing_component": failing_component,
        "action": None,
        "identity": {
            "lane": projection["active_lane"],
            "provider": projection["active_provider"],
            "model": projection["active_model"],
        },
        "evidence": {
            "observed_at": observed_at,
            "age_seconds": age_seconds,
            "age_text": age_text,
        },
        "components": _components(inspection["record"]),
        "progressing": progressing,
    }
    snapshot["action"] = _resolve_action(_action_kind(snapshot), surface)
    return snapshot


def render_brain_health_lines(snapshot: BrainSnapshot) -> list[str]:
    lines = ["Brain Health", f"  {snapshot['headline']}"]
    identity = snapshot["identity"]
    lane = identity["lane"]
    provider = identity["provider"]
    model = identity["model"]
    component = snapshot["failing_component"]
    component_suffix = f" ({component})" if component else ""
    if lane and provider and model:
        if snapshot["state"] == "ready":
            age = snapshot["evidence"]["age_text"]
            if age:
                lines.append(f"  {lane} {provider}/{model}, checked {age} ago")
            else:
                lines.append(f"  {lane} {provider}/{model}")
        else:
            lines.append(
                f"  {lane} {provider}/{model} — "
                f"{snapshot['reason_text']}{component_suffix}"
            )
    elif lane or provider or model:
        lines.append(f"  {snapshot['reason_text']}{component_suffix}")
    action = snapshot["action"]
    if action is not None:
        target = action.get("href") or action.get("command")
        if target:
            lines.append(f"  → {action['label']}: {target}")
        else:
            lines.append(f"  → {action['label']}")
    return lines


def request_brain_refresh(*, surface: BrainSurface) -> bool:
    ref = f"brain-refresh:{surface}:{uuid.uuid4().hex}"
    try:
        return callosum_send(
            "supervisor",
            "request",
            cmd=["journal", "brain", "refresh"],
            ref=ref,
        )
    except Exception:
        logger.warning("brain refresh request failed", exc_info=True)
        return False


def brain_exit_status(state: str | None) -> Literal["ok", "warn"]:
    if state in {"ready", "checking"}:
        return "ok"
    return "warn"


__all__ = [
    "BrainAction",
    "BrainActionKind",
    "BrainComponentSnapshot",
    "BrainComponentsSnapshot",
    "BrainEvidenceView",
    "BrainIdentity",
    "BrainSnapshot",
    "BrainSurface",
    "HEADLINES",
    "LOCAL_RUNTIME_REASON_CODES",
    "brain_age",
    "brain_exit_status",
    "brain_reason_text",
    "build_brain_snapshot",
    "render_brain_health_lines",
    "request_brain_refresh",
]
