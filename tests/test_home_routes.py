# SPDX-License-Identifier: AGPL-3.0-only
# Copyright (c) 2026 sol pbc

from __future__ import annotations

from datetime import datetime

from solstone.convey import create_app
from solstone.think.day_accumulator import append_record


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
