# SPDX-License-Identifier: AGPL-3.0-only
# Copyright (c) 2026 sol pbc
"""Append-only speaker identify operation ledger.

Sole write-owner of:
  journal/speakers/identify-operations.jsonl
"""

from __future__ import annotations

import hashlib
import json
import logging
from dataclasses import dataclass
from dataclasses import field as dataclass_field
from pathlib import Path
from typing import Any

from solstone.think.entities.history import trust_operation_lock
from solstone.think.journal_io import append_jsonl, hold_lock
from solstone.think.utils import get_journal

logger = logging.getLogger(__name__)

IDENTIFY_OPERATION_SCHEMA_VERSION = 1

EVENT_KINDS = {
    "prepared",
    "checkpoint",
    "committed",
    "repair_required",
    "undo_prepared",
    "undo_checkpoint",
    "undo_committed",
    "undo_repair_required",
}
FORWARD_PHASE_ORDER = (
    "entity",
    "keep_separate",
    "direct_voiceprints",
    "corrections",
    "labels",
    "retro_tracker",
    "sentinel",
)
UNDO_PHASE_ORDER = (
    "labels",
    "corrections",
    "voiceprints",
    "tracker",
    "sentinel",
    "entity",
)


class IdentifyOperationError(RuntimeError):
    """Raised when the identify operation ledger cannot be read or written."""


@dataclass(frozen=True)
class OperationState:
    """Folded state for one identify operation."""

    operation_id: str
    request_id: str
    request_fingerprint: str
    cluster_member_set: frozenset[tuple[str, str, str, str, int]]
    target_entity_id: str | None
    target_entity_name: str | None
    will_create: bool
    entity_type: str | None
    reviewed_near_match_entity_ids: tuple[str, ...]
    completed_phases: tuple[str, ...]
    pending_phases: tuple[str, ...]
    terminal_status: str
    result: dict[str, Any] | None
    undo_report: dict[str, Any] | None
    phase_checkpoints: dict[str, dict[str, Any]]
    prepared_plan: dict[str, Any]
    repair_required: dict[str, Any] | None = None
    undo_repair_required: dict[str, Any] | None = None
    undo_phase_checkpoints: dict[str, dict[str, Any]] = dataclass_field(
        default_factory=dict
    )


def identify_operations_dir() -> Path:
    """Return the speaker operation-ledger directory, creating it if needed."""
    path = Path(get_journal()) / "speakers"
    path.mkdir(parents=True, exist_ok=True)
    return path


def identify_operations_path() -> Path:
    """Return the speaker identify operation ledger path."""
    return identify_operations_dir() / "identify-operations.jsonl"


def operation_id_for_request(request_id: str) -> str:
    """Return the deterministic public operation id for a caller request id."""
    if not isinstance(request_id, str) or not request_id:
        raise ValueError("request_id must be a non-empty string")
    digest = hashlib.sha256(request_id.encode("utf-8")).hexdigest()
    return f"idop_{digest[:24]}"


def request_fingerprint(
    *,
    cluster_members: list[dict[str, Any]],
    target_entity_id: str,
    will_create: bool,
    entity_type: str,
    reviewed_near_match_entity_ids: list[str] | tuple[str, ...] | set[str],
) -> str:
    """Hash the immutable identity of an identify request."""
    payload = {
        "cluster_members": [
            list(member)
            for member in sorted(_member_tuple(item) for item in cluster_members)
        ],
        "target_entity_id": str(target_entity_id),
        "will_create": bool(will_create),
        "entity_type": str(entity_type),
        "reviewed_near_match_entity_ids": sorted(
            {str(item) for item in reviewed_near_match_entity_ids}
        ),
    }
    encoded = json.dumps(payload, sort_keys=True, separators=(",", ":"))
    return hashlib.sha256(encoded.encode("utf-8")).hexdigest()


def append_event(event: dict[str, Any]) -> None:
    """Strict-validate and append one identify ledger event."""
    _validate_row(event)
    path = identify_operations_path()
    with trust_operation_lock():
        with hold_lock(path):
            append_jsonl(path, event)


def load_operations() -> list[dict[str, Any]]:
    """Strict-load raw identify ledger events."""
    return _load_jsonl_rows(identify_operations_path())


def fold_operation(operation_id: str) -> OperationState | None:
    """Fold events for one operation id into current operation state."""
    events = [
        event for event in load_operations() if event["operation_id"] == operation_id
    ]
    if not events:
        return None
    return _fold_events(events)


def fold_all_operations() -> list[OperationState]:
    """Fold all operation ids in the identify ledger."""
    grouped: dict[str, list[dict[str, Any]]] = {}
    for event in load_operations():
        grouped.setdefault(event["operation_id"], []).append(event)
    return [
        _fold_events(events)
        for _operation_id, events in sorted(grouped.items(), key=lambda item: item[0])
    ]


def _load_jsonl_rows(path: Path) -> list[dict[str, Any]]:
    if not path.exists():
        return []

    rows: list[dict[str, Any]] = []
    try:
        with open(path, encoding="utf-8") as handle:
            for lineno, line in enumerate(handle, start=1):
                raw = line.strip()
                if not raw:
                    continue
                try:
                    row = json.loads(raw)
                except json.JSONDecodeError as exc:
                    message = f"malformed identify operation JSONL at {path}:{lineno}"
                    logger.error(message)
                    raise IdentifyOperationError(message) from exc
                if not isinstance(row, dict):
                    message = f"non-object identify operation JSONL at {path}:{lineno}"
                    logger.error(message)
                    raise IdentifyOperationError(message)
                try:
                    _validate_row(row)
                except IdentifyOperationError as exc:
                    message = (
                        f"invalid identify operation row at {path}:{lineno}: {exc}"
                    )
                    logger.error(message)
                    raise IdentifyOperationError(message) from exc
                rows.append(row)
    except OSError as exc:
        message = f"failed to read identify operation ledger {path}: {exc}"
        logger.error(message)
        raise IdentifyOperationError(message) from exc
    return rows


def _validate_row(row: dict[str, Any]) -> None:
    if row.get("schema_version") != IDENTIFY_OPERATION_SCHEMA_VERSION:
        raise IdentifyOperationError("invalid schema_version")
    event_kind = _required_str(row, "event_kind")
    if event_kind not in EVENT_KINDS:
        raise IdentifyOperationError(f"unknown event_kind: {event_kind}")

    _required_str(row, "event_id")
    _required_str(row, "operation_id")
    _required_str(row, "request_id")
    _required_str(row, "ts")
    _required_str(row, "caller")
    if "actor" not in row:
        raise IdentifyOperationError("missing actor")
    actor = row["actor"]
    if actor is not None and not isinstance(actor, str):
        raise IdentifyOperationError("actor must be a string or null")

    if event_kind == "prepared":
        _validate_prepared(row)
    elif event_kind == "checkpoint":
        phase = _required_str(row, "phase")
        if phase not in FORWARD_PHASE_ORDER:
            raise IdentifyOperationError(f"invalid checkpoint phase: {phase}")
        _validate_checkpoint(phase, _required_dict(row, "checkpoint"))
    elif event_kind == "committed":
        _required_dict(row, "result")
    elif event_kind == "repair_required":
        _validate_repair(row, undo=False)
    elif event_kind == "undo_prepared":
        _required_str(row, "undo_started_at")
    elif event_kind == "undo_checkpoint":
        phase = _required_str(row, "phase")
        if phase not in UNDO_PHASE_ORDER:
            raise IdentifyOperationError(f"invalid undo checkpoint phase: {phase}")
        _required_dict(row, "undo_report_delta")
    elif event_kind == "undo_committed":
        _required_dict(row, "undo_report")
    elif event_kind == "undo_repair_required":
        _validate_repair(row, undo=True)


def _validate_prepared(row: dict[str, Any]) -> None:
    fingerprint = _required_str(row, "request_fingerprint")
    if len(fingerprint) != 64:
        raise IdentifyOperationError("request_fingerprint must be a sha256 hex digest")
    plan = _required_dict(row, "prepared_plan")
    if plan.get("plan_schema_version") != 1:
        raise IdentifyOperationError("prepared_plan.plan_schema_version must be 1")
    if plan.get("operation_id") != row["operation_id"]:
        raise IdentifyOperationError("prepared_plan operation_id mismatch")
    if plan.get("request_id") != row["request_id"]:
        raise IdentifyOperationError("prepared_plan request_id mismatch")

    for field in (
        "planned_at",
        "request",
        "cluster",
        "target",
        "entity_identity",
        "direct_voiceprints",
        "segments",
        "retro_confirm",
        "sentinel",
        "keep_separate_assertions",
    ):
        if field not in plan:
            raise IdentifyOperationError(f"prepared_plan missing {field}")

    request = _required_dict(plan, "request")
    for field in (
        "cluster_id",
        "name",
        "entity_id",
        "resolve_only",
        "create_new",
        "entity_type",
        "reviewed_near_match_entity_ids",
    ):
        if field not in request:
            raise IdentifyOperationError(f"prepared_plan.request missing {field}")

    cluster = _required_dict(plan, "cluster")
    members = _required_list(cluster, "members")
    if int(cluster.get("member_count", -1)) != len(members):
        raise IdentifyOperationError("prepared_plan cluster member_count mismatch")
    for member in members:
        if not isinstance(member, dict):
            raise IdentifyOperationError("prepared_plan cluster member is not object")
        _member_tuple(member)

    target = _required_dict(plan, "target")
    _required_str(target, "entity_id")
    _required_str(target, "entity_name")
    if not isinstance(target.get("will_create"), bool):
        raise IdentifyOperationError("prepared_plan target.will_create must be bool")

    _required_dict(plan, "entity_identity")
    _required_dict(plan, "direct_voiceprints")
    _required_list(plan, "segments")
    _required_dict(plan, "retro_confirm")
    _required_dict(plan, "sentinel")
    _required_list(plan, "keep_separate_assertions")


def _validate_checkpoint(phase: str, checkpoint: dict[str, Any]) -> None:
    if checkpoint.get("phase_status") != "complete":
        raise IdentifyOperationError("checkpoint.phase_status must be complete")
    _required_str(checkpoint, "completed_at")
    _required_dict(checkpoint, "counts")
    _required_dict(checkpoint, "skipped_reasons")
    if phase == "entity":
        _required_str(checkpoint, "entity_id")
        _required_bool(checkpoint, "entity_created")
        _required_str(checkpoint, "identity_after_hash")
        _required_list(checkpoint, "history_event_refs")
    elif phase == "keep_separate":
        _required_list(checkpoint, "pair_keys")
        _required_int(checkpoint, "recorded_count")
        _required_int(checkpoint, "already_present_count")
    elif phase == "direct_voiceprints":
        _required_list(checkpoint, "saved_keys")
        _required_int(checkpoint, "saved_count")
        _required_int(checkpoint, "skipped_existing_count")
    elif phase == "corrections":
        _required_list(checkpoint, "appended_keys")
        _required_int(checkpoint, "appended_count")
        _required_int(checkpoint, "skipped_existing_count")
        _required_int(checkpoint, "segment_count")
    elif phase == "labels":
        _required_list(checkpoint, "patched_sentence_keys")
        _required_list(checkpoint, "inserted_sentence_keys")
        _required_int(checkpoint, "patched_count")
        _required_int(checkpoint, "inserted_count")
        _required_int(checkpoint, "skipped_already_intended_count")
        _required_int(checkpoint, "segment_count")
    elif phase == "retro_tracker":
        _required_bool(checkpoint, "matched")
        if checkpoint.get("candidate_id") is not None and not isinstance(
            checkpoint.get("candidate_id"), int
        ):
            raise IdentifyOperationError("retro checkpoint candidate_id invalid")
        _required_list(checkpoint, "saved_keys")
        _required_int(checkpoint, "voiceprints_saved_count")
        _required_int(checkpoint, "voiceprints_skipped_existing_count")
        _required_bool(checkpoint, "tracker_updated")
    elif phase == "sentinel":
        _required_str(checkpoint, "cluster_key")
        _required_bool(checkpoint, "written")


def _validate_repair(row: dict[str, Any], *, undo: bool) -> None:
    phase = _required_str(row, "phase")
    valid_phases = UNDO_PHASE_ORDER if undo else FORWARD_PHASE_ORDER
    if phase not in valid_phases:
        raise IdentifyOperationError(f"invalid repair phase: {phase}")
    _required_str(row, "repair_code")
    _required_dict(row, "repair_categories")
    _required_dict(row, "undo_report" if undo else "partial_report")


def _fold_events(events: list[dict[str, Any]]) -> OperationState:
    deduped = _dedupe_events(events)
    prepared_events = [event for event in deduped if event["event_kind"] == "prepared"]
    if len(prepared_events) != 1:
        raise IdentifyOperationError("operation must have exactly one prepared event")
    prepared = prepared_events[0]
    plan = prepared["prepared_plan"]

    phase_checkpoints: dict[str, dict[str, Any]] = {}
    for event in deduped:
        if event["event_kind"] != "checkpoint":
            continue
        phase = event["phase"]
        if (
            phase in phase_checkpoints
            and phase_checkpoints[phase] != event["checkpoint"]
        ):
            raise IdentifyOperationError(f"conflicting checkpoint for phase {phase}")
        phase_checkpoints[phase] = event["checkpoint"]

    undo_phase_checkpoints: dict[str, dict[str, Any]] = {}
    for event in deduped:
        if event["event_kind"] != "undo_checkpoint":
            continue
        phase = event["phase"]
        if (
            phase in undo_phase_checkpoints
            and undo_phase_checkpoints[phase] != event["undo_report_delta"]
        ):
            raise IdentifyOperationError(
                f"conflicting undo checkpoint for phase {phase}"
            )
        undo_phase_checkpoints[phase] = event["undo_report_delta"]

    completed = tuple(
        phase for phase in FORWARD_PHASE_ORDER if phase in phase_checkpoints
    )
    terminal = _terminal_status(deduped)
    pending = _pending_phases(terminal, completed, deduped)
    request = plan["request"]
    target = plan["target"]
    entity_type = (
        str(request.get("entity_type")) if request.get("entity_type") else None
    )
    return OperationState(
        operation_id=prepared["operation_id"],
        request_id=prepared["request_id"],
        request_fingerprint=prepared["request_fingerprint"],
        cluster_member_set=frozenset(
            _member_tuple(member) for member in plan["cluster"]["members"]
        ),
        target_entity_id=target.get("entity_id"),
        target_entity_name=target.get("entity_name"),
        will_create=bool(target.get("will_create")),
        entity_type=entity_type,
        reviewed_near_match_entity_ids=tuple(
            str(item) for item in request.get("reviewed_near_match_entity_ids", [])
        ),
        completed_phases=completed,
        pending_phases=pending,
        terminal_status=terminal,
        result=_last_payload(deduped, "committed", "result"),
        undo_report=_last_payload(deduped, "undo_committed", "undo_report"),
        phase_checkpoints=phase_checkpoints,
        prepared_plan=plan,
        repair_required=_last_event(deduped, "repair_required"),
        undo_repair_required=_last_event(deduped, "undo_repair_required"),
        undo_phase_checkpoints=undo_phase_checkpoints,
    )


def _dedupe_events(events: list[dict[str, Any]]) -> list[dict[str, Any]]:
    by_id: dict[str, dict[str, Any]] = {}
    ordered: list[dict[str, Any]] = []
    for event in events:
        event_id = event["event_id"]
        existing = by_id.get(event_id)
        if existing is None:
            by_id[event_id] = event
            ordered.append(event)
            continue
        if existing != event:
            raise IdentifyOperationError(f"conflicting duplicate event_id {event_id}")
    return ordered


def _terminal_status(events: list[dict[str, Any]]) -> str:
    kinds = [event["event_kind"] for event in events]
    if "undo_repair_required" in kinds:
        return "undo_repair_required"
    if "undo_committed" in kinds:
        return "undone"
    if "repair_required" in kinds:
        return "repair_required"
    if "committed" in kinds:
        return "committed"
    return "in_progress"


def _pending_phases(
    terminal: str,
    completed: tuple[str, ...],
    events: list[dict[str, Any]],
) -> tuple[str, ...]:
    if terminal == "in_progress":
        completed_set = set(completed)
        return tuple(
            phase for phase in FORWARD_PHASE_ORDER if phase not in completed_set
        )
    if terminal == "repair_required":
        repair = _last_event(events, "repair_required")
        if repair:
            report = repair.get("partial_report", {})
            pending = report.get("pending_phases")
            if isinstance(pending, list):
                return tuple(str(phase) for phase in pending)
    return ()


def _last_event(events: list[dict[str, Any]], kind: str) -> dict[str, Any] | None:
    for event in reversed(events):
        if event["event_kind"] == kind:
            return event
    return None


def _last_payload(
    events: list[dict[str, Any]], kind: str, field: str
) -> dict[str, Any] | None:
    event = _last_event(events, kind)
    if event is None:
        return None
    payload = event.get(field)
    return payload if isinstance(payload, dict) else None


def _member_tuple(member: dict[str, Any]) -> tuple[str, str, str, str, int]:
    try:
        return (
            str(member["day"]),
            str(member["stream"]),
            str(member["segment_key"]),
            str(member["source"]),
            int(member["sentence_id"]),
        )
    except (KeyError, TypeError, ValueError) as exc:
        raise IdentifyOperationError("invalid cluster member provenance") from exc


def _required_str(row: dict[str, Any], field: str) -> str:
    value = row.get(field)
    if not isinstance(value, str) or not value:
        raise IdentifyOperationError(f"missing or invalid {field}")
    return value


def _required_dict(row: dict[str, Any], field: str) -> dict[str, Any]:
    value = row.get(field)
    if not isinstance(value, dict):
        raise IdentifyOperationError(f"missing or invalid {field}")
    return value


def _required_list(row: dict[str, Any], field: str) -> list[Any]:
    value = row.get(field)
    if not isinstance(value, list):
        raise IdentifyOperationError(f"missing or invalid {field}")
    return value


def _required_int(row: dict[str, Any], field: str) -> int:
    value = row.get(field)
    if not isinstance(value, int):
        raise IdentifyOperationError(f"missing or invalid {field}")
    return value


def _required_bool(row: dict[str, Any], field: str) -> bool:
    value = row.get(field)
    if not isinstance(value, bool):
        raise IdentifyOperationError(f"missing or invalid {field}")
    return value
