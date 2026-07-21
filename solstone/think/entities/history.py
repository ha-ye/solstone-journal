# SPDX-License-Identifier: AGPL-3.0-only
# Copyright (c) 2026 sol pbc

"""Durable entity identity history and trust-operation locking.

Entity trust operations use one per-journal, process-reentrant lock. The fixed
global lock order is:

    trust -> facet attached-store ->
    (facet relationship / observations / voiceprints npz / activities
    locked_modify / speaker segment / speaker candidate tracker /
    speaker identify-ledger / speaker dismissal store / speaker keep-separate
    store) owner locks -> entity ambiguity store

Mutating speaker identify operations hold the trust lock across planning,
write-ahead prepare, phase execution, and commit/undo terminal events. The
identify-ledger lock is acquired only around append-only ledger writes, and
owner locks (voiceprints / attribution / tracker / sentinel / speaker stores)
are acquired only by their owner primitives inside the trust lock, never
inverted. The ambiguity-store lock is never acquired before the trust or owner
locks. Preview and read-only paths must not acquire these locks at all.

The trust lock is backed by ``journal_io.hold_lock`` at
``journal/health/locks/entity-trust``. ``hold_lock`` creates a persistent
``<name>.lock`` sidecar, so journal-tree hash checks that assert byte-neutral
preview/read behavior must exclude lock sidecars. Preview and read-only paths
must not acquire the trust lock at all.

History layout:

- ``entities/<id>/history/events/`` contains visible, append-only events.
- ``entities/<id>/history/prepared/`` contains unpublished staging records.
- ``entities/<id>/history/private/`` is reserved for merge inverse payloads.

Visible readers list only ``events/*.json`` and never inspect staging.
Prepared events are reconciled by a write verb before later mutations: publish
iff the current identity equals the event's ``identity_after`` snapshot, discard
iff it equals ``identity_before``, otherwise raise repair-required and leave the
journal unchanged.
"""

from __future__ import annotations

import copy
import json
import logging
import shutil
import threading
import uuid
from collections.abc import Iterator, Mapping
from contextlib import contextmanager
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Literal

from solstone.think.entities.core import EntityDict
from solstone.think.journal_io import (
    MalformedPolicy,
    atomic_replace,
    contained_path,
    hold_lock,
    read_json,
)
from solstone.think.utils import get_journal

logger = logging.getLogger(__name__)

HISTORY_SCHEMA_VERSION = 1
EVENT_KINDS = {"create", "update", "restore", "merge", "merge_undo"}
MERGE_EVENT_KINDS = {"merge", "merge_undo"}
RECORDING_KIND = Literal["create", "update", "restore", "merge", "merge_undo"]

_IN_PROCESS_TRUST_LOCK = threading.RLock()
_TRUST_DEPTH = 0
_TRUST_MANAGER: Any | None = None


class EntityHistoryError(RuntimeError):
    """Base error for entity history operations."""


class EntityHistoryRepairRequired(EntityHistoryError):
    """Raised when staged history cannot be safely reconciled."""


@dataclass(frozen=True)
class EntityOperationContext:
    """Explicit context for a history-bearing entity identity write."""

    kind: RECORDING_KIND
    caller: Any = None
    actor: Any = None
    metadata: Mapping[str, Any] = field(default_factory=dict)


@contextmanager
def trust_operation_lock() -> Iterator[None]:
    """Hold the reentrant per-journal trust-operation lock."""

    global _TRUST_DEPTH, _TRUST_MANAGER

    _IN_PROCESS_TRUST_LOCK.acquire()
    entered = False
    try:
        if _TRUST_DEPTH == 0:
            manager = hold_lock(_trust_lock_path())
            manager.__enter__()
            _TRUST_MANAGER = manager
        _TRUST_DEPTH += 1
        entered = True
        yield
    finally:
        if entered:
            _TRUST_DEPTH -= 1
            if _TRUST_DEPTH == 0:
                manager = _TRUST_MANAGER
                _TRUST_MANAGER = None
                if manager is not None:
                    manager.__exit__(None, None, None)
        _IN_PROCESS_TRUST_LOCK.release()


def iter_entity_history(entity_id: str) -> Iterator[dict[str, Any]]:
    """Yield visible history events for one entity in per-entity sequence order."""

    events_dir = _events_dir(entity_id)
    if not events_dir.is_dir():
        return
    for path in sorted(events_dir.glob("*.json")):
        event = read_json(path, on_error=MalformedPolicy.RAISE)
        if isinstance(event, dict):
            yield event
        else:
            raise EntityHistoryError(f"history event is not an object: {path}")


def load_entity_history_event(
    entity_id: str,
    version_id: str,
) -> dict[str, Any] | None:
    """Load a visible history event by version id without mutating history."""

    for event in iter_entity_history(entity_id):
        if event.get("version_id") == version_id:
            return event
    return None


def record_entity_merge_payload(
    entity_id: str,
    merge_id: str,
    payload: Mapping[str, Any],
) -> str:
    """Write one committed merge inverse payload and return its journal path."""

    with trust_operation_lock():
        data = copy.deepcopy(dict(payload))
        _validate_merge_payload(data)
        path = _merge_payload_path(entity_id, merge_id)
        _write_json(path, data)
        return _rel(path)


def load_entity_merge_payload(entity_id: str, merge_id: str) -> dict[str, Any]:
    """Load one committed merge inverse payload by entity and merge id."""

    path = _merge_payload_path(entity_id, merge_id)
    payload = read_json(path, on_error=MalformedPolicy.RAISE, default=None)
    if payload is None:
        raise EntityHistoryError(
            f"missing private merge payload for {entity_id}: {merge_id}"
        )
    if not isinstance(payload, dict):
        raise EntityHistoryError(f"private merge payload is not an object: {path}")
    payload = copy.deepcopy(payload)
    try:
        _validate_merge_payload(payload)
    except Exception as exc:
        raise EntityHistoryError(
            f"invalid private merge payload for {entity_id}:{merge_id}: {exc}"
        ) from exc
    return payload


def load_entity_merge_payloads(entity_id: str) -> list[dict[str, Any]]:
    """Load all active private merge payloads stored under one entity."""

    private_dir = _private_dir(entity_id)
    if not private_dir.is_dir():
        return []
    payloads = [
        load_entity_merge_payload(entity_id, path.stem)
        for path in sorted(private_dir.glob("*.json"))
    ]
    return sorted(payloads, key=lambda item: int(item.get("commit_seq") or 0))


def find_entity_merge_payload(merge_id: str) -> tuple[str, dict[str, Any]] | None:
    """Locate an active merge payload by scanning visible entity history.

    The audit log is intentionally not consulted: lineage rebasing can make the
    audit row's target stale, while visible history records move with the active
    bundle.
    """

    for entity_id in _entity_ids_with_history():
        if not _history_mentions_merge(entity_id, merge_id):
            continue
        if _merge_payload_path(entity_id, merge_id).is_file():
            return entity_id, load_entity_merge_payload(entity_id, merge_id)
    return None


def remove_entity_merge_payload(entity_id: str, merge_id: str) -> None:
    """Remove one private merge payload if it still exists."""

    with trust_operation_lock():
        path = _merge_payload_path(entity_id, merge_id)
        if path.exists():
            path.unlink()


def move_entity_merge_payload(
    source_entity_id: str,
    target_entity_id: str,
    merge_id: str,
    *,
    rebased_from_entity_id: str | None = None,
) -> tuple[dict[str, Any], str]:
    """Move/rebase one private merge payload between entity histories."""

    with trust_operation_lock():
        payload = load_entity_merge_payload(source_entity_id, merge_id)
        payload["target_id"] = target_entity_id
        if rebased_from_entity_id is not None:
            payload["rebased_from_entity_id"] = rebased_from_entity_id
        target_rel = record_entity_merge_payload(target_entity_id, merge_id, payload)
        if source_entity_id != target_entity_id:
            remove_entity_merge_payload(source_entity_id, merge_id)
        return payload, target_rel


def consolidate_prepared_history(entity_id: str) -> None:
    """Publish or discard prepared history for one entity before mutation."""

    with trust_operation_lock():
        prepared_dir = _prepared_dir(entity_id)
        if not prepared_dir.is_dir():
            return
        for event_path in sorted(prepared_dir.glob("*/event.json")):
            event = _read_history_event(event_path)
            if event.get("entity_id") != entity_id:
                raise EntityHistoryRepairRequired(
                    f"prepared history for {entity_id} contains event for "
                    f"{event.get('entity_id')}"
                )
            current = _load_identity_snapshot(entity_id)
            before = event.get("identity_before")
            after = event.get("identity_after")
            if current == after:
                _publish_prepared_event(entity_id, event)
                logger.info(
                    "published prepared entity history event %s for %s",
                    event["version_id"],
                    entity_id,
                )
            elif current == before:
                _discard_prepared_event(entity_id, event)
                logger.info(
                    "discarded prepared entity history event %s for %s",
                    event["version_id"],
                    entity_id,
                )
            else:
                raise EntityHistoryRepairRequired(
                    "prepared history for "
                    f"{entity_id} cannot be reconciled; current identity is "
                    "neither the recorded before nor after snapshot"
                )


def save_entity_identity_with_history(
    entity: EntityDict,
    *,
    operation: EntityOperationContext | None = None,
) -> None:
    """Persist one entity identity and append a durable history event."""

    entity_id = entity.get("id")
    if not entity_id:
        raise ValueError("Entity must have an 'id' field")

    with trust_operation_lock():
        consolidate_prepared_history(entity_id)
        before = _load_identity_snapshot(entity_id)
        after = _identity_snapshot(entity)
        if before == after and operation is None:
            return

        kind = _event_kind(before, operation)
        event = _build_history_event(
            entity_id=entity_id,
            kind=kind,
            before=before,
            after=after,
            operation=operation,
        )
        _prepare_history_event(entity_id, event)
        _write_identity_snapshot(entity_id, after)
        _publish_prepared_event(entity_id, event)


def restore_journal_entity_version(
    entity_id: str,
    version_id: str,
    *,
    caller: Any = None,
) -> dict[str, Any]:
    """Restore a visible identity snapshot and append a restore event.

    Generic identity restore is intentionally blocked when the requested range
    crosses a recorded merge event. Callers must use recorded-merge undo for
    those cases so inverse payloads and non-identity stores stay consistent.
    """

    with trust_operation_lock():
        consolidate_prepared_history(entity_id)
        events = list(iter_entity_history(entity_id))
        event = next(
            (
                candidate
                for candidate in events
                if candidate.get("version_id") == version_id
            ),
            None,
        )
        if event is None:
            raise EntityHistoryError(
                f"history version {version_id!r} was not found for entity {entity_id!r}"
            )
        _guard_restore_does_not_cross_merge(event, events)

        snapshot = copy.deepcopy(event.get("identity_after"))
        if not isinstance(snapshot, dict):
            raise EntityHistoryError(
                f"history version {version_id!r} has no restorable identity snapshot"
            )
        if snapshot.get("id") != entity_id:
            raise EntityHistoryError(
                "generic identity restore cannot change the entity id; "
                "use recorded-merge undo for merge-related changes"
            )
        _guard_restore_principal(snapshot, entity_id)

        save_entity_identity_with_history(
            snapshot,
            operation=EntityOperationContext(
                kind="restore",
                caller=caller,
                metadata={"restored_version_id": version_id},
            ),
        )
        restore_event = list(iter_entity_history(entity_id))[-1]
        return restore_event


def _trust_lock_path() -> Path:
    return Path(get_journal()) / "health" / "locks" / "entity-trust"


def _journal_root() -> Path:
    return Path(get_journal())


def _rel(path: Path) -> str:
    return path.resolve().relative_to(_journal_root().resolve()).as_posix()


def _entities_dir() -> Path:
    return _journal_root() / "entities"


def _entity_dir(entity_id: str) -> Path:
    return _entities_dir() / entity_id


def _identity_path(entity_id: str) -> Path:
    return _entity_dir(entity_id) / "entity.json"


def _history_dir(entity_id: str) -> Path:
    return _entity_dir(entity_id) / "history"


def _events_dir(entity_id: str) -> Path:
    return _history_dir(entity_id) / "events"


def _prepared_dir(entity_id: str) -> Path:
    return _history_dir(entity_id) / "prepared"


def _private_dir(entity_id: str) -> Path:
    return _history_dir(entity_id) / "private"


def _merge_payload_path(entity_id: str, merge_id: str) -> Path:
    if "/" in merge_id or "\\" in merge_id or ".." in Path(merge_id).parts:
        raise EntityHistoryError(f"invalid merge id for private payload: {merge_id!r}")
    return _private_dir(entity_id) / f"{merge_id}.json"


def _entity_ids_with_history() -> list[str]:
    entities_dir = _entities_dir()
    if not entities_dir.is_dir():
        return []
    return sorted(
        path.name for path in entities_dir.iterdir() if (path / "history").is_dir()
    )


def _history_mentions_merge(entity_id: str, merge_id: str) -> bool:
    for event in iter_entity_history(entity_id):
        operation = event.get("operation")
        if not isinstance(operation, dict):
            continue
        if operation.get("merge_id") == merge_id:
            return True
    return False


def _validate_merge_payload(payload: Mapping[str, Any]) -> None:
    journal = _journal_root()
    source_id = payload.get("source_id")
    target_id = payload.get("target_id")
    if not isinstance(source_id, str) or not source_id:
        raise EntityHistoryError("merge payload missing source entity id")
    if not isinstance(target_id, str) or not target_id:
        raise EntityHistoryError("merge payload missing target entity id")
    contained_path(journal, (Path("entities") / source_id).as_posix())
    contained_path(journal, (Path("entities") / target_id).as_posix())

    if "source_state" not in payload:
        raise EntityHistoryError("merge payload missing source_state")
    source_state = payload["source_state"]
    if not isinstance(source_state, Mapping):
        raise EntityHistoryError("merge payload source_state is not an object")
    if "snapshots" not in source_state:
        raise EntityHistoryError("merge payload missing snapshots")
    snapshots = source_state["snapshots"]
    if not isinstance(snapshots, list):
        raise EntityHistoryError("merge payload snapshots is not a list")
    for snapshot in snapshots:
        if not isinstance(snapshot, Mapping):
            raise EntityHistoryError("merge payload snapshot is not an object")
        rel = snapshot.get("rel")
        if not isinstance(rel, str):
            raise EntityHistoryError("manifest snapshot missing relative path")
        contained_path(journal, rel)
        files = snapshot.get("files", [])
        if not isinstance(files, list):
            raise EntityHistoryError("manifest snapshot files is not a list")
        for item in files:
            if not isinstance(item, Mapping):
                raise EntityHistoryError("manifest snapshot file is not an object")
            item_rel = item.get("rel")
            if not isinstance(item_rel, str):
                raise EntityHistoryError("manifest snapshot file missing relative path")
            contained_path(journal, (Path(rel) / item_rel).as_posix())

    if "manifest" not in payload:
        raise EntityHistoryError("merge payload missing manifest")
    manifest = payload["manifest"]
    if not isinstance(manifest, Mapping):
        raise EntityHistoryError("merge payload manifest is not an object")
    if "identity" not in manifest:
        raise EntityHistoryError("merge payload missing identity manifest")
    identity = manifest["identity"]
    if not isinstance(identity, Mapping):
        raise EntityHistoryError("merge payload identity manifest is not an object")
    for support_field in ("aka_support", "email_support", "scalar_support"):
        if support_field not in identity:
            raise EntityHistoryError(f"merge payload identity missing {support_field}")
        if not isinstance(identity[support_field], list):
            raise EntityHistoryError(
                f"merge payload identity {support_field} is not a list"
            )
    if "target_before" not in identity:
        raise EntityHistoryError("merge payload identity missing target_before")
    if not isinstance(identity["target_before"], Mapping):
        raise EntityHistoryError(
            "merge payload identity target_before is not an object"
        )
    if "voiceprints" not in manifest:
        raise EntityHistoryError("merge payload missing voiceprints manifest")
    voiceprints = manifest["voiceprints"]
    if not isinstance(voiceprints, Mapping):
        raise EntityHistoryError("merge payload voiceprints manifest is not an object")
    if "support" not in voiceprints:
        raise EntityHistoryError("merge payload voiceprints missing support")
    if not isinstance(voiceprints["support"], list):
        raise EntityHistoryError("merge payload voiceprints support is not a list")
    if "facets" not in manifest:
        raise EntityHistoryError("merge payload missing facets manifest")
    facets = manifest["facets"]
    if not isinstance(facets, Mapping):
        raise EntityHistoryError("merge payload facets manifest is not an object")
    if "entries" not in facets:
        raise EntityHistoryError("merge payload facets missing entries")
    facet_entries = facets["entries"]
    if not isinstance(facet_entries, list):
        raise EntityHistoryError("merge payload facet entries is not a list")
    for entry in facet_entries:
        if not isinstance(entry, Mapping):
            raise EntityHistoryError("merge payload facet entry is not an object")
        facet = entry.get("facet")
        if not isinstance(facet, str) or not facet:
            raise EntityHistoryError("manifest facet entry missing facet name")
        contained_path(
            journal,
            (Path("facets") / facet / "entities" / target_id).as_posix(),
        )
    for section in ("segments", "activities", "observation_relations"):
        if section not in manifest:
            raise EntityHistoryError(f"merge payload missing {section} manifest")
        section_manifest = manifest[section]
        if not isinstance(section_manifest, Mapping):
            raise EntityHistoryError(
                f"merge payload {section} manifest is not an object"
            )
        if "entries" not in section_manifest:
            raise EntityHistoryError(f"merge payload {section} missing entries")
        entries = section_manifest["entries"]
        if not isinstance(entries, list):
            raise EntityHistoryError(f"merge payload {section} entries is not a list")
        for entry in entries:
            if not isinstance(entry, Mapping):
                raise EntityHistoryError(
                    f"merge payload {section} entry is not an object"
                )
            rel = entry.get("path")
            if not isinstance(rel, str):
                raise EntityHistoryError(f"manifest {section} entry missing path")
            contained_path(journal, rel)
    if "rebased_merge_ids" not in manifest:
        raise EntityHistoryError("merge payload missing rebased_merge_ids")
    if not isinstance(manifest["rebased_merge_ids"], list):
        raise EntityHistoryError("merge payload rebased_merge_ids is not a list")


def _identity_snapshot(entity: Mapping[str, Any] | None) -> dict[str, Any] | None:
    if entity is None:
        return None
    return copy.deepcopy(dict(entity))


def _load_identity_snapshot(entity_id: str) -> dict[str, Any] | None:
    path = _identity_path(entity_id)
    data = read_json(path, on_error=MalformedPolicy.RAISE, default=None)
    if data is None:
        return None
    if not isinstance(data, dict):
        raise EntityHistoryError(f"entity identity is not an object: {path}")
    data = copy.deepcopy(data)
    data["id"] = entity_id
    return data


def _write_identity_snapshot(entity_id: str, entity: Mapping[str, Any]) -> None:
    atomic_replace(
        _identity_path(entity_id),
        json.dumps(entity, ensure_ascii=False, indent=2) + "\n",
    )


def _build_history_event(
    *,
    entity_id: str,
    kind: str,
    before: dict[str, Any] | None,
    after: dict[str, Any],
    operation: EntityOperationContext | None,
) -> dict[str, Any]:
    version_id = f"vh_{uuid.uuid4().hex}"
    metadata = dict(operation.metadata) if operation else {}
    return {
        "schema_version": HISTORY_SCHEMA_VERSION,
        "version_id": version_id,
        "seq": _next_entity_seq(entity_id),
        "ts": _now_iso(),
        "entity_id": entity_id,
        "kind": kind,
        "caller": operation.caller if operation else None,
        "actor": operation.actor if operation else None,
        "identity_before": copy.deepcopy(before),
        "identity_after": copy.deepcopy(after),
        "operation": metadata,
    }


def _event_kind(
    before: dict[str, Any] | None,
    operation: EntityOperationContext | None,
) -> str:
    if operation is None:
        return "create" if before is None else "update"
    if operation.kind not in EVENT_KINDS:
        raise ValueError(f"unknown entity history operation kind: {operation.kind}")
    return operation.kind


def _next_entity_seq(entity_id: str) -> int:
    max_seq = 0
    for event in iter_entity_history(entity_id):
        seq = event.get("seq")
        if not isinstance(seq, int):
            raise EntityHistoryError(
                f"history event for {entity_id} has non-integer seq: {seq!r}"
            )
        max_seq = max(max_seq, seq)
    return max_seq + 1


def _prepare_history_event(entity_id: str, event: Mapping[str, Any]) -> None:
    version_id = _require_version_id(event)
    path = _prepared_event_path(entity_id, version_id)
    _write_json(path, event)
    _private_dir(entity_id).mkdir(parents=True, exist_ok=True)


def _publish_prepared_event(entity_id: str, event: Mapping[str, Any]) -> None:
    final_path = _visible_event_path(entity_id, event)
    existing = read_json(final_path, on_error=MalformedPolicy.RAISE, default=None)
    if existing is not None:
        if existing != dict(event):
            raise EntityHistoryRepairRequired(
                f"visible history event collision for {entity_id}: {final_path.name}"
            )
    else:
        _write_json(final_path, event)
    _discard_prepared_event(entity_id, event)


def _discard_prepared_event(entity_id: str, event: Mapping[str, Any]) -> None:
    version_id = _require_version_id(event)
    event_dir = _prepared_event_path(entity_id, version_id).parent
    if event_dir.exists():
        shutil.rmtree(event_dir)


def _prepared_event_path(entity_id: str, version_id: str) -> Path:
    return _prepared_dir(entity_id) / version_id / "event.json"


def _visible_event_path(entity_id: str, event: Mapping[str, Any]) -> Path:
    seq = event.get("seq")
    if not isinstance(seq, int):
        raise EntityHistoryError("history event seq must be an integer")
    version_id = _require_version_id(event)
    return _events_dir(entity_id) / f"{seq:020d}-{version_id}.json"


def _read_history_event(path: Path) -> dict[str, Any]:
    event = read_json(path, on_error=MalformedPolicy.RAISE)
    if not isinstance(event, dict):
        raise EntityHistoryError(f"history event is not an object: {path}")
    return event


def _require_version_id(event: Mapping[str, Any]) -> str:
    version_id = event.get("version_id")
    if not isinstance(version_id, str) or not version_id.startswith("vh_"):
        raise EntityHistoryError("history event has an invalid version_id")
    return version_id


def _write_json(path: Path, obj: Mapping[str, Any]) -> None:
    atomic_replace(
        path,
        json.dumps(obj, ensure_ascii=False, indent=2, sort_keys=True) + "\n",
    )


def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat().replace("+00:00", "Z")


def _guard_restore_does_not_cross_merge(
    target_event: Mapping[str, Any],
    events: list[dict[str, Any]],
) -> None:
    target_seq = target_event.get("seq")
    if not isinstance(target_seq, int):
        raise EntityHistoryError("target history event has invalid seq")
    if target_event.get("kind") in MERGE_EVENT_KINDS:
        raise EntityHistoryError(
            "generic identity restore cannot target a recorded merge event; "
            "use recorded-merge undo instead"
        )
    for event in events:
        seq = event.get("seq")
        if (
            isinstance(seq, int)
            and seq > target_seq
            and event.get("kind") in MERGE_EVENT_KINDS
        ):
            raise EntityHistoryError(
                "generic identity restore cannot cross a recorded merge event; "
                "use recorded-merge undo instead"
            )


def _guard_restore_principal(snapshot: Mapping[str, Any], entity_id: str) -> None:
    if not snapshot.get("is_principal"):
        return
    entities_dir = Path(get_journal()) / "entities"
    if not entities_dir.is_dir():
        return
    for path in sorted(entities_dir.glob("*/entity.json")):
        other_id = path.parent.name
        if other_id == entity_id:
            continue
        other = read_json(path, on_error=MalformedPolicy.RAISE, default=None)
        if isinstance(other, dict) and other.get("is_principal"):
            raise EntityHistoryError(
                "generic identity restore cannot create a second principal entity; "
                "use recorded-merge undo for merge-related identity changes"
            )
