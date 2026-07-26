# SPDX-License-Identifier: AGPL-3.0-only
# Copyright (c) 2026 sol pbc

from __future__ import annotations

import json
import re
from pathlib import Path

import pytest

from solstone.convey import backlog_copy

DASHBOARD_JS_PATH = Path(__file__).resolve().parents[1] / "static" / "dashboard.js"
APP_ROOT = Path(__file__).resolve().parents[1]

BACKLOG_COPY_KEYS = [
    "BACKLOG_ACTION_PROCESS_NOW",
    "BACKLOG_ACTION_REDO_SCRATCH",
    "BACKLOG_CONFIRM_REDO_SCRATCH",
    "BACKLOG_VERDICT_CAUGHT_UP",
    "BACKLOG_VERDICT_STUCK_ONLY_PLURAL",
    "BACKLOG_VERDICT_STUCK_ONLY_SINGULAR",
    "BACKLOG_VERDICT_PENDING_ONLY_PLURAL",
    "BACKLOG_VERDICT_PENDING_ONLY_SINGULAR",
    "BACKLOG_VERDICT_MIXED_STUCK_PLURAL",
    "BACKLOG_VERDICT_MIXED_STUCK_SINGULAR",
    "BACKLOG_VERDICT_MIXED_PENDING_PLURAL",
    "BACKLOG_VERDICT_MIXED_PENDING_SINGULAR",
    "BACKLOG_VERDICT_CANT_TELL",
    "BACKLOG_BUCKET_HEADING",
    "BACKLOG_BUCKET_DESCRIPTION",
    "BACKLOG_DAY_BADGE",
    "BACKLOG_REASON_CORRUPT_RAW",
    "BACKLOG_REASON_FAILING_STEP",
    "BACKLOG_REASON_MISSING_CONFIG",
    "BACKLOG_REASON_PROVIDER_DOWN",
    "BACKLOG_REASON_PROVIDER_REFUSED",
    "BACKLOG_QUEUED_FEEDBACK",
    "BACKLOG_WHY_NEVER_ATTEMPTED",
    "BACKLOG_WHY_FAILED",
    "BACKLOG_WHY_SENSED_NOT_THOUGHT",
    "BACKLOG_CATCHING_UP_DAY",
    "BACKLOG_CATCHING_UP_AGGREGATE",
    "BACKLOG_CATCHING_UP_TAIL",
]

BACKLOG_COPY_LITERALS = {
    "BACKLOG_ACTION_PROCESS_NOW": "process now",
    "BACKLOG_ACTION_REDO_SCRATCH": "redo from scratch",
    "BACKLOG_CONFIRM_REDO_SCRATCH": (
        "redo this whole day from scratch? this re-does the parts sol already "
        "finished, so it'll take longer. the day you see now won't change until it's done."
    ),
    "BACKLOG_VERDICT_CAUGHT_UP": "your journal's all caught up.",
    "BACKLOG_VERDICT_STUCK_ONLY_PLURAL": (
        "caught up except {stuck_n} days that need a hand."
    ),
    "BACKLOG_VERDICT_STUCK_ONLY_SINGULAR": (
        "caught up except 1 day that needs a hand."
    ),
    "BACKLOG_VERDICT_PENDING_ONLY_PLURAL": ("{pending_n} days are still catching up."),
    "BACKLOG_VERDICT_PENDING_ONLY_SINGULAR": "1 day is still catching up.",
    "BACKLOG_VERDICT_MIXED_STUCK_PLURAL": "{stuck_n} days need a hand",
    "BACKLOG_VERDICT_MIXED_STUCK_SINGULAR": "1 day needs a hand",
    "BACKLOG_VERDICT_MIXED_PENDING_PLURAL": (
        "{pending_n} more days are still catching up"
    ),
    "BACKLOG_VERDICT_MIXED_PENDING_SINGULAR": ("1 more day is still catching up"),
    "BACKLOG_VERDICT_CANT_TELL": (
        "still checking — give me a moment to see where your journal stands."
    ),
    "BACKLOG_BUCKET_HEADING": "days that need a hand",
    "BACKLOG_BUCKET_DESCRIPTION": (
        "these days stopped on their own and can't pick back up without you — "
        "here's why, and what to try."
    ),
    "BACKLOG_DAY_BADGE": "stuck",
    "BACKLOG_REASON_CORRUPT_RAW": (
        "original raw media is missing or damaged — re-import it"
    ),
    "BACKLOG_REASON_FAILING_STEP": "a processing step keeps failing — try again",
    "BACKLOG_REASON_MISSING_CONFIG": "a setting's missing — check your journal's setup",
    "BACKLOG_REASON_PROVIDER_DOWN": "the AI provider was unreachable. try again",
    "BACKLOG_REASON_PROVIDER_REFUSED": (
        "the AI provider refused a request sol sent — retrying won't help; "
        "this is a defect in sol"
    ),
    "BACKLOG_QUEUED_FEEDBACK": "queued, working on it now",
    "BACKLOG_WHY_NEVER_ATTEMPTED": "not looked at yet",
    "BACKLOG_WHY_FAILED": "couldn't finish — will retry",
    "BACKLOG_WHY_SENSED_NOT_THOUGHT": "taken in, not yet thought through",
    "BACKLOG_CATCHING_UP_DAY": "catching up",
    "BACKLOG_CATCHING_UP_AGGREGATE": "{pending_n} day(s) catching up",
    "BACKLOG_CATCHING_UP_TAIL": (
        "sol's working through these on its own, freshest day first."
    ),
}


@pytest.fixture
def stats_env(tmp_path, monkeypatch):
    """Create a temporary journal for stats app testing."""

    def _create():
        journal = tmp_path / "journal"
        journal.mkdir(exist_ok=True)

        config_dir = journal / "config"
        config_dir.mkdir(parents=True, exist_ok=True)
        config_file = config_dir / "journal.json"
        config_file.write_text(
            json.dumps(
                {
                    "setup": {"completed_at": 1700000000000},
                },
                indent=2,
            ),
            encoding="utf-8",
        )

        monkeypatch.setenv("SOLSTONE_JOURNAL", str(journal))

        from solstone.convey import create_app

        app = create_app(journal=str(journal))
        client = app.test_client()

        class Env:
            def __init__(self):
                self.journal = journal
                self.client = client
                self.app = app

        return Env()

    return _create


def _render_stats_workspace(stats_env) -> str:
    env = stats_env()
    response = env.client.get("/app/stats/workspace")
    assert response.status_code == 200
    return response.get_data(as_text=True)


def test_backlog_copy_constants_are_literal():
    for key, value in BACKLOG_COPY_LITERALS.items():
        assert getattr(backlog_copy, key) == value


def test_backlog_copy_script_carries_all_keys(stats_env):
    rendered = _render_stats_workspace(stats_env)

    assert "window.BACKLOG_COPY" in rendered
    assert 'data-stats-url="/app/stats/api/stats"' in rendered
    assert '<script src="/app/stats/static/dashboard.js"></script>' in rendered
    assert "Dashboard.load('/app/stats/api/stats')" in rendered
    for key in BACKLOG_COPY_KEYS:
        js_key = key.removeprefix("BACKLOG_")
        assert f"{js_key}:" in rendered

    script_values = {}
    for key in BACKLOG_COPY_KEYS:
        js_key = key.removeprefix("BACKLOG_")
        match = re.search(rf"{js_key}:\s*(?P<value>\"(?:\\.|[^\"])*\")", rendered)
        assert match is not None, key
        script_values[key] = json.loads(match.group("value"))

    assert script_values == {
        key: getattr(backlog_copy, key) for key in BACKLOG_COPY_KEYS
    }


def test_stats_workspace_observation_hours_heading_copy(stats_env):
    rendered = _render_stats_workspace(stats_env)

    assert "<h2>hours sol was with you, per day</h2>" in rendered


def test_stats_model_selector_copy_is_byte_identical_across_render_paths(stats_env):
    rendered = _render_stats_workspace(stats_env)
    dashboard = DASHBOARD_JS_PATH.read_text(encoding="utf-8")

    assert (
        '<label for="modelSelector" class="sr-only">filter by model</label>' in rendered
    )
    rendered_match = re.search(r'<option value="">([^<]+)</option>', rendered)
    dashboard_match = re.search(r"heading: '([^']+)',", dashboard)
    assert rendered_match is not None
    assert dashboard_match is not None
    rendered_copy = rendered_match.group(1)
    dashboard_copy = dashboard_match.group(1)
    assert rendered_copy == dashboard_copy
    assert rendered_copy == "select a model…"
    assert "Select Model..." not in rendered
    assert "Select a model" not in dashboard


def test_backlog_mixed_arm_constants_are_literal():
    assert backlog_copy.BACKLOG_VERDICT_MIXED_STUCK_SINGULAR == "1 day needs a hand"
    assert (
        backlog_copy.BACKLOG_VERDICT_MIXED_STUCK_PLURAL == "{stuck_n} days need a hand"
    )
    assert (
        backlog_copy.BACKLOG_VERDICT_MIXED_PENDING_SINGULAR
        == "1 more day is still catching up"
    )
    assert (
        backlog_copy.BACKLOG_VERDICT_MIXED_PENDING_PLURAL
        == "{pending_n} more days are still catching up"
    )


def test_dashboard_backlog_verdict_uses_mixed_arms_without_string_surgery():
    source = DASHBOARD_JS_PATH.read_text(encoding="utf-8")
    start = source.index("function backlogVerdict(stats)")
    end = source.index("function backlogDepth", start)
    body = source[start:end]

    assert ".split(" not in body
    assert "VERDICT_" + "BOTH_PLURAL" not in body
    assert "VERDICT_MIXED_STUCK_SINGULAR" in body
    assert "VERDICT_MIXED_STUCK_PLURAL" in body
    assert "VERDICT_MIXED_PENDING_SINGULAR" in body
    assert "VERDICT_MIXED_PENDING_PLURAL" in body


def test_stats_index_serves_spa_shell(stats_env):
    env = stats_env()

    response = env.client.get("/app/stats/")

    assert response.status_code == 200
    assert b'data-solstone-shell="spa"' in response.data


def test_stats_workspace_is_served_verbatim(stats_env):
    env = stats_env()

    response = env.client.get("/app/stats/workspace")

    assert response.status_code == 200
    assert response.data == (APP_ROOT / "workspace.html").read_bytes()


def test_stats_routes_resolve(stats_env):
    env = stats_env()
    adapter = env.app.url_map.bind("localhost")

    expected = {
        "/app/stats/api/stats": "app:stats.stats_data",
        "/app/stats/static/dashboard.js": "app:stats.static",
    }
    for path, endpoint in expected.items():
        matched, _args = adapter.match(path, method="GET")
        assert matched == endpoint


def test_stats_routes_do_not_use_render_template():
    assert "render_template" not in (APP_ROOT / "routes.py").read_text(encoding="utf-8")


def _style_block() -> str:
    raw_template = (APP_ROOT / "workspace.html").read_text(encoding="utf-8")
    style_start = raw_template.index("<style>")
    style_end = raw_template.index("</style>", style_start)
    return raw_template[style_start:style_end]


def _rule_block(css: str, selector: str, start: int = 0) -> tuple[int, str]:
    index = css.find(f"{selector} {{", start)
    assert index != -1, f"{selector} rule was not found"
    close = css.find("}", index)
    assert close != -1, f"{selector} rule is not closed"
    return index, css[index : close + 1]


def _px_declaration(rule: str, prop: str) -> float:
    match = re.search(rf"(?m)^\s*{re.escape(prop)}:\s*([^;]+);", rule)
    assert match is not None, f"{prop} declaration was not found"
    value = match.group(1).strip()
    px_match = re.fullmatch(r"-?(?:\d+(?:\.\d+)?|\.\d+)px", value)
    assert px_match is not None, f"{prop} must be a single px length, got {value!r}"
    return float(px_match.group(0)[:-2])


def test_stats_bar_label_gutter_keeps_legend_clearance():
    css = _style_block()
    _, bar_label_rule = _rule_block(css, ".bar-label")
    _, bar_chart_rule = _rule_block(css, ".bar-chart")

    bottom_offset = _px_declaration(bar_label_rule, "bottom")
    margin_bottom = _px_declaration(bar_chart_rule, "margin-bottom")

    assert margin_bottom >= abs(bottom_offset) + 2


def test_stats_bar_chart_replacement_keeps_nonzero_flex_growth():
    css = _style_block()
    _, chart_rule = _rule_block(css, ".chart")
    _, bar_chart_rule = _rule_block(css, ".bar-chart")

    grow_match = re.search(r"(?m)^\s*flex-grow:\s*([^;]+);", bar_chart_rule)
    if grow_match is not None:
        grow_value = grow_match.group(1).strip()
    else:
        flex_match = re.search(r"(?m)^\s*flex:\s*([^;]+);", bar_chart_rule)
        assert flex_match is not None, "bar-chart flex declaration was not found"
        grow_value = flex_match.group(1).strip().split()[0]

    try:
        flex_grow = float(grow_value)
    except ValueError:
        raise AssertionError(
            f"bar-chart flex grow must be numeric, got {grow_value!r}"
        ) from None

    assert flex_grow > 0
    assert "height: 100%;" not in bar_chart_rule
    assert "display: flex;" in chart_rule
    assert "flex-direction: column;" in chart_rule


def test_stats_bar_tooltip_rest_state_uses_sanctioned_restore_mechanism():
    css = _style_block()
    _, rest_rule = _rule_block(css, ".bar::after")
    _, hover_rule = _rule_block(css, ".bar:hover::after")

    rest_nulls_content = re.search(r'(?m)^\s*content:\s*(?:""|\'\');', rest_rule)
    rest_hides_display = re.search(r"(?m)^\s*display:\s*none;", rest_rule)
    hover_restores_content = re.search(
        r"(?m)^\s*content:\s*attr\(data-tip\);", hover_rule
    )
    hover_display_match = re.search(r"(?m)^\s*display:\s*([^;]+);", hover_rule)
    hover_restores_display = (
        hover_display_match is not None
        and hover_display_match.group(1).strip().lower() != "none"
    )

    assert rest_nulls_content or rest_hides_display
    assert hover_restores_content or hover_restores_display


def test_stats_bar_after_rest_content_attr_guard_is_scoped_to_bar_tooltip():
    css = _style_block()
    _, rest_rule = _rule_block(css, ".bar::after")

    # .heatmap-cell::after has the same pattern but is deliberately out of scope; this guard does not claim to cover it.
    assert "content: attr(" not in rest_rule


def test_stats_backlog_badge_declares_inline_axis_self_alignment():
    css = _style_block()
    _, badge_rule = _rule_block(css, ".backlog-badge")
    match = re.search(r"(?m)^\s*justify-self:\s*([^;]+);", badge_rule)
    assert match is not None, "backlog-badge justify-self declaration was not found"

    assert match.group(1).strip() in {
        "start",
        "flex-start",
        "self-start",
        "left",
        "center",
        "end",
        "flex-end",
        "self-end",
        "right",
    }
