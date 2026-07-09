# SPDX-License-Identifier: AGPL-3.0-only
# Copyright (c) 2026 sol pbc

from __future__ import annotations

from datetime import datetime
from pathlib import Path
from typing import Any

import pytest

from solstone.convey import create_app
from solstone.think.day_accumulator import append_record


def _patch_minimal_pulse_context(
    monkeypatch,
    *,
    pulse_needs: list[Any],
    briefing_needs: list[Any],
    attention: Any = None,
):
    import solstone.apps.home.routes as home_routes

    briefing = None
    if briefing_needs:
        needs_items = [
            item if isinstance(item, dict) else {"text": item, "source_id": ""}
            for item in briefing_needs
        ]
        briefing = {
            "metadata": {"generated": "2026-04-16T09:00:00"},
            "your_day": [],
            "yesterday": [],
            "needs_attention": needs_items,
            "forward_look": [],
            "reading": [],
        }
    monkeypatch.setattr(
        home_routes,
        "get_capture_health",
        lambda: {"status": "active", "observers": []},
    )
    monkeypatch.setattr(home_routes, "get_cached_state", lambda: {})
    monkeypatch.setattr(home_routes, "get_current", lambda: None)
    monkeypatch.setattr(home_routes, "_resolve_attention", lambda awareness: attention)
    monkeypatch.setattr(home_routes, "_today", lambda: "20260416")
    monkeypatch.setattr(home_routes, "_yesterday", lambda: "20260415")
    monkeypatch.setattr(home_routes, "_count_journal_age_days", lambda today: 8)
    monkeypatch.setattr(home_routes, "_load_stats", lambda today: {})
    monkeypatch.setattr(home_routes, "_load_flow_md", lambda today: (None, None))
    monkeypatch.setattr(
        home_routes,
        "_load_pulse_narrative",
        lambda today: ("content", "09:00", pulse_needs),
    )
    monkeypatch.setattr(
        home_routes,
        "load_briefing",
        lambda today: briefing,
    )
    monkeypatch.setattr(
        home_routes, "_collect_anticipated_activities", lambda today: []
    )
    monkeypatch.setattr(home_routes, "_collect_activities", lambda today: [])
    monkeypatch.setattr(home_routes, "_load_latest_weekly_reflection", lambda: None)
    monkeypatch.setattr(home_routes, "read_steward_health", lambda: None)
    monkeypatch.setattr(home_routes, "read_steward_summary", lambda *a, **k: None)
    monkeypatch.setattr(home_routes, "_thinking_blocked", lambda: False)
    monkeypatch.setattr(
        home_routes,
        "_summarize_yesterday_processing",
        lambda yesterday, journal_age_days: {
            "title": "Yesterday's processing",
            "mode": "healthy",
            "default_collapsed": True,
            "summary_line": "I wrote 2 newsletters.",
            "details": [],
            "sparse_lines": None,
            "first_week_framing": None,
        },
    )
    return home_routes


def test_api_pulse_includes_needs_you_items_json_shape(journal_copy, monkeypatch):
    import solstone.apps.home.routes as home_routes

    needs_you_item = {
        "text": "Review the launch checklist",
        "kind": "chat",
        "payload": {"prompt": "let's dig into Review the launch checklist"},
        "disabled": False,
        "reason": "",
    }

    monkeypatch.setattr(
        home_routes,
        "_build_pulse_context",
        lambda: {
            "now": datetime(2026, 5, 24, 12, 0),
            "attention": None,
            "needs_you_items": [needs_you_item],
            "show_welcome": False,
        },
    )

    client = create_app(str(journal_copy)).test_client()
    response = client.get("/app/home/api/pulse")

    assert response.status_code == 200
    payload = response.get_json()
    assert payload["needs_you_items"] == [needs_you_item]
    assert list(payload["needs_you_items"][0]) == [
        "disabled",
        "kind",
        "payload",
        "reason",
        "text",
    ]


@pytest.mark.parametrize(
    "age, curate_active, expected_state, expect_framing",
    [
        (0, False, "welcome", True),
        (7, False, "welcome", True),
        (8, False, "welcome", False),
        (3, True, "active", False),
        (30, False, "welcome", False),
    ],
)
def test_pulse_welcome_signal_matrix(
    monkeypatch, tmp_path, age, curate_active, expected_state, expect_framing
):
    import solstone.apps.home.routes as home_routes

    journal = tmp_path / "journal"
    monkeypatch.setenv("SOLSTONE_JOURNAL", str(journal))
    monkeypatch.setattr(home_routes, "_count_journal_age_days", lambda today: age)
    if curate_active:
        monkeypatch.setattr(
            home_routes,
            "_collect_activities",
            lambda today: [
                {"description": "curated activity", "display_time": "09:00"}
            ],
        )

    ctx = home_routes._build_pulse_context()

    assert ctx["journal_age_days"] == age
    assert ctx["home_state"] == expected_state
    if expect_framing:
        assert ctx["welcome_framing"] is home_routes._FIRST_WEEK_FRAMING
    else:
        assert ctx["welcome_framing"] is None


def test_load_pulse_narrative_reads_today_record_strictly(monkeypatch, tmp_path):
    import solstone.apps.home.routes as home_routes

    journal = tmp_path / "journal"
    monkeypatch.setenv("SOLSTONE_JOURNAL", str(journal))
    today = "20260524"
    yesterday = "20260523"

    assert home_routes._load_pulse_narrative(today) == (None, None, [])

    append_record(
        yesterday,
        "pulse",
        {
            "title": "Yesterday",
            "one_sentence": "Yesterday had context.",
            "full_details": "This should not show for today's strict gate.",
            "needs_you": ["Yesterday-only item."],
            "ts": int(datetime(2026, 5, 23, 10, 0).timestamp() * 1000),
        },
    )
    assert home_routes._load_pulse_narrative(today) == (None, None, [])

    append_record(
        today,
        "pulse",
        {
            "title": "Blank",
            "one_sentence": "Blank details should be ignored.",
            "full_details": "   ",
            "needs_you": ["Ignored item."],
            "ts": int(datetime(2026, 5, 24, 9, 0).timestamp() * 1000),
        },
    )
    assert home_routes._load_pulse_narrative(today) == (None, None, [])

    ts = int(datetime(2026, 5, 24, 12, 34).timestamp() * 1000)
    append_record(
        today,
        "pulse",
        {
            "title": "Current",
            "one_sentence": "Today has a pulse.",
            "full_details": "The current pulse narrative.",
            "needs_you": ["Review the launch checklist.", 42, ""],
            "ts": ts,
        },
    )

    assert home_routes._load_pulse_narrative(today) == (
        "The current pulse narrative.",
        datetime.fromtimestamp(ts / 1000).strftime("%H:%M"),
        ["Review the launch checklist.", "42"],
    )


def test_pulse_and_briefing_needs_dedup_by_shared_source(monkeypatch):
    source = "sol://20260313/archon/091500_300"
    home_routes = _patch_minimal_pulse_context(
        monkeypatch,
        pulse_needs=[
            {
                "text": "Q3 report needs your review",
                "kind": "chat",
                "payload": {"prompt": "dig into Q3"},
                "source_id": source,
            }
        ],
        briefing_needs=[{"text": "Look at the Q3 numbers", "source_id": source}],
    )

    ctx = home_routes._build_pulse_context()

    assert len(ctx["needs_you_items"]) == 1
    assert ctx["briefing_needs_shared_count"] == 1
    assert ctx["briefing_needs_deduped"] == []


def test_briefing_repeated_source_identity_renders_once(monkeypatch):
    source = "sol://20260313/archon/091500_300"
    home_routes = _patch_minimal_pulse_context(
        monkeypatch,
        pulse_needs=[],
        briefing_needs=[
            {"text": "Review the Q3 report", "source_id": source},
            {"text": "Look at the Q3 numbers", "source_id": source},
        ],
    )

    ctx = home_routes._build_pulse_context()

    assert ctx["briefing_needs_shared_count"] == 0
    assert ctx["briefing_needs_deduped"] == ["Review the Q3 report"]


def test_briefing_different_source_identities_stay_distinct(monkeypatch):
    source_a = "sol://20260313/archon/091500_300"
    source_b = "sol://facets/work/news/20260326"
    home_routes = _patch_minimal_pulse_context(
        monkeypatch,
        pulse_needs=[],
        briefing_needs=[
            {"text": "Review the report", "source_id": source_a},
            {"text": "Review the report", "source_id": source_b},
        ],
    )

    ctx = home_routes._build_pulse_context()

    assert ctx["briefing_needs_shared_count"] == 0
    assert ctx["briefing_needs_deduped"] == [
        "Review the report",
        "Review the report",
    ]


def test_legacy_plain_string_needs_still_dedup_by_normalized_text(monkeypatch):
    home_routes = _patch_minimal_pulse_context(
        monkeypatch,
        pulse_needs=["Follow up with Acme"],
        briefing_needs=["follow   up with acme"],
    )

    ctx = home_routes._build_pulse_context()

    assert len(ctx["needs_you_items"]) == 1
    assert ctx["briefing_needs_shared_count"] == 1
    assert ctx["briefing_needs_deduped"] == []


def test_markdown_briefing_loader_removed():
    import solstone.apps.home.routes as home_routes

    source = Path(home_routes.__file__).read_text(encoding="utf-8")

    assert not hasattr(home_routes, "_load_briefing_md")
    assert not hasattr(home_routes, "_BRIEFING_SECTIONS")
    assert 'startswith("## ")' not in source


def test_briefing_needs_dedup_by_inline_sol_link(monkeypatch):
    source = "sol://20260313/archon/091500_300"
    home_routes = _patch_minimal_pulse_context(
        monkeypatch,
        pulse_needs=[],
        briefing_needs=[
            {
                "text": f"Review the Q3 report ([standup]({source}))",
                "source_id": "",
            },
            {
                "text": f"Look at the Q3 numbers ([standup]({source}))",
                "source_id": "",
            },
        ],
    )

    ctx = home_routes._build_pulse_context()

    assert ctx["briefing_needs_shared_count"] == 0
    assert ctx["briefing_needs_deduped"] == [
        f"Review the Q3 report ([standup]({source}))"
    ]
