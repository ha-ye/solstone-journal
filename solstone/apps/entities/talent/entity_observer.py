# SPDX-License-Identifier: AGPL-3.0-only
# Copyright (c) 2026 sol pbc

"""Entity observer talent hook — pre-computes context and applies observation ops.

pre_process:  Assembles entity context and injects it as $observer_context.

post_process: Parses JSON operations, validates them against attached entities,
              applies clean ops through the observations storage primitive, and
              writes an outcome sidecar.
"""

from __future__ import annotations

import json
import logging

from solstone.talent.story import ALLOWED_RELATION_KINDS
from solstone.think.entities.context import assemble_observer_context
from solstone.think.entities.loading import detected_entities_path, load_entities
from solstone.think.entities.matching import find_matching_entity
from solstone.think.entities.observations import record_observation_ops
from solstone.think.journal_io import LockTimeout
from solstone.think.utils import now_ms

logger = logging.getLogger(__name__)


def pre_process(context: dict) -> dict | None:
    facet = context.get("facet")
    day = context.get("day")
    if not facet or not day:
        return None

    observer_context = assemble_observer_context(facet, day)
    return {"template_vars": {"observer_context": observer_context}}


def _empty_counts() -> dict[str, int]:
    return {
        "update": 0,
        "add": 0,
        "drop": 0,
        "keep": 0,
        "skipped": 0,
        "relation_unresolved": 0,
    }


def _write_outcome(
    facet: str,
    day: str,
    counts: dict[str, int],
    error: str | None,
) -> None:
    payload = {**counts, "error": error, "ts": now_ms()}
    out = detected_entities_path(facet, day).parent / f"{day}_observer_outcome.json"
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text(json.dumps(payload, ensure_ascii=False) + "\n", encoding="utf-8")


def _target_index(value: object) -> int | None:
    if isinstance(value, int) and not isinstance(value, bool):
        return value
    return None


def _target_quote(value: object) -> str | None:
    if value is None:
        return None
    if not isinstance(value, str):
        return None
    value = value.strip()
    return value or None


def _clean_relation(
    value: object,
    op: str,
    entities: list[dict],
) -> tuple[dict | None, str | None]:
    if value is None or op in {"drop", "keep"}:
        return None, None
    if not isinstance(value, dict):
        logger.warning("entity_observer: invalid relation payload for %s", op)
        return None, "skipped"

    kind = value.get("kind")
    target_name = value.get("target_name")
    note = value.get("note")
    if (
        not isinstance(kind, str)
        or not isinstance(target_name, str)
        or not isinstance(note, str)
    ):
        logger.warning("entity_observer: invalid relation fields for %s", op)
        return None, "skipped"
    if kind not in ALLOWED_RELATION_KINDS:
        logger.warning("entity_observer: invalid relation kind %r", kind)
        return None, "skipped"
    if kind == "other" and not note.strip():
        logger.warning("entity_observer: relation kind 'other' requires note")
        return None, "skipped"

    match = find_matching_entity(target_name, entities, fuzzy_threshold=90)
    if not match:
        logger.warning(
            "entity_observer: unresolved relation target %r for %s op",
            target_name,
            op,
        )
        return None, "relation_unresolved"

    return {
        "kind": kind,
        "target_entity_id": match["id"],
        "target_name": target_name,
        "note": note,
    }, None


def _clean_operation(
    item: object, seen_indexes: set[int], entities: list[dict]
) -> tuple[dict | None, str | None]:
    if not isinstance(item, dict):
        return None, "skipped"

    op = item.get("op")
    if op == "add":
        content = item.get("content")
        if not isinstance(content, str) or not content.strip():
            return None, "skipped"
        relation, status = _clean_relation(item.get("relation"), op, entities)
        if status is not None:
            return None, status
        cleaned = {"op": "add", "content": content.strip()}
        if relation is not None:
            cleaned["relation"] = relation
        return cleaned, None

    if op not in {"update", "drop", "keep"}:
        return None, "skipped"

    target_index = _target_index(item.get("target_index"))
    if target_index is None:
        return None, "skipped"
    content: str | None = None
    if op == "update":
        raw_content = item.get("content")
        if not isinstance(raw_content, str) or not raw_content.strip():
            return None, "skipped"
        content = raw_content.strip()
    raw_quote = item.get("target_quote")
    if raw_quote is not None and not isinstance(raw_quote, str):
        return None, "skipped"
    quote = _target_quote(raw_quote)
    if target_index in seen_indexes:
        return None, "skipped"
    seen_indexes.add(target_index)

    cleaned: dict = {"op": op, "target_index": target_index}
    if quote is not None:
        cleaned["target_quote"] = quote

    if content is not None:
        cleaned["content"] = content

    relation, status = _clean_relation(item.get("relation"), op, entities)
    if status is not None:
        return None, status
    if relation is not None:
        cleaned["relation"] = relation

    return cleaned, None


def _merge_counts(target: dict[str, int], source: dict[str, int]) -> None:
    for key in target:
        target[key] += int(source.get(key, 0))


def post_process(result: str, context: dict) -> str | None:
    facet = context.get("facet")
    day = context.get("day")
    if not facet or not day:
        return None

    facet = str(facet)
    day = str(day)
    counts = _empty_counts()
    error: str | None = None

    try:
        try:
            data = json.loads(result)
        except json.JSONDecodeError:
            logger.warning("entity_observer: could not parse result as JSON")
            return None

        if not isinstance(data, dict):
            logger.warning("entity_observer: result is not a JSON object")
            return None

        entities = data.get("entities")
        if not isinstance(entities, list):
            logger.warning("entity_observer: entities is not a list")
            return None

        attached_entities = load_entities(facet)
        valid_entity_ids = {
            entity.get("id") for entity in attached_entities if entity.get("id")
        }

        for entry in entities:
            if not isinstance(entry, dict):
                logger.debug("Skipping non-dict entity operation entry: %r", entry)
                continue
            entity_id = entry.get("entity_id")
            operations = entry.get("operations")
            if not isinstance(operations, list):
                logger.debug("Skipping entity entry without operations list: %r", entry)
                continue
            if not isinstance(entity_id, str):
                counts["skipped"] += len(operations)
                logger.debug("Skipping entity entry with invalid entity_id: %r", entry)
                continue
            # entity_id is enumerated in $observer_context and validated with load_entities(facet).
            # Name-first would touch assemble_observer_context, prompt numbering, record_observation_ops.
            if entity_id not in valid_entity_ids:
                counts["skipped"] += len(operations)
                logger.debug("Skipping unrecognized entity_id: %s", entity_id)
                continue

            clean_ops: list[dict] = []
            seen_indexes: set[int] = set()
            for item in operations:
                clean_op, status = _clean_operation(
                    item, seen_indexes, attached_entities
                )
                if status is not None:
                    counts[status] += 1
                    continue
                if clean_op is not None:
                    clean_ops.append(clean_op)

            if not clean_ops:
                continue

            try:
                op_counts = record_observation_ops(facet, entity_id, clean_ops, day)
            except (LockTimeout, OSError) as exc:
                error = f"{type(exc).__name__}: {exc}"
                counts["skipped"] += len(clean_ops)
                logger.warning(
                    "entity_observer: observation ops failed for %s: %s",
                    entity_id,
                    exc,
                )
                continue

            _merge_counts(counts, op_counts)
    except Exception as exc:
        error = f"{type(exc).__name__}: {exc}"
        logger.warning("entity_observer post-hook failed: %s", exc)
    finally:
        try:
            _write_outcome(facet, day, counts, error)
        except Exception as exc:
            logger.warning("entity_observer outcome write failed: %s", exc)

    return None
