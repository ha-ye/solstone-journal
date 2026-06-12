# SPDX-License-Identifier: AGPL-3.0-only
# Copyright (c) 2026 sol pbc

"""Pre-hook for the morning briefing generate talent."""

from __future__ import annotations

import json
import logging
from datetime import datetime, timedelta
from pathlib import Path
from typing import Any

from solstone.think.activities import load_activity_records
from solstone.think.facets import get_enabled_facets, get_facet_news
from solstone.think.indexer.journal import search_journal
from solstone.think.utils import get_journal

logger = logging.getLogger(__name__)


def pre_process(config: dict) -> dict | None:
    """Gather briefing sources and return template vars for generation."""
    if config.get("dry_run"):
        logger.debug("morning briefing pre-hook dry_run: read-only gather")

    day = str(config.get("day") or "").strip()
    if not day:
        return {"skip_reason": "missing day"}

    try:
        analysis_day = datetime.strptime(day, "%Y%m%d")
    except ValueError:
        return {"skip_reason": f"invalid day: {day}"}

    try:
        journal_root = Path(get_journal())
    except Exception as exc:
        logger.exception("morning briefing pre-hook could not resolve journal")
        return {"skip_reason": f"journal unavailable: {exc}"}

    try:
        packet = _build_packet(
            day=day,
            analysis_day=analysis_day,
            journal_root=journal_root,
            model=str(config.get("model") or "unknown"),
        )
    except Exception as exc:
        logger.exception("morning briefing pre-hook failed")
        return {"skip_reason": f"morning briefing pre-hook failed: {exc}"}

    return {"template_vars": packet}


def _build_packet(
    *,
    day: str,
    analysis_day: datetime,
    journal_root: Path,
    model: str,
) -> dict[str, str]:
    gaps: list[str] = []
    counts: dict[str, int | str] = {
        "segments": 0,
        "anticipated_activities": 0,
        "facet_newsletters": 0,
        "followups": 0,
        "steward_health": "missing",
    }

    facets = _load_facets(gaps)
    newsletters = _load_facet_newsletters(facets, day, gaps)
    anticipated_today = _load_anticipated_activities(
        facets,
        [day],
        gaps,
        empty_gap="no anticipated activities today",
    )
    forward_days = [
        (analysis_day + timedelta(days=offset)).strftime("%Y%m%d")
        for offset in range(1, 8)
    ]
    anticipated_forward = _load_anticipated_activities(
        facets,
        forward_days,
        gaps,
        empty_gap="no anticipated activities in the next 7 days",
    )
    followups_total, followup_results = _search_agent(
        day,
        "followups",
        "follow-up items",
        gaps,
    )
    decisions_total, decision_results = _search_agent(
        day,
        "decisions",
        "decision items",
        gaps,
    )

    pulse = _read_pulse_surface(day, gaps)
    partner = _read_identity_file(journal_root, "partner.md", "partner profile", gaps)
    health = _read_identity_file(
        journal_root,
        "health.md",
        "steward health surface",
        gaps,
    )

    counts["facet_newsletters"] = len(newsletters)
    counts["anticipated_activities"] = len(anticipated_today)
    counts["followups"] = len(followup_results)
    counts["steward_health"] = "present" if health else "missing"
    counts["segments"] = len(_distinct_result_paths(followup_results, decision_results))

    return {
        "generated": datetime.now().isoformat(timespec="seconds"),
        "model": model,
        "active_facets": _render_facets(facets),
        "facet_newsletters": _render_newsletters(newsletters),
        "anticipated_today": _render_activities(anticipated_today),
        "anticipated_forward": _render_activities(
            anticipated_forward, group_by_day=True
        ),
        "pulse_surface": pulse or "(missing)",
        "partner_surface": partner or "(missing)",
        "health_surface": health or "(missing)",
        "followups": _render_search_results(followup_results),
        "decisions": _render_search_results(decision_results),
        "source_counts": _render_source_counts(counts),
        "source_gaps": json.dumps(gaps),
        "coverage_preamble": _render_coverage_preamble(
            counts,
            gaps,
            decisions_total=decisions_total,
            forward_count=len(anticipated_forward),
            followups_total=followups_total,
        ),
    }


def _load_facets(gaps: list[str]) -> dict[str, dict[str, object]]:
    try:
        facets = get_enabled_facets()
    except Exception as exc:
        logger.warning("morning briefing facets unavailable: %s", exc)
        gaps.append(f"active facets unavailable: {exc}")
        return {}
    if not facets:
        gaps.append("no active facets available")
    return facets


def _load_facet_newsletters(
    facets: dict[str, dict[str, object]],
    day: str,
    gaps: list[str],
) -> list[dict[str, str]]:
    newsletters: list[dict[str, str]] = []
    for facet in sorted(facets):
        try:
            payload = get_facet_news(facet, day=day, limit=1)
        except Exception as exc:
            logger.warning("morning briefing news unavailable for %s: %s", facet, exc)
            gaps.append(f"facet newsletter unavailable for {facet}: {exc}")
            continue
        days = payload.get("days") if isinstance(payload, dict) else None
        day_payload = days[0] if isinstance(days, list) and days else None
        raw_content = ""
        if isinstance(day_payload, dict):
            raw_content = str(day_payload.get("raw_content") or "").strip()
        if raw_content:
            newsletters.append({"facet": facet, "day": day, "content": raw_content})
        else:
            gaps.append(f"no facet newsletter available for {facet}")
    if facets and not newsletters:
        gaps.append("no facet newsletters available")
    return newsletters


def _load_anticipated_activities(
    facets: dict[str, dict[str, object]],
    days: list[str],
    gaps: list[str],
    *,
    empty_gap: str,
) -> list[dict[str, Any]]:
    activities: list[dict[str, Any]] = []
    for day in days:
        for facet in sorted(facets):
            try:
                records = load_activity_records(facet, day, include_hidden=False)
            except Exception as exc:
                logger.warning(
                    "morning briefing activities unavailable for %s/%s: %s",
                    facet,
                    day,
                    exc,
                )
                gaps.append(
                    f"anticipated activities unavailable for {facet} {day}: {exc}"
                )
                continue
            for record in records:
                if record.get("source") != "anticipated":
                    continue
                item = dict(record)
                item["facet"] = str(item.get("facet") or facet)
                item["day"] = str(item.get("target_date") or day)
                activities.append(item)
    activities.sort(
        key=lambda item: (
            str(item.get("day") or ""),
            str(item.get("start") or ""),
            str(item.get("facet") or ""),
            str(item.get("title") or ""),
        )
    )
    if facets and not activities:
        gaps.append(empty_gap)
    return activities


def _search_agent(
    day: str,
    agent: str,
    label: str,
    gaps: list[str],
) -> tuple[int, list[dict[str, Any]]]:
    try:
        total, results = search_journal("", limit=10, day=day, agent=agent)
    except Exception as exc:
        logger.warning("morning briefing %s search unavailable: %s", agent, exc)
        gaps.append(f"{label} search unavailable: {exc}")
        return 0, []
    if not results:
        gaps.append(f"no {label} found")
    return total, results


def _read_identity_file(
    journal_root: Path,
    file_name: str,
    label: str,
    gaps: list[str],
) -> str:
    path = journal_root / "identity" / file_name
    if not path.exists():
        gaps.append(f"{label} missing")
        return ""
    try:
        content = path.read_text(encoding="utf-8").strip()
    except Exception as exc:
        logger.warning(
            "morning briefing identity read failed for %s: %s", file_name, exc
        )
        gaps.append(f"{label} unavailable: {exc}")
        return ""
    if not content:
        gaps.append(f"{label} empty")
    return content


def _read_pulse_surface(day: str, gaps: list[str]) -> str | None:
    from solstone.think.day_accumulator import read_latest

    try:
        record = read_latest(day, "pulse")
    except Exception as exc:
        logger.warning("morning briefing pulse read unavailable: %s", exc)
        gaps.append(f"pulse surface unavailable: {exc}")
        return None
    if not record:
        gaps.append("pulse surface")
        return None

    parts = []
    details = str(record.get("full_details") or "").strip()
    if details:
        parts.append(details)
    needs = [str(n).strip() for n in record.get("needs_you", []) if str(n).strip()]
    if needs:
        parts.append("Needs you:\n" + "\n".join(f"- {n}" for n in needs))
    text = "\n\n".join(parts).strip()
    if not text:
        gaps.append("pulse surface")
        return None
    return text


def _render_facets(facets: dict[str, dict[str, object]]) -> str:
    if not facets:
        return "(none)"
    lines = []
    for name, meta in sorted(facets.items()):
        title = str(meta.get("title") or name)
        lines.append(f"- {name}: {title}")
    return "\n".join(lines)


def _render_newsletters(newsletters: list[dict[str, str]]) -> str:
    if not newsletters:
        return "(none)"
    blocks = []
    for item in newsletters:
        blocks.append(
            "\n".join(
                [
                    f"### {item['facet']} newsletter",
                    f"Source: sol://facets/{item['facet']}/news/{item['day']}",
                    item["content"],
                ]
            )
        )
    return "\n\n".join(blocks)


def _render_activities(
    activities: list[dict[str, Any]],
    *,
    group_by_day: bool = False,
) -> str:
    if not activities:
        return "(none)"
    lines: list[str] = []
    last_day: str | None = None
    for item in activities:
        day = str(item.get("day") or "")
        if group_by_day and day != last_day:
            if lines:
                lines.append("")
            lines.append(f"### {day}")
            last_day = day
        time_text = _activity_time(item)
        title = str(item.get("title") or item.get("activity") or "Untitled activity")
        activity = str(item.get("activity") or "activity")
        facet = str(item.get("facet") or "unknown")
        participants = _activity_participants(item)
        detail = f"- {time_text} {title} [{activity}, {facet}]"
        if participants:
            detail += f" - participants: {participants}"
        lines.append(detail)
    return "\n".join(lines)


def _activity_time(item: dict[str, Any]) -> str:
    start = _short_time(item.get("start"))
    end = _short_time(item.get("end"))
    if start and end:
        return f"{start}-{end}"
    if start:
        return start
    return "unscheduled"


def _short_time(value: Any) -> str:
    text = str(value or "").strip()
    if not text:
        return ""
    return text[:5] if len(text) >= 5 else text


def _activity_participants(item: dict[str, Any]) -> str:
    names: list[str] = []
    participation = item.get("participation")
    if isinstance(participation, list):
        for entry in participation:
            if not isinstance(entry, dict):
                continue
            name = str(entry.get("name") or entry.get("entity_id") or "").strip()
            if name:
                names.append(name)
    if not names:
        active_entities = item.get("active_entities")
        if isinstance(active_entities, list):
            names = [
                str(value).strip() for value in active_entities if str(value).strip()
            ]
    return ", ".join(names)


def _render_search_results(results: list[dict[str, Any]]) -> str:
    if not results:
        return "(none)"
    blocks = []
    for result in results:
        metadata = result.get("metadata") or {}
        source_id = str(result.get("id") or "")
        facet = str(metadata.get("facet") or "unknown")
        day = str(metadata.get("day") or "unknown")
        text = str(result.get("text") or "").strip()
        blocks.append(f"- {source_id} [{day}, {facet}]\n  {text}")
    return "\n".join(blocks)


def _distinct_result_paths(
    *result_groups: list[dict[str, Any]],
) -> set[str]:
    paths: set[str] = set()
    for results in result_groups:
        for result in results:
            metadata = result.get("metadata") or {}
            path = str(metadata.get("path") or result.get("id") or "").strip()
            if path:
                paths.add(path)
    return paths


def _render_source_counts(counts: dict[str, int | str]) -> str:
    return "\n".join(
        [
            f"  segments: {counts['segments']}",
            f"  anticipated_activities: {counts['anticipated_activities']}",
            f"  facet_newsletters: {counts['facet_newsletters']}",
            f"  followups: {counts['followups']}",
            f"  steward_health: {counts['steward_health']}",
        ]
    )


def _render_coverage_preamble(
    counts: dict[str, int | str],
    gaps: list[str],
    *,
    decisions_total: int,
    forward_count: int,
    followups_total: int,
) -> str:
    parts = [
        f"{counts['segments']} indexed source paths",
        f"{counts['anticipated_activities']} anticipated activities today",
        f"{forward_count} forward-looking anticipated activities",
        f"{counts['facet_newsletters']} facet newsletters",
        f"{counts['followups']} follow-ups",
        f"{decisions_total} decision results",
    ]
    sentence = "Built from " + ", ".join(parts) + "."
    if followups_total > counts["followups"]:
        sentence += f" Follow-up search returned {followups_total} total matches."
    if gaps:
        sentence += " Gaps: " + "; ".join(gaps) + "."
    else:
        sentence += " No gaps."
    return sentence
