# SPDX-License-Identifier: AGPL-3.0-only
# Copyright (c) 2026 sol pbc

from __future__ import annotations

import shutil
import subprocess
from datetime import datetime
from pathlib import Path

APP_ROOT = Path(__file__).resolve().parents[1]
WORKSPACE_PATH = APP_ROOT / "workspace.html"
HOME_JS_PATH = APP_ROOT / "static" / "home.js"


def _workspace() -> str:
    return WORKSPACE_PATH.read_text(encoding="utf-8")


def _home_js() -> str:
    return HOME_JS_PATH.read_text(encoding="utf-8")


def test_home_spa_shell_workspace_and_route_resolution(home_env):
    env = home_env()
    routes_source = (APP_ROOT / "routes.py").read_text(encoding="utf-8")
    workspace = _workspace()

    index_response = env.client.get("/app/home/")
    workspace_response = env.client.get("/app/home/workspace")

    assert index_response.status_code == 200
    assert b'data-solstone-shell="spa"' in index_response.data
    assert workspace_response.status_code == 200
    assert workspace_response.data == WORKSPACE_PATH.read_bytes()
    assert '<script src="/app/home/static/home.js"></script>' in workspace
    assert "render_template" not in routes_source
    assert all(token not in workspace for token in ("{{", "{%", "{#"))

    adapter = env.app.url_map.bind("localhost")
    for path in (
        "/app/home/static/home.js",
        "/app/home/api/pulse",
        "/app/home/api/briefing",
    ):
        endpoint, _args = adapter.match(path, method="GET")
        assert endpoint


def test_home_static_js_syntax_check():
    node = shutil.which("node")
    if node is None:
        return

    subprocess.run([node, "--check", str(HOME_JS_PATH)], check=True, text=True)


def test_home_init_uses_workspace_mount_and_complete_document():
    source = _home_js()

    assert "DOMContentLoaded" not in source
    assert "location.reload" not in source
    assert "document.addEventListener('workspace:mounted'" in source
    assert "document.readyState === 'complete'" in source
    assert "let homeInitialized = false;" in source
    assert "loadPulse();" in source


def test_home_api_pulse_field_coverage(monkeypatch, home_env):
    import solstone.apps.home.routes as home_routes

    class Attention:
        placeholder_text = "Pipeline needs review"
        context_lines = ["Review the launch checklist"]

    monkeypatch.setattr(
        home_routes,
        "_build_pulse_context",
        lambda: {
            "today": "20260524",
            "now": datetime(2026, 5, 24, 12, 0),
            "health_glance": {
                "verdict": "ok",
                "severity": "green",
                "headline": "everything's working",
                "last_observation": None,
                "cta": None,
                "issues": [],
            },
            "attention": Attention(),
            "pipeline_status": None,
            "segment_count": 0,
            "facet_data": {},
            "narrative_content": "## pulse",
            "narrative_updated_at": "12:00",
            "narrative_source": "pulse",
            "narrative_header": "today's flow",
            "pulse_needs": [],
            "flow_content": None,
            "flow_updated_at": None,
            "anticipated_activities": [],
            "activities": [],
            "needs_you_items": [],
            "briefing_sections": {},
            "briefing_meta": None,
            "briefing_phase": "pending",
            "briefing_lateness": {"late": False, "late_hours": 0},
            "briefing_exists": False,
            "briefing_summary": None,
            "briefing_needs_deduped": [],
            "briefing_needs_shared_count": 0,
            "briefing_needs_badge": None,
            "latest_weekly_reflection": None,
            "yesterday_processing": None,
            "show_welcome": False,
            "journal_age_days": 3,
            "home_state": "active",
            "welcome_framing": "Most of what I learn becomes useful after about a week.",
            "narrative_summary": "pulse at 12:00",
            "today_summary": "",
            "needs_summary": "",
        },
    )

    response = home_env().client.get("/app/home/api/pulse")

    assert response.status_code == 200
    payload = response.get_json()
    assert payload["home_state"] == "active"
    assert payload["welcome_framing"]
    assert payload["attention"] == {
        "placeholder_text": "Pipeline needs review",
        "context_lines": ["Review the launch checklist"],
    }
    assert payload["now"] == "2026-05-24T12:00:00"
    assert payload["narrative_content"] == "## pulse"
    assert "show_welcome" not in payload


def test_home_node_init_and_error_retry(tmp_path):
    node = shutil.which("node")
    if node is None:
        return

    script = tmp_path / "home-init-test.js"
    script.write_text(
        """
const fs = require('fs');
const vm = require('vm');
const source = fs.readFileSync(process.argv[2], 'utf8');

function assert(condition, message) {
  if (!condition) {
    throw new Error(message);
  }
}

const pulsePayload = {
  today: '20260524',
  now: '2026-05-24T12:00:00',
  health_glance: {
    verdict: 'ok',
    severity: 'green',
    headline: "everything's working",
    last_observation: null,
    cta: null,
    issues: [],
  },
  attention: { placeholder_text: '', context_lines: [] },
  segment_count: 0,
  facet_data: {},
  narrative_content: 'hello pulse',
  narrative_updated_at: '12:00',
  narrative_source: 'pulse',
  narrative_header: "today's flow",
  anticipated_activities: [],
  activities: [],
  needs_you_items: [],
  briefing_sections: {},
  briefing_meta: null,
  briefing_phase: 'eod',
  briefing_lateness: { late: false, late_hours: 0 },
  briefing_exists: false,
  briefing_summary: null,
  briefing_needs_deduped: [],
  briefing_needs_shared_count: 0,
  briefing_needs_badge: null,
  latest_weekly_reflection: null,
  yesterday_processing: null,
  journal_age_days: 3,
  home_state: 'active',
  welcome_framing: null,
  narrative_summary: 'hello pulse',
  today_summary: '',
  needs_summary: '',
};

function flush() {
  return new Promise(resolve => setImmediate(resolve));
}

function makeContext(apiJson) {
  const listeners = {};
  const apiCalls = [];
  const errorCalls = [];
  let retryHandler = null;
  let reloadCalls = 0;
  const root = {
    addEventListener() {},
    querySelector() { return null; },
  };
  const retryButton = {
    addEventListener(type, handler) {
      if (type === 'click') {
        retryHandler = handler;
      }
    },
  };
  const surface = {
    innerHTML: '',
    querySelector(selector) {
      if (selector === '.surface-state-retry' && this.innerHTML.includes('surface-state-retry')) {
        return retryButton;
      }
      return null;
    },
    insertAdjacentHTML(_position, html) {
      this.innerHTML += html;
    },
  };
  const document = {
    readyState: 'complete',
    addEventListener(type, handler) {
      listeners[type] = handler;
    },
    querySelector(selector) {
      if (selector === '[data-home-root]') return root;
      if (selector === '[data-pulse-surface]') return surface;
      return null;
    },
    getElementById() {
      return null;
    },
    createElement() {
      return {
        set textContent(value) { this._text = String(value || ''); },
        get textContent() { return this._text || ''; },
        setAttribute() {},
        appendChild() {},
      };
    },
  };
  const window = {
    apiJson(url) {
      apiCalls.push(url);
      return apiJson(url);
    },
    SurfaceState: {
      loading({ text }) {
        return `<div class="surface-state--loading">${text}</div>`;
      },
      error(options) {
        errorCalls.push(options);
        return '<div class="surface-state--error"><button class="surface-state-retry">retry</button></div>';
      },
    },
    AppServices: {
      renderMarkdown(raw) {
        return `<p>${String(raw || '')}</p>`;
      },
    },
    appEvents: {
      listen() {},
    },
    CONVEY_COPY: { RELOAD_HINT: 'try again' },
    location: {
      href: '',
      reload() {
        reloadCalls += 1;
      },
    },
    logError() {},
  };
  const context = {
    console,
    setImmediate,
    document,
    window,
    sessionStorage: {
      getItem() { return null; },
      setItem() {},
    },
  };
  vm.createContext(context);
  vm.runInContext(source, context);
  return {
    apiCalls,
    errorCalls,
    listeners,
    surface,
    triggerRetry() {
      assert(retryHandler, 'retry handler was not wired');
      retryHandler();
    },
    reloadCalls() {
      return reloadCalls;
    },
  };
}

(async () => {
  const success = makeContext(url => {
    if (url === '/app/home/api/pulse') return Promise.resolve(pulsePayload);
    if (url === '/app/home/api/briefing') {
      return Promise.resolve({
        exists: false,
        phase: 'eod',
        summary: null,
        meta: null,
        sections: {},
        needs_deduped: [],
        needs_shared_count: 0,
        needs_badge: null,
      });
    }
    return Promise.reject(new Error(`unexpected url ${url}`));
  });
  success.listeners['workspace:mounted']({ detail: { app: 'home' } });
  await flush();
  await flush();
  assert(success.apiCalls.filter(url => url === '/app/home/api/pulse').length === 1, 'pulse should fetch once');
  assert(success.surface.innerHTML.includes('pulse-vitals'), 'pulse render did not populate vitals');
  assert(success.surface.innerHTML.includes('pulse-narrative'), 'pulse render did not populate narrative');

  let failureCalls = 0;
  const failed = makeContext(url => {
    if (url !== '/app/home/api/pulse') return Promise.reject(new Error(`unexpected url ${url}`));
    failureCalls += 1;
    return Promise.reject(new Error('pulse failed'));
  });
  await flush();
  await flush();
  assert(failed.errorCalls.length === 1, 'initial failure should render one error');
  failed.triggerRetry();
  await flush();
  await flush();
  assert(failed.errorCalls.length === 2, 'retry failure should re-render error once');
  assert(failureCalls === 2, 'retry should make exactly one fresh pulse call');
  assert(failed.reloadCalls() === 0, 'home.js must never reload the page');
  await flush();
  assert(failureCalls === 2, 'failed retry must not loop');
})().catch(error => {
  console.error(error);
  process.exit(1);
});
""",
        encoding="utf-8",
    )

    subprocess.run([node, str(script), str(HOME_JS_PATH)], check=True, text=True)
