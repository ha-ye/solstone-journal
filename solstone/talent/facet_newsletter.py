# SPDX-License-Identifier: AGPL-3.0-only
# Copyright (c) 2026 sol pbc

"""Pre-hook source packet and post-hook persistence for facet newsletters."""

from __future__ import annotations

import json
import logging
import os
from datetime import datetime
from pathlib import Path
from typing import Any

from solstone.think.activities import load_activity_records
from solstone.think.entities.loading import load_entities
from solstone.think.facets import get_facet_news
from solstone.think.indexer.journal import search_journal
from solstone.think.tools.facets import facet_news, get_facet
from solstone.think.utils import get_journal

logger = logging.getLogger(__name__)

_MAX_ACTIVITY_RECORDS = 12
_MAX_NARRATIVES_PER_ACTIVITY = 4
_INDEX_RESULTS_PER_AGENT = 10
_MAX_ATTACHED_ENTITY_RECORDS = 12
_MAX_DETECTED_ENTITY_RECORDS = 12
_MAX_ENTITY_RESULTS = 12
_MAX_TITLE_CHARS = 220
_MAX_DESCRIPTION_CHARS = 700
_MAX_DETAILS_CHARS = 1200
_MAX_STORY_BODY_CHARS = 1800
_MAX_NARRATIVE_CHARS = 2400
_MAX_INDEX_TEXT_CHARS = 1800
_MAX_ENTITY_TEXT_CHARS = 1200
_MAX_PRIOR_NEWSLETTER_CHARS = 4000
_MAX_FACET_SUMMARY_CHARS = 3000
_MAX_PACKET_CHARS = 56000

_TIER_ONE_INDEX_AGENTS = ("flow", "span", "event", "meetings")
_TIER_TWO_INDEX_AGENTS = ("decisions", "followups")


def pre_process(config: dict) -> dict | None:
    """Gather deterministic facet/day sources for one-shot newsletter generation."""
    facet_value = config.get("facet")
    if facet_value is None:
        return {"skip_reason": "missing facet"}

    day_value = config.get("day")
    if day_value is None:
        return {"skip_reason": "missing day"}

    facet = str(facet_value).strip()
    day = str(day_value).strip()
    if not day:
        return {"skip_reason": "missing day"}

    if not _check_valid_day(day):
        return {"skip_reason": f"invalid day: {day}"}

    if _check_unsafe_facet(facet):
        return {"skip_reason": f"unsafe facet: {facet}"}

    try:
        journal_root = Path(get_journal())
    except Exception as exc:
        logger.exception("facet newsletter pre-hook could not resolve journal")
        return {"skip_reason": f"journal unavailable: {exc}"}

    try:
        packet = _gather_packet(facet=facet, day=day, journal_root=journal_root)
    except Exception as exc:
        logger.exception("facet newsletter pre-hook failed")
        return {"skip_reason": f"facet newsletter pre-hook failed: {exc}"}

    if packet["substantive_items"] == 0:
        return {"skip_reason": "no substantive facet/day sources"}

    return {
        "template_vars": {
            "source_packet": packet["source_packet"],
            "source_counts": packet["source_counts"],
            "source_gaps": json.dumps(packet["gaps"]),
            "coverage_preamble": packet["coverage_preamble"],
        }
    }


def _check_valid_day(day: str) -> bool:
    if len(day) != 8 or not day.isdigit():
        return False
    try:
        parsed = datetime.strptime(day, "%Y%m%d")
    except ValueError:
        return False
    return parsed.strftime("%Y%m%d") == day


def _check_unsafe_facet(facet: str) -> bool:
    return (
        not facet
        or "/" in facet
        or "\\" in facet
        or ".." in facet
        or os.sep in facet
        or facet.startswith(".")
    )


def _gather_packet(*, facet: str, day: str, journal_root: Path) -> dict[str, Any]:
    gaps: list[str] = []
    items: list[dict[str, Any]] = []

    activity_records = _gather_activity_records(facet=facet, day=day, gaps=gaps)
    items.extend(activity_records)
    items.extend(
        _gather_activity_narratives(
            facet=facet,
            day=day,
            journal_root=journal_root,
            gaps=gaps,
        )
    )

    for agent in _TIER_ONE_INDEX_AGENTS:
        items.extend(_search_day_evidence(agent, facet=facet, day=day, gaps=gaps))
    for agent in _TIER_TWO_INDEX_AGENTS:
        items.extend(_search_day_evidence(agent, facet=facet, day=day, gaps=gaps))

    items.extend(_load_facet_metadata(facet, gaps))
    items.extend(_load_facet_entity_context(facet, day, gaps))
    items.extend(_load_prior_newsletter(facet, day, gaps))
    items.extend(_search_facet_entities(facet, day, gaps))

    included, dropped_gaps = _gather_budgeted_items(items)
    gaps.extend(dropped_gaps)

    counts = _gather_source_counts(included)
    _add_available_source_counts(counts, items)
    substantive_items = sum(1 for item in included if item["tier"] in (1, 2))
    counts["substantive_items"] = substantive_items

    return {
        "source_packet": _render_packet(included),
        "source_counts": _render_source_counts(counts),
        "coverage_preamble": _render_coverage_preamble(counts, gaps),
        "gaps": gaps,
        "substantive_items": substantive_items,
    }


def _gather_activity_records(
    *, facet: str, day: str, gaps: list[str]
) -> list[dict[str, Any]]:
    try:
        records = load_activity_records(facet, day)
    except Exception as exc:
        logger.warning(
            "facet newsletter activity records failed for %s %s: %s", facet, day, exc
        )
        gaps.append(f"failed: activity_record failed for {facet} {day}: {exc}")
        return []

    if not records:
        gaps.append(f"missing: activity_record absent for {facet} {day}")
        return []

    sorted_records = sorted(records, key=_render_activity_order)
    if len(sorted_records) > _MAX_ACTIVITY_RECORDS:
        gaps.append(
            "capped: activity_record limited to "
            f"{_MAX_ACTIVITY_RECORDS}/{len(sorted_records)} items"
        )
        sorted_records = sorted_records[:_MAX_ACTIVITY_RECORDS]

    items: list[dict[str, Any]] = []
    for index, record in enumerate(sorted_records):
        origin = str(record.get("id") or f"activity-{index}")
        text, clipped = _render_activity_record(record, origin, gaps)
        items.append(
            _gather_item(
                source_class="activity_record",
                origin=origin,
                tier=1,
                text=text,
                clipped=clipped,
                order_key=(0, *_render_activity_order(record)),
            )
        )
    return items


def _gather_activity_narratives(
    *, facet: str, day: str, journal_root: Path, gaps: list[str]
) -> list[dict[str, Any]]:
    activities_dir = journal_root / "facets" / facet / "activities" / day
    if not activities_dir.exists():
        gaps.append(f"missing: activity_narrative absent for {facet} {day}")
        return []

    items: list[dict[str, Any]] = []
    for activity_dir in sorted(
        path for path in activities_dir.iterdir() if path.is_dir()
    ):
        md_files = sorted(activity_dir.glob("*.md"))
        if not md_files:
            continue
        if len(md_files) > _MAX_NARRATIVES_PER_ACTIVITY:
            gaps.append(
                "capped: activity_narrative limited to "
                f"{_MAX_NARRATIVES_PER_ACTIVITY}/{len(md_files)} items"
            )
            md_files = md_files[:_MAX_NARRATIVES_PER_ACTIVITY]
        for file_index, path in enumerate(md_files):
            origin = f"{activity_dir.name}/{path.name}"
            try:
                raw = path.read_text(encoding="utf-8").strip()
            except Exception as exc:
                logger.warning(
                    "facet newsletter activity narrative failed for %s %s: %s",
                    facet,
                    origin,
                    exc,
                )
                gaps.append(
                    f"failed: activity_narrative failed for {facet} {day}: {exc}"
                )
                continue
            text, clipped = _render_clipped_text(
                raw,
                _MAX_NARRATIVE_CHARS,
                gaps,
                "activity_narrative",
                origin,
                "markdown",
            )
            items.append(
                _gather_item(
                    source_class="activity_narrative",
                    origin=origin,
                    tier=1,
                    text=text,
                    clipped=clipped,
                    agent=path.name,
                    order_key=(1, activity_dir.name, file_index, path.name),
                )
            )

    if not items:
        gaps.append(f"missing: activity_narrative absent for {facet} {day}")
    return items


def _search_day_evidence(
    agent: str,
    *,
    facet: str,
    day: str,
    gaps: list[str],
) -> list[dict[str, Any]]:
    source_label = f"index_result:{agent}"
    try:
        total, results = search_journal(
            "",
            limit=_INDEX_RESULTS_PER_AGENT,
            offset=0,
            day=day,
            facet=facet,
            agent=agent,
        )
    except Exception as exc:
        logger.warning(
            "facet newsletter %s search failed for %s %s: %s",
            agent,
            facet,
            day,
            exc,
        )
        gaps.append(f"failed: {source_label} failed for {facet} {day}: {exc}")
        return []

    if not results:
        gaps.append(f"missing: {source_label} absent for {facet} {day}")
        return []

    if total > len(results):
        gaps.append(f"capped: {source_label} limited to {len(results)}/{total} items")

    tier = 1 if agent in _TIER_ONE_INDEX_AGENTS else 2
    source_order = {
        "flow": 2,
        "span": 3,
        "event": 4,
        "meetings": 5,
        "decisions": 6,
        "followups": 7,
    }.get(agent, 9)
    return [
        _gather_index_item(
            result,
            source_class="index_result",
            agent=agent,
            tier=tier,
            text_limit=_MAX_INDEX_TEXT_CHARS,
            gaps=gaps,
            order_key=(
                source_order,
                index,
                _render_result_path(result),
                result.get("id", ""),
            ),
        )
        for index, result in enumerate(results)
    ]


def _load_prior_newsletter(
    facet: str, day: str, gaps: list[str]
) -> list[dict[str, Any]]:
    try:
        payload = get_facet_news(facet, cursor=day, limit=1)
    except Exception as exc:
        logger.warning("facet newsletter prior news failed for %s: %s", facet, exc)
        gaps.append(f"failed: prior_newsletter failed for {facet}: {exc}")
        return []

    days = payload.get("days") if isinstance(payload, dict) else None
    day_payload = days[0] if isinstance(days, list) and days else None
    raw_content = ""
    source_day = ""
    if isinstance(day_payload, dict):
        raw_content = str(day_payload.get("raw_content") or "").strip()
        source_day = str(day_payload.get("date") or "").strip()
    if not raw_content:
        gaps.append(f"missing: prior_newsletter absent for {facet}")
        return []

    origin = f"{facet}:{source_day or 'unknown'}"
    text, clipped = _render_clipped_text(
        raw_content,
        _MAX_PRIOR_NEWSLETTER_CHARS,
        gaps,
        "prior_newsletter",
        origin,
        "markdown",
    )
    return [
        _gather_item(
            source_class="prior_newsletter",
            origin=origin,
            tier=3,
            text=text,
            clipped=clipped,
            order_key=(1, source_day),
        )
    ]


def _load_facet_metadata(facet: str, gaps: list[str]) -> list[dict[str, Any]]:
    try:
        payload = get_facet(facet)
    except Exception as exc:
        logger.warning("facet newsletter metadata failed for %s: %s", facet, exc)
        gaps.append(f"failed: facet_metadata failed for {facet}: {exc}")
        return []

    if not isinstance(payload, dict):
        gaps.append(f"failed: facet_metadata failed for {facet}: invalid response")
        return []
    if payload.get("error"):
        error = str(payload["error"])
        if "not found" in error.lower():
            gaps.append(f"missing: facet_metadata absent for {facet}")
        else:
            gaps.append(f"failed: facet_metadata failed for {facet}: {error}")
        return []

    summary = str(payload.get("summary") or "").strip()
    if not summary:
        gaps.append(f"missing: facet_metadata absent for {facet}")
        return []

    text, clipped = _render_clipped_text(
        summary,
        _MAX_FACET_SUMMARY_CHARS,
        gaps,
        "facet_metadata",
        facet,
        "summary",
    )
    return [
        _gather_item(
            source_class="facet_metadata",
            origin=facet,
            tier=3,
            text=text,
            clipped=clipped,
            order_key=(0, facet),
        )
    ]


def _load_facet_entity_context(
    facet: str, day: str, gaps: list[str]
) -> list[dict[str, Any]]:
    items: list[dict[str, Any]] = []

    try:
        attached = load_entities(facet)
    except Exception as exc:
        logger.warning(
            "facet newsletter attached entities failed for %s: %s", facet, exc
        )
        gaps.append(f"failed: facet_entities:attached failed for {facet}: {exc}")
        attached = []

    if not attached:
        gaps.append(f"missing: facet_entities:attached absent for {facet}")
    else:
        if len(attached) > _MAX_ATTACHED_ENTITY_RECORDS:
            gaps.append(
                "capped: facet_entities:attached limited to "
                f"{_MAX_ATTACHED_ENTITY_RECORDS}/{len(attached)} items"
            )
            attached = attached[:_MAX_ATTACHED_ENTITY_RECORDS]
        for index, entity in enumerate(attached):
            origin = _render_entity_origin(
                "attached", entity, fallback=f"attached-{index}"
            )
            text, clipped = _render_entity_text(entity, origin, "attached", gaps)
            items.append(
                _gather_item(
                    source_class="facet_entities",
                    origin=origin,
                    tier=3,
                    text=text,
                    clipped=clipped,
                    agent="attached",
                    source_label="facet_entities:attached",
                    order_key=(2, 0, index, origin),
                )
            )

    try:
        detected = load_entities(facet, day)
    except Exception as exc:
        logger.warning(
            "facet newsletter detected entities failed for %s %s: %s",
            facet,
            day,
            exc,
        )
        gaps.append(f"failed: facet_entities:detected failed for {facet} {day}: {exc}")
        detected = []

    if not detected:
        gaps.append(f"missing: facet_entities:detected absent for {facet} {day}")
    else:
        if len(detected) > _MAX_DETECTED_ENTITY_RECORDS:
            gaps.append(
                "capped: facet_entities:detected limited to "
                f"{_MAX_DETECTED_ENTITY_RECORDS}/{len(detected)} items"
            )
            detected = detected[:_MAX_DETECTED_ENTITY_RECORDS]
        for index, entity in enumerate(detected):
            origin = _render_entity_origin(
                "detected", entity, fallback=f"detected-{index}"
            )
            text, clipped = _render_entity_text(entity, origin, "detected", gaps)
            items.append(
                _gather_item(
                    source_class="facet_entities",
                    origin=origin,
                    tier=3,
                    text=text,
                    clipped=clipped,
                    agent="detected",
                    source_label="facet_entities:detected",
                    order_key=(2, 1, index, origin),
                )
            )

    return items


def _search_facet_entities(
    facet: str, day: str, gaps: list[str]
) -> list[dict[str, Any]]:
    try:
        total, results = search_journal(
            "",
            limit=_MAX_ENTITY_RESULTS,
            offset=0,
            day=day,
            facet=facet,
            agent="entity",
        )
    except Exception as exc:
        logger.warning(
            "facet newsletter entity search failed for %s %s: %s", facet, day, exc
        )
        gaps.append(f"failed: facet_entities:indexed failed for {facet} {day}: {exc}")
        return []

    if not results:
        gaps.append(f"missing: facet_entities:indexed absent for {facet} {day}")
        return []

    if total > len(results):
        gaps.append(
            f"capped: facet_entities:indexed limited to {len(results)}/{total} items"
        )

    return [
        _gather_index_item(
            result,
            source_class="facet_entities",
            agent="entity",
            tier=3,
            text_limit=_MAX_ENTITY_TEXT_CHARS,
            gaps=gaps,
            source_label="facet_entities:indexed",
            order_key=(2, 2, index, _render_result_path(result), result.get("id", "")),
        )
        for index, result in enumerate(results)
    ]


def _gather_index_item(
    result: dict[str, Any],
    *,
    source_class: str,
    agent: str,
    tier: int,
    text_limit: int,
    gaps: list[str],
    order_key: tuple,
    source_label: str | None = None,
) -> dict[str, Any]:
    path = _render_result_path(result)
    origin = f"{result.get('id', path)} ({path}; agent={agent})"
    text, clipped = _render_clipped_text(
        str(result.get("text") or "").strip(),
        text_limit,
        gaps,
        source_class,
        origin,
        "text",
        agent=agent,
    )
    return _gather_item(
        source_class=source_class,
        origin=origin,
        tier=tier,
        text=text,
        clipped=clipped,
        agent=agent,
        path=path,
        result_id=str(result.get("id") or ""),
        source_label=source_label,
        order_key=order_key,
    )


def _gather_item(
    *,
    source_class: str,
    origin: str,
    tier: int,
    text: str,
    clipped: bool,
    order_key: tuple,
    agent: str | None = None,
    path: str | None = None,
    result_id: str | None = None,
    source_label: str | None = None,
) -> dict[str, Any]:
    return {
        "source_class": source_class,
        "source_label": source_label,
        "agent": agent,
        "origin": origin,
        "tier": tier,
        "text": text,
        "clipped": clipped,
        "length": len(text),
        "order_key": order_key,
        "path": path,
        "result_id": result_id,
    }


def _gather_budgeted_items(
    items: list[dict[str, Any]],
) -> tuple[list[dict[str, Any]], list[str]]:
    included: list[dict[str, Any]] = []
    gaps: list[str] = []
    for item in sorted(items, key=lambda row: (row["tier"], row["order_key"])):
        if len(_render_packet([*included, item])) <= _MAX_PACKET_CHARS:
            included.append(item)
            continue
        gaps.append(
            "dropped: "
            f"{_render_source_label(item)} {item['origin']} "
            "dropped under total packet budget"
        )
    return included, gaps


def _gather_source_counts(items: list[dict[str, Any]]) -> dict[str, int]:
    counts: dict[str, int] = {
        "total_included": len(items),
        "tier1_included": 0,
        "tier2_included": 0,
        "tier3_included": 0,
        "activity_record": 0,
        "activity_narrative": 0,
        "index_result:event": 0,
        "index_result:meetings": 0,
        "index_result:decisions": 0,
        "index_result:followups": 0,
        "index_result:flow": 0,
        "index_result:span": 0,
        "prior_newsletter": 0,
        "facet_metadata": 0,
        "facet_entities:attached": 0,
        "facet_entities:detected": 0,
        "facet_entities:indexed": 0,
    }
    for item in items:
        tier_key = f"tier{item['tier']}_included"
        counts[tier_key] = counts.get(tier_key, 0) + 1
        label = _render_source_label(item)
        counts[label] = counts.get(label, 0) + 1
    return counts


def _add_available_source_counts(
    counts: dict[str, int], available_items: list[dict[str, Any]]
) -> None:
    available = _gather_source_counts(available_items)
    counts["total_available"] = available["total_included"]
    counts["tier1_available"] = available["tier1_included"]
    counts["tier2_available"] = available["tier2_included"]
    counts["tier3_available"] = available["tier3_included"]
    for key, value in available.items():
        if key == "total_included" or key.startswith("tier"):
            continue
        counts[f"{key}_available"] = value
        counts[f"{key}_included"] = counts.get(key, 0)


def _render_activity_order(record: dict[str, Any]) -> tuple[int, str, str]:
    try:
        created_at = int(record.get("created_at") or 0)
    except (TypeError, ValueError):
        created_at = 0
    return (
        created_at,
        str(record.get("start") or ""),
        str(record.get("id") or ""),
    )


def _render_activity_record(
    record: dict[str, Any], origin: str, gaps: list[str]
) -> tuple[str, bool]:
    clipped = False

    def field_text(field: str, limit: int) -> str:
        nonlocal clipped
        text, was_clipped = _render_clipped_text(
            str(record.get(field) or "").strip(),
            limit,
            gaps,
            "activity_record",
            origin,
            field,
        )
        clipped = clipped or was_clipped
        return text

    lines = [f"Activity ID: {origin}"]
    title = field_text("title", _MAX_TITLE_CHARS)
    if title:
        lines.append(f"Title: {title}")
    activity = str(record.get("activity") or "").strip()
    if activity:
        lines.append(f"Activity: {activity}")
    source = str(record.get("source") or "").strip()
    if source:
        lines.append(f"Source: {source}")
    start = str(record.get("start") or "").strip()
    end = str(record.get("end") or "").strip()
    if start or end:
        lines.append(f"Time: {start or '?'}-{end or '?'}")
    target_date = str(record.get("target_date") or "").strip()
    if target_date:
        lines.append(f"Target date: {target_date}")
    description = field_text("description", _MAX_DESCRIPTION_CHARS)
    if description:
        lines.append(f"Description: {description}")
    details = field_text("details", _MAX_DETAILS_CHARS)
    if details:
        lines.append(f"Details: {details}")
    lines.extend(_render_list_field(record, "active_entities", "Active entities"))
    lines.extend(_render_list_field(record, "segments", "Segments"))
    lines.extend(_render_list_field(record, "participation", "Participation"))
    story = record.get("story")
    if isinstance(story, dict):
        story_body, story_clipped = _render_clipped_text(
            str(story.get("body") or "").strip(),
            _MAX_STORY_BODY_CHARS,
            gaps,
            "activity_record",
            origin,
            "story.body",
        )
        clipped = clipped or story_clipped
        if story_body:
            lines.append(f"Story: {story_body}")
        topics = story.get("topics")
        if isinstance(topics, list) and topics:
            lines.append("Story topics: " + ", ".join(str(topic) for topic in topics))
    for field in ("commitments", "closures", "decisions"):
        values = record.get(field)
        if values:
            lines.append(f"{field.title()}: {json.dumps(values, ensure_ascii=False)}")
    return "\n".join(lines), clipped


def _render_list_field(record: dict[str, Any], field: str, label: str) -> list[str]:
    values = record.get(field)
    if not values:
        return []
    return [f"{label}: {json.dumps(values, ensure_ascii=False)}"]


def _render_clipped_text(
    text: str,
    limit: int,
    gaps: list[str],
    source_class: str,
    origin: str,
    field: str,
    *,
    agent: str | None = None,
) -> tuple[str, bool]:
    if len(text) <= limit:
        return text, False
    gaps.append(
        "clipped: "
        f"{_render_source_label({'source_class': source_class, 'agent': agent})} "
        f"{origin} field {field} clipped to {limit} chars"
    )
    return text[:limit].rstrip(), True


def _render_result_path(result: dict[str, Any]) -> str:
    metadata = result.get("metadata")
    if isinstance(metadata, dict):
        return str(metadata.get("path") or "")
    return ""


def _render_source_label(item: dict[str, Any]) -> str:
    source_label = item.get("source_label")
    if source_label:
        return str(source_label)
    source_class = str(item.get("source_class") or "")
    agent = item.get("agent")
    if source_class == "index_result" and agent:
        return f"index_result:{agent}"
    if source_class == "facet_entities" and agent:
        entity_kind = "indexed" if agent == "entity" else str(agent)
        return f"facet_entities:{entity_kind}"
    return source_class


def _render_entity_origin(kind: str, entity: dict[str, Any], *, fallback: str) -> str:
    name = str(entity.get("name") or entity.get("id") or fallback).strip()
    entity_id = str(entity.get("id") or "").strip()
    if entity_id and entity_id != name:
        return f"{kind}:{name} ({entity_id})"
    return f"{kind}:{name}"


def _render_entity_text(
    entity: dict[str, Any], origin: str, kind: str, gaps: list[str]
) -> tuple[str, bool]:
    payload = {
        key: value
        for key, value in entity.items()
        if value not in (None, "", [], {})
        and key
        in {
            "id",
            "type",
            "name",
            "description",
            "aka",
            "relationship",
            "attached_at",
            "updated_at",
            "last_seen",
            "last_active_day",
            "count",
        }
    }
    text = json.dumps(
        payload or entity, default=str, ensure_ascii=False, sort_keys=True
    )
    return _render_clipped_text(
        text,
        _MAX_ENTITY_TEXT_CHARS,
        gaps,
        "facet_entities",
        origin,
        "json",
        agent=kind,
    )


def _render_packet(items: list[dict[str, Any]]) -> str:
    if not items:
        return "(no included sources)"

    lines: list[str] = []
    current_label = ""
    for item in sorted(items, key=lambda row: (row["tier"], row["order_key"])):
        label = _render_source_label(item)
        if label != current_label:
            if lines:
                lines.append("")
            lines.append(f"## {label}")
            current_label = label
        lines.append("")
        lines.append(f"### {item['origin']}")
        provenance = [
            f"source_class={item['source_class']}",
            f"origin={item['origin']}",
            f"tier={item['tier']}",
            f"clipped={str(bool(item['clipped'])).lower()}",
        ]
        if item.get("agent"):
            provenance.append(f"agent={item['agent']}")
        if item.get("path"):
            provenance.append(f"path={item['path']}")
        if item.get("result_id"):
            provenance.append(f"result_id={item['result_id']}")
        lines.append("Provenance: " + "; ".join(provenance))
        if item["text"]:
            lines.append("")
            lines.append(item["text"])
    return "\n".join(lines).strip()


def _render_source_counts(counts: dict[str, int]) -> str:
    return "\n".join(f"  {key}: {counts[key]}" for key in sorted(counts))


def _render_coverage_preamble(counts: dict[str, int], gaps: list[str]) -> str:
    lines = [
        "Coverage:",
        f"- Included sources: {counts.get('total_included', 0)}",
        f"- Substantive sources: {counts.get('substantive_items', 0)}",
        f"- Tier 1 / 2 / 3: {counts.get('tier1_included', 0)} / "
        f"{counts.get('tier2_included', 0)} / {counts.get('tier3_included', 0)}",
    ]
    if gaps:
        lines.append("Gaps:")
        lines.extend(f"- {gap}" for gap in gaps)
    else:
        lines.append("Gaps: none")
    return "\n".join(lines)


def post_process(result: str, context: dict) -> None:
    """Persist non-empty facet newsletters through the facet news tool."""
    facet = str(context.get("facet") or "").strip()
    day = str(context.get("day") or "").strip()
    if not facet:
        logger.error("facet_newsletter hook: missing facet")
        return None
    if not day:
        logger.error("facet_newsletter hook: missing day")
        return None

    content = (result or "").strip()
    if not content:
        logger.info("facet_newsletter hook: blank newsletter for %s %s", facet, day)
        return None
    if content == "No activity":
        logger.info("facet_newsletter hook: no activity for %s %s", facet, day)
        return None

    try:
        response = facet_news(facet, day, markdown=content)
    except Exception as exc:
        logger.error("facet_newsletter hook: failed to save %s %s: %s", facet, day, exc)
        return None

    if response.get("error"):
        logger.error(
            "facet_newsletter hook: failed to save %s %s: %s",
            facet,
            day,
            response["error"],
        )
    return None
