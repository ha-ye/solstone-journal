# SPDX-License-Identifier: AGPL-3.0-only
# Copyright (c) 2026 sol pbc

"""Hook for extracting entities from model JSON output and writing to JSONL.

This hook is invoked via "hook": {"post": "entities"} in generator frontmatter.
It parses the structured entity payload and writes deduplicated entities to a
JSONL file next to the agent output.
"""

import json
import logging
from pathlib import Path

from solstone.think.indexer.journal import index_file
from solstone.think.utils import get_journal

logger = logging.getLogger(__name__)
ENTITY_TYPES = {"Person", "Company", "Project", "Tool"}


def post_process(result: str, context: dict) -> str | None:
    """Parse entity JSON and write to an adjacent JSONL file.

    Args:
        result: The generated output content (JSON entity object).
        context: HookContext with keys including day, segment, name,
            output_path, meta, transcript.

    Returns:
        None - this hook does not modify the output result.
    """
    try:
        data = json.loads(result)
    except json.JSONDecodeError:
        logger.warning("entities hook: malformed payload (invalid JSON)")
        return None

    if not isinstance(data, dict) or not isinstance(data.get("entities"), list):
        logger.warning("entities hook: malformed payload (bad shape)")
        return None

    entities = []
    dropped = 0
    for item in data["entities"]:
        if not isinstance(item, dict):
            dropped += 1
            continue

        etype = item.get("type")
        name = item.get("name")
        description = item.get("description")
        if (
            not isinstance(etype, str)
            or not isinstance(name, str)
            or not isinstance(description, str)
        ):
            dropped += 1
            continue

        entity = {
            "type": etype.strip(),
            "name": name.strip(),
            "description": description.strip(),
        }
        if (
            not entity["type"]
            or not entity["name"]
            or not entity["description"]
            or entity["type"] not in ENTITY_TYPES
        ):
            dropped += 1
            continue

        entities.append(entity)

    if dropped:
        logger.warning("entities hook: dropped %d malformed entity items", dropped)

    if not entities:
        logger.info("entities hook: no entities extracted")
        return None

    # Deduplicate by (type, name) - keep first occurrence
    seen = set()
    unique_entities = []
    for entity in entities:
        key = (entity["type"].lower(), entity["name"].lower())
        if key not in seen:
            seen.add(key)
            unique_entities.append(entity)

    if len(unique_entities) < len(entities):
        logger.info(
            "entities hook: deduplicated %d -> %d entities",
            len(entities),
            len(unique_entities),
        )

    # Write entities.jsonl alongside the agent output in the talents/ directory
    output_path_value = context.get("output_path")
    if not output_path_value:
        logger.error("entities hook: missing output_path in context")
        return None

    output_path = Path(output_path_value)
    agents_dir = output_path.parent
    jsonl_path = agents_dir / "entities.jsonl"

    # Write JSONL file
    try:
        jsonl_path.parent.mkdir(parents=True, exist_ok=True)
        with open(jsonl_path, "w", encoding="utf-8") as f:
            for entity in unique_entities:
                f.write(json.dumps(entity) + "\n")
        logger.info(
            "entities hook: wrote %d entities to %s",
            len(unique_entities),
            jsonl_path,
        )
    except Exception as e:
        logger.error("entities hook: failed to write JSONL: %s", e)
        return None

    try:
        index_file(get_journal(), str(jsonl_path))
    except Exception:
        logger.warning("entities hook: index failed for %s", jsonl_path, exc_info=True)

    return None  # Don't modify insight result
