# SPDX-License-Identifier: AGPL-3.0-only
# Copyright (c) 2026 sol pbc

"""Entity observations management.

Observations are durable factoids about entities stored in:
    facets/<facet>/entities/<id>/observations.jsonl

They capture useful information like preferences, expertise, relationships,
and biographical facts that help with future interactions.
"""

import json
import random
import time
from pathlib import Path
from typing import Any, Iterator

from solstone.think.entities.journal import load_all_journal_entities
from solstone.think.entities.relationships import entity_memory_path
from solstone.think.journal_io import LockTimeout, atomic_replace, hold_lock
from solstone.think.utils import get_journal, now_ms


def observations_file_path(facet: str, name: str) -> Path:
    """Return path to observations file for an entity.

    Observations are stored in the entity's memory folder:
    facets/{facet}/entities/{entity_slug}/observations.jsonl

    Args:
        facet: Facet name (e.g., "personal", "work")
        name: Entity name (will be slugified)

    Returns:
        Path to observations.jsonl file

    Raises:
        ValueError: If name slugifies to empty string
    """
    folder = entity_memory_path(facet, name)
    return folder / "observations.jsonl"


def _iter_observation_files() -> Iterator[Path]:
    facets_dir = Path(get_journal()) / "facets"
    if not facets_dir.is_dir():
        return

    for path in facets_dir.glob("*/entities/*/observations.jsonl"):
        if path.is_file():
            yield path


def _count_observation_file(obs_file: Path) -> int:
    if not obs_file.exists():
        return 0

    try:
        with open(obs_file, "r", encoding="utf-8") as f:
            count = sum(1 for line in f if line.strip())
    except OSError:
        return 0

    return count


def _observation_depth_by_slug() -> dict[str, int]:
    depths: dict[str, int] = {}
    for obs_file in _iter_observation_files():
        count = _count_observation_file(obs_file)
        if count <= 0:
            continue
        slug = obs_file.parent.name
        depths[slug] = depths.get(slug, 0) + count
    return depths


def count_entities_with_min_observation_depth(min_depth: int) -> int:
    """Count observed entity slugs whose total observations meet ``min_depth``."""
    return sum(
        1 for depth in _observation_depth_by_slug().values() if depth >= min_depth
    )


def iter_entity_names_for_recall() -> list[str]:
    """Return lowercased names and aliases for entities with observations."""
    observed_slugs = set(_observation_depth_by_slug())
    entities = load_all_journal_entities()
    names: set[str] = set()

    for slug in observed_slugs:
        entity = entities.get(slug)
        if not entity:
            continue

        name = str(entity.get("name") or "").strip()
        if name:
            names.add(name.lower())

        aka = entity.get("aka") or []
        if isinstance(aka, list):
            for alias in aka:
                alias_name = str(alias).strip()
                if alias_name:
                    names.add(alias_name.lower())

    return sorted(names)


def load_observations(facet: str, name: str) -> list[dict[str, Any]]:
    """Load observations for an entity.

    Args:
        facet: Facet name
        name: Entity name

    Returns:
        List of observation dictionaries with content, observed_at, source_day keys.
        Returns empty list if file doesn't exist.

    Example:
        >>> load_observations("work", "Alice Johnson")
        [{"content": "Prefers async communication", "observed_at": 1736784000000, "source_day": "20250113"}]
    """
    path = observations_file_path(facet, name)

    if not path.exists():
        return []

    observations = []
    with open(path, "r", encoding="utf-8") as f:
        for line in f:
            line = line.strip()
            if not line:
                continue
            try:
                data = json.loads(line)
                observations.append(data)
            except json.JSONDecodeError:
                continue  # Skip malformed lines

    return observations


def count_observations(facet: str, name: str) -> int:
    """Count observations for an entity."""
    try:
        obs_file = entity_memory_path(facet, name) / "observations.jsonl"
    except ValueError:
        return 0

    return _count_observation_file(obs_file)


def save_observations(
    facet: str, name: str, observations: list[dict[str, Any]]
) -> None:
    """Save observations to entity's observations file using atomic write.

    Args:
        facet: Facet name
        name: Entity name
        observations: List of observation dictionaries
    """
    path = observations_file_path(facet, name)

    # Format observations as JSONL
    content = "".join(
        json.dumps(obs, ensure_ascii=False) + "\n" for obs in observations
    )
    atomic_replace(path, content)


def add_observation(
    facet: str,
    name: str,
    content: str,
    source_day: str | None = None,
    max_retries: int = 3,
) -> dict[str, Any]:
    """Add an observation to an entity with file locking.

    Acquires an exclusive file lock to serialize concurrent writes to the
    same entity's observations file.

    Args:
        facet: Facet name
        name: Entity name
        content: The observation text
        source_day: Optional day (YYYYMMDD) when observation was made
        max_retries: Maximum attempts on transient OS errors (default 3)

    Returns:
        Dictionary with updated observations list and count

    Raises:
        ValueError: If content is empty
        OSError: If all retries exhausted

    Example:
        >>> add_observation("work", "Alice", "Prefers morning meetings", "20250113")
        {"observations": [...], "count": 1}
    """
    content = content.strip()
    if not content:
        raise ValueError("Observation content cannot be empty")

    path = observations_file_path(facet, name)

    last_error: Exception | None = None
    for attempt in range(max_retries):
        try:
            with hold_lock(path):
                observations = load_observations(facet, name)

                observation: dict[str, Any] = {
                    "content": content,
                    "observed_at": now_ms(),
                }
                if source_day:
                    observation["source_day"] = source_day

                observations.append(observation)
                save_observations(facet, name, observations)

                return {
                    "observations": observations,
                    "count": len(observations),
                }
        except ValueError:
            raise  # Logical errors — don't retry
        except OSError as exc:
            last_error = exc
            if attempt < max_retries - 1:
                time.sleep(random.uniform(0.05, 0.3) * (attempt + 1))

    raise last_error  # type: ignore[misc]


def _operation_counts() -> dict[str, int]:
    return {"update": 0, "add": 0, "drop": 0, "keep": 0, "skipped": 0}


def _target_index_in_snapshot(index: Any, snapshot: list[dict[str, Any]]) -> bool:
    return (
        isinstance(index, int)
        and not isinstance(index, bool)
        and 0 <= index < len(snapshot)
    )


def _target_quote_matches(observation: dict[str, Any], target_quote: Any) -> bool:
    if not isinstance(target_quote, str) or not target_quote.strip():
        return True
    content = str(observation.get("content") or "")
    return target_quote.strip().casefold() in content.casefold()


def _new_observation(
    content: str, source_day: str | None, relation: dict[str, Any] | None = None
) -> dict[str, Any]:
    observation: dict[str, Any] = {
        "content": content,
        "observed_at": now_ms(),
    }
    if source_day is not None:
        observation["source_day"] = source_day
    if relation is not None:
        observation["relation"] = dict(relation)
    return observation


def _apply_observation_ops(
    snapshot: list[dict[str, Any]],
    ops: list[dict],
    source_day: str | None,
) -> tuple[list[dict[str, Any]], dict[str, int], bool]:
    counts = _operation_counts()
    updates: dict[int, dict[str, Any]] = {}
    drops: set[int] = set()
    additions: list[dict[str, Any]] = []
    changed = False

    for op in ops:
        if not isinstance(op, dict):
            counts["skipped"] += 1
            continue

        action = op.get("op")
        relation = op.get("relation")
        if action == "add":
            content = op.get("content")
            if not isinstance(content, str) or not content.strip():
                counts["skipped"] += 1
                continue
            additions.append(_new_observation(content.strip(), source_day, relation))
            counts["add"] += 1
            changed = True
            continue

        if action not in {"update", "drop", "keep"}:
            counts["skipped"] += 1
            continue

        target_index = op.get("target_index")
        if not _target_index_in_snapshot(target_index, snapshot):
            counts["skipped"] += 1
            continue

        if not _target_quote_matches(snapshot[target_index], op.get("target_quote")):
            counts["skipped"] += 1
            continue

        if action == "keep":
            counts["keep"] += 1
            continue

        if action == "drop":
            drops.add(target_index)
            updates.pop(target_index, None)
            counts["drop"] += 1
            changed = True
            continue

        content = op.get("content")
        if not isinstance(content, str) or not content.strip():
            counts["skipped"] += 1
            continue
        updates[target_index] = _new_observation(content.strip(), source_day, relation)
        drops.discard(target_index)
        counts["update"] += 1
        changed = True

    if not changed:
        return snapshot, counts, False

    observations: list[dict[str, Any]] = []
    for index, observation in enumerate(snapshot):
        if index in drops:
            continue
        if index in updates:
            observations.append(updates[index])
        else:
            observations.append(observation)
    observations.extend(additions)

    return observations, counts, True


def record_observation_ops(
    facet: str,
    name: str,
    ops: list[dict],
    source_day: str | None = None,
    max_retries: int = 3,
) -> dict[str, int]:
    """Apply observation update/drop/add/keep operations under a file lock."""
    path = observations_file_path(facet, name)

    last_error: Exception | None = None
    for attempt in range(max_retries):
        try:
            with hold_lock(path):
                snapshot = load_observations(facet, name)
                observations, counts, changed = _apply_observation_ops(
                    snapshot, ops, source_day
                )
                if changed:
                    save_observations(facet, name, observations)
                return counts
        except (LockTimeout, OSError) as exc:
            last_error = exc
            if attempt < max_retries - 1:
                time.sleep(random.uniform(0.05, 0.3) * (attempt + 1))

    raise last_error  # type: ignore[misc]
