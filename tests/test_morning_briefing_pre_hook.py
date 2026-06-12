# SPDX-License-Identifier: AGPL-3.0-only
# Copyright (c) 2026 sol pbc

import json

from solstone.talent import morning_briefing
from solstone.think.day_accumulator import append_record


def _result(day: str = "20260422") -> dict:
    return {
        "id": "20260422/work/090000_300/talents/followups.md:0",
        "text": "Follow up with Alice about the launch checklist.",
        "metadata": {
            "day": day,
            "facet": "work",
            "agent": "followups",
            "stream": "work",
            "path": f"{day}/work/090000_300/talents/followups.md",
            "idx": 0,
        },
        "score": -1.0,
    }


def test_morning_briefing_pre_hook_builds_source_packet(tmp_path, monkeypatch):
    journal = tmp_path / "journal"
    monkeypatch.setenv("SOLSTONE_JOURNAL", str(journal))
    identity = journal / "identity"
    identity.mkdir(parents=True)
    append_record(
        "20260422",
        "pulse",
        {
            "full_details": "Pulse needs focus time.",
            "needs_you": ["Review the launch checklist."],
            "ts": 1,
        },
    )
    (identity / "partner.md").write_text("Partner profile.", encoding="utf-8")
    (identity / "health.md").write_text(
        "## Needs your attention\n\nnone", encoding="utf-8"
    )

    monkeypatch.setattr(morning_briefing, "get_journal", lambda: str(journal))
    monkeypatch.setattr(
        morning_briefing,
        "get_enabled_facets",
        lambda: {"work": {"title": "Work"}},
    )
    monkeypatch.setattr(
        morning_briefing,
        "get_facet_news",
        lambda facet, **kwargs: {
            "days": [{"date": kwargs["day"], "raw_content": "Work shipped a release."}]
        },
    )

    def fake_load_activity_records(facet, day, *, include_hidden=False):
        if day == "20260422":
            return [
                {
                    "source": "anticipated",
                    "activity": "meeting",
                    "target_date": "20260422",
                    "start": "09:00:00",
                    "end": "10:00:00",
                    "title": "Planning meeting",
                    "participation": [{"name": "Alice"}],
                }
            ]
        if day == "20260423":
            return [
                {
                    "source": "anticipated",
                    "activity": "deadline",
                    "target_date": "20260423",
                    "start": "17:00:00",
                    "title": "Proposal deadline",
                    "active_entities": ["Bob"],
                }
            ]
        return []

    monkeypatch.setattr(
        morning_briefing,
        "load_activity_records",
        fake_load_activity_records,
    )
    monkeypatch.setattr(
        morning_briefing,
        "search_journal",
        lambda query, limit, day, agent: (1, [_result(day)]),
    )

    packet = morning_briefing.pre_process({"day": "20260422", "model": "test-model"})[
        "template_vars"
    ]

    expected = {
        "active_facets",
        "facet_newsletters",
        "anticipated_today",
        "anticipated_forward",
        "pulse_surface",
        "partner_surface",
        "health_surface",
        "followups",
        "decisions",
        "source_counts",
        "source_gaps",
        "coverage_preamble",
    }
    assert expected <= set(packet)
    assert "Planning meeting" in packet["anticipated_today"]
    assert "Proposal deadline" in packet["anticipated_forward"]
    assert "Work shipped a release." in packet["facet_newsletters"]
    assert "Pulse needs focus time." in packet["pulse_surface"]
    assert "- Review the launch checklist." in packet["pulse_surface"]
    assert "  anticipated_activities: 1" in packet["source_counts"]
    assert json.loads(packet["source_gaps"]) == []


def test_morning_briefing_pre_hook_missing_sources_are_visible_gaps(
    tmp_path, monkeypatch
):
    journal = tmp_path / "journal"
    journal.mkdir()
    monkeypatch.setenv("SOLSTONE_JOURNAL", str(journal))

    monkeypatch.setattr(morning_briefing, "get_journal", lambda: str(journal))
    monkeypatch.setattr(
        morning_briefing,
        "get_enabled_facets",
        lambda: {"work": {"title": "Work"}},
    )
    monkeypatch.setattr(
        morning_briefing,
        "get_facet_news",
        lambda facet, **kwargs: {"days": []},
    )
    monkeypatch.setattr(
        morning_briefing,
        "load_activity_records",
        lambda facet, day, *, include_hidden=False: [],
    )
    monkeypatch.setattr(
        morning_briefing,
        "search_journal",
        lambda query, limit, day, agent: (0, []),
    )

    packet = morning_briefing.pre_process({"day": "20260422"})["template_vars"]
    gaps = json.loads(packet["source_gaps"])

    assert any("no facet newsletter available" in gap for gap in gaps)
    assert any("no anticipated activities today" in gap for gap in gaps)
    assert any("steward health surface missing" in gap for gap in gaps)
    assert "Gaps:" in packet["coverage_preamble"]
