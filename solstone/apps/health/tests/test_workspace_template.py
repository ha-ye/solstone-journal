# SPDX-License-Identifier: AGPL-3.0-only
# Copyright (c) 2026 sol pbc

from __future__ import annotations

import html
import json
import re
from pathlib import Path

from solstone.apps.health import copy as health_copy

WORKSPACE_PATH = Path(__file__).resolve().parents[1] / "workspace.html"
LOGS_COPY_KEYS = [
    "LOGS_SERVICE_FILTER_LABEL",
    "LOGS_STREAM_FILTER_LABEL",
    "LOGS_LEVEL_FILTER_LABEL",
    "LOGS_LEVEL_OPTION_ALL",
    "LOGS_LEVEL_OPTION_ERROR",
    "LOGS_LEVEL_OPTION_WARNING",
    "LOGS_LEVEL_OPTION_INFO",
    "LOGS_SERVICE_COLLAPSED",
]
HEALTH_GLANCE_COPY_KEYS = [
    "HEALTH_GLANCE_OK",
    "HEALTH_GLANCE_SERVICES_ATTENTION",
    "HEALTH_GLANCE_CATCHING_UP",
    "HEALTH_GLANCE_OBSERVER_SILENT",
    "HEALTH_GLANCE_SERVICES_UNREACHABLE",
]
HEALTH_GLANCE_LITERALS = {
    "HEALTH_GLANCE_OK": "everything's working — last observation {age} ago.",
    "HEALTH_GLANCE_SERVICES_ATTENTION": "{n} service(s) need attention — {service_names}.",
    "HEALTH_GLANCE_CATCHING_UP": (
        "I'm catching up on {n} task(s) in the background — last update {age} ago."
    ),
    "HEALTH_GLANCE_OBSERVER_SILENT": (
        "I haven't heard from your observer in {age} — it may have stopped."
    ),
    "HEALTH_GLANCE_SERVICES_UNREACHABLE": (
        "I couldn't reach my own services — check that solstone is running."
    ),
}


def _render_health_workspace(health_env) -> str:
    env = health_env()
    response = env.client.get("/app/health/")
    assert response.status_code == 200
    return response.get_data(as_text=True)


def test_logs_copy_and_controls_render(health_env):
    rendered = _render_health_workspace(health_env)
    decoded = html.unescape(rendered)

    for value in (
        health_copy.LOGS_SERVICE_FILTER_LABEL,
        health_copy.LOGS_STREAM_FILTER_LABEL,
        health_copy.LOGS_LEVEL_FILTER_LABEL,
        health_copy.LOGS_LEVEL_OPTION_ALL,
        health_copy.LOGS_LEVEL_OPTION_ERROR,
        health_copy.LOGS_LEVEL_OPTION_WARNING,
        health_copy.LOGS_LEVEL_OPTION_INFO,
    ):
        assert value in decoded

    assert 'label for="logServiceFilter"' in rendered
    assert 'label for="logLevelFilter"' in rendered
    assert 'label for="logStreamFilter"' in rendered
    assert '<select id="logLevelFilter">' in rendered
    assert decoded.count("<option value=") >= 8
    assert 'id="logsAnnouncer"' in rendered
    assert 'class="logs-announcer"' in rendered
    assert 'role="status"' in rendered
    assert 'aria-live="polite"' in rendered


def test_health_logs_copy_script_carries_all_keys(health_env):
    rendered = _render_health_workspace(health_env)

    assert "window.HEALTH_LOGS_COPY" in rendered
    for key in LOGS_COPY_KEYS:
        assert f"{key}:" in rendered

    script_values = {}
    for key in LOGS_COPY_KEYS:
        match = re.search(rf"{key}:\s*(?P<value>\"(?:\\.|[^\"])*\")", rendered)
        assert match is not None, key
        script_values[key] = json.loads(match.group("value"))

    assert script_values == {key: getattr(health_copy, key) for key in LOGS_COPY_KEYS}


def test_health_glance_copy_constants_are_literal():
    for key, value in HEALTH_GLANCE_LITERALS.items():
        assert getattr(health_copy, key) == value


def test_health_glance_copy_script_carries_all_keys(health_env):
    rendered = _render_health_workspace(health_env)

    assert "window.HEALTH_GLANCE_COPY" in rendered
    for key in HEALTH_GLANCE_COPY_KEYS:
        assert f"{key}:" in rendered

    script_values = {}
    for key in HEALTH_GLANCE_COPY_KEYS:
        match = re.search(rf"{key}:\s*(?P<value>\"(?:\\.|[^\"])*\")", rendered)
        assert match is not None, key
        script_values[key] = json.loads(match.group("value"))

    assert script_values == {
        key: getattr(health_copy, key) for key in HEALTH_GLANCE_COPY_KEYS
    }


def test_select_glance_sentence_exists(health_env):
    rendered = _render_health_workspace(health_env)

    assert "function selectGlanceSentence(state, now)" in rendered


def test_glance_precedence_order(health_env):
    rendered = _render_health_workspace(health_env)
    start = rendered.index("function selectGlanceSentence(state, now)")
    end = rendered.index("function formatGlanceSentence", start)
    selector = rendered[start:end]

    witnesses = [
        "HEALTH_GLANCE_SERVICES_UNREACHABLE",
        "HEALTH_GLANCE_SERVICES_ATTENTION",
        "HEALTH_GLANCE_OBSERVER_SILENT",
        "HEALTH_GLANCE_CATCHING_UP",
        "HEALTH_GLANCE_OK",
    ]
    positions = [selector.index(witness) for witness in witnesses]
    assert positions == sorted(positions)


def test_error_summary_dom_order(health_env):
    rendered = _render_health_workspace(health_env)

    assert rendered.index('id="healthGlance"') < rendered.index('class="vitals-bar"')
    assert rendered.index('class="vitals-bar"') < rendered.index('id="errorSummary"')
    assert rendered.index('id="errorSummary"') < rendered.index(
        'class="dashboard-card observe-card"'
    )


def test_status_summary_text_removed(health_env):
    source = WORKSPACE_PATH.read_text(encoding="utf-8")
    rendered = _render_health_workspace(health_env)

    assert "statusSummaryText" not in source
    assert 'id="statusSummaryText"' not in rendered


def test_vitals_sections_have_role_group(health_env):
    rendered = _render_health_workspace(health_env)

    sections = re.findall(r'<div class="vitals-section"[^>]*role="group"', rendered)
    assert len(sections) == 6
    assert rendered.count('class="vitals-label" aria-hidden="true"') == 6
    values = re.findall(r'<div class="vitals-value"[^>]*aria-hidden="true"', rendered)
    assert len(values) == 6


def test_cost_fetch_uses_em_dash_on_failure():
    source = WORKSPACE_PATH.read_text(encoding="utf-8")
    start = source.index("fetch('/app/tokens/api/usage?day='")
    end = source.index("// State management", start)
    cost_fetch = source[start:end]

    assert ".catch(() =>" in cost_fetch
    assert "textContent = '—';" in cost_fetch


def _health_info_catch_block(source: str) -> str:
    fetch_start = source.index("fetch('/app/health/api/info')")
    catch_start = source.index("    .catch(() => {", fetch_start)
    catch_end = source.index("    });", catch_start) + len("    });")
    return source[catch_start:catch_end]


def test_connection_catch_has_no_dom_writes():
    source = WORKSPACE_PATH.read_text(encoding="utf-8")
    catch_block = _health_info_catch_block(source)

    assert "document.createElement" not in catch_block
    assert "appendChild" not in catch_block
    assert ".textContent =" not in catch_block
    assert ".innerHTML =" not in catch_block


def test_connect_error_indicator_handled_in_renderer():
    source = WORKSPACE_PATH.read_text(encoding="utf-8")
    catch_block = _health_info_catch_block(source)
    update_start = source.index("function updateVitals()")
    branch_end = source.index(
        "    // Combine running and crashed services", update_start
    )
    update_vitals_branch = source[update_start:branch_end]

    assert "' Connection error'" not in catch_block
    assert "' Connection error'" in update_vitals_branch
    assert "indicator.className = 'status-indicator crashed';" in update_vitals_branch


def test_no_legacy_stream_classes_in_render_paths(health_env):
    rendered = _render_health_workspace(health_env)

    assert 'class="logs-line stderr"' not in rendered
    assert 'class="logs-line log"' not in rendered
    assert re.search(r"\.logs-line\.stderr\s*\{", rendered) is None
    assert re.search(r"\.logs-line\.log\s*\{", rendered) is None


def test_deep_link_branch_uses_classifier():
    source = WORKSPACE_PATH.read_text(encoding="utf-8")
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
