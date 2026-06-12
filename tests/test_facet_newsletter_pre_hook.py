# SPDX-License-Identifier: AGPL-3.0-only
# Copyright (c) 2026 sol pbc

from __future__ import annotations

import json
from pathlib import Path

from solstone.talent import facet_newsletter

DAY = "20260609"
FACET = "work"


def _result(
    *,
    agent: str = "event",
    text: str = "Decision made on the launch plan.",
    path: str | None = None,
) -> dict:
    path = path or f"facets/{FACET}/events/{DAY}.jsonl"
    return {
        "id": f"{path}:0",
        "text": text,
        "metadata": {
            "day": DAY,
            "facet": FACET,
            "agent": agent,
            "stream": "",
            "path": path,
            "idx": 0,
        },
        "score": -1.0,
    }


def _empty_newsletter(*args, **kwargs) -> dict:
    return {"days": [], "next_cursor": None, "has_more": False}


def _facet_summary(facet: str) -> dict:
    return {"facet": facet, "summary": "# Work\n\nProduct launch facet."}


def _empty_search(query, limit=10, offset=0, **kwargs):
    return 0, []


def _activity(title: str = "Launch review") -> dict:
    return {
        "id": "meeting_090000_300",
        "activity": "meeting",
        "title": title,
        "description": "Reviewed launch readiness.",
        "segments": ["090000_300"],
        "active_entities": ["alice"],
        "created_at": 1,
    }


def _install_common(monkeypatch, tmp_path: Path) -> Path:
    journal = tmp_path / "journal"
    monkeypatch.setenv("SOLSTONE_JOURNAL", str(journal))
    monkeypatch.setattr(facet_newsletter, "get_journal", lambda: str(journal))
    monkeypatch.setattr(facet_newsletter, "get_facet_news", _empty_newsletter)
    monkeypatch.setattr(facet_newsletter, "get_facet", _facet_summary)
    monkeypatch.setattr(facet_newsletter, "search_journal", _empty_search)
    monkeypatch.setattr(facet_newsletter, "load_entities", lambda facet, day=None: [])
    monkeypatch.setattr(
        facet_newsletter,
        "load_activity_records",
        lambda facet, day: [_activity()],
    )
    return journal


def test_pre_hook_builds_source_packet_when_substance_present(tmp_path, monkeypatch):
    journal = _install_common(monkeypatch, tmp_path)
    narrative_dir = (
        journal / "facets" / FACET / "activities" / DAY / "meeting_090000_300"
    )
    narrative_dir.mkdir(parents=True)
    (narrative_dir / "session_review.md").write_text(
        "# Session Review\n\nNarrative launch context.",
        encoding="utf-8",
    )

    def fake_search(query, limit=10, offset=0, **kwargs):
        if kwargs.get("agent") == "span":
            return 1, [_result(agent="span", text="Indexed span context.")]
        return 0, []

    monkeypatch.setattr(facet_newsletter, "search_journal", fake_search)

    packet = facet_newsletter.pre_process({"facet": FACET, "day": DAY})["template_vars"]

    assert {
        "source_packet",
        "source_counts",
        "source_gaps",
        "coverage_preamble",
    } <= set(packet)
    assert "Launch review" in packet["source_packet"]
    assert "Narrative launch context." in packet["source_packet"]
    assert "Indexed span context." in packet["source_packet"]
    assert "activity_record_available" in packet["source_counts"]
    assert "activity_record_included" in packet["source_counts"]
    assert isinstance(json.loads(packet["source_gaps"]), list)


def test_pre_hook_skip_strings(monkeypatch):
    monkeypatch.setattr(
        facet_newsletter,
        "get_journal",
        lambda: (_ for _ in ()).throw(AssertionError("should not resolve journal")),
    )

    assert facet_newsletter.pre_process({"day": DAY}) == {
        "skip_reason": "missing facet"
    }
    assert facet_newsletter.pre_process({"facet": FACET}) == {
        "skip_reason": "missing day"
    }
    assert facet_newsletter.pre_process({"facet": FACET, "day": "2026-06-09"}) == {
        "skip_reason": "invalid day: 2026-06-09"
    }
    for facet in ("../etc", "a/b", ".hidden"):
        assert facet_newsletter.pre_process({"facet": facet, "day": DAY}) == {
            "skip_reason": f"unsafe facet: {facet}"
        }


def test_pre_hook_search_scoping(tmp_path, monkeypatch):
    _install_common(monkeypatch, tmp_path)
    monkeypatch.setattr(
        facet_newsletter, "load_activity_records", lambda facet, day: []
    )
    calls: list[dict] = []

    def fake_search(query, limit=10, offset=0, **kwargs):
        calls.append(dict(kwargs))
        if kwargs.get("agent") == "decisions":
            return 1, [_result(agent="decisions")]
        if kwargs.get("agent") == "entity":
            return 1, [_result(agent="entity", path="entity_search:alice")]
        return 0, []

    monkeypatch.setattr(facet_newsletter, "search_journal", fake_search)

    packet = facet_newsletter.pre_process({"facet": FACET, "day": DAY})

    assert "template_vars" in packet
    day_agents = {"event", "meetings", "decisions", "followups", "flow", "span"}
    day_calls = [call for call in calls if call.get("agent") in day_agents]
    assert {call["agent"] for call in day_calls} == day_agents
    assert all(call.get("facet") == FACET for call in day_calls)
    assert all(call.get("day") == DAY for call in day_calls)
    entity_calls = [call for call in calls if call.get("agent") == "entity"]
    assert len(entity_calls) == 1
    assert entity_calls[0].get("facet") == FACET
    assert entity_calls[0].get("day") == DAY


def test_pre_hook_includes_attached_detected_and_indexed_entities(
    tmp_path, monkeypatch
):
    _install_common(monkeypatch, tmp_path)
    monkeypatch.setattr(
        facet_newsletter, "load_activity_records", lambda facet, day: []
    )

    def fake_load_entities(facet, day=None):
        assert facet == FACET
        if day is None:
            return [
                {
                    "id": "alice",
                    "type": "Person",
                    "name": "Alice",
                    "description": "Product lead.",
                    "last_seen": DAY,
                }
            ]
        assert day == DAY
        return [
            {
                "id": "bob",
                "type": "Person",
                "name": "Bob",
                "description": "Detected collaborator.",
            }
        ]

    def fake_search(query, limit=10, offset=0, **kwargs):
        if kwargs.get("agent") == "flow":
            return 1, [_result(agent="flow", text="Launch flow context.")]
        if kwargs.get("agent") == "entity":
            assert kwargs.get("facet") == FACET
            assert kwargs.get("day") == DAY
            return 1, [
                _result(
                    agent="entity",
                    text="Alice appears in launch planning.",
                    path="entity_search:alice",
                )
            ]
        return 0, []

    monkeypatch.setattr(facet_newsletter, "load_entities", fake_load_entities)
    monkeypatch.setattr(facet_newsletter, "search_journal", fake_search)

    packet = facet_newsletter.pre_process({"facet": FACET, "day": DAY})["template_vars"]

    assert "facet_entities:attached" in packet["source_packet"]
    assert "facet_entities:detected" in packet["source_packet"]
    assert "facet_entities:indexed" in packet["source_packet"]
    assert "Product lead." in packet["source_packet"]
    assert "Detected collaborator." in packet["source_packet"]
    assert "Alice appears in launch planning." in packet["source_packet"]
    assert "facet_entities:attached_available: 1" in packet["source_counts"]
    assert "facet_entities:detected_available: 1" in packet["source_counts"]
    assert "facet_entities:indexed_available: 1" in packet["source_counts"]


def test_pre_hook_tier_three_only_skips_no_substantive_sources(tmp_path, monkeypatch):
    _install_common(monkeypatch, tmp_path)
    monkeypatch.setattr(
        facet_newsletter, "load_activity_records", lambda facet, day: []
    )
    monkeypatch.setattr(
        facet_newsletter,
        "get_facet_news",
        lambda facet, **kwargs: {
            "days": [{"date": "20260608", "raw_content": "# Prior news"}]
        },
    )

    def fake_search(query, limit=10, offset=0, **kwargs):
        if kwargs.get("agent") == "entity":
            return 1, [_result(agent="entity", path="entity_search:alice")]
        return 0, []

    monkeypatch.setattr(facet_newsletter, "search_journal", fake_search)

    assert facet_newsletter.pre_process({"facet": FACET, "day": DAY}) == {
        "skip_reason": "no substantive facet/day sources"
    }


def test_pre_hook_decisions_and_followups_alone_count_as_substance(
    tmp_path, monkeypatch
):
    for agent in ("decisions", "followups"):
        _install_common(monkeypatch, tmp_path)
        monkeypatch.setattr(
            facet_newsletter, "load_activity_records", lambda facet, day: []
        )

        def fake_search(query, limit=10, offset=0, **kwargs):
            if kwargs.get("agent") == agent:
                return 1, [_result(agent=agent)]
            return 0, []

        monkeypatch.setattr(facet_newsletter, "search_journal", fake_search)

        packet = facet_newsletter.pre_process({"facet": FACET, "day": DAY})

        assert "template_vars" in packet
        assert f"index_result:{agent}" in packet["template_vars"]["source_packet"]


def test_pre_hook_missing_and_failed_gaps_are_distinct(tmp_path, monkeypatch):
    _install_common(monkeypatch, tmp_path)

    def fail_activities(facet, day):
        raise RuntimeError("activity boom")

    def fake_search(query, limit=10, offset=0, **kwargs):
        if kwargs.get("agent") == "decisions":
            return 1, [_result(agent="decisions")]
        return 0, []

    monkeypatch.setattr(facet_newsletter, "load_activity_records", fail_activities)
    monkeypatch.setattr(facet_newsletter, "search_journal", fake_search)

    gaps = json.loads(
        facet_newsletter.pre_process({"facet": FACET, "day": DAY})["template_vars"][
            "source_gaps"
        ]
    )

    assert f"failed: activity_record failed for {FACET} {DAY}: activity boom" in gaps
    assert f"missing: index_result:event absent for {FACET} {DAY}" in gaps


def test_pre_hook_caps_and_clips_sources(tmp_path, monkeypatch):
    _install_common(monkeypatch, tmp_path)
    long_text = "x" * (facet_newsletter._MAX_DESCRIPTION_CHARS + 50)
    activities = [
        {
            **_activity(title=f"Activity {index}"),
            "id": f"activity_{index}",
            "description": long_text,
            "created_at": index,
        }
        for index in range(facet_newsletter._MAX_ACTIVITY_RECORDS + 1)
    ]
    monkeypatch.setattr(
        facet_newsletter,
        "load_activity_records",
        lambda facet, day: activities,
    )

    packet = facet_newsletter.pre_process({"facet": FACET, "day": DAY})["template_vars"]
    gaps = json.loads(packet["source_gaps"])

    assert any(gap.startswith("clipped: activity_record") for gap in gaps)
    assert (
        f"capped: activity_record limited to "
        f"{facet_newsletter._MAX_ACTIVITY_RECORDS}/{len(activities)} items"
    ) in gaps
    assert long_text not in packet["source_packet"]


def test_pre_hook_total_budget_drops_lower_tier_items(tmp_path, monkeypatch):
    _install_common(monkeypatch, tmp_path)
    monkeypatch.setattr(facet_newsletter, "_MAX_PACKET_CHARS", 800)
    monkeypatch.setattr(
        facet_newsletter,
        "get_facet",
        lambda facet: {"facet": facet, "summary": "Tier 3 metadata " * 50},
    )
    monkeypatch.setattr(
        facet_newsletter,
        "get_facet_news",
        lambda facet, **kwargs: {
            "days": [{"date": "20260608", "raw_content": "Prior newsletter " * 50}]
        },
    )

    packet = facet_newsletter.pre_process({"facet": FACET, "day": DAY})["template_vars"]
    gaps = json.loads(packet["source_gaps"])

    assert "Launch review" in packet["source_packet"]
    assert "Tier 3 metadata" not in packet["source_packet"]
    assert any(gap.startswith("dropped: facet_metadata") for gap in gaps)
    assert "  facet_metadata: 0" in packet["source_counts"]


def test_pre_hook_path_guard_precedes_io_and_normal_run_writes_no_news(
    tmp_path, monkeypatch
):
    monkeypatch.setattr(
        facet_newsletter,
        "load_activity_records",
        lambda facet, day: (_ for _ in ()).throw(AssertionError("activity called")),
    )
    monkeypatch.setattr(
        facet_newsletter,
        "search_journal",
        lambda *args, **kwargs: (_ for _ in ()).throw(AssertionError("search called")),
    )

    assert facet_newsletter.pre_process({"facet": "a/b", "day": DAY}) == {
        "skip_reason": "unsafe facet: a/b"
    }

    journal = _install_common(monkeypatch, tmp_path)
    packet = facet_newsletter.pre_process({"facet": FACET, "day": DAY})

    assert "template_vars" in packet
    assert not (journal / "facets" / FACET / "news" / f"{DAY}.md").exists()
