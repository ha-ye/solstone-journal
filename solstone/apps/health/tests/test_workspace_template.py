# SPDX-License-Identifier: AGPL-3.0-only
# Copyright (c) 2026 sol pbc

from __future__ import annotations

import json
import re
import shutil
import subprocess
from pathlib import Path

from solstone.convey import backlog_copy

APP_ROOT = Path(__file__).resolve().parents[1]
WORKSPACE_PATH = APP_ROOT / "workspace.html"
HEALTH_JS_PATH = APP_ROOT / "static" / "health.js"

LOGS_COPY = {
    "LOGS_LEVEL_FILTER_LABEL": "level",
    "LOGS_LEVEL_OPTION_ALL": "all levels",
    "LOGS_LEVEL_OPTION_ERROR": "errors only",
    "LOGS_LEVEL_OPTION_INFO": "info & above",
    "LOGS_LEVEL_OPTION_WARNING": "warnings & errors",
    "LOGS_SERVICE_COLLAPSED": "── {service} ── ({n} lines, ★ {errors} errors)",
    "LOGS_SERVICE_FILTER_LABEL": "service",
    "LOGS_STREAM_FILTER_LABEL": "stream",
}
HEALTH_GLANCE_COPY = {
    "HEALTH_GLANCE_CATCHING_UP": "i'm catching up on {n} task(s) in the background. last update {age} ago.",
    "HEALTH_GLANCE_OBSERVER_SILENT": "one of your devices hasn't reached your journal recently.",
    "HEALTH_GLANCE_OK": "everything's working. sol last added to your journal {age} ago.",
    "HEALTH_GLANCE_READINESS_BLOCKED": "{summary}",
    "HEALTH_GLANCE_READINESS_UNKNOWN": "still checking AI readiness. provider setup will be confirmed shortly.",
    "HEALTH_GLANCE_SERVICES_ATTENTION": "{n} service(s) need attention: {service_names}.",
    "HEALTH_GLANCE_SERVICES_UNREACHABLE": "i couldn't reach my own services. check that your journal is running.",
}
BACKLOG_COPY_SUBSET = {
    "bucket_heading": backlog_copy.BACKLOG_BUCKET_HEADING,
    "bucket_description": backlog_copy.BACKLOG_BUCKET_DESCRIPTION,
    "day_badge": backlog_copy.BACKLOG_DAY_BADGE,
    "action_process_now": backlog_copy.BACKLOG_ACTION_PROCESS_NOW,
    "action_redo_scratch": backlog_copy.BACKLOG_ACTION_REDO_SCRATCH,
    "confirm_redo_scratch": backlog_copy.BACKLOG_CONFIRM_REDO_SCRATCH,
    "queued_feedback": backlog_copy.BACKLOG_QUEUED_FEEDBACK,
}


def _workspace() -> str:
    return WORKSPACE_PATH.read_text(encoding="utf-8")


def _health_js() -> str:
    return HEALTH_JS_PATH.read_text(encoding="utf-8")


def _js_const_payload(name: str) -> dict:
    source = _health_js()
    prefix = f"  const {name} = "
    start = source.index(prefix) + len(prefix)
    payload, end = json.JSONDecoder().raw_decode(source[start:])
    assert source[start + end :].lstrip().startswith(";")
    return payload


def _backlog(
    *,
    pending_days=0,
    stuck_days=0,
    days=None,
    errors=None,
    degraded=False,
) -> dict:
    return {
        "degraded": degraded,
        "pending_days": pending_days,
        "stuck_days": stuck_days,
        "days": days or [],
        "errors": errors or [],
    }


def _state_with_stats(health_env, stats_payload: dict) -> dict:
    env = health_env()
    (env.journal / "stats.json").write_text(
        json.dumps(stats_payload),
        encoding="utf-8",
    )
    response = env.client.get("/app/health/api/state")
    assert response.status_code == 200
    return response.get_json()


def _copy(key: str, *, pending=None, stuck=None) -> str:
    return (
        getattr(backlog_copy, key)
        .replace("{pending_n}", "" if pending is None else str(pending))
        .replace("{stuck_n}", "" if stuck is None else str(stuck))
    )


def test_health_spa_shell_workspace_and_route_resolution(health_env):
    env = health_env()
    routes_source = (APP_ROOT / "routes.py").read_text(encoding="utf-8")
    workspace = _workspace()

    index_response = env.client.get("/app/health/")
    workspace_response = env.client.get("/app/health/workspace")

    assert index_response.status_code == 200
    assert b'data-solstone-shell="spa"' in index_response.data
    assert workspace_response.status_code == 200
    assert workspace_response.data == WORKSPACE_PATH.read_bytes()
    assert '<script src="/app/health/static/health.js"></script>' in workspace
    assert "render_template" not in routes_source
    assert "_inject_" + "health" + "_copy" not in routes_source
    assert "_inject_backlog_copy" not in routes_source
    assert "window.HEALTH_READINESS" not in workspace
    assert "window.HEALTH_AGENT_ERRORS" not in workspace
    assert all(token not in workspace for token in ("{{", "{%", "{#"))

    adapter = env.app.url_map.bind("localhost")
    for path in (
        "/app/health/static/health.js",
        "/app/health/api/state",
        "/app/health/api/info",
        "/app/health/api/log",
    ):
        endpoint, _args = adapter.match(path, method="GET")
        assert endpoint


def test_health_workspace_device_copy_replaces_observer_headings():
    workspace = _workspace()

    assert workspace.count("what sol is taking in") == 2
    assert (
        '<h2 class="surface-state-heading">sol isn\'t running on any device yet.</h2>'
        in workspace
    )
    assert ">set up a device →</a>" in workspace
    assert '<div class="card-title">your devices</div>' in workspace
    assert ">manage observers →</a>" not in workspace
    assert "no observations active" not in workspace
    assert "registered observers" not in workspace
    assert '<div class="observe-section-title">screen</div>' in workspace


def test_health_static_js_screen_copy_is_pinned_byte_for_byte():
    source = _health_js()

    assert "label.textContent = 'taking in your screen';" in source
    assert "status: `taking in ${streamCount} ${displayLabel}, ~${mins} min`," in source
    assert "status: `observing (${captures} snapshots, ~${mins} min)`," in source


def test_health_static_js_syntax_check():
    node = shutil.which("node")
    if node is None:
        return

    subprocess.run([node, "--check", str(HEALTH_JS_PATH)], check=True, text=True)


def test_logs_copy_and_controls_render_from_static_js(health_env):
    rendered = health_env().client.get("/app/health/workspace").get_data(as_text=True)

    assert _js_const_payload("HEALTH_LOGS_COPY") == LOGS_COPY
    for key in (
        "LOGS_SERVICE_FILTER_LABEL",
        "LOGS_STREAM_FILTER_LABEL",
        "LOGS_LEVEL_FILTER_LABEL",
        "LOGS_LEVEL_OPTION_ALL",
        "LOGS_LEVEL_OPTION_ERROR",
        "LOGS_LEVEL_OPTION_WARNING",
        "LOGS_LEVEL_OPTION_INFO",
    ):
        assert f'data-health-copy="{key}"' in rendered

    assert 'label for="logServiceFilter"' in rendered
    assert 'label for="logLevelFilter"' in rendered
    assert 'label for="logStreamFilter"' in rendered
    assert '<select id="logLevelFilter">' in rendered
    assert rendered.count("<option value=") >= 8
    assert 'id="logsAnnouncer"' in rendered
    assert 'class="logs-announcer"' in rendered
    assert 'role="status"' in rendered
    assert 'aria-live="polite"' in rendered
    assert "function applyHealthCopy()" in _health_js()


def test_health_glance_copy_literal_and_precedence():
    source = _health_js()

    assert _js_const_payload("HEALTH_GLANCE_COPY") == HEALTH_GLANCE_COPY
    assert "function selectGlanceSentence(state, now)" in source
    start = source.index("function selectGlanceSentence(state, now)")
    end = source.index("function formatGlanceSentence", start)
    selector = source[start:end]
    witnesses = [
        "HEALTH_GLANCE_SERVICES_UNREACHABLE",
        "HEALTH_GLANCE_SERVICES_ATTENTION",
        "HEALTH_GLANCE_READINESS_BLOCKED",
        "HEALTH_GLANCE_OBSERVER_SILENT",
        "HEALTH_GLANCE_CATCHING_UP",
        "HEALTH_GLANCE_READINESS_UNKNOWN",
        "HEALTH_GLANCE_OK",
    ]
    positions = [selector.index(witness) for witness in witnesses]
    assert positions == sorted(positions)


def test_health_status_and_log_toggle_copy_is_folded():
    source = _health_js()

    assert "healthLabel = 'ok';" in source
    assert "mainSpan.textContent = 'ok';" in source
    assert (
        "sections[3]?.setAttribute('aria-label', 'Health: ' + healthLabel);" in source
    )
    assert "elements.logsCollapseIndicator.textContent = '▼ hide';" in source
    assert "state.logsCollapsed ? '▶ show' : '▼ hide';" in source
    assert "healthLabel = 'OK';" not in source
    assert "mainSpan.textContent = 'OK';" not in source
    assert "'▼ Hide'" not in source


def test_agent_error_state_seed_and_dedupe_are_wired():
    source = _health_js()

    assert "window.HEALTH_AGENT_ERRORS" not in source
    assert "window.HEALTH_AGENT_ERRORS_OK" not in source
    assert "function renderAgentErrorsState(agentErrors)" in source
    assert "seedAgentErrors(Array.isArray(data.items) ? data.items : []);" in source
    assert "function seedAgentErrors(seed)" in source
    assert (
        "existing?.id === entry.id && (existing.type || '') === (entry.type || '')"
        in source
    )
    assert "state.recentErrors.push(entry);" in source
    assert "const key = recentErrorGroupKey(entry);" in source
    assert "existing.count += 1;" in source
    assert "countSpan.textContent = `×${count} `;" in source


def test_agent_error_degraded_copy_is_wired():
    source = _health_js()

    assert "couldn't check talent errors today." in source
    assert "elements.glanceErrorsValue.textContent = '—';" in source
    assert (
        "state.recentErrors.length === 0 && !state.recentErrorsFilter && "
        "state.agentErrorsOk"
    ) in source


def test_health_state_fetch_error_renders_retry_surface():
    source = _health_js()
    workspace = _workspace()

    assert "data-health-state-error" in workspace
    assert "function renderHealthStateError(error)" in source
    assert "window.SurfaceState.error({" in source
    assert "retry: true" in source
    assert "loadHealthState();" in source


def test_init_uses_workspace_mount_and_complete_document():
    source = _health_js()

    assert "DOMContentLoaded" not in source
    assert "document.addEventListener('workspace:mounted'" in source
    assert "document.readyState === 'complete'" in source
    assert "let healthInitialized = false;" in source
    assert "loadHealthState();" in source


def test_recent_errors_glance_click_focus_is_wired():
    source = _health_js()

    assert "function runRecentErrorsFocus(day, talent)" in source
    assert "getElementById('glanceErrors')?.addEventListener('click'" in source
    assert "runRecentErrorsFocus('today', '')" in source
    assert "window.addEventListener('hashchange', focusRecentErrors)" in source


def test_recent_error_rows_expand_with_button_panel():
    source = _health_js()
    workspace = _workspace()

    assert "setAttribute('data-action', 'toggle-error')" in source
    assert "setAttribute('aria-expanded', 'false')" in source
    assert "setAttribute('aria-controls', panelId)" in source
    assert "if (action === 'toggle-error')" in source
    assert "onclick" not in workspace
    assert "panel.id = panelId;" in source
    assert "let recentErrorPanelSeq = 0;" in source


def test_error_summary_dom_order():
    rendered = _workspace()

    assert rendered.index('id="healthGlance"') < rendered.index('id="backlogVerdict"')
    assert rendered.index('id="backlogVerdict"') < rendered.index('class="vitals-bar"')
    assert rendered.index('class="vitals-bar"') < rendered.index('id="errorSummary"')
    assert rendered.index('id="errorSummary"') < rendered.index(
        'class="dashboard-card observe-card"'
    )


def test_backlog_verdict_caught_up(health_env):
    state = _state_with_stats(health_env, {"backlog": _backlog()})

    assert state["backlog"]["verdict"] == backlog_copy.BACKLOG_VERDICT_CAUGHT_UP


def test_backlog_verdict_pending_only_singular_and_plural(health_env):
    state = _state_with_stats(health_env, {"backlog": _backlog(pending_days=1)})

    assert (
        state["backlog"]["verdict"]
        == backlog_copy.BACKLOG_VERDICT_PENDING_ONLY_SINGULAR
    )
    assert "1 day(s)" not in state["backlog"]["verdict"]
    assert "caught up" not in state["backlog"]["verdict"]

    state = _state_with_stats(health_env, {"backlog": _backlog(pending_days=4)})

    assert state["backlog"]["verdict"] == _copy(
        "BACKLOG_VERDICT_PENDING_ONLY_PLURAL",
        pending=4,
    )
    assert "caught up" not in state["backlog"]["verdict"]


def test_backlog_verdict_stuck_only_singular_and_plural(health_env):
    state = _state_with_stats(health_env, {"backlog": _backlog(stuck_days=1)})

    assert (
        state["backlog"]["verdict"] == backlog_copy.BACKLOG_VERDICT_STUCK_ONLY_SINGULAR
    )

    state = _state_with_stats(health_env, {"backlog": _backlog(stuck_days=3)})

    assert state["backlog"]["verdict"] == _copy(
        "BACKLOG_VERDICT_STUCK_ONLY_PLURAL",
        stuck=3,
    )


def test_backlog_verdict_mixed_uses_independent_arms(health_env):
    state = _state_with_stats(
        health_env,
        {"backlog": _backlog(pending_days=3, stuck_days=2)},
    )

    assert (
        state["backlog"]["verdict"]
        == "2 days need a hand. 3 more days are still catching up."
    )
    assert "caught up" not in state["backlog"]["verdict"]
    assert "5" not in state["backlog"]["verdict"]


def test_backlog_missing_or_degraded_renders_cant_tell(health_env):
    state = _state_with_stats(health_env, {})

    assert state["backlog"]["verdict"] == backlog_copy.BACKLOG_VERDICT_CANT_TELL
    assert state["backlog"]["stuck_rows"] == []

    state = _state_with_stats(health_env, {"backlog": _backlog(degraded=True)})

    assert state["backlog"]["verdict"] == backlog_copy.BACKLOG_VERDICT_CANT_TELL
    assert state["backlog"]["stuck_rows"] == []


def test_backlog_segment_repair_stuck_day_returns_needs_hand_row(health_env):
    state = _state_with_stats(
        health_env,
        {
            "backlog": _backlog(
                stuck_days=1,
                days=[
                    {
                        "day": "20260323",
                        "state": "stuck",
                        "segments": 0,
                        "units": 0,
                        "reason": "segment_repair_stuck",
                        "reason_code": "segment_repair_stuck",
                        "segment_repair_status": "stuck",
                        "segment_repair_attempts": 3,
                        "segment_repair_consecutive_non_completion": 3,
                        "segment_repair_last_outcome": "timeout",
                        "segment_repair_next_retry_at": 1600.0,
                        "segment_repair_reason_code": "wall_clock_exceeded",
                        "segment_repair_timeout_seconds": 300,
                        "segment_repair_bounded": True,
                    }
                ],
            )
        },
    )

    assert state["backlog"]["verdict"] != backlog_copy.BACKLOG_VERDICT_CAUGHT_UP
    assert state["backlog"]["stuck_rows"] == [
        {
            "day": "20260323",
            "reason": backlog_copy.BACKLOG_REASON_FAILING_STEP,
            "depth": None,
            "reason_code": "segment_repair_stuck",
        }
    ]


def test_backlog_needs_hand_rows_and_copy_subset(health_env):
    state = _state_with_stats(
        health_env,
        {
            "backlog": _backlog(
                stuck_days=1,
                days=[
                    {
                        "day": "20260320",
                        "state": "stuck",
                        "segments": 2,
                        "units": 1,
                        "reason": "corrupt_raw",
                    },
                    {
                        "day": "20260321",
                        "state": "pending",
                        "segments": 0,
                        "units": 4,
                        "reason": "failing_step",
                    },
                    {
                        "day": "20260322",
                        "state": "pending",
                        "segments": 8,
                        "units": 0,
                        "reason": "failing_step",
                    },
                ],
                errors=[
                    {
                        "day": "20260321",
                        "stage": "terminal_states",
                        "message": "boom",
                    }
                ],
            )
        },
    )

    assert state["backlog"]["copy"] == BACKLOG_COPY_SUBSET
    rows = state["backlog"]["stuck_rows"]
    assert [row["day"] for row in rows] == ["20260320", "20260321"]
    assert rows[0]["reason"] == backlog_copy.BACKLOG_REASON_CORRUPT_RAW
    assert rows[1]["reason"] == backlog_copy.BACKLOG_REASON_FAILING_STEP
    assert rows[0]["depth"] == 3
    assert rows[1]["depth"] == 4


def test_backlog_renderer_builds_rows_and_reprocess_buttons():
    source = _health_js()

    assert "function renderBacklogState(backlogState)" in source
    assert "document.querySelector('[data-backlog-stuck-rows]')" in source
    assert "process.dataset.flavor = 'process-now';" in source
    assert "redo.dataset.flavor = 'from-scratch';" in source
    assert "redo.dataset.confirm = backlogCopy.confirm_redo_scratch || '';" in source
    assert "wireBacklogReprocessActions();" in source


def test_status_summary_text_removed():
    source = _workspace()

    assert "statusSummaryText" not in source
    assert 'id="statusSummaryText"' not in source


def test_vitals_sections_have_role_group():
    rendered = _workspace()

    sections = re.findall(r'<div class="vitals-section"[^>]*role="group"', rendered)
    assert len(sections) == 6
    assert rendered.count('class="vitals-label" aria-hidden="true"') == 6
    values = re.findall(r'<div class="vitals-value"[^>]*aria-hidden="true"', rendered)
    assert len(values) == 6


def test_cost_fetch_uses_em_dash_on_failure():
    source = _health_js()
    start = source.index("fetch('/app/tokens/api/usage?day='")
    end = source.index("// State management", start)
    cost_fetch = source[start:end]

    assert ".catch(() =>" in cost_fetch
    assert "textContent = '—';" in cost_fetch


def test_relative_time_helper_is_locally_defined():
    source = _health_js()

    references = re.findall(r"\brelativeTime\s*\(", source)
    definitions = re.findall(r"\bfunction\s+relativeTime\s*\(", source)

    assert len(references) > len(definitions)
    assert len(definitions) == 1


def _health_info_catch_block(source: str) -> str:
    fetch_start = source.index("fetch('/app/health/api/info')")
    catch_start = source.index("    .catch(() => {", fetch_start)
    catch_end = source.index("    });", catch_start) + len("    });")
    return source[catch_start:catch_end]


def test_connection_catch_has_no_dom_writes():
    source = _health_js()
    catch_block = _health_info_catch_block(source)

    assert "document.createElement" not in catch_block
    assert "appendChild" not in catch_block
    assert ".textContent =" not in catch_block
    assert ".innerHTML =" not in catch_block


def test_connect_error_indicator_handled_in_renderer():
    source = _health_js()
    catch_block = _health_info_catch_block(source)
    update_start = source.index("function updateVitals()")
    branch_end = source.index(
        "    // Combine running and crashed services", update_start
    )
    update_vitals_branch = source[update_start:branch_end]

    assert "' Connection error'" not in catch_block
    assert "' Connection error'" in update_vitals_branch
    assert "indicator.className = 'status-indicator crashed';" in update_vitals_branch


def test_no_legacy_stream_classes_in_render_paths():
    source = _workspace() + "\n" + _health_js()

    assert 'class="logs-line stderr"' not in source
    assert 'class="logs-line log"' not in source
    assert re.search(r"\.logs-line\.stderr\s*\{", source) is None
    assert re.search(r"\.logs-line\.log\s*\{", source) is None


def test_deep_link_branch_uses_classifier():
    source = _health_js()
    start = source.index(
        "// Deep-link: display log file content if ?log= param is present"
    )
    end = source.index("function focusRecentErrors", start)
    branch = source[start:end]

    assert "classifyLogLevel(" in branch
    assert 'className = "logs-line stderr"' not in branch
    assert "className = 'logs-line stderr'" not in branch
    assert 'className = "logs-line log"' not in branch
    assert "className = 'logs-line log'" not in branch
    assert "data-hhmmss" not in branch
    assert "dataset.hhmmss" not in branch
