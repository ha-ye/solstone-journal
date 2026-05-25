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
    end = source.index("const hashParams", start)
    branch = source[start:end]

    assert "classifyLogLevel(" in branch
    assert 'className = "logs-line stderr"' not in branch
    assert "className = 'logs-line stderr'" not in branch
    assert 'className = "logs-line log"' not in branch
    assert "className = 'logs-line log'" not in branch
    assert "data-hhmmss" not in branch
    assert "dataset.hhmmss" not in branch
