# SPDX-License-Identifier: AGPL-3.0-only
# Copyright (c) 2026 sol pbc

from __future__ import annotations

import json
import re
from datetime import datetime

from solstone.apps.home.needs_you import classify_needs_you
from solstone.apps.settings import copy as settings_copy
from solstone.convey import create_app

BANNED_RE = re.compile(r"\b(watch|capture|monitor|track|collect)\b", re.IGNORECASE)


def _assert_clean(text: str) -> None:
    assert BANNED_RE.findall(text) == []


def _minimal_home_context() -> dict:
    item = classify_needs_you(
        None,
        ["Review the launch checklist"],
        [{"text": "Draft the partner note"}],
    )[0].to_dict()
    return {
        "today": "20260524",
        "now": datetime(2026, 5, 24, 12, 0),
        "capture_status": "offline",
        "capture_display_text": "observer offline",
        "last_observe_relative": None,
        "attention": None,
        "pipeline_status": None,
        "segment_count": 0,
        "duration_minutes": 0,
        "facet_data": {},
        "narrative_content": None,
        "narrative_updated_at": None,
        "narrative_source": "flow",
        "narrative_header": "today's flow",
        "pulse_needs": ["Review the launch checklist"],
        "flow_content": None,
        "flow_updated_at": None,
        "anticipated_activities": [],
        "activities": [],
        "todos": [{"text": "Draft the partner note"}],
        "needs_you_items": [item],
        "routines": [],
        "skills": [],
        "skills_summary": "",
        "skills_content": {},
        "briefing_sections": {},
        "briefing_meta": None,
        "briefing_phase": "eod",
        "briefing_lateness": {"late": False, "late_hours": 0},
        "briefing_exists": False,
        "briefing_summary": None,
        "briefing_needs_deduped": [],
        "briefing_needs_shared_count": 0,
        "briefing_needs_badge": None,
        "latest_weekly_reflection": None,
        "yesterday_processing": None,
        "show_welcome": False,
        "narrative_summary": "",
        "routines_summary": "",
        "today_summary": "",
        "needs_summary": "1 item needs attention",
    }


def test_facet_detail_copy_constants_use_allowed_terms():
    for name in settings_copy.__all__:
        if name.startswith("FACET_DETAIL_"):
            _assert_clean(getattr(settings_copy, name))


def test_facet_detail_template_render_uses_allowed_terms(journal_copy):
    facet_dir = journal_copy / "facets" / "terminology-test"
    facet_dir.mkdir(parents=True, exist_ok=True)
    (facet_dir / "facet.json").write_text(
        json.dumps({"title": "Test Facet", "emoji": "TF", "color": "#123456"}),
        encoding="utf-8",
    )

    client = create_app(str(journal_copy)).test_client()
    response = client.get("/app/settings/facets/terminology-test")

    assert response.status_code == 200
    html = response.get_data(as_text=True)
    start = html.index('<section\n  class="facet-detail-page"')
    end = html.index("<script>", start)
    _assert_clean(html[start:end])


def test_home_needs_you_strings_use_allowed_terms(journal_copy, monkeypatch):
    import solstone.apps.home.routes as home_routes

    classifier_items = classify_needs_you(
        {"placeholder_text": "Pipeline needs review"},
        ["Review the launch checklist"],
        [{"text": "Draft the partner note"}],
    )
    for item in classifier_items:
        _assert_clean(item.text)
        for value in item.payload.values():
            if isinstance(value, str):
                _assert_clean(value)

    monkeypatch.setattr(home_routes, "_build_pulse_context", _minimal_home_context)
    client = create_app(str(journal_copy)).test_client()
    response = client.get("/app/home/")

    assert response.status_code == 200
    html = response.get_data(as_text=True)
    start = html.index('<div class="pulse-needs"')
    end = html.index("<script>", start)
    _assert_clean(html[start:end])
