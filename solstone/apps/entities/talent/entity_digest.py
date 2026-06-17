# SPDX-License-Identifier: AGPL-3.0-only
# Copyright (c) 2026 sol pbc

from __future__ import annotations

from solstone.think.activities import assemble_activity_records_and_narratives
from solstone.think.entities.loading import load_entities


def _entity_line(entity: dict) -> str:
    name = str(entity.get("name") or "")
    etype = str(entity.get("type") or "")
    desc = str(entity.get("description") or "")
    return f"- {name} ({etype}): {desc}"


def _entities_section(entities: list[dict], empty_msg: str) -> str:
    if not entities:
        return empty_msg
    ordered = sorted(
        entities,
        key=lambda e: (
            str(e.get("name") or "").casefold(),
            str(e.get("id") or ""),
        ),
    )
    return "\n".join(_entity_line(e) for e in ordered)


def assemble_facet_day_digest(facet: str, day: str) -> str:
    records_and_narratives = assemble_activity_records_and_narratives(facet, day)
    detected_section = _entities_section(
        load_entities(facet, day),
        "No entities detected on this day.",
    )
    attached_section = _entities_section(
        load_entities(facet),
        "No entities attached to this facet.",
    )

    return (
        f"# Entity digest for {facet} on {day}\n\n"
        f"{records_and_narratives}\n\n"
        f"## Detected entities (this day)\n{detected_section}\n\n"
        f"## Already-attached entities\n{attached_section}"
    )


def pre_process(context: dict) -> dict | None:
    facet = context.get("facet")
    day = context.get("day")
    if not facet or not day:
        return None
    return {
        "template_vars": {"facet_day_digest": assemble_facet_day_digest(facet, day)}
    }
