# SPDX-License-Identifier: AGPL-3.0-only
# Copyright (c) 2026 sol pbc

from __future__ import annotations

import re
from datetime import datetime

import pytest

from solstone.apps.home.health_glance import build_health_glance

BANNED_RE = re.compile(
    r"\b(watch|capture|record|monitor|track|collect)\b",
    re.IGNORECASE,
)
EXTENDED_BANNED_RE = re.compile(
    r"\b("
    r"capture|watch|record|monitor|track|collect|credentials|api|key|gemini|"
    r"cloud|model|provider|llm|configure"
    r")\b",
    re.IGNORECASE,
)
HEALTH_DETAIL_HREF = "/app/health#focus=recent-errors&day=today"


def _june_22_ms() -> float:
    return datetime(2026, 6, 22, 12, 0, 0).timestamp() * 1000


def _degraded_observer(name: str) -> dict:
    return {
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
            "segment": "20260622/120000_300",
        },
    }


def _degraded_capture(*names: str) -> dict:
    return {
        "status": "degraded",
        "observers": [_degraded_observer(name) for name in names or ("fedora",)],
    }


def _active_capture() -> dict:
    return {"status": "active", "observers": [{"name": "fedora", "status": "active"}]}


def test_degraded_capture_returns_red_attention_issue():
    result = build_health_glance(_degraded_capture("fedora"), None, None)

    assert result["verdict"] == "attention"
    assert result["severity"] == "red"
    assert len(result["issues"]) == 1
    assert result["issues"][0]["severity"] == "red"
    assert result["issues"][0]["href"] == "/app/health"
    assert "fedora" in result["issues"][0]["text"]
    assert result["headline"] != "everything's working"


def test_degraded_capture_collapses_multiple_observers_to_one_issue():
    result = build_health_glance(
        _degraded_capture("fedora", "phone", "tablet"), None, None
    )

    assert len(result["issues"]) == 1
    assert "and 2 more" in result["issues"][0]["text"]


def test_degraded_capture_and_pipeline_warning_returns_two_issues_red_verdict():
    result = build_health_glance(
        _degraded_capture("fedora"),
        {"status": "warning", "headline": "processing needs attention"},
        None,
    )

    assert result["verdict"] == "attention"
    assert result["severity"] == "red"
    assert len(result["issues"]) == 2
    assert result["headline"] != "everything's working"


def test_pipeline_warning_alone_returns_amber_attention_issue():
    result = build_health_glance(
        _active_capture(),
        {"status": "warning", "headline": "processing needs attention"},
        None,
    )

    assert result["verdict"] == "attention"
    assert result["severity"] == "amber"
    assert len(result["issues"]) == 1
    assert result["issues"][0]["href"] == HEALTH_DETAIL_HREF
    assert result["headline"] != "everything's working"


def test_pipeline_warning_without_headline_uses_fallback_text():
    result = build_health_glance(
        _active_capture(),
        {"status": "warning", "message": "some bullet"},
        None,
    )

    assert result["verdict"] != "ok"
    assert result["headline"] != "everything's working"
    assert result["issues"][0]["text"] == "processing is behind"


@pytest.mark.parametrize(
    "capture_health",
    [
        {"status": "offline", "observers": []},
        {"status": "stale", "observers": [{"name": "fedora", "status": "stale"}]},
    ],
)
def test_offline_and_stale_capture_do_not_return_ok(capture_health):
    result = build_health_glance(capture_health, None, None)

    assert result["verdict"] != "ok"
    assert result["headline"] != "everything's working"


def test_active_capture_without_pipeline_returns_ok_with_last_observation():
    result = build_health_glance(_active_capture(), None, "5m ago")

    assert result["verdict"] == "ok"
    assert result["severity"] == "green"
    assert result["headline"] == "everything's working"
    assert result["last_observation"] == "5m ago"
    assert result["cta"] is None


def test_no_observers_returns_ok_with_setup_cta():
    result = build_health_glance(
        {"status": "no_observers", "observers": []}, None, None
    )

    assert result["verdict"] == "ok"
    assert result["severity"] == "green"
    assert result["headline"] == "no observers yet"
    assert result["last_observation"] is None
    assert result["cta"] == {"text": "set one up →", "href": "/app/observer/"}


def test_unknown_observer_state_returns_unavailable():
    result = build_health_glance({"status": "unknown", "observers": []}, None, None)

    assert result["verdict"] == "unavailable"
    assert result["severity"] == "amber"
    assert result["headline"] == "observer status unavailable"
    assert result["verdict"] != "ok"
    assert result["headline"] != "everything's working"


def test_pipeline_open_support_action_routes_to_support():
    result = build_health_glance(
        _active_capture(),
        {
            "status": "warning",
            "headline": "processing needs attention",
            "suggested_action": "open_support",
        },
        None,
    )

    assert result["issues"][0]["href"] == "/app/support"


@pytest.mark.parametrize(
    "pipeline_status",
    [
        {
            "status": "warning",
            "headline": "processing needs attention",
            "suggested_action": "open_health_detail",
        },
        {
            "status": "warning",
            "headline": "processing needs attention",
            "suggested_action": "none",
        },
        {"status": "warning", "headline": "processing needs attention"},
    ],
)
def test_pipeline_health_actions_route_to_recent_errors(pipeline_status):
    result = build_health_glance(_active_capture(), pipeline_status, None)

    assert result["issues"][0]["href"] == HEALTH_DETAIL_HREF


def test_thinking_blocked_alone_returns_amber_attention_issue():
    result = build_health_glance(_active_capture(), None, None, thinking_blocked=True)

    assert result["issues"] == [
        {
            "text": "sol needs a way to think",
            "severity": "amber",
            "href": "/app/thinking/",
        }
    ]
    assert result["verdict"] == "attention"
    assert result["severity"] == "amber"
    assert result["headline"] == "1 thing needs your attention"


def test_thinking_blocked_combines_with_red_capture_issue():
    result = build_health_glance(
        _degraded_capture("fedora"), None, None, thinking_blocked=True
    )

    assert len(result["issues"]) == 2
    assert result["verdict"] == "attention"
    assert result["severity"] == "red"
    assert result["headline"] == "2 things need your attention"
    assert {
        "text": "sol needs a way to think",
        "severity": "amber",
        "href": "/app/thinking/",
    } in result["issues"]
    assert any(issue["severity"] == "red" for issue in result["issues"])


def test_thinking_not_blocked_keeps_ok_glance():
    result = build_health_glance(
        _active_capture(), None, "5m ago", thinking_blocked=False
    )

    assert result["verdict"] == "ok"
    assert result["headline"] == "everything's working"
    assert all(
        issue["text"] != "sol needs a way to think" for issue in result["issues"]
    )


def test_all_issue_and_cta_hrefs_are_local_paths():
    states = [
        build_health_glance(_degraded_capture("fedora"), None, None),
        build_health_glance({"status": "offline", "observers": []}, None, None),
        build_health_glance(
            {"status": "stale", "observers": [{"name": "fedora", "status": "stale"}]},
            None,
            None,
        ),
        build_health_glance(
            _active_capture(),
            {
                "status": "warning",
                "headline": "processing needs attention",
                "suggested_action": "open_support",
            },
            None,
        ),
        build_health_glance({"status": "no_observers", "observers": []}, None, None),
    ]

    for state in states:
        for issue in state["issues"]:
            assert issue["href"].startswith("/")
            assert not issue["href"].startswith("//")
        if state["cta"] is not None:
            assert state["cta"]["href"].startswith("/")
            assert not state["cta"]["href"].startswith("//")


def test_malformed_pipeline_drops_only_pipeline_issue():
    result = build_health_glance(_degraded_capture("fedora"), "warning", None)

    assert result["verdict"] == "attention"
    assert result["severity"] == "red"
    assert len(result["issues"]) == 1
    assert result["issues"][0]["severity"] == "red"


def test_owner_facing_strings_use_allowed_terms():
    states = [
        build_health_glance(_degraded_capture("fedora"), None, None),
        build_health_glance({"status": "offline", "observers": []}, None, None),
        build_health_glance(
            {"status": "stale", "observers": [{"name": "fedora", "status": "stale"}]},
            None,
            None,
        ),
        build_health_glance(_active_capture(), None, "5m ago"),
        build_health_glance(_active_capture(), None, None, thinking_blocked=True),
        build_health_glance({"status": "no_observers", "observers": []}, None, None),
        build_health_glance({"status": "unknown", "observers": []}, None, None),
        build_health_glance(
            _active_capture(),
            {"status": "warning", "headline": "processing needs attention"},
            None,
        ),
    ]

    for state in states:
        strings = [state["headline"]]
        if state["last_observation"] is not None:
            strings.append(state["last_observation"])
        if state["cta"] is not None:
            strings.append(state["cta"]["text"])
        strings.extend(issue["text"] for issue in state["issues"])
        for text in strings:
            assert BANNED_RE.findall(text) == []


def test_thinking_blocked_chip_uses_owner_copy() -> None:
    result = build_health_glance(_active_capture(), None, None, thinking_blocked=True)
    chip = next(
        issue
        for issue in result["issues"]
        if issue["text"] == "sol needs a way to think"
    )

    assert chip["text"] == "sol needs a way to think"
    assert chip["text"] == chip["text"].lower()
    assert EXTENDED_BANNED_RE.findall(chip["text"]) == []
