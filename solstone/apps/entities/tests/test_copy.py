# SPDX-License-Identifier: AGPL-3.0-only
# Copyright (c) 2026 sol pbc

"""Tests for entities owner-facing copy discipline."""

from __future__ import annotations

import re
from pathlib import Path

from solstone.apps.entities import copy as entity_copy
from solstone.apps.entities.copy import entities_copy_payload, entities_copy_values
from solstone.think.indexer.edges import ATTENDANCE_KINDS, KINDS

EXPECTED_CONN_STRINGS = {
    "ENT_CONN_WITH_YOU_EYEBROW": "with you",
    "ENT_CONN_WITH_YOU_SUMMARY": "{n} moments together · latest {kind} ({day})",
    "ENT_CONN_WITH_YOU_SUMMARY_ONE": "1 moment together · latest {kind} ({day})",
    "ENT_CONN_WITH_YOU_TOGGLE": "the moments behind this ›",
    "ENT_CONN_WITH_YOU_FAILED": (
        "couldn't check your own history with this entity just now."
    ),
    "ENT_CONN_SUMMARY": "{s} with direct evidence · {a} from shared events only",
    "ENT_CONN_EVENTS_ONLY": "events only",
    "ENT_CONN_ROW_META": "{n} moments · {day}",
    "ENT_CONN_ROW_META_ATTENDANCE": "{n} events together · {day}",
    "ENT_CONN_ROW_META_ONE": "1 moment · {day}",
    "ENT_CONN_ROW_META_ONE_ATTENDANCE": "1 event together · {day}",
    "ENT_CONN_EVIDENCE_CAPTION": (
        "the moments behind this — evidence, not a conclusion"
    ),
    "ENT_CONN_EVIDENCE_MORE": "showing {k} of {n} — show more",
    "ENT_CONN_EVIDENCE_VIEW_ENTITY": "view entity →",
    "ENT_CONN_EVIDENCE_FAILED": "couldn't load these moments. try again.",
    "ENT_CONN_UPCOMING": " (upcoming)",
    "ENT_CONN_EMPTY": (
        "no connections recorded yet — they appear as sol notices who and what this "
        "entity shows up with."
    ),
    "ENT_CONN_INDEX_UNAVAILABLE": "the connections index hasn't been built yet.",
    "ENT_CONN_INDEX_ACTION": "check system health →",
    "ENT_CONN_AMBIGUOUS": "sol isn't sure which entity this is yet.",
    "ENT_CONN_LOAD_FAILED": "couldn't load connections. try again.",
}

EXPECTED_CONN_KIND_WORDS = {
    "works-with": "works with",
    "works-at": "works at",
    "reports-to": "reports to",
    "family-of": "family",
    "knows": "knows",
    "uses": "uses",
    "created": "created",
    "decided-with": "decided together",
    "committed-to": "committed",
    "spoke-with": "spoke",
    "mentioned": "mentioned",
    "messaged-with": "messaged",
    "scheduled-with": "scheduled",
    "party-of": "party to",
    "other": "related",
    "attended-with": "events",
    "co-present": "around together",
}

EXPECTED_CONN_KIND_CHIP_WORDS = {
    "committed-to": "commitments",
    "mentioned": "mentions",
}

EXPECTED_CONN_SOURCE_WORDS = {
    "event-legacy": "older calendar events",
    "co-presence": "shared moments",
    "participation": "activity records",
    "commitment": "commitments",
    "closure": "commitments",
    "decision": "decisions",
    "relation": "what sol has learned",
    "observation": "what sol has learned",
    "speaker": "conversation",
    "mention": "conversation",
    "messaging": "messages",
    "calendar": "calendar events",
    "document": "documents",
}

EXPECTED_CONN_LABEL_FALLBACKS = {
    "spoke-with": "a conversation",
    "co-present": "time in the same place",
    "mentioned": "came up in conversation",
}


def test_no_literal_copy_in_templates():
    """Templates reference ENT_COPY constants; prose values are never inlined."""

    root = Path("solstone/apps/entities")

    hits: list[tuple[Path, str]] = []
    for path in root.rglob("*.html"):
        text = path.read_text(encoding="utf-8")
        for value in entities_copy_values():
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
        for path in Path("solstone/apps/entities").rglob("*.html")
    )

    missing = [name for name in entities_copy_payload() if name not in html]

    assert missing == []


def test_entities_index_injects_copy(client):
    resp = client.get("/app/entities/api/state")
    assert resp.status_code == 200
    assert resp.get_json() == {
        "entities_copy": entities_copy_payload(),
        "attendance_kinds": sorted(ATTENDANCE_KINDS),
    }


def test_connection_copy_is_pinned_byte_for_byte():
    for name, value in EXPECTED_CONN_STRINGS.items():
        assert getattr(entity_copy, name) == value

    assert entity_copy.ENT_CONN_KIND_WORDS == EXPECTED_CONN_KIND_WORDS
    assert entity_copy.ENT_CONN_KIND_CHIP_WORDS == EXPECTED_CONN_KIND_CHIP_WORDS
    assert entity_copy.ENT_CONN_SOURCE_WORDS == EXPECTED_CONN_SOURCE_WORDS
    assert entity_copy.ENT_CONN_SOURCE_WORD_FALLBACK == "what sol has learned"
    assert entity_copy.ENT_CONN_LABEL_FALLBACKS == EXPECTED_CONN_LABEL_FALLBACKS


def test_connection_copy_maps_match_edge_contracts():
    assert set(entity_copy.ENT_CONN_KIND_WORDS) == KINDS
    assert set(entity_copy.ENT_CONN_SOURCE_WORDS) == {
        "event-legacy",
        "co-presence",
        "participation",
        "commitment",
        "closure",
        "decision",
        "relation",
        "observation",
        "speaker",
        "mention",
        "messaging",
        "calendar",
        "document",
    }


def test_connection_dict_copy_values_are_flattened():
    values = entities_copy_values()

    assert "works with" in values
    assert "commitments" in values
    assert "what sol has learned" in values
    assert "a conversation" in values


def test_entities_index_serves_spa_shell(client):
    resp = client.get("/app/entities/")

    assert resp.status_code == 200
    assert b'data-solstone-shell="spa"' in resp.data


def test_entities_state_path_resolves(client):
    adapter = client.application.url_map.bind("localhost")

    endpoint, _args = adapter.match("/app/entities/api/state", method="GET")

    assert endpoint == "app:entities.api_state"


def test_entities_state_error_returns_envelope(client, monkeypatch):
    from solstone.apps.entities import routes

    def raise_copy_payload():
        raise RuntimeError("copy failed")

    monkeypatch.setattr(routes, "entities_copy_payload", raise_copy_payload)
    resp = client.get("/app/entities/api/state")

    assert resp.status_code == 500
    payload = resp.get_json()
    assert payload["reason_code"] == "entity_operation_failed"
    assert payload["detail"] == "Failed to load entities state."
