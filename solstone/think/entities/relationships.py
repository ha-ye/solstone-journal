# SPDX-License-Identifier: AGPL-3.0-only
# Copyright (c) 2026 sol pbc

"""Facet relationship management and entity memory.

Facet relationships link journal entities to specific facets with context:
    facets/<facet>/entities/<id>/entity.json

Facet entity memory (observations) is stored alongside relationships:
    facets/<facet>/entities/<id>/observations.jsonl

Note: Voiceprints are stored at journal level (entities/<id>/voiceprints.npz)
since they are identity-specific, not facet-specific.
"""

import json
import shutil
from pathlib import Path
from typing import Any

from solstone.think.entities.core import EntityDict, entity_slug
from solstone.think.entities.errors import EntityExistsError, EntityNotFoundError
from solstone.think.journal_io import atomic_replace
from solstone.think.utils import get_journal


def facet_relationship_path(facet: str, entity_id: str) -> Path:
    """Return path to facet relationship file.

    Args:
        facet: Facet name
        entity_id: Entity ID (slug)

    Returns:
        Path to facets/<facet>/entities/<id>/entity.json
    """
    return (
        Path(get_journal()) / "facets" / facet / "entities" / entity_id / "entity.json"
    )


def load_facet_relationship(facet: str, entity_id: str) -> EntityDict | None:
    """Load a facet relationship for an entity.

    Args:
        facet: Facet name
        entity_id: Entity ID (slug)

    Returns:
        Relationship dict with entity_id, description, timestamps, etc.,
        or None if not found.
    """
    path = facet_relationship_path(facet, entity_id)
    if not path.exists():
        return None

    try:
        with open(path, "r", encoding="utf-8") as f:
            data = json.load(f)
        # Ensure entity_id is present
        data["entity_id"] = entity_id

        return data
    except (json.JSONDecodeError, OSError):
        return None


def save_facet_relationship(
    facet: str, entity_id: str, relationship: EntityDict
) -> None:
    """Save a facet relationship using atomic write.

    Creates the directory if needed.

    Args:
        facet: Facet name
        entity_id: Entity ID (slug)
        relationship: Relationship dict with description, timestamps, etc.
    """
    path = facet_relationship_path(facet, entity_id)

    # Ensure entity_id is in the relationship
    relationship["entity_id"] = entity_id

    content = json.dumps(relationship, ensure_ascii=False, indent=2) + "\n"
    atomic_replace(path, content)


def scan_facet_relationships(facet: str) -> list[str]:
    """List all entity IDs with relationships in a facet.

    Scans facets/<facet>/entities/ for subdirectories containing entity.json.

    Args:
        facet: Facet name

    Returns:
        List of entity IDs (directory names)
    """
    entities_dir = Path(get_journal()) / "facets" / facet / "entities"
    if not entities_dir.exists():
        return []

    entity_ids = []
    for entry in entities_dir.iterdir():
        if entry.is_dir() and (entry / "entity.json").exists():
            entity_ids.append(entry.name)

    entity_ids.sort()
    return entity_ids


def load_all_facet_relationships(facet: str) -> dict[str, EntityDict]:
    """Load all facet relationships for a facet.

    Returns:
        Dict mapping entity_id to relationship dict
    """
    entity_ids = scan_facet_relationships(facet)
    relationships = {}
    for entity_id in entity_ids:
        relationship = load_facet_relationship(facet, entity_id)
        if relationship:
            relationships[entity_id] = relationship

    return relationships


def load_all_facet_relationships_across_facets() -> dict[
    str, list[tuple[str, EntityDict]]
]:
    """Load facet relationships across every facet in sorted facet order.

    Returns:
        Dict mapping entity_id to [(facet_name, relationship_dict), ...]
    """
    from solstone.think.facets import get_facets

    relationships_by_entity: dict[str, list[tuple[str, EntityDict]]] = {}
    facet_names = set(get_facets())
    facets_dir = Path(get_journal()) / "facets"
    if facets_dir.is_dir():
        facet_names.update(path.name for path in facets_dir.iterdir() if path.is_dir())

    for facet_name in sorted(facet_names):
        for entity_id, relationship in load_all_facet_relationships(facet_name).items():
            relationships_by_entity.setdefault(entity_id, []).append(
                (facet_name, relationship)
            )

    return relationships_by_entity


def enrich_relationship_with_journal(
    relationship: EntityDict,
    journal_entity: EntityDict | None,
) -> EntityDict:
    """Merge journal entity fields into relationship for unified view.

    Creates a combined entity dict that has identity fields (name, type, aka,
    is_principal, blocked) from journal and relationship fields (description,
    timestamps, etc.) from facet.

    Args:
        relationship: Facet relationship dict
        journal_entity: Journal-level entity dict (or None)

    Returns:
        Merged entity dict with all fields
    """
    # Start with relationship data
    result = dict(relationship)

    # Add identity fields from journal entity
    if journal_entity:
        result["id"] = journal_entity.get("id", relationship.get("entity_id", ""))
        result["name"] = journal_entity.get("name", "")
        result["type"] = journal_entity.get("type", "")
        if journal_entity.get("aka"):
            result["aka"] = journal_entity["aka"]
        if journal_entity.get("is_principal"):
            result["is_principal"] = True
        if journal_entity.get("blocked"):
            result["blocked"] = True
    else:
        # No journal entity - use entity_id as id
        result["id"] = relationship.get("entity_id", "")

    # Remove entity_id from result (use id instead)
    result.pop("entity_id", None)

    return result


def entity_memory_path(facet: str, name: str) -> Path:
    """Return path to entity's facet-scoped memory folder.

    Facet entity memory folders store facet-specific data about entities,
    such as observations (durable facts learned in this facet's context).

    Args:
        facet: Facet name (e.g., "personal", "work")
        name: Entity name (will be slugified)

    Returns:
        Path to facets/{facet}/entities/{entity_slug}/

    Raises:
        ValueError: If name slugifies to empty string
    """
    slug = entity_slug(name)
    if not slug:
        raise ValueError(f"Entity name '{name}' slugifies to empty string")

    return Path(get_journal()) / "facets" / facet / "entities" / slug


def ensure_entity_memory(facet: str, name: str) -> Path:
    """Create entity memory folder if needed, return path.

    Args:
        facet: Facet name (e.g., "personal", "work")
        name: Entity name (will be slugified)

    Returns:
        Path to the created/existing folder

    Raises:
        ValueError: If name slugifies to empty string
    """
    folder = entity_memory_path(facet, name)
    folder.mkdir(parents=True, exist_ok=True)
    return folder


def rename_entity_memory(facet: str, old_name: str, new_name: str) -> bool:
    """Rename entity memory folder if it exists.

    Called when an entity is renamed to keep folder in sync.

    Args:
        facet: Facet name
        old_name: Previous entity name
        new_name: New entity name

    Returns:
        True if folder was renamed, False if old folder didn't exist
        or names slugify to the same value

    Raises:
        ValueError: If either name slugifies to empty string
        OSError: If rename fails (e.g., target exists)
    """
    old_folder = entity_memory_path(facet, old_name)
    new_folder = entity_memory_path(facet, new_name)

    # No rename needed if slugified names are the same
    if old_folder == new_folder:
        return False

    if not old_folder.exists():
        return False

    if new_folder.exists():
        raise OSError(f"Target folder already exists: {new_folder}")

    shutil.move(str(old_folder), str(new_folder))
    return True


def move_facet_entity(
    *,
    entity_name: str,
    from_facet: str,
    to_facet: str,
    merge: bool = False,
) -> dict[str, Any]:
    """Move or merge an entity's facet-scoped memory between facets."""
    entity_id = entity_slug(entity_name)
    src_dir = entity_memory_path(from_facet, entity_name)
    dst_dir = entity_memory_path(to_facet, entity_name)

    if not src_dir.exists():
        raise EntityNotFoundError(entity_name)

    if dst_dir.exists() and not merge:
        raise EntityExistsError(entity_name)

    if dst_dir.exists():
        from solstone.think.entities.observations import (
            load_observations,
            save_observations,
        )

        src_relationship = load_facet_relationship(from_facet, entity_id)
        dst_relationship = load_facet_relationship(to_facet, entity_id)
        if src_relationship is not None and dst_relationship is None:
            save_facet_relationship(to_facet, entity_id, src_relationship)

        src_obs = load_observations(from_facet, entity_name)
        dst_obs = load_observations(to_facet, entity_name)

        existing_keys = {(o["content"], o.get("observed_at")) for o in dst_obs}
        merged = list(dst_obs) + [
            o
            for o in src_obs
            if (o["content"], o.get("observed_at")) not in existing_keys
        ]
        save_observations(to_facet, entity_name, merged)

        shutil.rmtree(str(src_dir))
        did_merge = True
    else:
        dst_dir.parent.mkdir(parents=True, exist_ok=True)
        shutil.move(str(src_dir), str(dst_dir))
        did_merge = False

    return {
        "entity": entity_name,
        "entity_id": entity_id,
        "moved_from": from_facet,
        "moved_to": to_facet,
        "merged": did_merge,
    }
