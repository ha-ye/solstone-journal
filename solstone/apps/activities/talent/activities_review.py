# SPDX-License-Identifier: AGPL-3.0-only
# Copyright (c) 2026 sol pbc

from __future__ import annotations

from solstone.think.activities import assemble_activity_records_and_narratives


def assemble_activity_evidence(facet: str, day: str) -> str:
    return (
        f"# Activity evidence for {facet} on {day}\n\n"
        + assemble_activity_records_and_narratives(facet, day)
    )


def pre_process(context: dict) -> dict | None:
    facet = context.get("facet")
    day = context.get("day")
    if not facet or not day:
        return None
    return {
        "template_vars": {"activity_evidence": assemble_activity_evidence(facet, day)}
    }
