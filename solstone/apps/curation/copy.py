# SPDX-License-Identifier: AGPL-3.0-only
# Copyright (c) 2026 sol pbc

"""Owner-facing copy constants for the curation app."""

from __future__ import annotations

from typing import Any

CUR_HEADING = "Suggestions"
CUR_FACET_BODY = (
    "solstone noticed recent activity that doesn't fit your facets well. "
    'Create a "{name}" facet?'
)
CUR_FACET_CREATE_ACTION = "Create facet"
CUR_FACET_DISMISS_ACTION = "Not now"
CUR_FACET_EVIDENCE_ACTION = "view evidence"
CUR_ENTITY_BODY = '"{a}" and "{b}" look like the same entity. Merge them?'
CUR_ENTITY_MERGE_ACTION = "Merge"
CUR_ENTITY_DISMISS_ACTION = "Keep separate"
CUR_EMPTY_STATE = (
    "Nothing to review — solstone hasn't spotted new structure to suggest."
)
CUR_ENTITY_PREVIEW_LEAD = "Before merging, here's what will change."
CUR_ENTITY_CONFIRM_ACTION = "Confirm merge"
CUR_ENTITY_CANCEL_ACTION = "Cancel"
CUR_ENTITY_PREVIEW_EMPTY = "No journal changes are needed for this merge."
CUR_ENTITY_PREVIEW_ERRORS = "Some segment updates may need attention."
CUR_PREVIEW_AKAS_LABEL = "Aliases added"
CUR_PREVIEW_EMAILS_LABEL = "Emails added"
CUR_PREVIEW_FACETS_LABEL = "Facet links"
CUR_PREVIEW_OBSERVATIONS_LABEL = "Observations moved"
CUR_PREVIEW_SEGMENTS_LABEL = "Speaker labels updated"
CUR_PREVIEW_VOICEPRINTS_LABEL = "Voice samples moved"


def curation_copy_payload() -> dict[str, Any]:
    """Return copy constants for templates and browser code."""
    return {
        name: value
        for name, value in globals().items()
        if name.startswith("CUR_") and name.isupper()
    }


def curation_copy_values() -> list[str]:
    """Return all verbatim copy values, flattening list constants."""
    values: list[str] = []
    for value in curation_copy_payload().values():
        if isinstance(value, str):
            values.append(value)
        elif isinstance(value, list):
            values.extend(item for item in value if isinstance(item, str))
    return values
