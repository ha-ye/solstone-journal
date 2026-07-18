# SPDX-License-Identifier: AGPL-3.0-only
# Copyright (c) 2026 sol pbc

"""Tests for curation owner-facing copy discipline."""

from __future__ import annotations

import re
from pathlib import Path

from solstone.apps.curation import copy as copy_module
from solstone.apps.curation.copy import (
    curation_copy_payload,
    curation_copy_values,
)


def test_no_literal_copy_in_templates():
    """Templates reference CUR_COPY constants; prose values are never inlined."""

    root = Path("solstone/apps/curation")
    structural_js_literals = {"merge"}

    hits: list[tuple[Path, str]] = []
    for path in root.rglob("*.html"):
        text = path.read_text(encoding="utf-8")
        for value in curation_copy_values():
            if value in structural_js_literals:
                continue
            literal_patterns = (
                re.compile(rf">\s*{re.escape(value)}\s*<"),
                re.compile(rf"(?<!=)['\"`]{re.escape(value)}['\"`]"),
            )
            if any(pattern.search(text) for pattern in literal_patterns):
                hits.append((path, value))

    assert hits == []


def test_all_copy_constants_referenced_by_render_surface():
    html = "\n".join(
        path.read_text(encoding="utf-8")
        for path in Path("solstone/apps/curation").rglob("*.html")
    )

    missing = [name for name in curation_copy_payload() if name not in html]

    assert missing == []


def test_speaker_copy_avoids_banned_verbs():
    banned = re.compile(r"\b(capture|watch|record|monitor|track|collect)\b", re.I)
    hits = {
        name: value
        for name, value in vars(copy_module).items()
        if name.startswith("CUR_SPEAKER_")
        and isinstance(value, str)
        and banned.search(value)
    }

    assert hits == {}


def test_preview_observations_label_is_pinned_byte_for_byte():
    assert copy_module.CUR_PREVIEW_OBSERVATIONS_LABEL == "notes moved"


def test_curation_copy_literals_are_folded_byte_for_byte():
    expected = {
        "CUR_HEADING": "suggestions",
        "CUR_FACET_BODY": (
            "solstone noticed recent activity that doesn't fit your facets well. "
            'create a "{name}" facet?'
        ),
        "CUR_FACET_CREATE_ACTION": "create facet",
        "CUR_FACET_DISMISS_ACTION": "not now",
        "CUR_FACET_EVIDENCE_ACTION": "view evidence",
        "CUR_ENTITY_BODY": '"{a}" and "{b}" look like the same entity. merge them?',
        "CUR_ENTITY_MERGE_ACTION": "merge",
        "CUR_ENTITY_DISMISS_ACTION": "keep separate",
        "CUR_SPEAKER_BODY": (
            'solstone noticed "{source}" and "{target}" may be the same speaker. '
            "merge them?"
        ),
        "CUR_SPEAKER_MERGE_ACTION": "review merge",
        "CUR_SPEAKER_DISMISS_ACTION": "keep separate",
        "CUR_EMPTY_STATE": (
            "nothing to review — solstone hasn't spotted new structure to suggest."
        ),
        "CUR_ENTITY_PREVIEW_LEAD": "before merging, here's what will change.",
        "CUR_ENTITY_CONFIRM_ACTION": "confirm merge",
        "CUR_ENTITY_CANCEL_ACTION": "cancel",
        "CUR_ENTITY_SELECT_ALL_ACTION": "select all",
        "CUR_ENTITY_BATCH_MERGE_ACTION": "merge selected",
        "CUR_ENTITY_BATCH_DISMISS_ACTION": "keep selected separate",
        "CUR_ENTITY_BATCH_MERGE_LEAD": "these pairs will be merged:",
        "CUR_ENTITY_BATCH_DISMISS_LEAD": "these pairs will be kept separate:",
        "CUR_ENTITY_BATCH_CONFIRM_MERGE_ACTION": "merge all",
        "CUR_ENTITY_BATCH_CONFIRM_DISMISS_ACTION": "keep all separate",
        "CUR_ENTITY_BATCH_MERGE_SUMMARY": "merged {ok} of {total}.",
        "CUR_ENTITY_BATCH_DISMISS_SUMMARY": "kept {ok} of {total} separate.",
        "CUR_ENTITY_BATCH_FAILED_NOTE": "{failed} still need attention below.",
        "CUR_AMBIGUITY_ORIGIN_LABEL": "noticed in",
        "CUR_AMBIGUITY_CHOOSE_ACTION": "choose {name}",
        "CUR_UNDO_ACTION": "undo merge",
        "CUR_UNDO_DONE": "merge undone.",
        "CUR_UNDO_UNAVAILABLE": "undo isn't available for this earlier merge.",
        "CUR_UNDO_FAILED": "the merge couldn't be undone.",
        "CUR_ENTITY_PREVIEW_EMPTY": "no journal changes are needed for this merge.",
        "CUR_ENTITY_PREVIEW_ERRORS": "some segment updates may need attention.",
        "CUR_PREVIEW_AKAS_LABEL": "aliases added",
        "CUR_PREVIEW_EMAILS_LABEL": "emails added",
        "CUR_PREVIEW_FACETS_LABEL": "facet links",
        "CUR_PREVIEW_OBSERVATIONS_LABEL": "notes moved",
        "CUR_PREVIEW_SEGMENTS_LABEL": "speaker labels updated",
        "CUR_PREVIEW_VOICEPRINTS_LABEL": "voice samples moved",
    }

    for name, value in expected.items():
        assert getattr(copy_module, name) == value


def test_curation_state_serves_copy(curation_env):
    env = curation_env()

    resp = env.client.get("/app/curation/api/state")

    assert resp.status_code == 200
    assert resp.get_json()["copy"] == curation_copy_payload()
