# SPDX-License-Identifier: AGPL-3.0-only
# Copyright (c) 2026 sol pbc

from __future__ import annotations

from datetime import datetime

import pytest

from solstone.apps.home.routes import _format_capture_vitals_text
from solstone.convey import create_app
from solstone.think.day_accumulator import append_record


def _june_22_ms() -> float:
    return datetime(2026, 6, 22, 12, 0, 0).timestamp() * 1000


def _degraded_capture(name: str = "fedora") -> dict:
    return {
        "status": "degraded",
        "observers": [
            {
                "name": name,
                "status": "degraded",
                "ingest_rejection": {
                    "reason_code": "ingest_contract_invalid",
                    "active_count": 79,
                    "first_ts": _june_22_ms(),
                    "latest_ts": _june_22_ms(),
                    "summary": "screen.jsonl:2: value is invalid",
                    "stream": name,
                    "version": "0.3.1",
                },
            }
        ],
    }


def test_format_capture_vitals_text_degraded_single():
    assert (
        _format_capture_vitals_text(_degraded_capture(), datetime.now())
        == "fedora isn't reaching your journal — 79 rejected since jun 22"
    )


def test_format_capture_vitals_text_degraded_multiple():
    capture = _degraded_capture()
    capture["observers"].append(
        {
            "name": "phone",
            "status": "degraded",
            "ingest_rejection": {
                "reason_code": "ingest_contract_invalid",
                "active_count": 2,
                "first_ts": _june_22_ms(),
                "latest_ts": _june_22_ms(),
                "summary": "screen.jsonl:2: value is invalid",
                "stream": "phone",
                "version": None,
            },
        }
    )

    assert (
        _format_capture_vitals_text(capture, datetime.now())
        == "fedora isn't reaching your journal — 79 rejected since jun 22, and 1 more"
    )


def test_format_capture_vitals_text_degraded_without_usable_observer():
    result = _format_capture_vitals_text(
        {
            "status": "degraded",
            "observers": [{"name": "fedora", "status": "degraded"}],
        },
        datetime.now(),
    )

    assert result == "an observer isn't reaching your journal"
    assert "since None" not in result


def test_format_capture_vitals_text_active_unchanged():
    assert (
        _format_capture_vitals_text(
            {"status": "active", "observers": [{"name": "fedora", "status": "active"}]},
            datetime.now(),
        )
        == "observer active"
    )


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
