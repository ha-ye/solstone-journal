# SPDX-License-Identifier: AGPL-3.0-only
# Copyright (c) 2026 sol pbc

"""Entity saving functions.

This module handles saving entities to storage:
- save_entities: Save attached or detected entities for a facet
- save_detected_entity: Concurrency-safe single entity detection with file locking
"""

import json
import logging
import random
import time
from collections.abc import Callable
from pathlib import Path
from typing import TypeVar

from solstone.think.entities.core import EntityDict, entity_slug
from solstone.think.entities.errors import (
    AkaConflictError,
    EntityBlockedError,
    EntityExistsError,
    EntityNotFoundError,
    EntityWriteError,
)
from solstone.think.entities.journal import (
    create_journal_entity,
    load_journal_entity,
    save_journal_entity,
)
from solstone.think.entities.loading import (
    detected_entities_path,
    load_entities,
)
from solstone.think.entities.matching import validate_aka_uniqueness
from solstone.think.entities.relationships import (
    load_facet_relationship,
    rename_entity_memory,
    save_facet_relationship,
)
from solstone.think.journal_io import atomic_replace, hold_lock
from solstone.think.utils import get_journal, now_ms

logger = logging.getLogger(__name__)
T = TypeVar("T")


def _save_entities_detected(facet: str, entities: list[EntityDict], day: str) -> None:
    """Save detected entities to day-specific JSONL file."""
    path = detected_entities_path(facet, day)

    # Ensure id field is present
    for entity in entities:
        name = entity.get("name", "")
        expected_id = entity_slug(name)
        if entity.get("id") != expected_id:
            entity["id"] = expected_id

    # Sort by type, then name for consistency
    sorted_entities = sorted(
        entities, key=lambda e: (e.get("type", ""), e.get("name", ""))
    )

    # Format as JSONL and write atomically
    content = "".join(json.dumps(e, ensure_ascii=False) + "\n" for e in sorted_entities)
    atomic_replace(path, content)


def _save_entities_attached(facet: str, entities: list[EntityDict]) -> None:
    """Save attached entities to new structure (journal entities + facet relationships)."""
    # Validate uniqueness
    seen_names: set[str] = set()
    seen_ids: set[str] = set()

    for entity in entities:
        name = entity.get("name", "")
        expected_id = entity_slug(name)

        # Set or update id
        if entity.get("id") != expected_id:
            entity["id"] = expected_id

        name_lower = name.lower()
        if name_lower in seen_names:
            raise ValueError(f"Duplicate entity name '{name}' in facet '{facet}'")
        seen_names.add(name_lower)

        if expected_id in seen_ids:
            raise ValueError(
                f"Duplicate entity id '{expected_id}' in facet '{facet}' "
                f"(names may slugify to same value)"
            )
        seen_ids.add(expected_id)

    # Fields that belong to journal entity (identity)
    journal_fields = {
        "id",
        "name",
        "type",
        "aka",
        "is_principal",
        "created_at",
        "emails",
    }

    # Process each entity
    for entity in entities:
        entity_id = entity["id"]
        name = entity.get("name", "")
        entity_type = entity.get("type", "")
        aka = entity.get("aka")
        is_detached = entity.get("detached", False)

        # Load existing journal entity, or create one. Skip principal
        # flagging for detached entities.
        journal_entity = load_journal_entity(entity_id) or create_journal_entity(
            entity_id=entity_id,
            name=name,
            entity_type=entity_type,
            aka=aka if isinstance(aka, list) else None,
            skip_principal=is_detached,
        )

        # Update journal entity if name/type/aka changed
        journal_updated = False
        if journal_entity.get("name") != name:
            journal_entity["name"] = name
            journal_updated = True
        if journal_entity.get("type") != entity_type:
            journal_entity["type"] = entity_type
            journal_updated = True
        if aka and isinstance(aka, list):
            # Merge aka lists (union)
            existing_aka = set(journal_entity.get("aka", []))
            new_aka = existing_aka | set(aka)
            if new_aka != existing_aka:
                journal_entity["aka"] = sorted(new_aka)
                journal_updated = True
        # Merge emails (union, lowercased, deduplicated)
        emails = entity.get("emails")
        if emails and isinstance(emails, list):
            existing_emails = set(e.lower() for e in journal_entity.get("emails", []))
            new_emails = existing_emails | set(e.lower() for e in emails)
            if new_emails != existing_emails:
                journal_entity["emails"] = sorted(new_emails)
                journal_updated = True
        # Only propagate is_principal if explicitly set and entity not detached
        if (
            entity.get("is_principal")
            and not is_detached
            and not journal_entity.get("is_principal")
        ):
            journal_entity["is_principal"] = True
            journal_updated = True

        if journal_updated:
            save_journal_entity(journal_entity)

        # Build relationship record (all non-identity fields)
        relationship: EntityDict = {
            "entity_id": entity_id,
        }
        for key, value in entity.items():
            if key not in journal_fields:
                relationship[key] = value

        # Save facet relationship
        save_facet_relationship(facet, entity_id, relationship)


def save_entities(
    facet: str, entities: list[EntityDict], day: str | None = None
) -> None:
    """Save entities to storage.

    For detected entities (day provided), writes to day-specific JSONL files.
    For attached entities (day=None), writes to:
    - Journal-level entity files: entities/<id>/entity.json (identity)
    - Facet relationship files: facets/<facet>/entities/<id>/entity.json

    Ensures all entities have an `id` field (generates from name if missing).
    For attached entities, validates name uniqueness within the facet.

    Args:
        facet: Facet name
        entities: List of entity dictionaries (must have type, name, description keys;
                  attached entities may also have id, attached_at, updated_at timestamps)
        day: Optional day in YYYYMMDD format for detected entities

    Raises:
        ValueError: If duplicate names found in attached entities (day=None)
    """
    if day is not None:
        _save_entities_detected(facet, entities, day)
    else:
        _save_entities_attached(facet, entities)


def attached_store_lock_path(facet: str) -> Path:
    """Return the per-facet sentinel path used to lock attached entity writes."""
    return Path(get_journal()) / "facets" / facet / "entities" / ".attached-entities"


def _locked_attached(
    facet: str,
    fn: Callable[[], T],
    max_retries: int = 3,
) -> T:
    """Run an attached-entity mutation under the facet-wide attached lock."""
    last_error: Exception | None = None
    for attempt in range(max_retries):
        try:
            with hold_lock(attached_store_lock_path(facet)):
                return fn()
        except EntityWriteError:
            raise
        except OSError as exc:
            last_error = exc
            if attempt < max_retries - 1:
                time.sleep(random.uniform(0.05, 0.3) * (attempt + 1))

    raise last_error  # type: ignore[misc]


def attach_or_reactivate_entity(
    facet: str,
    *,
    entity_type: str,
    name: str,
    description: str,
) -> tuple[EntityDict, bool]:
    """Attach a new entity or reactivate a detached exact-name match."""

    def _attach() -> tuple[EntityDict, bool]:
        entities = load_entities(
            facet,
            include_detached=True,
            include_blocked=True,
        )
        name_lower = name.lower()

        for entity in entities:
            if entity.get("name", "").lower() != name_lower:
                continue

            if entity.get("blocked"):
                raise EntityBlockedError()

            entity_id = entity["id"]
            if entity.get("detached"):
                relationship = load_facet_relationship(facet, entity_id)
                if relationship is None:
                    raise EntityNotFoundError()
                relationship.pop("detached", None)
                if description:
                    relationship["description"] = description
                relationship["updated_at"] = now_ms()
                save_facet_relationship(facet, entity_id, relationship)

                journal_entity = load_journal_entity(entity_id)
                if journal_entity and journal_entity.get("type") != entity_type:
                    journal_entity["type"] = entity_type
                    save_journal_entity(journal_entity)

                return relationship, True

            raise EntityExistsError()

        entity_id = entity_slug(name)
        journal_entity = load_journal_entity(entity_id)
        if journal_entity and journal_entity.get("blocked"):
            raise EntityBlockedError()
        if journal_entity is None:
            create_journal_entity(entity_id, name, entity_type)

        now = now_ms()
        relationship: EntityDict = {
            "entity_id": entity_id,
            "description": description,
            "attached_at": now,
            "updated_at": now,
        }
        save_facet_relationship(facet, entity_id, relationship)
        return relationship, False

    return _locked_attached(facet, _attach)


def detach_facet_entity(facet: str, entity_id: str) -> EntityDict:
    """Detach an active relationship from a facet."""

    def _detach() -> EntityDict:
        relationship = load_facet_relationship(facet, entity_id)
        if relationship is None or relationship.get("detached"):
            raise EntityNotFoundError()
        relationship["detached"] = True
        relationship["updated_at"] = now_ms()
        save_facet_relationship(facet, entity_id, relationship)
        return relationship

    return _locked_attached(facet, _detach)


def update_facet_entity_description(
    facet: str,
    entity_id: str,
    description: str,
) -> EntityDict:
    """Update an active facet relationship description."""

    def _update() -> EntityDict:
        relationship = load_facet_relationship(facet, entity_id)
        if relationship is None or relationship.get("detached"):
            raise EntityNotFoundError()
        relationship["description"] = description
        relationship["updated_at"] = now_ms()
        save_facet_relationship(facet, entity_id, relationship)
        return relationship

    return _locked_attached(facet, _update)


def update_facet_entity_identity(
    facet: str,
    *,
    old_name: str,
    new_name: str,
    entity_type: str,
    aka_list: list[str],
) -> EntityDict:
    """Faithfully port the existing route identity update semantics."""

    def _update() -> EntityDict:
        entities = load_entities(facet, include_detached=True)
        target: EntityDict | None = None
        for entity in entities:
            if not entity.get("detached") and entity.get("name") == old_name:
                target = entity
                break

        if target is None:
            raise EntityNotFoundError()

        if new_name.lower() != old_name.lower():
            for entity in entities:
                if entity is target or entity.get("detached"):
                    continue
                if entity.get("name", "").lower() == new_name.lower():
                    raise EntityExistsError()

        for aka in aka_list:
            conflict = validate_aka_uniqueness(
                aka,
                entities,
                exclude_entity_name=old_name,
            )
            if conflict:
                raise AkaConflictError(aka, conflict)

        new_id = entity_slug(new_name)
        old_id = target["id"]
        journal_entity = load_journal_entity(new_id) or create_journal_entity(
            new_id,
            new_name,
            entity_type or target.get("type", ""),
        )

        journal_entity["name"] = new_name
        if entity_type:
            journal_entity["type"] = entity_type
        if aka_list:
            journal_entity["aka"] = sorted(
                set(journal_entity.get("aka", [])) | set(aka_list)
            )
        save_journal_entity(journal_entity)

        relationship = load_facet_relationship(facet, old_id) or {
            "entity_id": old_id,
            "description": target.get("description", ""),
            "attached_at": target.get("attached_at"),
            "updated_at": target.get("updated_at"),
        }
        relationship["updated_at"] = now_ms()
        save_facet_relationship(facet, new_id, relationship)

        if new_name != old_name:
            try:
                rename_entity_memory(facet, old_name, new_name)
            except OSError as exc:
                logger.warning(
                    "Failed to rename entity memory from %s to %s in facet %s: %s",
                    old_name,
                    new_name,
                    facet,
                    exc,
                )

        return journal_entity

    return _locked_attached(facet, _update)


def add_entity_aka(
    facet: str,
    entity_id: str,
    aka: str,
    *,
    exclude_name: str,
) -> list[str]:
    """Add a single alias to a journal entity after locked facet dedup checks."""

    def _add() -> list[str]:
        entities = load_entities(
            facet,
            include_detached=True,
            include_blocked=True,
        )
        conflict = validate_aka_uniqueness(
            aka,
            entities,
            exclude_entity_name=exclude_name,
        )
        if conflict:
            raise AkaConflictError(aka, conflict)

        journal_entity = load_journal_entity(entity_id)
        if journal_entity is None:
            raise EntityNotFoundError()

        aliases = set(journal_entity.get("aka", []))
        aliases.add(aka)
        journal_entity["aka"] = sorted(aliases)
        save_journal_entity(journal_entity)
        return journal_entity["aka"]

    return _locked_attached(facet, _add)


def delete_detected_entity(facet: str, day: str, name: str) -> list[EntityDict]:
    """Delete detected entities with an exact name match from one day."""
    removed: list[EntityDict] = []

    def _delete(entities: list[EntityDict]) -> list[EntityDict]:
        nonlocal removed
        removed = [entity for entity in entities if entity.get("name") == name]
        return [entity for entity in entities if entity.get("name") != name]

    _locked_modify_detected(facet, day, _delete)
    return removed


def _locked_modify_detected(
    facet: str,
    day: str,
    modify_fn: callable,
    max_retries: int = 3,
) -> list[EntityDict]:
    """Perform a locked read-modify-write on detected entities.

    Acquires an exclusive file lock, loads current state, applies the
    mutation function, and writes back atomically. Retries with randomized
    backoff on transient OS errors.

    Args:
        facet: Facet name
        day: Day in YYYYMMDD format
        modify_fn: Called with current entity list, must return the new list.
                   May raise ValueError for logical errors (not retried).
        max_retries: Maximum attempts (default 3)

    Returns:
        The entity list as written

    Raises:
        ValueError: From modify_fn (logical errors, not retried)
        OSError: If all retries exhausted on transient errors
    """
    path = detected_entities_path(facet, day)

    last_error: Exception | None = None
    for attempt in range(max_retries):
        try:
            with hold_lock(path):
                # Fresh load inside lock — sees all prior writers' changes
                entities = load_entities(facet, day)
                entities = modify_fn(entities)
                _save_entities_detected(facet, entities, day)
                return entities
        except ValueError:
            raise  # Logical errors (duplicate, not found) — don't retry
        except OSError as exc:
            last_error = exc
            if attempt < max_retries - 1:
                time.sleep(random.uniform(0.05, 0.3) * (attempt + 1))

    raise last_error  # type: ignore[misc]


def save_detected_entity(
    facet: str,
    day: str,
    entity_type: str,
    name: str,
    description: str,
) -> EntityDict:
    """Add a single detected entity with concurrency-safe file locking.

    Uses exclusive file locking to serialize concurrent writers to the same
    facet+day file, preventing lost updates. Retries with randomized backoff
    on transient OS errors.

    Args:
        facet: Facet name
        day: Day in YYYYMMDD format
        entity_type: Entity type (e.g. "Person", "Company")
        name: Entity name
        description: Entity description

    Returns:
        The saved entity dict (with generated id)

    Raises:
        ValueError: If entity with same name already detected for this day
        OSError: If all retries exhausted
    """
    new_entity: EntityDict = {
        "type": entity_type,
        "name": name,
        "description": description,
    }
    name_lower = name.lower()

    def _add_entity(entities: list[EntityDict]) -> list[EntityDict]:
        for e in entities:
            if e.get("name", "").lower() == name_lower:
                raise ValueError(f"Entity '{name}' already detected for {day}")
        entities.append(new_entity)
        return entities

    _locked_modify_detected(facet, day, _add_entity)

    # Return with id filled in (set by _save_entities_detected)
    return new_entity


def update_detected_entity(
    facet: str,
    day: str,
    name: str,
    description: str,
) -> EntityDict:
    """Update a detected entity's description with concurrency-safe locking.

    Args:
        facet: Facet name
        day: Day in YYYYMMDD format
        name: Entity name to find
        description: New description

    Returns:
        The updated entity dict

    Raises:
        ValueError: If entity not found
        OSError: If all retries exhausted
    """

    def _update_entity(entities: list[EntityDict]) -> list[EntityDict]:
        for e in entities:
            if e.get("name") == name:
                e["description"] = description
                return entities
        raise ValueError(f"Entity '{name}' not found for {day}")

    result = _locked_modify_detected(facet, day, _update_entity)
    return next(e for e in result if e.get("name") == name)
