# SPDX-License-Identifier: AGPL-3.0-only
# Copyright (c) 2026 sol pbc

from __future__ import annotations

import json
import logging
from pathlib import Path
from typing import Any

from solstone.think.cluster import _find_segment_dir
from solstone.think.entities.core import EntityDict, entity_slug
from solstone.think.entities.loading import detected_entities_path, load_entities
from solstone.think.entities.matching import find_matching_entity
from solstone.think.entities.observations import load_observations
from solstone.think.entities.relationships import load_facet_relationship
from solstone.think.entities.saving import upsert_detection_segment
from solstone.think.facets import get_facets
from solstone.think.utils import now_ms

logger = logging.getLogger(__name__)

VALID_TYPES = {"Person", "Company", "Project", "Tool"}


def _composite_segment_id(seg_dir: Path) -> str:
    parts = seg_dir.parts
    try:
        ci = parts.index("chronicle")
    except ValueError:
        return seg_dir.name
    return "/".join(parts[ci + 1 :])


def _read_sense(seg_dir: Path) -> dict | None:
    try:
        data = json.loads((seg_dir / "talents" / "sense.json").read_text())
    except (FileNotFoundError, OSError, ValueError):
        return None
    return data if isinstance(data, dict) else None


def _resolve_segment_dir(context: dict) -> Path | None:
    day = context.get("day")
    segment = context.get("segment")
    stream = context.get("stream")
    if not day or not segment:
        return None
    return _find_segment_dir(str(day), str(segment), str(stream) if stream else None)


def _segment_facets(sense: dict) -> list[dict[str, Any]]:
    rows = sense.get("facets") or []
    if not isinstance(rows, list):
        return []
    return [
        row
        for row in rows
        if isinstance(row, dict) and isinstance(row.get("facet"), str) and row["facet"]
    ]


def _candidate_rows(sense: dict) -> list[dict[str, Any]]:
    rows = sense.get("entities") or []
    if not isinstance(rows, list):
        return []
    return [row for row in rows if isinstance(row, dict)]


def _find_attached_context(
    name: str,
    segment_facets: list[dict[str, Any]],
) -> tuple[str, dict[str, Any], dict[str, Any] | None] | None:
    for facet_row in segment_facets:
        facet = str(facet_row["facet"])
        match = find_matching_entity(name, load_entities(facet))
        if match:
            entity_id = str(
                match.get("id") or entity_slug(str(match.get("name", name)))
            )
            relationship = load_facet_relationship(facet, entity_id)
            return facet, dict(match), relationship
    return None


def _current_detection_lines(
    facet: str,
    day: str,
    slug: str,
    composite: str,
) -> list[str]:
    matches = [
        entity
        for entity in load_entities(facet, day)
        if str(entity.get("id") or entity_slug(str(entity.get("name", "")))) == slug
    ]
    if not matches:
        return []

    lines = [f"Current detection in {facet}:"]
    for entity in matches:
        description = str(entity.get("description") or "").strip()
        if description:
            lines.append(f"- Current description: {description}")
        prior = [
            str(row.get("contribution") or "").strip()
            for row in entity.get("segments") or []
            if row.get("segment") != composite
            and str(row.get("contribution") or "").strip()
        ]
        for contribution in prior:
            lines.append(f"- Prior contribution: {contribution}")
    return lines


def _build_packet(
    day: str,
    seg_dir: Path,
    segment_facets: list[dict[str, Any]],
    candidates: list[dict[str, Any]],
) -> str:
    composite = _composite_segment_id(seg_dir)
    configured_facets = get_facets()
    lines = [f"## Segment: {composite}", "", "## Facets active in this segment"]

    for facet_row in segment_facets:
        facet = str(facet_row["facet"])
        description = str(configured_facets.get(facet, {}).get("description") or "")
        activity = str(facet_row.get("activity") or "")
        level = str(facet_row.get("level") or "")
        heading = f"### {facet}"
        if description:
            heading += f" - {description}"
        lines.extend([heading, f"This segment: {activity} (level: {level})", ""])

    lines.append("## Candidate entities")

    for candidate in candidates:
        name = str(candidate.get("name") or "").strip()
        entity_type = str(candidate.get("type") or "").strip()
        role = str(candidate.get("role") or "").strip()
        context = str(candidate.get("context") or "").strip()
        if not name:
            continue

        slug = entity_slug(name)
        lines.extend([f"### {name} ({entity_type}) - role: {role}"])
        if context:
            lines.append(f"Sense context: {context}")

        attached = _find_attached_context(name, segment_facets)
        if attached:
            facet, match, relationship = attached
            entity_id = str(match.get("id") or slug)
            lines.append(f"Attached identity in {facet}: {match.get('name', name)}")
            if relationship:
                rel_desc = str(relationship.get("description") or "").strip()
                if rel_desc:
                    lines.append(f"Facet relationship: {rel_desc}")
            observations = load_observations(facet, entity_id)[-3:]
            for observation in observations:
                content = str(observation.get("content") or "").strip()
                source_day = str(observation.get("source_day") or "").strip()
                if content:
                    suffix = f" ({source_day})" if source_day else ""
                    lines.append(f"Observation{suffix}: {content}")

        for facet_row in segment_facets:
            lines.extend(
                _current_detection_lines(
                    str(facet_row["facet"]),
                    day,
                    slug,
                    composite,
                )
            )

        lines.append("")

    return "\n".join(lines).strip() + "\n"


def pre_process(context: dict) -> dict | None:
    day = context.get("day")
    segment = context.get("segment")
    if not day or not segment:
        return {"skip_reason": "no_sense"}

    seg_dir = _resolve_segment_dir(context)
    if seg_dir is None:
        return {"skip_reason": "no_sense"}

    sense = _read_sense(seg_dir)
    if sense is None:
        return {"skip_reason": "no_sense"}

    segment_facets = _segment_facets(sense)
    if not segment_facets:
        return {"skip_reason": "no_facets"}

    candidates = _candidate_rows(sense)
    if not candidates:
        return {"skip_reason": "no_candidates"}

    packet = _build_packet(str(day), seg_dir, segment_facets, candidates)
    return {"template_vars": {"detection_packet": packet}}


def _write_outcome(seg_dir: Path, counts: dict[str, int], error: str | None) -> None:
    payload = {**counts, "error": error, "ts": now_ms()}
    out = seg_dir / "talents" / "detection_outcome.json"
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text(json.dumps(payload, ensure_ascii=False) + "\n", encoding="utf-8")


def post_process(result: str, context: dict) -> None:
    counts = {"wrote": 0, "skipped": 0, "dropped": 0, "errored": 0}
    error: str | None = None
    seg_dir: Path | None = None

    try:
        seg_dir = _resolve_segment_dir(context)
        if seg_dir is None:
            return None

        day = str(context.get("day"))
        composite = _composite_segment_id(seg_dir)
        sense = _read_sense(seg_dir) or {}
        facet_meta = {
            str(row["facet"]): (
                str(row.get("activity") or ""),
                str(row.get("level") or ""),
            )
            for row in _segment_facets(sense)
        }
        segment_facets = set(facet_meta)

        try:
            data = json.loads(result)
        except json.JSONDecodeError:
            logger.warning("detection post-hook received invalid JSON")
            return None

        if not isinstance(data, dict):
            logger.warning("detection post-hook result is not a JSON object")
            return None
        raw_detections = data.get("detections")
        if not isinstance(raw_detections, list):
            logger.warning("detection post-hook result missing detections array")
            return None

        kept_by_facet: dict[str, list[EntityDict]] = {}
        for item in raw_detections:
            if not isinstance(item, dict):
                counts["dropped"] += 1
                continue
            name = item.get("name")
            entity_type = item.get("type")
            detect = item.get("detect")
            if (
                not isinstance(name, str)
                or not name.strip()
                or entity_type not in VALID_TYPES
                or not isinstance(detect, bool)
            ):
                counts["dropped"] += 1
                continue
            if detect is False:
                counts["skipped"] += 1
                continue

            facet = item.get("facet")
            contribution = item.get("contribution")
            if not isinstance(facet, str) or facet not in segment_facets:
                counts["dropped"] += 1
                continue
            if not isinstance(contribution, str):
                counts["dropped"] += 1
                continue

            activity, level = facet_meta[facet]
            kept_by_facet.setdefault(facet, []).append(
                {
                    "name": name.strip(),
                    "type": str(entity_type),
                    "facet_activity": activity,
                    "level": level,
                    "contribution": contribution.strip(),
                }
            )

        for facet in sorted(segment_facets):
            kept = kept_by_facet.get(facet, [])
            if not kept and not detected_entities_path(facet, day).exists():
                continue
            try:
                res = upsert_detection_segment(facet, day, composite, kept)
                counts["wrote"] += int(res.get("wrote", 0))
            except Exception as exc:
                counts["errored"] += 1
                error = f"{type(exc).__name__}: {exc}"
                logger.warning("detection reconcile failed for %s: %s", facet, exc)
    except Exception as exc:
        counts["errored"] += 1
        error = f"{type(exc).__name__}: {exc}"
        logger.warning("detection post-hook failed: %s", exc)
    finally:
        if seg_dir is not None:
            try:
                _write_outcome(seg_dir, counts, error)
            except Exception as exc:
                logger.warning("detection outcome write failed: %s", exc)

    return None
