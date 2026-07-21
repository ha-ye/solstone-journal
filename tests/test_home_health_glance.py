# SPDX-License-Identifier: AGPL-3.0-only
# Copyright (c) 2026 sol pbc

from __future__ import annotations

import re
from datetime import datetime

import pytest

from solstone.apps.home.health_glance import build_health_glance
from solstone.think.brain_health import HEADLINES

BANNED_RE = re.compile(
    r"\b(watch|capture|record|monitor|track|collect|observer|observation)\b",
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


def _brain(
    state: str,
    *,
    progressing: bool = False,
    action: dict | None = None,
) -> dict:
    return {
        "state": state,
        "headline": HEADLINES[state],
        "progressing": progressing,
        "action": action,
    }


def _ready_brain() -> dict:
    return _brain("ready")


def _checking_brain() -> dict:
    return _brain("checking", progressing=True)


def _blocked_brain(
    *,
    progressing: bool = False,
    action: dict | None = None,
) -> dict:
    if action is None and not progressing:
        action = {"label": "open thinking", "href": "/app/thinking/#main"}
    return _brain("blocked", progressing=progressing, action=action)


def _bundled_runtime_brain(*, progressing: bool) -> dict:
    action = (
        None
        if progressing
        else {"label": "open local setup", "href": "/app/thinking/#local-setup"}
    )
    return _blocked_brain(progressing=progressing, action=action)


def _unhealthy_brain() -> dict:
    return _brain(
        "unhealthy", action={"label": "open thinking", "href": "/app/thinking/#main"}
    )


def _unknown_brain() -> dict:
    return _brain(
        "unknown", action={"label": "view health", "href": "/app/health/#brain"}
    )


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


def _no_observers_capture() -> dict:
    return {"status": "no_observers", "observers": []}


def _assert_status_row(result: dict, *, verdict: str, headline: str) -> None:
    assert result["verdict"] == verdict
    assert result["severity"] == "amber"
    assert result["headline"] == headline
    assert result["issues"] == []
    assert result["cta"] is None
    assert result["last_observation"] is None


def _assert_unavailable_row(result: dict) -> None:
    assert result["verdict"] == "unavailable"
    assert result["severity"] == "amber"
    assert result["headline"] == "i don't know the status of your devices right now."
    assert result["issues"] == []
    assert result["cta"] is None
    assert result["last_observation"] is None


def _assert_no_observers_row(result: dict) -> None:
    assert result["verdict"] == "ok"
    assert result["severity"] == "green"
    assert (
        result["headline"]
        == "no devices are running sol yet. set one up to start your journal."
    )
    assert result["issues"] == []
    assert result["last_observation"] is None
    assert result["cta"] == {"text": "set one up →", "href": "/app/observer/"}


def _assert_single_brain_issue(result: dict, *, text: str, href: str) -> None:
    assert result["verdict"] == "attention"
    assert result["severity"] == "amber"
    assert result["headline"] == "1 thing needs your attention"
    assert result["last_observation"] is None
    assert result["cta"] is None
    assert len(result["issues"]) == 1
    assert result["issues"][0] == {"text": text, "severity": "amber", "href": href}


def _assert_no_action_anywhere(result: dict) -> None:
    assert "action" not in result
    assert result["cta"] is None
    assert all("action" not in issue for issue in result["issues"])


def test_degraded_capture_returns_red_attention_issue():
    result = build_health_glance(_degraded_capture("fedora"), None, None)

    assert result["verdict"] == "attention"
    assert result["severity"] == "red"
    assert len(result["issues"]) == 1
    assert result["issues"][0]["severity"] == "red"
    assert result["issues"][0]["href"] == "/app/health"
    assert (
        result["issues"][0]["text"]
        == "one of your devices isn't reaching your journal."
    )
    assert result["headline"] != "everything's working"


def test_degraded_capture_collapses_multiple_observers_to_one_issue():
    result = build_health_glance(
        _degraded_capture("fedora", "phone", "tablet"), None, None
    )

    assert len(result["issues"]) == 1
    assert (
        result["issues"][0]["text"]
        == "one of your devices isn't reaching your journal."
    )


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
    ("capture_health", "issue_text"),
    [
        (
            {"status": "offline", "observers": []},
            "nothing is reaching your journal.",
        ),
        (
            {"status": "stale", "observers": [{"name": "fedora", "status": "stale"}]},
            "one of your devices hasn't reached your journal recently.",
        ),
    ],
)
def test_offline_and_stale_capture_do_not_return_ok(capture_health, issue_text):
    result = build_health_glance(capture_health, None, None)

    assert result["verdict"] != "ok"
    assert result["headline"] != "everything's working"
    assert result["issues"][0]["text"] == issue_text


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
    assert (
        result["headline"]
        == "no devices are running sol yet. set one up to start your journal."
    )
    assert result["last_observation"] is None
    assert result["cta"] == {"text": "set one up →", "href": "/app/observer/"}


def test_unknown_observer_state_returns_unavailable():
    result = build_health_glance({"status": "unknown", "observers": []}, None, None)

    assert result["verdict"] == "unavailable"
    assert result["severity"] == "amber"
    assert result["headline"] == "i don't know the status of your devices right now."
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


def test_brain_blocked_alone_returns_amber_attention_issue():
    result = build_health_glance(_active_capture(), None, None, brain=_blocked_brain())

    assert result["issues"] == [
        {
            "text": HEADLINES["blocked"],
            "severity": "amber",
            "href": "/app/thinking/#main",
        }
    ]
    assert result["verdict"] == "attention"
    assert result["severity"] == "amber"
    assert result["headline"] == "1 thing needs your attention"


def test_brain_blocked_combines_with_red_capture_issue():
    result = build_health_glance(
        _degraded_capture("fedora"), None, None, brain=_blocked_brain()
    )

    assert len(result["issues"]) == 2
    assert result["verdict"] == "attention"
    assert result["severity"] == "red"
    assert result["headline"] == "2 things need your attention"
    assert {
        "text": HEADLINES["blocked"],
        "severity": "amber",
        "href": "/app/thinking/#main",
    } in result["issues"]
    assert any(issue["severity"] == "red" for issue in result["issues"])


def test_brain_ready_keeps_ok_glance():
    result = build_health_glance(
        _active_capture(), None, "5m ago", brain=_ready_brain()
    )

    assert result["verdict"] == "ok"
    assert result["headline"] == "everything's working"
    assert all(issue["text"] != HEADLINES["blocked"] for issue in result["issues"])


def test_checking_brain_returns_status_only_amber_row():
    result = build_health_glance(
        _active_capture(), None, "5m ago", brain=_checking_brain()
    )

    _assert_status_row(
        result,
        verdict="checking",
        headline=HEADLINES["checking"],
    )


def test_progressing_blocked_brain_returns_status_only_amber_row():
    result = build_health_glance(
        _active_capture(),
        None,
        "5m ago",
        brain=_bundled_runtime_brain(progressing=True),
    )

    _assert_status_row(
        result,
        verdict="progressing",
        headline=HEADLINES["blocked"],
    )


@pytest.mark.parametrize(
    ("brain", "verdict", "headline"),
    [
        (_checking_brain(), "checking", HEADLINES["checking"]),
        (
            _bundled_runtime_brain(progressing=True),
            "progressing",
            HEADLINES["blocked"],
        ),
    ],
)
def test_no_observers_with_inflight_brain_returns_brain_status_row(
    brain, verdict, headline
):
    result = build_health_glance(_no_observers_capture(), None, None, brain=brain)

    _assert_status_row(result, verdict=verdict, headline=headline)
    assert result["verdict"] != "ok"
    assert result["severity"] != "green"


@pytest.mark.parametrize("brain", [_ready_brain(), None])
def test_no_observers_ready_or_absent_brain_keeps_setup_cta(brain):
    result = build_health_glance(_no_observers_capture(), None, None, brain=brain)

    _assert_no_observers_row(result)


@pytest.mark.parametrize(
    "capture_health",
    [
        {"status": "unknown", "observers": []},
        {"status": "unavailable", "observers": []},
    ],
)
@pytest.mark.parametrize(
    "brain",
    [_checking_brain(), _bundled_runtime_brain(progressing=True)],
)
def test_unavailable_capture_with_inflight_brain_returns_unavailable_row(
    capture_health, brain
):
    result = build_health_glance(capture_health, None, None, brain=brain)

    _assert_unavailable_row(result)


def test_bundled_runtime_progressing_suppresses_brain_action():
    result = build_health_glance(
        _active_capture(),
        None,
        None,
        brain=_bundled_runtime_brain(progressing=True),
    )

    _assert_status_row(
        result,
        verdict="progressing",
        headline=HEADLINES["blocked"],
    )
    _assert_no_action_anywhere(result)


def test_bundled_runtime_not_progressing_returns_local_setup_issue():
    result = build_health_glance(
        _active_capture(),
        None,
        None,
        brain=_bundled_runtime_brain(progressing=False),
    )

    _assert_single_brain_issue(
        result,
        text=HEADLINES["blocked"],
        href="/app/thinking/#local-setup",
    )


@pytest.mark.parametrize(
    ("brain", "text", "href"),
    [
        (
            _blocked_brain(progressing=False),
            HEADLINES["blocked"],
            "/app/thinking/#main",
        ),
        (_unhealthy_brain(), HEADLINES["unhealthy"], "/app/thinking/#main"),
        (_unknown_brain(), HEADLINES["unknown"], "/app/health/#brain"),
    ],
)
def test_actionable_brain_states_return_single_amber_issue(brain, text, href):
    result = build_health_glance(_active_capture(), None, None, brain=brain)

    _assert_single_brain_issue(result, text=text, href=href)


@pytest.mark.parametrize(
    "brain",
    [_checking_brain(), _bundled_runtime_brain(progressing=True)],
)
def test_capture_and_pipeline_attention_precede_inflight_brain_status(brain):
    red_expected = build_health_glance(_degraded_capture("fedora"), None, None)
    red_actual = build_health_glance(
        _degraded_capture("fedora"), None, None, brain=brain
    )

    assert red_actual == red_expected

    pipeline = {"status": "warning", "headline": "processing needs attention"}
    amber_expected = build_health_glance(_active_capture(), pipeline, None)
    amber_actual = build_health_glance(
        _active_capture(), pipeline, None, brain=brain
    )

    assert amber_actual == amber_expected


@pytest.mark.parametrize(
    ("brain", "may_be_green"),
    [
        (_ready_brain(), True),
        (_checking_brain(), False),
        (_blocked_brain(progressing=False), False),
        (_bundled_runtime_brain(progressing=True), False),
        (_unhealthy_brain(), False),
        (_unknown_brain(), False),
    ],
)
def test_canonical_non_ready_brain_states_do_not_return_ok_green(
    brain, may_be_green
):
    result = build_health_glance(_active_capture(), None, "5m ago", brain=brain)

    if may_be_green:
        assert result["verdict"] == "ok"
        assert result["severity"] == "green"
        assert result["headline"] == "everything's working"
        assert result["last_observation"] == "5m ago"
        assert result["cta"] is None
        assert result["issues"] == []
    else:
        assert result["verdict"] != "ok"
        assert result["severity"] != "green"


@pytest.mark.parametrize("brain", [None, "checking"])
def test_absent_or_non_dict_brain_keeps_active_capture_green(brain):
    result = build_health_glance(_active_capture(), None, "5m ago", brain=brain)

    assert result["verdict"] == "ok"
    assert result["severity"] == "green"
    assert result["headline"] == "everything's working"
    assert result["last_observation"] == "5m ago"
    assert result["cta"] is None
    assert result["issues"] == []


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
        build_health_glance(
            _active_capture(),
            None,
            None,
            brain=_bundled_runtime_brain(progressing=False),
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
        build_health_glance(_active_capture(), None, None, brain=_blocked_brain()),
        build_health_glance(_active_capture(), None, None, brain=_checking_brain()),
        build_health_glance(
            _active_capture(),
            None,
            None,
            brain=_bundled_runtime_brain(progressing=True),
        ),
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


def test_brain_blocked_chip_uses_owner_copy() -> None:
    result = build_health_glance(_active_capture(), None, None, brain=_blocked_brain())
    chip = next(
        issue for issue in result["issues"] if issue["text"] == HEADLINES["blocked"]
    )

    assert chip["text"] == HEADLINES["blocked"]
    assert chip["text"] == chip["text"].lower()
    assert EXTENDED_BANNED_RE.findall(chip["text"]) == []
