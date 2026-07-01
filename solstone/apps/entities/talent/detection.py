# SPDX-License-Identifier: AGPL-3.0-only
# Copyright (c) 2026 sol pbc

from __future__ import annotations

import json
import logging
from pathlib import Path
from typing import Any

from solstone.think.cluster import _find_segment_dir
from solstone.think.entities.core import EntityDict, entity_slug
from solstone.think.entities.loading import load_entities
from solstone.think.entities.matching import find_matching_entity
from solstone.think.entities.relationships import load_facet_relationship
from solstone.think.entities.saving import upsert_detection_segment
from solstone.think.facets import get_facets
from solstone.think.utils import now_ms

logger = logging.getLogger(__name__)

NOTABILITY_LABELS = {
    "high": "This was a main focus",
    "medium": "This came up clearly",
    "low": "This came up in passing",
}

CENTRALITY_LABELS = {
    "high": "central to this moment",
    "medium": "meaningfully involved",
    "low": "a peripheral mention",
}


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


def _notability_label(raw_level: Any) -> str:
    return NOTABILITY_LABELS.get(str(raw_level), "This came up")


def _centrality_cue(raw_level: Any) -> str | None:
    return CENTRALITY_LABELS.get(str(raw_level))


def _known_lines_for_active_facets(
    name: str,
    segment_facets: list[dict[str, Any]],
) -> list[str]:
    lines: list[str] = []
    for facet_row in segment_facets:
        facet = str(facet_row["facet"])
        match = find_matching_entity(name, load_entities(facet))
        if not match:
            continue
        entity_id = str(match.get("id") or entity_slug(str(match.get("name") or name)))
        relationship = load_facet_relationship(facet, entity_id)
        if not relationship:
            continue
        description = str(relationship.get("description") or "").strip()
        if description:
            lines.append(f"- In {facet}: {description}")

    return lines or ["- No saved notes for the active facets."]


def _daily_summary_lines(
    day: str,
    name: str,
    segment_facets: list[dict[str, Any]],
) -> list[str]:
    slug = entity_slug(name)
    lines: list[str] = []
    for facet_row in segment_facets:
        facet = str(facet_row["facet"])
        for entity in load_entities(facet, day):
            entity_id = str(
                entity.get("id") or entity_slug(str(entity.get("name", "")))
            )
            if entity_id != slug:
                continue
            description = str(entity.get("description") or "").strip()
            if description:
                lines.append(f"Summary so far today in {facet}: {description}")
            break

    if not lines:
        return ["Summary so far today: Nothing saved yet in the active facets."]
    return lines


def _build_packet(
    day: str,
    seg_dir: Path,
    segment_facets: list[dict[str, Any]],
    candidates: list[dict[str, Any]],
) -> str:
    configured_facets = get_facets()
    lines = [
        "This is a moment from today. You keep a running daily log of who and "
        "what mattered, organized by facet.",
        "",
        "## Facets active in this moment",
        "",
    ]

    for facet_row in segment_facets:
        facet = str(facet_row["facet"])
        description = str(
            configured_facets.get(facet, {}).get("description")
            or "No description saved."
        )
        activity = str(facet_row.get("activity") or "")
        lines.extend(
            [
                f"### {facet}",
                f"Facet: {description}",
                f"What happened here: {activity}",
                f"Why it matters: {_notability_label(facet_row.get('level'))}.",
                "",
            ]
        )

    lines.extend(["## People and things noticed", ""])

    for candidate in candidates:
        name = str(candidate.get("name") or "").strip()
        context = str(candidate.get("context") or "").strip()
        if not name:
            continue

        entity_lines = [
            f"### {name}",
            "What's known:",
            *_known_lines_for_active_facets(name, segment_facets),
            *_daily_summary_lines(day, name, segment_facets),
            f"In this moment: {context or 'No one-line activity was provided.'}",
        ]
        cue = _centrality_cue(candidate.get("level"))
        if cue:
            entity_lines.append(f"How central it was: {cue}.")
        entity_lines.append("")
        lines.extend(entity_lines)

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
        segment_facets = {str(row["facet"]) for row in _segment_facets(sense)}
        sense_types = {
            entity_slug(str(candidate["name"]).strip()): str(
                candidate.get("type") or ""
            )
            for candidate in _candidate_rows(sense)
            if isinstance(candidate.get("name"), str) and candidate["name"].strip()
        }

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
            facet = item.get("facet")
            description = item.get("description")
            if (
                not isinstance(name, str)
                or not name.strip()
                or not isinstance(facet, str)
                or facet not in segment_facets
                or not isinstance(description, str)
            ):
                counts["dropped"] += 1
                continue

            slug = entity_slug(name.strip())
            entity_type = sense_types.get(slug)
            if entity_type is None:
                counts["dropped"] += 1
                continue

            kept_by_facet.setdefault(facet, []).append(
                {
                    "name": name.strip(),
                    "type": entity_type,
                    "description": description,
                }
            )

        for facet in sorted(kept_by_facet):
            kept = kept_by_facet.get(facet, [])
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
