# SPDX-License-Identifier: AGPL-3.0-only
# Copyright (c) 2026 sol pbc

"""Hook for merging storyteller outputs onto activity records."""

from __future__ import annotations

import json
import logging
import math
from typing import Any

from solstone.think.activities import merge_story_fields
from solstone.think.entities.loading import load_entities
from solstone.think.entities.matching import (
    EntityResolutionOutcome,
    ResolutionOrigin,
    ResolutionScope,
    record_entity_resolution,
)

logger = logging.getLogger(__name__)

ALLOWED_RESOLUTIONS = frozenset({"sent", "done", "signed", "dropped", "deferred"})
ALLOWED_RELATION_KINDS = frozenset(
    {
        "works-with",
        "works-at",
        "reports-to",
        "family-of",
        "knows",
        "uses",
        "created",
        "other",
    }
)


def _normalize_topics(value: Any) -> list[str] | None:
    if not isinstance(value, list):
        logger.warning("story hook: missing topics list")
        return None

    topics: list[str] = []
    seen: set[str] = set()
    for item in value:
        if not isinstance(item, str):
            logger.warning("story hook: invalid topics list")
            return None
        topic = item.strip().lower()
        if not topic or topic in seen:
            continue
        seen.add(topic)
        topics.append(topic)
        if len(topics) >= 10:
            break

    if not topics:
        logger.warning("story hook: empty topics after normalization")
        return None

    return topics


def _normalize_confidence(value: Any) -> float | None:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        logger.warning("story hook: invalid confidence value")
        return None

    confidence = float(value)
    if math.isnan(confidence):
        logger.warning("story hook: invalid confidence value")
        return None

    clamped = min(1.0, max(0.0, confidence))
    if clamped != confidence:
        logger.warning("story hook: clamped confidence %s to %s", confidence, clamped)
    return clamped


def _resolve_entity_id(
    name: str,
    entities: list[dict[str, Any]],
    *,
    scope: ResolutionScope,
    origin: ResolutionOrigin,
) -> str | None:
    resolution = record_entity_resolution(
        name,
        entities,
        scope=scope,
        origin=origin,
        fuzzy_threshold=90,
    )
    if resolution.outcome == EntityResolutionOutcome.RESOLVED and resolution.entity:
        return str(resolution.entity.get("id") or "") or None
    return None


def _validate_fields(
    entry: dict[str, Any], required_fields: tuple[str, ...]
) -> dict[str, str] | None:
    normalized: dict[str, str] = {}
    for field in required_fields:
        value = entry.get(field)
        if not isinstance(value, str):
            return None
        normalized[field] = value
    return normalized


def post_process(result: str, context: dict) -> str:
    """Validate storyteller JSON and merge it onto an activity record."""
    try:
        data = json.loads(result.strip())
    except (json.JSONDecodeError, ValueError) as exc:
        logger.error("story hook: failed to parse JSON: %s", exc)
        return ""

    if not isinstance(data, dict):
        logger.warning("story hook: expected top-level object")
        return ""

    body = data.get("body")
    topics = data.get("topics")
    confidence = data.get("confidence")
    commitments = data.get("commitments")
    closures = data.get("closures")
    decisions = data.get("decisions")
    relations = data.get("relations")

    if not isinstance(body, str) or not body.strip():
        logger.warning("story hook: missing body")
        return ""
    topics = _normalize_topics(topics)
    if topics is None:
        return ""
    confidence = _normalize_confidence(confidence)
    if confidence is None:
        return ""
    if not isinstance(commitments, list):
        logger.warning("story hook: missing commitments list")
        return ""
    if not isinstance(closures, list):
        logger.warning("story hook: missing closures list")
        return ""
    if not isinstance(decisions, list):
        logger.warning("story hook: missing decisions list")
        return ""
    if not isinstance(relations, list):
        logger.warning("story hook: missing relations list")
        return ""

    activity = context.get("activity")
    if not isinstance(activity, dict):
        logger.warning("story hook: missing activity context")
        return ""

    record_id = activity.get("id")
    if not isinstance(record_id, str) or not record_id:
        logger.warning("story hook: missing activity record id")
        return ""

    facet = context.get("facet")
    day = context.get("day")
    if not isinstance(facet, str) or not facet or not isinstance(day, str) or not day:
        logger.warning("story hook: missing facet/day context")
        return ""

    entities = load_entities(facet=facet, day=day)
    resolution_scope = ResolutionScope.facet_scope(facet)

    def origin(field: str) -> ResolutionOrigin:
        return ResolutionOrigin(
            lane="talent.story",
            facet=facet,
            day=day,
            record_id=record_id,
            field=field,
        )

    resolved_commitments: list[dict[str, Any]] = []
    for index, entry in enumerate(commitments):
        if not isinstance(entry, dict):
            logger.warning(
                "story hook: skipping commitment[%d]: expected object", index
            )
            continue
        normalized = _validate_fields(
            entry, ("owner", "action", "counterparty", "when", "context")
        )
        if normalized is None:
            logger.warning(
                "story hook: skipping commitment[%d]: missing required string field",
                index,
            )
            continue
        resolved_commitment = dict(normalized)
        resolved_commitment["owner_entity_id"] = _resolve_entity_id(
            normalized["owner"],
            entities,
            scope=resolution_scope,
            origin=origin("commitments.owner"),
        )
        resolved_commitment["counterparty_entity_id"] = _resolve_entity_id(
            normalized["counterparty"],
            entities,
            scope=resolution_scope,
            origin=origin("commitments.counterparty"),
        )
        resolved_commitments.append(resolved_commitment)

    resolved_closures: list[dict[str, Any]] = []
    for index, entry in enumerate(closures):
        if not isinstance(entry, dict):
            logger.warning("story hook: skipping closure[%d]: expected object", index)
            continue
        normalized = _validate_fields(
            entry, ("owner", "action", "counterparty", "resolution", "context")
        )
        if normalized is None:
            logger.warning(
                "story hook: skipping closure[%d]: missing required string field",
                index,
            )
            continue
        if normalized["resolution"] not in ALLOWED_RESOLUTIONS:
            logger.warning(
                "story hook: skipping closure[%d]: invalid resolution '%s'",
                index,
                normalized["resolution"],
            )
            continue
        resolved_closure = dict(normalized)
        resolved_closure["owner_entity_id"] = _resolve_entity_id(
            normalized["owner"],
            entities,
            scope=resolution_scope,
            origin=origin("closures.owner"),
        )
        resolved_closure["counterparty_entity_id"] = _resolve_entity_id(
            normalized["counterparty"],
            entities,
            scope=resolution_scope,
            origin=origin("closures.counterparty"),
        )
        resolved_closures.append(resolved_closure)

    resolved_decisions: list[dict[str, Any]] = []
    for index, entry in enumerate(decisions):
        if not isinstance(entry, dict):
            logger.warning("story hook: skipping decision[%d]: expected object", index)
            continue
        normalized = _validate_fields(entry, ("owner", "action", "context"))
        if normalized is None:
            logger.warning(
                "story hook: skipping decision[%d]: missing required string field",
                index,
            )
            continue
        counterparty = entry.get("counterparty")
        if counterparty is not None and not isinstance(counterparty, str):
            logger.warning(
                "story hook: skipping decision[%d]: invalid counterparty field",
                index,
            )
            continue
        resolved_decision = dict(normalized)
        resolved_decision["counterparty"] = counterparty
        resolved_decision["owner_entity_id"] = _resolve_entity_id(
            normalized["owner"],
            entities,
            scope=resolution_scope,
            origin=origin("decisions.owner"),
        )
        resolved_decision["counterparty_entity_id"] = (
            _resolve_entity_id(
                counterparty,
                entities,
                scope=resolution_scope,
                origin=origin("decisions.counterparty"),
            )
            if isinstance(counterparty, str) and counterparty.strip()
            else None
        )
        resolved_decisions.append(resolved_decision)

    resolved_relations: list[dict[str, Any]] = []
    for index, entry in enumerate(relations):
        if not isinstance(entry, dict):
            logger.warning("story hook: skipping relation[%d]: expected object", index)
            continue
        normalized = _validate_fields(entry, ("from", "to", "kind", "note"))
        if normalized is None:
            logger.warning(
                "story hook: skipping relation[%d]: missing required string field",
                index,
            )
            continue
        if normalized["kind"] not in ALLOWED_RELATION_KINDS:
            logger.warning(
                "story hook: skipping relation[%d]: invalid kind '%s'",
                index,
                normalized["kind"],
            )
            continue
        if normalized["kind"] == "other" and not normalized["note"].strip():
            logger.warning(
                "story hook: skipping relation[%d]: other kind requires note",
                index,
            )
            continue
        quote = entry.get("quote")
        if quote is not None and not isinstance(quote, str):
            logger.warning(
                "story hook: skipping relation[%d]: invalid quote field",
                index,
            )
            continue
        resolved_relation = dict(normalized)
        resolved_relation["quote"] = quote
        resolved_relation["from_entity_id"] = _resolve_entity_id(
            normalized["from"],
            entities,
            scope=resolution_scope,
            origin=origin("relations.from"),
        )
        resolved_relation["to_entity_id"] = _resolve_entity_id(
            normalized["to"],
            entities,
            scope=resolution_scope,
            origin=origin("relations.to"),
        )
        resolved_relations.append(resolved_relation)

    talent_name = context.get("name") or ""
    if not talent_name:
        logger.warning("story hook: missing talent name in context")

    story = {
        "talent": talent_name,
        "body": body.strip(),
        "topics": topics,
        "confidence": confidence,
    }

    merge_story_fields(
        facet,
        day,
        record_id,
        story=story,
        commitments=resolved_commitments,
        closures=resolved_closures,
        decisions=resolved_decisions,
        relations=resolved_relations,
        actor="story",
        note=None,
    )

    return ""
