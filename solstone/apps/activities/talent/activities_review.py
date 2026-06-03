# SPDX-License-Identifier: AGPL-3.0-only
# Copyright (c) 2026 sol pbc

from __future__ import annotations

from pathlib import Path

from solstone.think.activities import load_activity_records
from solstone.think.utils import get_journal


def _scalar(value: object) -> str:
    return str(value) if value else "none"


def _joined(values: object) -> str:
    if not values:
        return "none"
    return ", ".join(values) or "none"


def assemble_activity_evidence(facet: str, day: str) -> str:
    records = sorted(
        load_activity_records(facet, day),
        key=lambda r: (r.get("created_at") or 0, r.get("id") or ""),
    )
    record_lines = [
        (
            f"- id={_scalar(record.get('id'))} | "
            f"activity={_scalar(record.get('activity'))} | "
            f"title={_scalar(record.get('title'))} | "
            f"description={_scalar(record.get('description'))} | "
            f"segments={_joined(record.get('segments'))} | "
            f"active_entities={_joined(record.get('active_entities'))}"
        )
        for record in records
    ]
    records_section = (
        "\n".join(record_lines) if record_lines else "No existing activity records."
    )

    base = Path(get_journal()) / "facets" / facet / "activities" / day
    narrative_blocks = []
    for md_path in sorted(base.glob("*/*.md")):
        activity_id = md_path.parent.name
        filename = md_path.name
        body = md_path.read_text(encoding="utf-8").strip()
        narrative_blocks.append(f"### {activity_id}/{filename}\n\n{body}")
    narratives_section = (
        "\n\n".join(narrative_blocks) if narrative_blocks else "No per-span narratives."
    )

    return (
        f"# Activity evidence for {facet} on {day}\n\n"
        f"## Existing records\n{records_section}\n\n"
        f"## Per-span narratives\n{narratives_section}"
    )


def pre_process(context: dict) -> dict | None:
    facet = context.get("facet")
    day = context.get("day")
    if not facet or not day:
        return None
    return {
        "template_vars": {"activity_evidence": assemble_activity_evidence(facet, day)}
    }
