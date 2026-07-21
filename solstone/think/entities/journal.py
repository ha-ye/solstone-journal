# SPDX-License-Identifier: AGPL-3.0-only
# Copyright (c) 2026 sol pbc

"""Journal-level entity management.

Journal entities are the canonical identity records stored at:
    entities/<id>/entity.json

They contain identity fields: id, name, type, aka, is_principal, created_at, blocked.
Facet-specific data (description, timestamps) is stored in facet relationships.
"""

import json
import shutil
from collections import defaultdict
from pathlib import Path
from typing import Any

from solstone.think.entities.core import EntityDict, get_identity_names
from solstone.think.entities.history import (
    EntityOperationContext,
    iter_entity_history,
    save_entity_identity_with_history,
    trust_operation_lock,
)
from solstone.think.utils import get_journal, now_ms


def journal_entity_path(entity_id: str) -> Path:
    """Return path to journal-level entity file.

    Args:
        entity_id: Entity ID (slug)

    Returns:
        Path to entities/<id>/entity.json
    """
    return Path(get_journal()) / "entities" / entity_id / "entity.json"


def load_journal_entity(entity_id: str) -> EntityDict | None:
    """Load a journal-level entity by ID.

    Args:
        entity_id: Entity ID (slug)

    Returns:
        Entity dict with id, name, type, aka, is_principal, created_at fields,
        or None if not found.
    """
    path = journal_entity_path(entity_id)
    if not path.exists():
        return None

    try:
        with open(path, "r", encoding="utf-8") as f:
            data = json.load(f)
        # Ensure id is present
        data["id"] = entity_id

        return data
    except (json.JSONDecodeError, OSError):
        return None


def save_journal_entity(
    entity: EntityDict,
    *,
    operation: EntityOperationContext | None = None,
) -> None:
    """Save a journal-level entity and append durable identity history.

    The entity must have an 'id' field. Creates the directory if needed.

    Args:
        entity: Entity dict with id, name, type, aka (optional), is_principal (optional),
                created_at fields.
        operation: Optional explicit history operation context. Defaults to a
            regular create/update event.

    Raises:
        ValueError: If entity has no id field
    """
    save_entity_identity_with_history(entity, operation=operation)


def scan_journal_entities() -> list[str]:
    """List all entity IDs from journal-level entities.

    Scans entities/ directory for subdirectories containing entity.json.

    Returns:
        List of entity IDs (directory names)
    """
    entities_dir = Path(get_journal()) / "entities"
    if not entities_dir.exists():
        return []

    entity_ids = []
    for entry in entities_dir.iterdir():
        if entry.is_dir() and (entry / "entity.json").exists():
            entity_ids.append(entry.name)

    return sorted(entity_ids)


def load_all_journal_entities() -> dict[str, EntityDict]:
    """Load all journal-level entities.

    Returns:
        Dict mapping entity_id to entity dict
    """
    entity_ids = scan_journal_entities()
    entities = {}
    for entity_id in entity_ids:
        entity = load_journal_entity(entity_id)
        if entity:
            entities[entity_id] = entity

    return entities


def has_journal_principal() -> bool:
    """Check if any journal entity is already flagged as principal.

    Returns:
        True if a principal entity exists, False otherwise
    """
    return get_journal_principal() is not None


def get_journal_principal() -> EntityDict | None:
    """Get the principal (self) journal entity.

    Returns:
        The principal entity dict, or None if no principal exists
    """
    for entity_id in scan_journal_entities():
        entity = load_journal_entity(entity_id)
        if entity and entity.get("is_principal"):
            return entity
    return None


def _should_be_principal(name: str, aka: list[str] | None) -> bool:
    """Check if an entity should be flagged as principal based on identity config.

    Args:
        name: Entity name
        aka: Optional list of aliases

    Returns:
        True if the entity matches identity config, False otherwise
    """
    identity_names = get_identity_names()
    if not identity_names:
        return False

    # Check if name or any aka matches identity
    names_to_check = [name.lower()]
    if aka:
        names_to_check.extend(a.lower() for a in aka)

    for identity_name in identity_names:
        if identity_name.lower() in names_to_check:
            return True

    return False


def create_journal_entity(
    entity_id: str,
    name: str,
    entity_type: str,
    aka: list[str] | None = None,
    emails: list[str] | None = None,
    *,
    operation: EntityOperationContext | None = None,
    skip_principal: bool = False,
) -> EntityDict:
    """Create and persist a new journal-level entity.

    Caller must guarantee the entity does not already exist. Compose with
    `load_journal_entity` at the call site:
    `load_journal_entity(id) or create_journal_entity(id, ...)`.

    Args:
        entity_id: Entity ID (slug)
        name: Display name
        entity_type: Entity type (e.g. "Person", "Organization")
        aka: Optional list of alternate names
        emails: Optional list of email addresses (lowercased on save)
        skip_principal: If True, do not auto-flag as principal even when the
            name/aka match identity and no principal exists yet.

    Returns:
        The newly-created and persisted entity dict.
    """
    with trust_operation_lock():
        entity: EntityDict = {
            "id": entity_id,
            "name": name,
            "type": entity_type,
            "created_at": now_ms(),
        }
        if aka:
            entity["aka"] = aka
        if emails:
            entity["emails"] = [e.lower() for e in emails]

        if (
            not skip_principal
            and _should_be_principal(name, aka)
            and not has_journal_principal()
        ):
            entity["is_principal"] = True

        save_journal_entity(entity, operation=operation)
        return entity


def block_journal_entity(entity_id: str) -> dict[str, Any]:
    """Block a journal entity and detach all facet relationships.

    Sets `blocked: true` on the journal entity and `detached: true` on all
    facet relationships. This is a soft disable that hides the entity from
    active use while preserving all data.

    Args:
        entity_id: Entity ID (slug)

    Returns:
        Dict with:
            - success: True if blocked
            - facets_detached: List of facet names where relationships were detached

    Raises:
        ValueError: If entity not found or is the principal entity
    """
    # Import here to avoid circular dependency
    from solstone.think.entities.relationships import (
        load_facet_relationship,
        save_facet_relationship,
    )

    journal_entity = load_journal_entity(entity_id)
    if not journal_entity:
        raise ValueError(f"Entity '{entity_id}' not found")

    if journal_entity.get("is_principal"):
        raise ValueError("Cannot block the principal (self) entity")

    # Set blocked flag on journal entity
    journal_entity["blocked"] = True
    journal_entity["updated_at"] = now_ms()
    save_journal_entity(journal_entity)

    # Detach all facet relationships
    facets_detached = []
    facets_dir = Path(get_journal()) / "facets"
    if facets_dir.exists():
        for facet_path in facets_dir.iterdir():
            if not facet_path.is_dir():
                continue
            facet_name = facet_path.name

            relationship = load_facet_relationship(facet_name, entity_id)
            if relationship and not relationship.get("detached"):
                relationship["detached"] = True
                relationship["updated_at"] = now_ms()
                save_facet_relationship(facet_name, entity_id, relationship)
                facets_detached.append(facet_name)

    return {"success": True, "facets_detached": facets_detached}


def unblock_journal_entity(entity_id: str) -> dict[str, Any]:
    """Unblock a journal entity.

    Clears the `blocked` flag on the journal entity. Does NOT automatically
    reattach facet relationships - the user must do that manually per-facet.

    Args:
        entity_id: Entity ID (slug)

    Returns:
        Dict with:
            - success: True if unblocked

    Raises:
        ValueError: If entity not found or not blocked
    """
    journal_entity = load_journal_entity(entity_id)
    if not journal_entity:
        raise ValueError(f"Entity '{entity_id}' not found")

    if not journal_entity.get("blocked"):
        raise ValueError(f"Entity '{entity_id}' is not blocked")

    # Clear blocked flag
    journal_entity.pop("blocked", None)
    journal_entity["updated_at"] = now_ms()
    save_journal_entity(journal_entity)

    return {"success": True}


def delete_journal_entity(entity_id: str) -> dict[str, Any]:
    """Permanently delete a journal entity and all facet relationships.

    This is a destructive operation that removes:
    - The journal entity directory (entities/<id>/)
    - All facet relationship directories (facets/*/entities/<id>/)
    - All entity memory (voiceprints, observations) in those directories

    Args:
        entity_id: Entity ID (slug)

    Returns:
        Dict with:
            - success: True if deleted
            - facets_deleted: List of facet names where relationships were deleted

    Raises:
        ValueError: If entity not found or is the principal entity
    """
    with trust_operation_lock():
        journal_entity = load_journal_entity(entity_id)
        if not journal_entity:
            raise ValueError(f"Entity '{entity_id}' not found")

        if journal_entity.get("is_principal"):
            raise ValueError("Cannot delete the principal (self) entity")

        facets_deleted = []

        # Delete all facet relationship directories
        facets_dir = Path(get_journal()) / "facets"
        if facets_dir.exists():
            for facet_path in facets_dir.iterdir():
                if not facet_path.is_dir():
                    continue
                facet_name = facet_path.name

                # Check for relationship directory (contains entity.json and memory)
                rel_dir = facet_path / "entities" / entity_id
                if rel_dir.exists() and rel_dir.is_dir():
                    shutil.rmtree(rel_dir)
                    facets_deleted.append(facet_name)

        # Delete journal entity directory
        journal_dir = Path(get_journal()) / "entities" / entity_id
        if journal_dir.exists() and journal_dir.is_dir():
            shutil.rmtree(journal_dir)

        return {"success": True, "facets_deleted": facets_deleted}


def delete_created_entity_if_unreferenced(
    entity_id: str,
    *,
    operation_id: str,
    expected_identity: dict[str, Any],
    expected_history_refs: list[dict[str, Any]],
) -> dict[str, Any]:
    """Delete an identify-created entity only when no outside references remain."""
    with trust_operation_lock():
        blocked: dict[str, int] = defaultdict(int)
        current = load_journal_entity(entity_id)
        if current is None:
            return {
                "deleted": True,
                "blocked_categories": [],
                "blocked_counts": {},
            }
        if _meaningful_identity(current) != _meaningful_identity(expected_identity):
            blocked["concurrent_change"] += 1

        _check_expected_history(
            entity_id,
            operation_id=operation_id,
            expected_history_refs=expected_history_refs,
            blocked=blocked,
        )
        _check_entity_dir_contents(entity_id, blocked)
        _scan_entity_reference_surfaces(
            entity_id,
            operation_id=operation_id,
            blocked=blocked,
        )

        if blocked:
            blocked_counts = dict(sorted(blocked.items()))
            return {
                "deleted": False,
                "blocked_categories": sorted(blocked_counts),
                "blocked_counts": blocked_counts,
            }

        delete_journal_entity(entity_id)
        return {
            "deleted": True,
            "blocked_categories": [],
            "blocked_counts": {},
        }


def _meaningful_identity(entity: dict[str, Any]) -> dict[str, Any]:
    fields = ("id", "name", "type", "aka", "emails", "is_principal", "blocked")
    return {field: entity.get(field) for field in fields if field in entity}


def _check_expected_history(
    entity_id: str,
    *,
    operation_id: str,
    expected_history_refs: list[dict[str, Any]],
    blocked: dict[str, int],
) -> None:
    try:
        events = list(iter_entity_history(entity_id))
    except Exception:
        blocked["unreadable"] += 1
        return
    expected_refs = {
        (str(ref.get("version_id")), int(ref.get("seq")))
        for ref in expected_history_refs
        if ref.get("version_id") is not None and ref.get("seq") is not None
    }
    current_refs = {
        (str(event.get("version_id")), int(event.get("seq")))
        for event in events
        if event.get("version_id") is not None and event.get("seq") is not None
    }
    if len(events) != 1 or current_refs != expected_refs:
        blocked["concurrent_change"] += 1
        return
    event = events[0]
    operation = event.get("operation")
    if not isinstance(operation, dict):
        blocked["concurrent_change"] += 1
        return
    if operation.get("operation_kind") != "speaker_identify":
        blocked["concurrent_change"] += 1
    if operation.get("operation_id") != operation_id:
        blocked["concurrent_change"] += 1


def _check_entity_dir_contents(entity_id: str, blocked: dict[str, int]) -> None:
    entity_dir = Path(get_journal()) / "entities" / entity_id
    if not entity_dir.exists():
        return
    allowed_files = {entity_dir / "entity.json"}
    history_events = entity_dir / "history" / "events"
    if history_events.is_dir():
        allowed_files.update(history_events.glob("*.json"))
    for path in entity_dir.rglob("*"):
        if path.is_dir() or path.name.endswith(".lock"):
            continue
        if path not in allowed_files:
            blocked["unrecognized_file"] += 1


def _scan_entity_reference_surfaces(
    entity_id: str,
    *,
    operation_id: str,
    blocked: dict[str, int],
) -> None:
    root = Path(get_journal())
    _scan_facet_relationship_refs(root, entity_id, blocked)
    _scan_observation_refs(root, entity_id, blocked)
    _scan_activity_refs(root, entity_id, blocked)
    _scan_segment_speaker_refs(root, entity_id, operation_id, blocked)
    _scan_aka_crossrefs(root, entity_id, blocked)
    _scan_edge_refs(root, entity_id, blocked)
    _scan_jsonl_refs(
        root / "entities" / "ambiguities.jsonl",
        "ambiguity",
        entity_id,
        blocked,
        predicate=_ambiguity_refs_entity,
    )
    _scan_jsonl_refs(
        root / "entities" / "review-candidates.jsonl",
        "entity_review_candidate",
        entity_id,
        blocked,
        predicate=lambda row, eid: (
            row.get("source_slug") == eid or row.get("target_slug") == eid
        ),
    )
    _scan_jsonl_refs(
        root / "speakers" / "review-candidates.jsonl",
        "speaker_review_candidate",
        entity_id,
        blocked,
        predicate=lambda row, eid: (
            row.get("source_id") == eid or row.get("target_id") == eid
        ),
    )
    _scan_jsonl_refs(
        root / "speakers" / "candidate-pair-review-candidates.jsonl",
        "candidate_pair",
        entity_id,
        blocked,
        predicate=lambda row, eid: _json_value_present(row, eid),
    )
    _scan_speaker_candidate_refs(root, entity_id, blocked)
    _scan_keep_separate_refs(entity_id, blocked)
    _scan_jsonl_refs(
        root / "speakers" / "cluster-dismissals.jsonl",
        "dismissal",
        entity_id,
        blocked,
        predicate=lambda row, eid: _json_value_present(row, eid),
    )
    _scan_identify_operation_refs(entity_id, operation_id, blocked)


def _scan_facet_relationship_refs(
    root: Path,
    entity_id: str,
    blocked: dict[str, int],
) -> None:
    facets_dir = root / "facets"
    if not facets_dir.is_dir():
        return
    for rel_dir in facets_dir.glob(f"*/entities/{entity_id}"):
        if rel_dir.exists():
            blocked["facet_relationship"] += 1


def _scan_observation_refs(
    root: Path,
    entity_id: str,
    blocked: dict[str, int],
) -> None:
    for path in sorted((root / "facets").glob("*/entities/*/observations.jsonl")):
        if path.parent.name == entity_id:
            blocked["observation"] += 1
        _scan_jsonl_refs(
            path,
            "observation",
            entity_id,
            blocked,
            predicate=lambda row, eid: _json_key_value_present(
                row,
                eid,
                {"entity_id", "target_entity_id", "source_entity_id"},
            ),
        )


def _scan_activity_refs(root: Path, entity_id: str, blocked: dict[str, int]) -> None:
    keys = {
        "entity_id",
        "active_entities",
        "owner_entity_id",
        "counterparty_entity_id",
        "from_entity_id",
        "to_entity_id",
    }
    for path in sorted((root / "facets").glob("*/activities/*.jsonl")):
        _scan_jsonl_refs(
            path,
            "activity",
            entity_id,
            blocked,
            predicate=lambda row, eid: _json_key_value_present(row, eid, keys),
        )


def _scan_segment_speaker_refs(
    root: Path,
    entity_id: str,
    operation_id: str,
    blocked: dict[str, int],
) -> None:
    chronicle = root / "chronicle"
    for labels_path in sorted(chronicle.glob("*/*/*/talents/speaker_labels.json")):
        data = _read_json_object(labels_path, blocked)
        if data is None:
            continue
        labels = data.get("labels", [])
        if isinstance(labels, list):
            for label in labels:
                if isinstance(label, dict) and label.get("speaker") == entity_id:
                    blocked["segment_label"] += 1
    for corr_path in sorted(chronicle.glob("*/*/*/talents/speaker_corrections.json")):
        data = _read_json_object(corr_path, blocked)
        if data is None:
            continue
        corrections = data.get("corrections", [])
        if not isinstance(corrections, list):
            continue
        for row in corrections:
            if not isinstance(row, dict):
                continue
            if row.get("operation_id") == operation_id:
                continue
            if (
                row.get("original_speaker") == entity_id
                or row.get("corrected_speaker") == entity_id
            ):
                blocked["segment_correction"] += 1


def _scan_aka_crossrefs(root: Path, entity_id: str, blocked: dict[str, int]) -> None:
    for path in sorted((root / "entities").glob("*/entity.json")):
        if path.parent.name == entity_id:
            continue
        data = _read_json_object(path, blocked)
        if data is None:
            continue
        aka = data.get("aka")
        if isinstance(aka, list) and entity_id in aka:
            blocked["aka_crossref"] += 1


def _scan_edge_refs(root: Path, entity_id: str, blocked: dict[str, int]) -> None:
    if not (root / "indexer" / "journal.sqlite").is_file():
        return
    try:
        from solstone.think.indexer.edges import count_entity_edges

        count = count_entity_edges(entity_id)
    except Exception:
        blocked["unreadable"] += 1
        return
    if count:
        blocked["edge"] += int(count)


def _scan_speaker_candidate_refs(
    root: Path,
    entity_id: str,
    blocked: dict[str, int],
) -> None:
    data = _read_json_object(root / "awareness" / "speaker_candidates.json", blocked)
    if data is None:
        return
    candidates = data.get("candidates", [])
    if not isinstance(candidates, list):
        return
    for candidate in candidates:
        if (
            isinstance(candidate, dict)
            and candidate.get("confirmed_entity") == entity_id
        ):
            blocked["speaker_candidate"] += 1


def _scan_keep_separate_refs(entity_id: str, blocked: dict[str, int]) -> None:
    try:
        from solstone.think.speaker_keep_separate import fold_assertions

        for assertion in fold_assertions():
            if entity_id in (assertion.entity_id_a, assertion.entity_id_b):
                blocked["keep_separate"] += 1
    except Exception:
        blocked["unreadable"] += 1


def _scan_identify_operation_refs(
    entity_id: str,
    operation_id: str,
    blocked: dict[str, int],
) -> None:
    try:
        from solstone.think.speaker_identify_operations import fold_all_operations

        states = fold_all_operations()
    except Exception:
        blocked["unreadable"] += 1
        return
    for state in states:
        if state.operation_id == operation_id:
            continue
        if state.target_entity_id == entity_id:
            blocked["identify_operation"] += 1
            continue
        if entity_id in state.reviewed_near_match_entity_ids:
            blocked["identify_operation"] += 1
            continue
        for assertion in state.prepared_plan.get("keep_separate_assertions", []):
            if not isinstance(assertion, dict):
                continue
            if entity_id in (
                assertion.get("entity_id_a"),
                assertion.get("entity_id_b"),
                assertion.get("planned_target_entity_id"),
                assertion.get("reviewed_id"),
            ):
                blocked["identify_operation"] += 1
                break


def _scan_jsonl_refs(
    path: Path,
    category: str,
    entity_id: str,
    blocked: dict[str, int],
    *,
    predicate,
) -> None:
    if not path.is_file():
        return
    try:
        lines = path.read_text(encoding="utf-8").splitlines()
    except OSError:
        blocked["unreadable"] += 1
        return
    for line in lines:
        raw = line.strip()
        if not raw:
            continue
        try:
            row = json.loads(raw)
        except json.JSONDecodeError:
            blocked["unreadable"] += 1
            continue
        if isinstance(row, dict) and predicate(row, entity_id):
            blocked[category] += 1


def _read_json_object(path: Path, blocked: dict[str, int]) -> dict[str, Any] | None:
    if not path.is_file():
        return None
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (json.JSONDecodeError, OSError):
        blocked["unreadable"] += 1
        return None
    return data if isinstance(data, dict) else None


def _ambiguity_refs_entity(row: dict[str, Any], entity_id: str) -> bool:
    if row.get("resolved_entity_id") == entity_id:
        return True
    candidates = row.get("ranked_candidates")
    return isinstance(candidates, list) and any(
        isinstance(candidate, dict) and candidate.get("id") == entity_id
        for candidate in candidates
    )


def _json_key_value_present(value: Any, entity_id: str, keys: set[str]) -> bool:
    if isinstance(value, dict):
        for key, child in value.items():
            if key in keys:
                if child == entity_id:
                    return True
                if isinstance(child, list) and entity_id in child:
                    return True
            if _json_key_value_present(child, entity_id, keys):
                return True
    elif isinstance(value, list):
        return any(_json_key_value_present(item, entity_id, keys) for item in value)
    return False


def _json_value_present(value: Any, entity_id: str) -> bool:
    if value == entity_id:
        return True
    if isinstance(value, dict):
        return any(_json_value_present(item, entity_id) for item in value.values())
    if isinstance(value, list):
        return any(_json_value_present(item, entity_id) for item in value)
    return False


def journal_entity_memory_path(entity_id: str) -> Path:
    """Return path to journal entity's memory folder.

    Journal entity memory stores data that is identity-specific and not
    facet-scoped, such as voiceprints (voice recognition embeddings).

    Args:
        entity_id: Entity ID (slug)

    Returns:
        Path to entities/<id>/

    Raises:
        ValueError: If entity_id is empty
    """
    if not entity_id:
        raise ValueError("Entity ID cannot be empty")

    return Path(get_journal()) / "entities" / entity_id


def ensure_journal_entity_memory(entity_id: str) -> Path:
    """Create journal entity memory folder if needed, return path.

    Args:
        entity_id: Entity ID (slug)

    Returns:
        Path to the created/existing folder

    Raises:
        ValueError: If entity_id is empty
    """
    folder = journal_entity_memory_path(entity_id)
    folder.mkdir(parents=True, exist_ok=True)
    return folder
