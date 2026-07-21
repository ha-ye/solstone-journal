# SPDX-License-Identifier: AGPL-3.0-only
# Copyright (c) 2026 sol pbc

"""Eligibility helpers for attaching speaker clusters to journal people."""

from __future__ import annotations

from collections.abc import Collection, Mapping
from typing import Any

from solstone.think.entities.ambiguities import normalize_resolution_query
from solstone.think.entities.journal import get_journal_principal


def current_principal_id() -> str:
    """Return the current journal principal id, or an empty string."""
    principal = get_journal_principal()
    if principal is None:
        return ""
    return str(principal.get("id") or "")


def is_speaker_attach_candidate(
    entity: Mapping[str, Any] | None,
    *,
    principal_id: str | None = None,
) -> bool:
    """Return whether an entity can be attached to a speaker cluster."""
    if entity is None:
        return False
    entity_id = str(entity.get("id") or "")
    name = str(entity.get("name") or "")
    effective_principal_id = (
        current_principal_id() if principal_id is None else principal_id
    )
    return bool(
        entity_id
        and name
        and entity.get("type") == "Person"
        and not entity.get("blocked")
        and not entity.get("is_principal")
        and entity_id != effective_principal_id
    )


def speaker_attach_rejection_reason(
    entity_id: str,
    entities: Mapping[str, Mapping[str, Any]],
    *,
    target_id: str = "",
    visible_candidate_ids: Collection[str] | None = None,
    principal_id: str | None = None,
) -> str | None:
    """Return why a reviewed near-match id cannot be authorized, if any."""
    reviewed_id = str(entity_id or "")
    if target_id and reviewed_id == target_id:
        return "self"
    entity = entities.get(reviewed_id)
    if (
        entity is None
        or not reviewed_id
        or not entity.get("id")
        or not entity.get("name")
    ):
        return "nonexistent"
    effective_principal_id = (
        current_principal_id() if principal_id is None else principal_id
    )
    if entity.get("is_principal") or reviewed_id == effective_principal_id:
        return "principal"
    if entity.get("type") != "Person":
        return "non_person"
    if entity.get("blocked"):
        return "blocked"
    if visible_candidate_ids is not None and reviewed_id not in visible_candidate_ids:
        return "unshown"
    return None


def eligible_speaker_attach_entities(
    entities: Mapping[str, Mapping[str, Any]],
    *,
    principal_id: str | None = None,
) -> list[Mapping[str, Any]]:
    """Return entities eligible for speaker-cluster attachment."""
    effective_principal_id = (
        current_principal_id() if principal_id is None else principal_id
    )
    return [
        entity
        for entity in entities.values()
        if is_speaker_attach_candidate(entity, principal_id=effective_principal_id)
    ]


def principal_name_collision(
    name: str,
    entities: Mapping[str, Mapping[str, Any]],
    *,
    principal_id: str | None = None,
) -> bool:
    """Return whether a query matches the principal's name or aka."""
    effective_principal_id = (
        current_principal_id() if principal_id is None else principal_id
    )
    if not effective_principal_id:
        return False
    entity = entities.get(effective_principal_id)
    if entity is None:
        return False
    return _name_or_aka_collision(name, entity)


def blocked_person_name_collision(
    name: str,
    entities: Mapping[str, Mapping[str, Any]],
) -> bool:
    """Return whether a query matches a blocked Person's name or aka."""
    return any(
        entity.get("type") == "Person"
        and entity.get("blocked")
        and _name_or_aka_collision(name, entity)
        for entity in entities.values()
    )


def _name_or_aka_collision(name: str, entity: Mapping[str, Any]) -> bool:
    query = normalize_resolution_query(name)
    if not query:
        return False
    return any(
        normalize_resolution_query(value) == query for value in _name_values(entity)
    )


def _name_values(entity: Mapping[str, Any]) -> list[str]:
    values: list[str] = []
    name = entity.get("name")
    if isinstance(name, str) and name.strip():
        values.append(name)
    aka = entity.get("aka")
    if isinstance(aka, list):
        values.extend(
            value for value in aka if isinstance(value, str) and value.strip()
        )
    return values
