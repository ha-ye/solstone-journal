# SPDX-License-Identifier: AGPL-3.0-only
# Copyright (c) 2026 sol pbc

"""Entity activity tracking and detected entity management.

This module handles:
- Loading detected entities with aggregation
"""

import re
from datetime import datetime, timedelta
from pathlib import Path
from typing import Iterator

from solstone.think.entities.core import EntityDict
from solstone.think.entities.loading import load_entities, parse_entity_file
from solstone.think.entities.matching import find_matching_entity
from solstone.think.utils import get_journal


def load_detected_entities_recent(facet: str, days: int = 30) -> list[EntityDict]:
    """Load detected entities from last N days, excluding those matching attached entities.

    Scans detected entity files in reverse chronological order (newest first),
    aggregating by (type, name) to provide count and last_seen tracking.

    Uses fuzzy matching to exclude detected entities that match attached entities
    by name, aka, normalized form, first word, or fuzzy similarity.

    Args:
        facet: Facet name
        days: Number of days to look back (default: 30)

    Returns:
        List of detected entity dictionaries with aggregation data:
        - type: Entity type
        - name: Entity name
        - description: Description from most recent detection
        - count: Number of days entity was detected
        - last_seen: Most recent day (YYYYMMDD) entity was detected

        Entities are excluded if they match an attached entity via fuzzy matching.

    Example:
        >>> load_detected_entities_recent("personal", days=30)
        [{"type": "Person", "name": "Charlie", "description": "Met at coffee shop",
          "count": 3, "last_seen": "20250115"}]
    """
    journal = get_journal()

    # Load attached entities (excluding detached) for fuzzy matching
    # Detached entities should appear in detected list again
    attached = load_entities(facet, include_detached=False)

    # Cache for already-checked names to avoid repeated fuzzy matching
    # Maps detected name -> True (excluded) or False (not excluded)
    exclusion_cache: dict[str, bool] = {}

    def is_excluded(name: str) -> bool:
        """Check if a detected name matches any attached entity."""
        if name in exclusion_cache:
            return exclusion_cache[name]
        match = find_matching_entity(name, attached)
        excluded = match is not None
        exclusion_cache[name] = excluded
        return excluded

    # Calculate date range cutoff
    cutoff_date = datetime.now() - timedelta(days=days)
    cutoff_str = cutoff_date.strftime("%Y%m%d")

    # Get entities directory and find all day files
    entities_dir = Path(journal) / "facets" / facet / "entities"
    if not entities_dir.exists():
        return []

    # Glob day files and sort descending (newest first)
    day_files = sorted(entities_dir.glob("*.jsonl"), reverse=True)

    # Aggregate entities by (type, name)
    # Key: (type, name) -> {entity data with count, last_seen}
    detected_map: dict[tuple[str, str], EntityDict] = {}

    for day_file in day_files:
        day = day_file.stem  # YYYYMMDD

        # Skip files outside date range
        if day < cutoff_str:
            continue

        # Parse entities from this day
        day_entities = parse_entity_file(str(day_file))

        for entity in day_entities:
            etype = entity.get("type", "")
            name = entity.get("name", "")

            # Skip if matches attached entity (using fuzzy matching)
            if is_excluded(name):
                continue

            key = (etype, name)

            if key not in detected_map:
                # First occurrence (most recent day) - store full entity
                detected_map[key] = {
                    "type": etype,
                    "name": name,
                    "description": entity.get("description", ""),
                    "count": 1,
                    "last_seen": day,
                }
            else:
                # Subsequent occurrence - just increment count
                detected_map[key]["count"] += 1

    return list(detected_map.values())


def iter_detected_entity_names_since(since: str) -> Iterator[tuple[str, str, str]]:
    """Yield detected entity names from facet entity files since YYYYMMDD."""
    facets_dir = Path(get_journal()) / "facets"
    if not facets_dir.exists():
        return

    for day_file in sorted(facets_dir.glob("*/entities/*.jsonl")):
        day = day_file.stem
        if not re.fullmatch(r"\d{8}", day) or day < since:
            continue

        facet = day_file.parent.parent.name
        for entity in parse_entity_file(str(day_file)):
            name = str(entity.get("name") or "").strip()
            if name:
                yield name, facet, day
