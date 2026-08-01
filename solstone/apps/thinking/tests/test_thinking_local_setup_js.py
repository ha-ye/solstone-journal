# SPDX-License-Identifier: AGPL-3.0-only
# Copyright (c) 2026 sol pbc

from __future__ import annotations

import json
import shutil
import subprocess
from pathlib import Path

import pytest

from solstone.apps.thinking import copy as thinking_copy
from solstone.apps.thinking.tests.js_extract import (
    extract_js_const,
    extract_js_function,
)

STATIC = Path(__file__).resolve().parents[1] / "static" / "thinking.js"


LOCAL_DOM_STUB = """
const nodes = new Map();
const shownViews = [];
const providerEnv = {
  anthropic: 'ANTHROPIC_API_KEY',
  google: 'GOOGLE_API_KEY',
  openai: 'OPENAI_API_KEY',
};

function makeClassList() {
  return {
    values: new Set(),
    toggle(name, force) {
      const enabled = force === undefined ? !this.values.has(name) : !!force;
      if (enabled) {
        this.values.add(name);
      } else {
        this.values.delete(name);
      }
    },
    contains(name) {
      return this.values.has(name);
    },
  };
}

function attach(parent, child) {
  if (child && typeof child === 'object') child.parentElement = parent;
  parent.children.push(child);
}

function makeNode(id = '', tagName = 'div') {
  let ownText = '';
  const node = {
    id,
    tagName,
    children: [],
    dataset: {},
    attributes: {},
    events: {},
    className: '',
    classList: makeClassList(),
    value: '',
    hidden: false,
    disabled: false,
    checked: false,
    href: '',
    target: '',
    rel: '',
    type: '',
    name: '',
    parentElement: null,
    appendChild(child) {
      attach(this, child);
      return child;
    },
    append(...items) {
      items.forEach((item) => attach(this, item));
    },
    setAttribute(name, value) {
      this.attributes[name] = String(value);
    },
    removeAttribute(name) {
      delete this.attributes[name];
      if (name === 'data-tone') delete this.dataset.tone;
    },
    addEventListener(name, handler) {
      this.events[name] = handler;
    },
  };
  Object.defineProperty(node, 'textContent', {
    get() {
      return ownText;
    },
    set(value) {
      ownText = value === undefined || value === null ? '' : String(value);
      node.children = [];
    },
  });
  return node;
}

function $(id) {
  if (!nodes.has(id)) nodes.set(id, makeNode(id));
  return nodes.get(id);
}

function setText(id, message) {
  const node = $(id);
  node.textContent = message || '';
}

function setMessage(id, message, tone = '') {
  const node = $(id);
  node.textContent = message || '';
  if (tone) {
    node.dataset.tone = tone;
  } else {
    delete node.dataset.tone;
  }
}

function setHidden(id, hidden) {
  $(id).hidden = !!hidden;
}

function setButtonState(id, visible, disabled) {
  const button = $(id);
  button.hidden = !visible;
  button.disabled = !!disabled;
}

function setButtonText(id, text) {
  $(id).textContent = text || '';
}

function setPill(id, label, tone = '') {
  const pill = $(id);
  pill.textContent = label;
  pill.classList.toggle('hot', tone === 'hot');
  pill.classList.toggle('bad', tone === 'bad');
}

function showView(name, options = {}) {
  shownViews.push({name, options});
}

const document = {
  createElement(tagName) {
    return makeNode('', tagName);
  },
  createTextNode(text) {
    return String(text);
  },
};

function renderConfidentialCard() {}
function renderConfidentialDetailPanel() {}

function collectText(node) {
  if (typeof node === 'string') return node;
  return `${node.textContent || ''}${node.children.map(collectText).join('')}`;
}

function buttonState(id) {
  const node = $(id);
  return {visible: !node.hidden, enabled: !node.disabled, text: node.textContent || ''};
}

function pillState(id) {
  const node = $(id);
  return {
    label: node.textContent || '',
    hot: node.classList.contains('hot'),
    bad: node.classList.contains('bad'),
  };
}

function messageState(id) {
  const node = $(id);
  return {text: node.textContent || '', tone: node.dataset.tone || ''};
}

function linksText() {
  return collectText($('localSetupLinks'));
}

function resetHarness(nextState) {
  nodes.clear();
  shownViews.length = 0;
  state = nextState;
}
"""


def _node_script(body: str) -> str:
    source = STATIC.read_text(encoding="utf-8")
    parts = [
        extract_js_const(source, "installInFlightStates"),
        extract_js_const(source, "installTerminalStates"),
        extract_js_const(source, "localSetupMissingReasons"),
        extract_js_const(source, "localServerUnhealthyReasons"),
        extract_js_function(source, "formatInstallBytes"),
        extract_js_function(source, "installCopyForStatus"),
        extract_js_function(source, "installIsInFlight"),
        extract_js_function(source, "localRuntimeCopy"),
        extract_js_function(source, "laneCopy"),
        extract_js_function(source, "providerLabel"),
        extract_js_function(source, "configuredProviders"),
        extract_js_function(source, "localEndpointConfigured"),
        extract_js_function(source, "byoIsUsable"),
        extract_js_function(source, "defaultByoProvider"),
        extract_js_function(source, "byoKindForProvider"),
        extract_js_function(source, "localReadiness"),
        extract_js_function(source, "localIsReady"),
        extract_js_function(source, "localIsGpuBlocked"),
        extract_js_function(source, "activeLanePayload"),
        extract_js_function(source, "confidentialProvenancePresent"),
        extract_js_function(source, "activeBrain"),
        extract_js_function(source, "setCardActive"),
        extract_js_function(source, "laneDisplayLabel"),
        extract_js_function(source, "activeLaneLabel"),
        extract_js_function(source, "localCopy"),
        extract_js_function(source, "renderLocal"),
        extract_js_function(source, "renderMainLanes"),
        "function assert(condition, message) { if (!condition) throw new Error(message); }",
        "function assertEqual(actual, expected, message) { if (actual !== expected) throw new Error(`${message}: expected ${JSON.stringify(expected)}, got ${JSON.stringify(actual)}`); }",
        f"const copy = {json.dumps(thinking_copy.thinking_copy_payload())};",
        "const fallbackProviderLabels = {};",
        "let providerLabels = copy.provider_labels || fallbackProviderLabels;",
        "let state = null;",
        LOCAL_DOM_STUB,
        """
function runtimeView(overrides = {}) {
  return {
    status: 'ok',
    phase: 'artifact-not-ready',
    reason_code: null,
    health_revision: 0,
    desired_fingerprint_sha256: null,
    retry_revision: 0,
    retry_pending: false,
    can_retry: false,
    poll: false,
    updated_at: null,
    ...overrides,
  };
}

function providerStatus(issues = [], ready = false) {
  return {
    configured: ready,
    selected: true,
    generate_ready: ready,
    cogitate_ready: ready,
    issues,
  };
}

function baseState(options = {}) {
  const providerStatusPayload = options.omitProviderStatus
    ? {}
    : {local: providerStatus(options.issues || [], options.ready || false)};
  return {
    providers: {
      provider_status: providerStatusPayload,
      local_runtime: options.runtime === null ? null : runtimeView(options.runtime || {}),
      active_lane: {lane: options.lane || 'local'},
      local_override: {
        enabled: false,
        endpoint_url: '',
        served_model_id: '',
        credential_configured: false,
      },
      active: options.active || {provider: 'local', model: 'local/test'},
      model_tiers: {},
      byo_models: {},
      brain: {},
    },
    keys: {
      api_keys: options.apiKeys || {},
      key_validation: {},
    },
    install: options.install || {install_state: 'idle'},
    localAvailability: options.localAvailability === undefined ? null : options.localAvailability,
  };
}

function renderLocalFor(options = {}) {
  resetHarness(baseState(options));
  renderLocal();
}

function assertLocalSetupEnabled(reason) {
  renderLocalFor({issues: [reason], runtime: {phase: 'artifact-not-ready'}});
  const bootstrap = buttonState('localBootstrap');
  const pill = pillState('localSetupPill');
  assertEqual(pill.label, 'setup needed', `${reason} pill`);
  assertEqual(pill.bad, false, `${reason} pill tone`);
  assertEqual(bootstrap.visible, true, `${reason} bootstrap visible`);
  assertEqual(bootstrap.enabled, true, `${reason} bootstrap enabled`);
  assertEqual(bootstrap.text, copy.local_install.install, `${reason} bootstrap label`);
}

function assertExpectedChainDisposition(issue, expected, phase) {
  renderLocalFor({issues: [issue], runtime: {phase}});
  const bootstrap = buttonState('localBootstrap');
  const retry = buttonState('localRuntimeRetry');
  const pill = pillState('localSetupPill');
  if (expected === 'setup') {
    assertEqual(pill.label, 'setup needed', `${issue}/${phase} setup pill`);
    assertEqual(bootstrap.visible, true, `${issue}/${phase} bootstrap visible`);
    assertEqual(bootstrap.enabled, true, `${issue}/${phase} bootstrap enabled`);
    assertEqual(retry.visible, false, `${issue}/${phase} retry hidden`);
    return;
  }
  if (expected === 'gpu') {
    assertEqual(pill.label, 'unavailable', `${issue}/${phase} gpu pill`);
    assertEqual(pill.bad, true, `${issue}/${phase} gpu tone`);
    assertEqual(bootstrap.visible, false, `${issue}/${phase} bootstrap hidden`);
    return;
  }
  if (expected === 'bad') {
    assertEqual(pill.bad, true, `${issue}/${phase} bad tone`);
    assertEqual(bootstrap.visible, false, `${issue}/${phase} bootstrap hidden`);
    return;
  }
  if (expected === 'blocked-default') {
    assertEqual(pill.label, 'not ready', `${issue}/${phase} default pill`);
    assertEqual(pill.bad, true, `${issue}/${phase} default tone`);
    assertEqual(messageState('localSetupMessage').text, issue, `${issue}/${phase} raw issue`);
    assertEqual(bootstrap.visible, false, `${issue}/${phase} bootstrap hidden`);
    return;
  }
  throw new Error(`unknown expected disposition ${expected}`);
}
""",
        body,
    ]
    return "\n".join(parts)


def _run_node(script: str) -> None:
    node = shutil.which("node")
    if node is None:
        pytest.fail("node is not available")
    result = subprocess.run(
        [node, "-e", script],
        check=True,
        text=True,
        capture_output=True,
    )
    assert "PASS" in result.stdout


@pytest.mark.parametrize("phase", ["artifact-not-ready", "stopped"])
@pytest.mark.parametrize(
    ("issue", "expected"),
    [
        pytest.param(
            "local_endpoint_unreachable",
            "bad",
            id="local_endpoint_unreachable-byo-endpoint",
        ),
        pytest.param("runtime_missing", "setup", id="runtime_missing-darwin-mlx"),
        pytest.param("model_missing", "setup", id="model_missing-darwin-mlx"),
        pytest.param("model_missing", "setup", id="model_missing-non-darwin"),
        pytest.param("server_unhealthy", "bad", id="server_unhealthy-non-darwin"),
        pytest.param("gpu_unavailable", "gpu", id="gpu_unavailable-non-darwin"),
        pytest.param("binary_missing", "setup", id="binary_missing-non-darwin"),
        pytest.param(
            "failed to launch: boom",
            "blocked-default",
            id="failed_to_launch-non-darwin",
        ),
    ],
)
def test_local_readiness_issue_dispositions_reach_chain(
    issue: str,
    expected: str,
    phase: str,
) -> None:
    _run_node(
        _node_script(
            f"""
assertExpectedChainDisposition({json.dumps(issue)}, {json.dumps(expected)}, {json.dumps(phase)});
console.log('PASS');
"""
        )
    )


def test_local_copy_runtime_overlay_precedence_survives_setup_reason_bridge() -> None:
    _run_node(
        _node_script(
            """
const cases = [
  {issue: 'binary_missing', phase: 'failed', reason_code: 'launch-budget-exhausted', can_retry: true, label: 'binary_missing/non-darwin'},
  {issue: 'model_missing', phase: 'failed', reason_code: 'launch-budget-exhausted', can_retry: true, label: 'model_missing/non-darwin'},
  {issue: 'model_missing', phase: 'failed', reason_code: 'launch-budget-exhausted', can_retry: true, label: 'model_missing/darwin-mlx'},
  {issue: 'runtime_missing', phase: 'failed', reason_code: 'launch-budget-exhausted', can_retry: true, label: 'runtime_missing/darwin-mlx'},
  {issue: 'binary_missing', phase: 'ready', label: 'ready overlay'},
  {issue: 'model_missing', phase: 'ready-proof-unavailable', label: 'ready proof overlay'},
];
for (const item of cases) {
  const runtime = runtimeView({
    phase: item.phase,
    reason_code: item.reason_code || null,
    can_retry: item.can_retry || false,
    desired_fingerprint_sha256: item.can_retry ? 'fp-local' : null,
    health_revision: item.can_retry ? 7 : 0,
  });
  resetHarness(baseState({issues: [item.issue], runtime}));
  const expected = localRuntimeCopy(runtime, true, copy.local_recovery || {});
  const actual = localCopy();
  assert(
    JSON.stringify(actual) === JSON.stringify(expected),
    `${item.label} should preserve runtime overlay precedence`,
  );
}
console.log('PASS');
"""
        )
    )


@pytest.mark.parametrize(
    "reason",
    ["local_model_missing", "model_missing", "binary_missing", "runtime_missing"],
)
def test_render_local_bridged_missing_reasons_enable_install_button(
    reason: str,
) -> None:
    _run_node(
        _node_script(
            f"""
assertLocalSetupEnabled({json.dumps(reason)});
console.log('PASS');
"""
        )
    )


def test_render_local_active_stopped_runtime_declines_to_setup_chain() -> None:
    _run_node(
        _node_script(
            """
for (const lane of ['local', 'byo']) {
  // The BYO mirror proves this exercises the active local runtime overlay: without
  // it, a chain-only fix goes green while the active stopped overlay stays broken.
  renderLocalFor({
    issues: ['binary_missing'],
    lane,
    runtime: {
      status: 'ok',
      phase: 'stopped',
      reason_code: null,
      health_revision: 0,
      desired_fingerprint_sha256: null,
      retry_revision: 0,
      retry_pending: false,
      can_retry: false,
      poll: false,
      updated_at: null,
    },
  });
  const bootstrap = buttonState('localBootstrap');
  assertEqual(pillState('localSetupPill').label, 'setup needed', `${lane} stopped setup pill`);
  assertEqual(bootstrap.visible, true, `${lane} stopped bootstrap visible`);
  assertEqual(bootstrap.enabled, true, `${lane} stopped bootstrap enabled`);
}
console.log('PASS');
"""
        )
    )


def test_render_local_host_blocked_runtime_waiting_view_stays_overlay() -> None:
    _run_node(
        _node_script(
            """
renderLocalFor({
  issues: ['gpu_unavailable'],
  lane: 'local',
  runtime: {phase: 'host-blocked', reason_code: 'ram-insufficient'},
});
assertEqual(pillState('localSetupPill').label, 'waiting', 'host block waiting pill');
assertEqual(pillState('localSetupPill').bad, false, 'host block waiting tone');
assertEqual(buttonState('localBootstrap').visible, false, 'host block bootstrap hidden');
assertEqual($('localNotice').textContent, 'it will start when this computer is ready.', 'host block notice');
console.log('PASS');
"""
        )
    )


def test_render_local_gpu_unavailable_with_stopped_runtime_reaches_chain() -> None:
    _run_node(
        _node_script(
            """
renderLocalFor({
  issues: ['gpu_unavailable'],
  lane: 'local',
  runtime: {phase: 'stopped'},
});
assertEqual(pillState('localSetupPill').label, 'unavailable', 'gpu stopped pill');
assertEqual(pillState('localSetupPill').bad, true, 'gpu stopped tone');
assertEqual(buttonState('localBootstrap').visible, false, 'gpu stopped bootstrap hidden');
assert(linksText().includes('minimum requirements ↗'), 'requirements help link should render');
assert(linksText().includes(copy.active_lane_labels.byo), 'BYO help link should render');
console.log('PASS');
"""
        )
    )


def test_render_local_unknown_blocked_readiness_is_bad_without_bootstrap() -> None:
    _run_node(
        _node_script(
            """
renderLocalFor({
  issues: ['failed to launch: boom'],
  runtime: {phase: 'artifact-not-ready'},
});
assertEqual(pillState('localSetupPill').label, 'not ready', 'unknown blocked pill');
assertEqual(pillState('localSetupPill').bad, true, 'unknown blocked tone');
assertEqual(messageState('localSetupMessage').text, 'failed to launch: boom', 'raw issue visible');
assertEqual(messageState('localSetupMessage').tone, 'error', 'raw issue error tone');
assertEqual(buttonState('localBootstrap').visible, false, 'unknown blocked bootstrap hidden');
assertEqual(buttonState('localRefresh').visible, true, 'refresh visible');
assertEqual(buttonState('localRefresh').enabled, true, 'refresh enabled');
assert(linksText().includes('minimum requirements ↗'), 'requirements help link should render');
assert(linksText().includes(copy.active_lane_labels.byo), 'BYO help link should render');
console.log('PASS');
"""
        )
    )


def test_render_local_missing_readiness_stays_neutral_checking() -> None:
    _run_node(
        _node_script(
            """
renderLocalFor({
  omitProviderStatus: true,
  localAvailability: null,
  runtime: {phase: 'artifact-not-ready'},
});
assertEqual(pillState('localSetupPill').label, 'checking', 'missing readiness checking pill');
assertEqual(pillState('localSetupPill').bad, false, 'missing readiness neutral tone');
assertEqual(messageState('localSetupMessage').text, '', 'missing readiness empty message');
assertEqual(messageState('localSetupMessage').tone, '', 'missing readiness neutral message');
assertEqual(linksText(), '', 'missing readiness has no help links');
console.log('PASS');
"""
        )
    )


def test_render_local_failed_runtime_retry_precedes_server_unhealthy_chain() -> None:
    _run_node(
        _node_script(
            """
for (const issue of ['server_unhealthy', 'local_server_unhealthy']) {
  renderLocalFor({
    issues: [issue],
    lane: 'local',
    runtime: {
      phase: 'failed',
      reason_code: 'launch-budget-exhausted',
      can_retry: true,
      desired_fingerprint_sha256: 'fp-local',
      health_revision: 7,
    },
  });
  const retry = buttonState('localRuntimeRetry');
  assertEqual(pillState('localSetupPill').label, 'needs attention', `${issue} failed runtime pill`);
  assertEqual(retry.visible, true, `${issue} retry visible`);
  assertEqual(retry.enabled, true, `${issue} retry enabled`);
  assertEqual(buttonState('localBootstrap').visible, false, `${issue} bootstrap hidden`);
}
console.log('PASS');
"""
        )
    )


def test_gpu_unavailable_wins_before_binary_missing() -> None:
    _run_node(
        _node_script(
            """
// Decided behavior: a computer without a supported GPU cannot usefully run the
// bundled model, so offering install there would be a different dishonesty.
// A failed probe is distinct (`gpu_probe_failed`); `gpu_unavailable` is a verdict.
renderLocalFor({
  issues: ['gpu_unavailable', 'binary_missing'],
  runtime: {phase: 'artifact-not-ready'},
});
assertEqual(pillState('localSetupPill').label, 'unavailable', 'gpu first pill');
assertEqual(pillState('localSetupPill').bad, true, 'gpu first tone');
assertEqual(buttonState('localBootstrap').visible, false, 'gpu first bootstrap hidden');
assert(linksText().includes('minimum requirements ↗'), 'requirements help link should render');
assert(linksText().includes(copy.active_lane_labels.byo), 'BYO help link should render');
console.log('PASS');
"""
        )
    )


def test_local_lane_gpu_blocking_is_limited_to_gpu_reasons() -> None:
    _run_node(
        _node_script(
            """
const cases = [
  {issue: 'local_endpoint_unreachable', blocked: false, desc: 'local_endpoint_unreachable'},
  {issue: 'runtime_missing', blocked: false, desc: 'runtime_missing'},
  {issue: 'model_missing', blocked: false, desc: 'model_missing'},
  {issue: 'server_unhealthy', blocked: false, desc: 'server_unhealthy'},
  {issue: 'binary_missing', blocked: false, desc: 'binary_missing'},
  {issue: 'failed to launch: boom', blocked: false, desc: 'failed to launch: boom'},
  {
    issue: 'gpu_unavailable',
    blocked: true,
    desc: "this computer can't run a local model yet — it needs a supported GPU. minimum requirements ↗",
  },
];

for (const item of cases) {
  resetHarness(baseState({issues: [item.issue], runtime: {phase: 'artifact-not-ready'}}));
  assertEqual(localIsGpuBlocked(), item.blocked, `${item.issue} localIsGpuBlocked`);
  renderMainLanes();
  assertEqual($('lane-local').classList.contains('greyed'), item.blocked, `${item.issue} greyed`);
  assertEqual($('lane-local').attributes['aria-disabled'], item.blocked ? 'true' : 'false', `${item.issue} aria`);
  assertEqual(collectText($('localLaneDescription')), item.desc, `${item.issue} description`);
}
console.log('PASS');
"""
        )
    )
