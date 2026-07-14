# SPDX-License-Identifier: AGPL-3.0-only
# Copyright (c) 2026 sol pbc
"""Entity resolution ambiguity storage helpers.

Sole write-owner of:
  journal/entities/ambiguities.jsonl
"""

from __future__ import annotations

import hashlib
import json
import logging
import re
import unicodedata
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Callable, Literal

from solstone.think.entities.core import EntityDict
from solstone.think.journal_io import atomic_replace, hold_lock
from solstone.think.utils import get_journal

logger = logging.getLogger(__name__)

AMBIGUITY_SCHEMA_VERSION = 1
ResolutionScopeKind = Literal["journal", "facet"]


class EntityAmbiguityError(RuntimeError):
    """Raised when an ambiguity-store operation cannot be completed safely."""


@dataclass(frozen=True)
class ResolutionScope:
    """Discriminator for the entity set a name was resolved against."""

    kind: ResolutionScopeKind
    facet: str | None = None

    def __post_init__(self) -> None:
        """Validate scope shape."""
        if self.kind == "journal":
            if self.facet is not None:
                raise ValueError("journal resolution scope must not include a facet")
            return
        if self.kind == "facet":
            if not self.facet:
                raise ValueError("facet resolution scope requires a facet")
            return
        raise ValueError(f"unknown resolution scope kind: {self.kind}")

    @classmethod
    def journal(cls) -> ResolutionScope:
        """Return the journal-wide resolution scope."""
        return cls(kind="journal")

    @classmethod
    def facet_scope(cls, facet: str) -> ResolutionScope:
        """Return a facet-scoped resolution scope."""
        return cls(kind="facet", facet=facet)

    def key(self) -> str:
        """Return the deterministic key prefix for this scope."""
        if self.kind == "journal":
            return "journal"
        return f"facet:{self.facet}"

    def to_dict(self) -> dict[str, str]:
        """Return the JSON shape for this scope."""
        if self.kind == "journal":
            return {"kind": "journal"}
        return {"kind": "facet", "facet": str(self.facet)}


@dataclass(frozen=True)
class ResolutionOrigin:
    """Structured provenance for one resolution observation."""

    lane: str
    facet: str | None = None
    day: str | None = None
    record_id: str | None = None
    segment_id: str | None = None
    source_id: str | None = None
    field: str | None = None
    path: str | None = None
    invocation_id: str | None = None

    def to_dict(self) -> dict[str, str]:
        """Return a compact JSON object, omitting unset fields."""
        data = {
            "lane": self.lane,
            "facet": self.facet,
            "day": self.day,
            "record_id": self.record_id,
            "segment_id": self.segment_id,
            "source_id": self.source_id,
            "field": self.field,
            "path": self.path,
            "invocation_id": self.invocation_id,
        }
        return {key: value for key, value in data.items() if value is not None}

    def key(self) -> str:
        """Return the deterministic dedupe key for this origin."""
        return json.dumps(self.to_dict(), sort_keys=True, separators=(",", ":"))


def normalize_resolution_query(query: str) -> str:
    """Normalize a query for ambiguity identity while preserving punctuation."""
    normalized = unicodedata.normalize("NFKC", query)
    normalized = re.sub(r"\s+", " ", normalized.strip())
    return normalized.casefold()


def ambiguity_key(scope: ResolutionScope, normalized_query: str) -> str:
    """Return the deterministic key for one scoped normalized query."""
    return f"{scope.key()}|{normalized_query}"


def ambiguity_id_for_key(key: str) -> str:
    """Return a deterministic public id for one ambiguity key."""
    digest = hashlib.sha256(key.encode("utf-8")).hexdigest()[:24]
    return f"amb_{digest}"


def ambiguities_dir() -> Path:
    """Return the entity ambiguities directory path."""
    return Path(get_journal()) / "entities"


def ambiguities_path() -> Path:
    """Return the entity ambiguities JSONL path."""
    return ambiguities_dir() / "ambiguities.jsonl"


def _load_jsonl_rows(path: Path) -> list[dict[str, Any]]:
    """Load JSONL rows from *path*, skipping blanks and malformed lines."""
    if not path.exists():
        return []

    rows: list[dict[str, Any]] = []
    with open(path, encoding="utf-8") as handle:
        for lineno, line in enumerate(handle, start=1):
            raw = line.strip()
            if not raw:
                continue
            try:
                data = json.loads(raw)
            except json.JSONDecodeError:
                logger.warning(
                    "entity ambiguities: malformed JSONL line %s in %s",
                    lineno,
                    path,
                )
                continue
            if not isinstance(data, dict):
                logger.warning(
                    "entity ambiguities: non-object JSONL line %s in %s (got %s)",
                    lineno,
                    path,
                    type(data).__name__,
                )
                continue
            rows.append(data)
    return rows


def load_ambiguities() -> list[dict[str, Any]]:
    """Load entity resolution ambiguity rows."""
    return _load_jsonl_rows(ambiguities_path())


def _save_jsonl_rows(path: Path, rows: list[dict[str, Any]]) -> None:
    """Write *rows* to *path* as JSONL using an atomic replace."""
    content = ""
    if rows:
        content = "\n".join(json.dumps(row, ensure_ascii=False) for row in rows) + "\n"
    atomic_replace(path, content)


def save_ambiguities(rows: list[dict[str, Any]]) -> None:
    """Persist entity resolution ambiguity rows atomically."""
    _save_jsonl_rows(ambiguities_path(), rows)


def locked_modify_ambiguities(
    fn: Callable[[list[dict[str, Any]]], list[dict[str, Any]]],
) -> list[dict[str, Any]]:
    """Apply a locked read-modify-write cycle to ambiguities.jsonl."""
    path = ambiguities_path()
    with hold_lock(path):
        rows = _load_jsonl_rows(path)
        new_rows = fn(rows)
        _save_jsonl_rows(path, new_rows)
        return new_rows


def utc_now_iso() -> str:
    """Return the current UTC time as an ISO-8601 string ending in Z."""
    return (
        datetime.now(timezone.utc).isoformat(timespec="seconds").replace("+00:00", "Z")
    )


def _row_key(row: dict[str, Any]) -> str:
    scope_data = row.get("scope")
    if not isinstance(scope_data, dict):
        return ""
    kind = scope_data.get("kind")
    if kind == "journal":
        scope_key = "journal"
    elif kind == "facet":
        scope_key = f"facet:{scope_data.get('facet') or ''}"
    else:
        return ""
    return f"{scope_key}|{row.get('normalized_query') or ''}"


def find_ambiguity(
    rows: list[dict[str, Any]],
    scope: ResolutionScope,
    normalized_query: str,
) -> dict[str, Any] | None:
    """Return one ambiguity row by scope and normalized query."""
    target_key = ambiguity_key(scope, normalized_query)
    for row in rows:
        if _row_key(row) == target_key:
            return row
    return None


def load_resolved_ambiguity_choice(
    scope: ResolutionScope,
    normalized_query: str,
) -> dict[str, Any] | None:
    """Load a resolved ambiguity row for a scoped normalized query."""
    row = find_ambiguity(load_ambiguities(), scope, normalized_query)
    if row and row.get("status") == "resolved":
        return row
    return None


def record_ambiguity_observation(
    *,
    scope: ResolutionScope,
    query: str,
    normalized_query: str,
    observed_tier: int,
    ranked_candidates: list[dict[str, Any]],
    origin: ResolutionOrigin,
) -> dict[str, Any]:
    """Create or update one open entity ambiguity row."""
    row: dict[str, Any] | None = None

    def mutate(rows: list[dict[str, Any]]) -> list[dict[str, Any]]:
        nonlocal row
        key = ambiguity_key(scope, normalized_query)
        existing = find_ambiguity(rows, scope, normalized_query)
        now = utc_now_iso()
        origin_data = origin.to_dict()
        origin_key = origin.key()
        if existing is None:
            row = {
                "schema_version": AMBIGUITY_SCHEMA_VERSION,
                "ambiguity_id": ambiguity_id_for_key(key),
                "scope": scope.to_dict(),
                "normalized_query": normalized_query,
                "original_query": query,
                "latest_query": query,
                "observed_tier": observed_tier,
                "ranked_candidates": ranked_candidates,
                "origins": [origin_data],
                "origin_keys": [origin_key],
                "first_seen": now,
                "last_seen": now,
                "occurrence_count": 1,
                "status": "open",
                "resolved_entity_id": None,
                "resolved_at": None,
                "audit": {"prior_choices": []},
            }
            return list(rows) + [row]

        existing["latest_query"] = query
        existing["observed_tier"] = observed_tier
        existing["ranked_candidates"] = ranked_candidates

        origin_keys = existing.setdefault("origin_keys", [])
        origins = existing.setdefault("origins", [])
        if not isinstance(origin_keys, list):
            raise EntityAmbiguityError(
                f"ambiguity row has invalid origin_keys: {existing.get('ambiguity_id')}"
            )
        if not isinstance(origins, list):
            raise EntityAmbiguityError(
                f"ambiguity row has invalid origins: {existing.get('ambiguity_id')}"
            )
        if origin_key not in origin_keys:
            origin_keys.append(origin_key)
            origins.append(origin_data)
            existing["occurrence_count"] = (
                int(existing.get("occurrence_count") or 0) + 1
            )
            existing["last_seen"] = now
        row = existing
        return rows

    locked_modify_ambiguities(mutate)

    if row is None:  # pragma: no cover - defensive assertion
        raise EntityAmbiguityError("record-ambiguity-observation produced no row")

    return row


def _validate_choice_entity(
    entity_id: str,
    entities: list[EntityDict],
) -> EntityDict:
    for entity in entities:
        if entity.get("id") == entity_id:
            if entity.get("blocked"):
                raise EntityAmbiguityError(
                    f"resolved entity choice is blocked: {entity_id}"
                )
            return entity
    raise EntityAmbiguityError(
        f"resolved entity choice is not present in the resolution scope: {entity_id}"
    )


def record_ambiguity_choice(
    query: str,
    entity_id: str,
    entities: list[EntityDict],
    *,
    scope: ResolutionScope,
    origin: ResolutionOrigin | None = None,
) -> dict[str, Any]:
    """Record the chosen entity for an ambiguity row."""
    normalized_query = normalize_resolution_query(query)
    _validate_choice_entity(entity_id, entities)
    updated: dict[str, Any] | None = None

    def mutate(rows: list[dict[str, Any]]) -> list[dict[str, Any]]:
        nonlocal updated
        row = find_ambiguity(rows, scope, normalized_query)
        if row is None:
            raise EntityAmbiguityError(
                f"no ambiguity row for {scope.key()} query {normalized_query!r}"
            )
        if row.get("scope") != scope.to_dict():
            raise EntityAmbiguityError(
                f"ambiguity row scope mismatch: {row.get('ambiguity_id')}"
            )

        now = utc_now_iso()
        previous_id = row.get("resolved_entity_id")
        previous_at = row.get("resolved_at")
        if row.get("status") == "resolved" and previous_id and previous_id != entity_id:
            audit = row.setdefault("audit", {})
            if not isinstance(audit, dict):
                raise EntityAmbiguityError(
                    f"ambiguity row has invalid audit: {row.get('ambiguity_id')}"
                )
            prior_choices = audit.setdefault("prior_choices", [])
            if not isinstance(prior_choices, list):
                raise EntityAmbiguityError(
                    "ambiguity row has invalid audit.prior_choices: "
                    f"{row.get('ambiguity_id')}"
                )
            prior: dict[str, Any] = {
                "resolved_entity_id": previous_id,
                "resolved_at": previous_at,
                "replaced_at": now,
            }
            if origin is not None:
                prior["replaced_by_origin"] = origin.to_dict()
            prior_choices.append(prior)

        row["status"] = "resolved"
        row["resolved_entity_id"] = entity_id
        row["resolved_at"] = now
        updated = row
        return rows

    locked_modify_ambiguities(mutate)

    if updated is None:  # pragma: no cover - defensive assertion
        raise EntityAmbiguityError("record-ambiguity-choice produced no row")

    return updated
