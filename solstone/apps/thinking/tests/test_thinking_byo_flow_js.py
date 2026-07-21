# SPDX-License-Identifier: AGPL-3.0-only
# Copyright (c) 2026 sol pbc

from __future__ import annotations

import json
import shutil
import subprocess
from pathlib import Path

import pytest

from solstone.apps.thinking import copy as thinking_copy
from solstone.apps.thinking.tests.js_extract import extract_js_function

STATIC = Path(__file__).resolve().parents[1] / "static" / "thinking.js"

RENDER_DOM_STUB = """
const nodes = new Map();
const hiddenCalls = [];
const providerEnv = {
  anthropic: 'ANTHROPIC_API_KEY',
  google: 'GOOGLE_API_KEY',
  openai: 'OPENAI_API_KEY',
};
const providerTerms = {
  anthropic: 'https://www.anthropic.com/legal/commercial-terms',
  google: 'https://ai.google.dev/gemini-api/terms',
  openai: 'https://openai.com/policies/row-terms-of-use',
};

function makeClassList() {
  return {
    values: new Set(),
    toggle(name, force) {
      if (force) {
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

function makeNode(id = '', tagName = 'div') {
  return {
    id,
    tagName,
    children: [],
    dataset: {},
    attributes: {},
    events: {},
    className: '',
    classList: makeClassList(),
    textContent: '',
    value: '',
    hidden: false,
    disabled: false,
    checked: false,
    type: '',
    name: '',
    appendChild(child) {
      this.children.push(child);
      return child;
    },
    append(...items) {
      this.children.push(...items);
    },
    setAttribute(name, value) {
      this.attributes[name] = String(value);
    },
    addEventListener(name, handler) {
      this.events[name] = handler;
    },
  };
}

function $(id) {
  if (!nodes.has(id)) nodes.set(id, makeNode(id));
  return nodes.get(id);
}

function setText(id, message) {
  $(id).textContent = message || '';
}

function setMessage(id, message, tone = '') {
  const node = $(id);
  node.textContent = message || '';
  node.tone = tone;
}

function setHidden(id, hidden) {
  const node = $(id);
  node.hidden = !!hidden;
  hiddenCalls.push({id, hidden: !!hidden});
}

function setButtonText(id, message) {
  $(id).textContent = message || '';
}

function providerLabel(provider) {
  return {anthropic: 'Claude', google: 'Gemini', openai: 'GPT'}[provider] || provider;
}

function relativeTime() {
  return 'just now';
}

function activeLaneLabel(kind) {
  return copy.active_lane_labels?.[kind] || kind || '';
}

function collectText(node) {
  if (typeof node === 'string') return node;
  return `${node.textContent || ''}${node.children.map(collectText).join('')}`;
}

function lastHidden(id) {
  return hiddenCalls.filter((call) => call.id === id).at(-1)?.hidden;
}

const providerCards = ['anthropic', 'google', 'openai'].map((provider) => ({
  dataset: {providerCard: provider},
  classList: makeClassList(),
}));

const document = {
  activeElement: null,
  createElement(tagName) {
    return makeNode('', tagName);
  },
  querySelectorAll(selector) {
    if (selector === '[data-provider-card]') return providerCards;
    if (selector === '[data-byo-key-link]') return [];
    return [];
  },
};
"""


def _node_script(body: str) -> str:
    source = STATIC.read_text(encoding="utf-8")
    parts = [
        "const googleModelResolutionTargetsField = 'google_model_resolution_targets';",
        extract_js_function(source, "formatCopy"),
        extract_js_function(source, "byoReasonCopy"),
        extract_js_function(source, "byoModelStepAllowed"),
        extract_js_function(source, "byoEntryMode"),
        extract_js_function(source, "byoKeyInputEmpty"),
        extract_js_function(source, "byoTierList"),
        extract_js_function(source, "preselectByoModel"),
        extract_js_function(source, "byoTierRows"),
        extract_js_function(source, "byoModelLabel"),
        extract_js_function(source, "byoCustomText"),
        extract_js_function(source, "byoCustomShowsChecked"),
        extract_js_function(source, "byoSaveDisabled"),
        extract_js_function(source, "byoCustomInputDraft"),
        extract_js_function(source, "runByoKeyCheckFlow"),
        extract_js_function(source, "runByoModelSaveFlow"),
        extract_js_function(source, "runByoCustomProbeFlow"),
        extract_js_function(source, "selectedByoProvider"),
        extract_js_function(source, "clearByoModelResolutionTargets"),
        extract_js_function(source, "changeByoProvider"),
        "function assert(condition, message) { if (!condition) throw new Error(message); }",
        f"const copy = {json.dumps(thinking_copy.thinking_copy_payload())};",
        "const text = copy.byo_setup;",
        body,
    ]
    return "\n".join(parts)


def _extract_js_function_exact(source: str, function_name: str) -> str:
    marker = f"  function {function_name}("
    start = source.index(marker) + 2
    brace = source.index(") {", start) + 2
    depth = 0
    in_string: str | None = None
    escaped = False
    for index in range(brace, len(source)):
        char = source[index]
        if in_string:
            if escaped:
                escaped = False
            elif char == "\\":
                escaped = True
            elif char == in_string:
                in_string = None
            continue
        if char in {"'", '"', "`"}:
            in_string = char
            continue
        if char == "{":
            depth += 1
        elif char == "}":
            depth -= 1
            if depth == 0:
                return source[start : index + 1]
    raise AssertionError(f"could not extract {function_name}")


def _node_render_script(body: str) -> str:
    source = STATIC.read_text(encoding="utf-8")
    parts = [
        "const googleModelResolutionTargetsField = 'google_model_resolution_targets';",
        extract_js_function(source, "formatCopy"),
        extract_js_function(source, "byoReasonCopy"),
        extract_js_function(source, "byoModelStepAllowed"),
        extract_js_function(source, "byoEntryMode"),
        extract_js_function(source, "byoTierList"),
        extract_js_function(source, "preselectByoModel"),
        extract_js_function(source, "byoTierRows"),
        extract_js_function(source, "byoModelLabel"),
        extract_js_function(source, "byoCustomText"),
        extract_js_function(source, "byoCustomShowsChecked"),
        extract_js_function(source, "byoSaveDisabled"),
        extract_js_function(source, "resetByoDraft"),
        extract_js_function(source, "setSelectedByoProvider"),
        extract_js_function(source, "setByoModelResolutionTargets"),
        extract_js_function(source, "clearByoModelResolutionTargets"),
        extract_js_function(source, "hasConfidentialPriorResolutionTarget"),
        extract_js_function(source, "restoreOnlyModelResolutionActive"),
        extract_js_function(source, "selectedByoProvider"),
        extract_js_function(source, "changeByoProvider"),
        extract_js_function(source, "openGoogleModelResolutionGuidance"),
        extract_js_function(source, "renderConfigurationGuidance"),
        extract_js_function(source, "renderByoModelPanel"),
        _extract_js_function_exact(source, "renderByo"),
        extract_js_function(source, "bindOpenView"),
        _extract_js_function_exact(source, "bind"),
        "function assert(condition, message) { if (!condition) throw new Error(message); }",
        f"const copy = {json.dumps(thinking_copy.thinking_copy_payload())};",
        "const text = copy.byo_setup;",
        RENDER_DOM_STUB,
        body,
    ]
    return "\n".join(parts)


def _node_api_script(body: str) -> str:
    source = STATIC.read_text(encoding="utf-8")
    parts = [
        "const googleModelResolutionTargetsField = 'google_model_resolution_targets';",
        extract_js_function(source, "formatCopy"),
        extract_js_function(source, "byoReasonCopy"),
        extract_js_function(source, "api"),
        extract_js_function(source, "runByoModelSaveFlow"),
        extract_js_function(source, "runByoCustomProbeFlow"),
        "function assert(condition, message) { if (!condition) throw new Error(message); }",
        f"const copy = {json.dumps(thinking_copy.thinking_copy_payload())};",
        "const text = copy.byo_setup;",
        body,
    ]
    return "\n".join(parts)


def _run_node(script: str) -> None:
    node = shutil.which("node")
    if node is None:
        pytest.skip("node is not available")
    result = subprocess.run(
        [node, "-e", script],
        check=True,
        text=True,
        capture_output=True,
    )
    assert "PASS" in result.stdout


def test_byo_gating_reason_mapping_and_empty_guard() -> None:
    _run_node(
        _node_script(
            """
async function main() {
  assert(byoModelStepAllowed('anthropic', {valid: true}, false) === true, 'valid non-scout should allow model step');
  assert(byoModelStepAllowed('google', {valid: true}, true) === false, 'google scout should hide model step');
  assert(byoModelStepAllowed('google', {valid: true}, false) === true, 'google without scout should allow model step');
  assert(byoKeyInputEmpty('   ') === true, 'blank input should be empty');
  assert(byoKeyInputEmpty('sk-live') === false, 'nonblank input should not be empty');

  const unknownCodes = [
    'context_window_exceeded',
    'max_turns_exhausted',
    'provider_response_invalid',
    'provider_unavailable',
    'unknown',
    'made_up_code',
  ];
  for (const code of unknownCodes) {
    assert(
      byoReasonCopy(code, 'key', text, 'Claude') === "Claude couldn't be checked",
      `unknown reason ${code}`,
    );
  }
  assert(
    byoReasonCopy('provider_key_invalid', 'key', text, 'Claude') === "Claude didn't accept it",
    'invalid key reason',
  );
  assert(
    byoReasonCopy('provider_quota_exceeded', 'key', text, 'Claude') === "Claude says it's out of quota right now",
    'quota reason',
  );
  assert(
    byoReasonCopy('network_unreachable', 'key', text, 'Claude') === "couldn't reach Claude — check your connection",
    'network reason',
  );
  assert(
    byoReasonCopy('chat_timeout', 'key', text, 'Claude') === "couldn't reach Claude — check your connection",
    'timeout reason',
  );

  const calls = [];
  const statuses = [];
  const result = await runByoKeyCheckFlow({
    apiFn: async (path, options) => {
      calls.push({path, options});
      throw new Error('should not call api for empty input');
    },
    applyKeys: () => {},
    provider: 'anthropic',
    providerName: 'Claude',
    envVar: 'ANTHROPIC_API_KEY',
    value: '   ',
    text,
    providersPayload: {},
    scoutEnabled: false,
    setMode: () => {},
    selectModel: () => {},
    resetDraft: () => {},
    renderFn: () => {},
    showStatus: (message, tone) => statuses.push({message, tone}),
  });

  assert(result.status === 'empty', 'empty input should return empty status');
  assert(calls.length === 0, 'empty input should not write keys');
  assert(statuses.length === 1, 'empty input should render one status');
  console.log('PASS');
}
main().catch((error) => { console.error(error.stack || error); process.exit(1); });
"""
        )
    )


def test_byo_key_check_failure_and_success_branches() -> None:
    _run_node(
        _node_script(
            """
async function main() {
  const providersPayload = {
    model_tiers: {
      anthropic: [
        {tier: 'top', label: 'Injected Top', model: 'injected-top'},
        {tier: 'mid', label: 'Injected Mid', model: 'injected-mid'},
        {tier: 'lite', label: 'Injected Lite', model: 'injected-lite'},
      ],
      google: [
        {tier: 'top', label: 'Injected Google Top', model: 'g-top'},
        {tier: 'mid', label: 'Injected Google Mid', model: 'g-mid'},
        {tier: 'lite', label: 'Injected Google Lite', model: 'g-lite'},
      ],
    },
    byo_models: {},
    active: {provider: 'openai', model: 'not-anthropic'},
  };
  let mode = '';
  let selected = '';
  let resets = 0;
  let renders = 0;
  const statuses = [];
  const failedCalls = [];
  const failed = await runByoKeyCheckFlow({
    apiFn: async (path, options) => {
      failedCalls.push({path, options});
      return {valid: false, reason_code: 'provider_key_invalid'};
    },
    applyKeys: () => {},
    provider: 'anthropic',
    providerName: 'Claude',
    envVar: 'ANTHROPIC_API_KEY',
    value: 'bad-key',
    text,
    providersPayload,
    scoutEnabled: false,
    setMode: (next) => { mode = next; },
    selectModel: (model) => { selected = model; },
    resetDraft: () => { resets += 1; },
    renderFn: () => { renders += 1; },
    showStatus: (message, tone) => statuses.push({message, tone}),
  });

  assert(failed.status === 'invalid', 'invalid key should fail');
  assert(
    JSON.stringify(failedCalls.map((call) => call.path)) === JSON.stringify(['api/keys/check']),
    'invalid key should only check keys',
  );
  assert(!failedCalls.some((call) => call.path === 'api/providers'), 'invalid key should not write providers');
  assert(!failedCalls.some((call) => call.path === 'api/keys'), 'invalid check should not store keys');
  assert(mode === 'paste', 'invalid key should stay on paste');
  assert(selected === '', 'invalid key should not select a model');
  assert(resets === 1 && renders === 1, 'invalid key should reset and render');
  assert(statuses.at(-1).message.includes("Claude didn't accept it"), 'invalid key should map reason copy');
  assert(!statuses.at(-1).message.includes('provider_key_invalid'), 'raw reason code should not render');

  mode = '';
  selected = '';
  const validCalls = [];
  const valid = await runByoKeyCheckFlow({
    apiFn: async (path, options) => {
      validCalls.push({path, options});
      if (path === 'api/keys/check') return {valid: true, provider: 'anthropic'};
      if (path === 'api/keys') {
        return {
          key_validation: {anthropic: {valid: true, timestamp: '2026-07-13T12:00:00Z'}},
        };
      }
      throw new Error(`unexpected path ${path}`);
    },
    applyKeys: () => {},
    provider: 'anthropic',
    providerName: 'Claude',
    envVar: 'ANTHROPIC_API_KEY',
    value: 'good-key',
    text,
    providersPayload,
    scoutEnabled: false,
    setMode: (next) => { mode = next; },
    selectModel: (model) => { selected = model; },
    resetDraft: () => {},
    renderFn: () => {},
    showStatus: () => {},
  });

  assert(valid.status === 'model', 'valid non-scout key should open model step');
  assert(
    JSON.stringify(validCalls.map((call) => call.path)) === JSON.stringify(['api/keys/check', 'api/keys']),
    'valid key should check then store keys',
  );
  assert(mode === 'model', 'valid key should set model mode');
  assert(selected === 'injected-lite', 'valid key should preselect injected lite tier');

  mode = '';
  selected = '';
  const scout = await runByoKeyCheckFlow({
    apiFn: async (path) => {
      if (path === 'api/keys/check') return {valid: true, provider: 'google'};
      if (path === 'api/keys') {
        return {
          key_validation: {google: {valid: true, timestamp: '2026-07-13T12:00:00Z'}},
        };
      }
      throw new Error(`unexpected path ${path}`);
    },
    applyKeys: () => {},
    provider: 'google',
    providerName: 'Gemini',
    envVar: 'GOOGLE_API_KEY',
    value: 'google-key',
    text,
    providersPayload,
    scoutEnabled: true,
    setMode: (next) => { mode = next; },
    selectModel: (model) => { selected = model; },
    resetDraft: () => {},
    renderFn: () => {},
    showStatus: () => {},
  });

  assert(scout.status === 'checked', 'valid scout google key should not open model step');
  assert(mode === 'paste', 'scout google should stay on paste');
  assert(selected === '', 'scout google should not select a model');

  const disagreementCalls = [];
  statuses.length = 0;
  mode = '';
  const disagreement = await runByoKeyCheckFlow({
    apiFn: async (path, options) => {
      disagreementCalls.push({path, options});
      if (path === 'api/keys/check') return {valid: true, provider: 'anthropic'};
      if (path === 'api/keys') {
        return {
          key_validation: {anthropic: {valid: false, reason_code: 'provider_key_invalid'}},
        };
      }
      throw new Error(`unexpected path ${path}`);
    },
    applyKeys: () => {},
    provider: 'anthropic',
    providerName: 'Claude',
    envVar: 'ANTHROPIC_API_KEY',
    value: 'later-rejected-key',
    text,
    providersPayload,
    scoutEnabled: false,
    setMode: (next) => { mode = next; },
    selectModel: (model) => { selected = model; },
    resetDraft: () => {},
    renderFn: () => {},
    showStatus: (message, tone) => statuses.push({message, tone}),
  });

  assert(disagreement.status === 'invalid', 'PUT invalid record should fail');
  assert(
    JSON.stringify(disagreementCalls.map((call) => call.path)) === JSON.stringify(['api/keys/check', 'api/keys']),
    'PUT disagreement should still check then store',
  );
  assert(mode === 'paste', 'PUT invalid record should stay on paste');
  assert(statuses.at(-1).message.includes("Claude didn't accept it"), 'PUT invalid record should render key failure');

  const throwingCalls = [];
  let thrown = '';
  mode = 'paste';
  try {
    await runByoKeyCheckFlow({
      apiFn: async (path, options) => {
        throwingCalls.push({path, options});
        if (path === 'api/keys/check') return {valid: true, provider: 'anthropic'};
        throw new Error('write failed');
      },
      applyKeys: () => {},
      provider: 'anthropic',
      providerName: 'Claude',
      envVar: 'ANTHROPIC_API_KEY',
      value: 'write-fails-key',
      text,
      providersPayload,
      scoutEnabled: false,
      setMode: (next) => { mode = next; },
      selectModel: (model) => { selected = model; },
      resetDraft: () => {},
      renderFn: () => {},
      showStatus: (message, tone) => statuses.push({message, tone}),
    });
  } catch (error) {
    thrown = error.message;
  }

  assert(thrown === 'write failed', 'PUT throw should propagate honestly');
  assert(
    JSON.stringify(throwingCalls.map((call) => call.path)) === JSON.stringify(['api/keys/check', 'api/keys']),
    'PUT throw should happen after a valid check',
  );
  assert(mode === 'paste', 'PUT throw should not open model step');
  console.log('PASS');
}
main().catch((error) => { console.error(error.stack || error); process.exit(1); });
"""
        )
    )


def test_byo_preselection_and_payload_driven_tiers() -> None:
    _run_node(
        _node_script(
            """
const injected = {
  byo_models: {anthropic: 'remembered-model'},
  active: {provider: 'anthropic', model: 'active-model'},
  model_tiers: {
    anthropic: [
      {tier: 'lite', label: 'Injected Lite Label', model: 'payload-lite-id'},
      {tier: 'top', label: 'Injected Top Label', model: 'payload-top-id'},
      {tier: 'mid', label: 'Injected Mid Label', model: 'payload-mid-id'},
    ],
  },
};

assert(preselectByoModel('anthropic', injected) === 'remembered-model', 'remembered model wins');
delete injected.byo_models.anthropic;
assert(preselectByoModel('anthropic', injected) === 'active-model', 'matching active model wins second');
injected.active.provider = 'google';
assert(preselectByoModel('anthropic', injected) === 'payload-lite-id', 'lite tier wins last');

const rows = byoTierRows('anthropic', injected, 'payload-mid-id', text);
assert(
  JSON.stringify(rows.map((row) => row.tier)) === JSON.stringify(['top', 'mid', 'lite']),
  'tiers should render top mid lite',
);
assert(
  JSON.stringify(rows.map((row) => row.label)) === JSON.stringify([
    'Injected Top Label',
    'Injected Mid Label',
    'Injected Lite Label',
  ]),
  'tiers should use injected labels',
);
assert(
  JSON.stringify(rows.map((row) => row.model)) === JSON.stringify([
    'payload-top-id',
    'payload-mid-id',
    'payload-lite-id',
  ]),
  'tiers should use injected ids',
);
assert(rows[1].tag === text.tier_tag_current, 'active model should be current');
assert(rows[2].tag === text.tier_tag_suggested, 'lite model should be suggested');
assert(byoModelLabel('anthropic', 'payload-top-id', injected) === 'Injected Top Label', 'known model label');
assert(byoModelLabel('anthropic', 'custom-id', injected) === 'custom-id', 'custom id should stay raw');
console.log('PASS');
"""
        )
    )


def test_byo_model_panel_renders_payload_tier_cards() -> None:
    _run_node(
        _node_render_script(
            """
const state = {
  selectedByoProvider: 'anthropic',
  byoSelectedModel: '',
  byoCustomOpen: false,
  byoCustomModel: '',
  byoCustomCheckedModel: '',
  providers: {
    active: {provider: 'anthropic', model: 'injected-mid-id'},
    model_tiers: {
      anthropic: [
        {tier: 'lite', label: 'Injected Lite', model: 'injected-lite-id'},
        {tier: 'top', label: 'Injected Top', model: 'injected-top-id'},
        {tier: 'mid', label: 'Injected Mid', model: 'injected-mid-id'},
      ],
    },
  },
};

renderByoModelPanel('anthropic', {valid: true, timestamp: '2026-07-13T12:00:00Z'}, text);

const cards = $('byoModelGrid').children;
assert(cards.length === 3, 'three tier cards should render');
assert(
  JSON.stringify(cards.map((card) => collectText(card).includes('Injected Top'))) === JSON.stringify([true, false, false]),
  'top card should render injected label first',
);
assert(
  JSON.stringify(cards.map((card) => collectText(card).includes('Injected Mid'))) === JSON.stringify([false, true, false]),
  'mid card should render injected label second',
);
assert(
  JSON.stringify(cards.map((card) => collectText(card).includes('Injected Lite'))) === JSON.stringify([false, false, true]),
  'lite card should render injected label third',
);
assert(
  JSON.stringify(cards.map((card) => card.children[0].children[0].value)) === JSON.stringify([
    'injected-top-id',
    'injected-mid-id',
    'injected-lite-id',
  ]),
  'cards should carry injected model ids',
);
assert(cards.every((card) => collectText(card).includes(card.children[0].children[0].value)), 'each injected id should render as text');
assert(cards.every((card) => card.children[0].children[0].type === 'radio'), 'each card should contain a radio input');
assert(new Set(cards.map((card) => card.children[0].children[0].name)).size === 1, 'radio inputs should share one name');
assert(cards.every((card) => card.children[0].children[0].name === 'byoModelChoice'), 'radio input name should match BYO model group');
assert(collectText(cards[1]).includes(text.tier_tag_current), 'active injected tier should show current tag');
assert(collectText(cards[2]).includes(text.tier_tag_suggested), 'lite injected tier should show suggested tag');
console.log('PASS');
"""
        )
    )


def test_byo_model_panel_renders_configuration_guidance() -> None:
    _run_node(
        _node_render_script(
            """
const state = {
  selectedByoProvider: 'google',
  byoSelectedModel: '',
  byoCustomOpen: false,
  byoCustomModel: '',
  byoCustomCheckedModel: '',
  providers: {
    active: {provider: 'google', model: 'gemini-3.5-flash'},
    configuration_guidance: {
      id: 'choose_exact_gemini_model',
      heading: 'choose an exact Gemini model',
      google_model_resolution_targets: ['confidential_prior'],
      action: {label: 'choose model', href: '/app/thinking/#byo-setup'},
    },
    model_tiers: {
      google: [
        {tier: 'mid', label: 'Gemini 3.5 Flash', model: 'gemini-3.5-flash'},
        {tier: 'lite', label: 'Gemini 3.1 Flash Lite', model: 'gemini-3.1-flash-lite'},
      ],
    },
  },
};

renderByoModelPanel('google', {valid: true, timestamp: '2026-07-13T12:00:00Z'}, text);

const notice = $('byoConfigurationGuidance');
assert(notice.hidden === false, 'configuration guidance should be shown');
assert(collectText(notice).includes('choose an exact Gemini model'), 'heading should render');
assert(collectText(notice).includes('choose model'), 'action should render');
assert(notice.children[2].href === '/app/thinking/#byo-setup', 'action href should target BYO setup view');
console.log('PASS');
"""
        )
    )


def test_byo_configuration_guidance_click_opens_model_step_for_reported_slots() -> None:
    _run_node(
        _node_render_script(
            """
let state;
const shownViews = [];
function showView(name) {
  shownViews.push(name);
}

function runCase(target, providerState) {
  nodes.clear();
  hiddenCalls.length = 0;
  shownViews.length = 0;
  let prevented = 0;
  state = {
    selectedByoProvider: '',
    byoMode: 'pick',
    byoSelectedModel: '',
    byoCustomOpen: false,
    byoCustomModel: '',
    byoCustomCheckedModel: '',
    byoModelResolutionTargets: [],
    providers: {
      ...providerState,
      active_lane: {lane: 'confidential'},
      scout_enabled: false,
      configuration_guidance: {
        id: 'choose_exact_gemini_model',
        heading: 'choose an exact Gemini model',
        google_model_resolution_targets: [target],
        action: {label: 'choose model', href: '/app/thinking/#byo-setup'},
      },
      model_tiers: {
        google: [
          {tier: 'mid', label: 'Gemini 3.5 Flash', model: 'gemini-3.5-flash'},
          {tier: 'lite', label: 'Gemini 3.1 Flash Lite', model: 'gemini-3.1-flash-lite'},
        ],
      },
    },
    keys: {
      api_keys: {google: true},
      key_validation: {google: {valid: true, timestamp: '2026-07-13T12:00:00Z'}},
    },
  };

  renderConfigurationGuidance();
  const link = $('byoConfigurationGuidance').children[2];
  link.events.click({preventDefault: () => { prevented += 1; }});

  assert(prevented === 1, `${target} click should prevent default`);
  assert(JSON.stringify(shownViews) === JSON.stringify(['byo-setup']), `${target} should open BYO setup`);
  assert(!shownViews.includes('lane-switch'), `${target} should not show lane-switch`);
  assert(state.selectedByoProvider === 'google', `${target} should select Google`);
  assert(state.byoMode === 'model', `${target} should enter model mode`);
  assert(lastHidden('byoModelPanel') === false, `${target} should show model panel`);
  assert(JSON.stringify(state.byoModelResolutionTargets) === JSON.stringify([target]), `${target} should keep targets`);
}

runCase('active', {active: {provider: 'google', model: 'gemini-pro-latest'}, byo_models: {}});
runCase('remembered', {active: {provider: 'local', model: 'local/qwen3.5-4b'}, byo_models: {google: 'gemini-pro-latest'}});
runCase('confidential_prior', {active: {provider: 'local', model: 'local/qwen3.5-4b'}, byo_models: {}});
console.log('PASS');
"""
        )
    )


def test_byo_configuration_guidance_click_falls_back_to_key_step() -> None:
    _run_node(
        _node_render_script(
            """
let state;
const shownViews = [];
function showView(name) {
  shownViews.push(name);
}

function runCase(label, keys, scoutEnabled) {
  nodes.clear();
  hiddenCalls.length = 0;
  shownViews.length = 0;
  state = {
    selectedByoProvider: '',
    byoMode: 'pick',
    byoSelectedModel: '',
    byoCustomOpen: false,
    byoCustomModel: '',
    byoCustomCheckedModel: '',
    byoModelResolutionTargets: [],
    providers: {
      active: {provider: 'local', model: 'local/qwen3.5-4b'},
      active_lane: {lane: 'confidential'},
      scout_enabled: scoutEnabled,
      configuration_guidance: {
        id: 'choose_exact_gemini_model',
        heading: 'choose an exact Gemini model',
        google_model_resolution_targets: ['confidential_prior'],
        action: {label: 'choose model', href: '/app/thinking/#byo-setup'},
      },
      model_tiers: {
        google: [{tier: 'lite', label: 'Gemini Lite', model: 'gemini-3.1-flash-lite'}],
      },
    },
    keys,
  };

  renderConfigurationGuidance();
  $('byoConfigurationGuidance').children[2].events.click({preventDefault: () => {}});

  assert(JSON.stringify(shownViews) === JSON.stringify(['byo-setup']), `${label} should open BYO setup`);
  assert(!shownViews.includes('lane-switch'), `${label} should not show lane-switch`);
  assert(state.selectedByoProvider === 'google', `${label} should select Google`);
  assert(state.byoMode === 'paste', `${label} should enter paste mode`);
  assert(lastHidden('byoModelPanel') === true, `${label} should hide model panel`);
  assert(lastHidden('byoPastePanel') === false, `${label} should show paste panel`);
  assert(JSON.stringify(state.byoModelResolutionTargets) === JSON.stringify(['confidential_prior']), `${label} should keep targets`);
}

runCase('missing key', {api_keys: {}, key_validation: {}}, false);
runCase(
  'scout google',
  {api_keys: {google: true}, key_validation: {google: {valid: true, timestamp: '2026-07-13T12:00:00Z'}}},
  true,
);
console.log('PASS');
"""
        )
    )


def test_byo_render_shows_model_panel_only_for_valid_non_scout_key() -> None:
    _run_node(
        _node_render_script(
            """
const state = {
  selectedByoProvider: 'anthropic',
  byoMode: 'model',
  byoSelectedModel: '',
  byoCustomOpen: false,
  byoCustomModel: '',
  byoCustomCheckedModel: '',
  providers: {
    scout_enabled: false,
    active: {provider: 'anthropic', model: 'injected-mid-id'},
    model_tiers: {
      anthropic: [
        {tier: 'top', label: 'Injected Top', model: 'injected-top-id'},
        {tier: 'mid', label: 'Injected Mid', model: 'injected-mid-id'},
        {tier: 'lite', label: 'Injected Lite', model: 'injected-lite-id'},
      ],
      google: [
        {tier: 'top', label: 'Injected Google Top', model: 'google-top-id'},
        {tier: 'mid', label: 'Injected Google Mid', model: 'google-mid-id'},
        {tier: 'lite', label: 'Injected Google Lite', model: 'google-lite-id'},
      ],
    },
  },
  keys: {
    api_keys: {anthropic: true, google: true},
    key_validation: {
      anthropic: {valid: true, timestamp: '2026-07-13T12:00:00Z'},
      google: {valid: true, timestamp: '2026-07-13T12:00:00Z'},
    },
  },
};

renderByo();

assert(lastHidden('byoModelPanel') === false, 'valid non-scout key should show model panel');
assert(lastHidden('byoPastePanel') === true, 'valid non-scout key should hide paste panel');
assert($('byoModelGrid').children.length === 3, 'shown model panel should render tier cards');

hiddenCalls.length = 0;
nodes.get('byoModelGrid').children = [];
state.selectedByoProvider = 'google';
state.byoMode = 'model';
state.byoSelectedModel = '';
state.byoCustomOpen = false;
state.byoCustomModel = '';
state.byoCustomCheckedModel = '';
state.providers.scout_enabled = true;

renderByo();

assert(lastHidden('byoModelPanel') === true, 'scout google should not show model panel');
assert(lastHidden('byoPastePanel') === false, 'scout google should fall back to paste panel');
assert(state.byoMode === 'paste', 'scout google should reset model mode to paste');
assert(state.byoSelectedModel === '', 'scout google fallback should clear selected model');
console.log('PASS');
"""
        )
    )


def test_byo_custom_probe_flow() -> None:
    _run_node(
        _node_script(
            """
async function main() {
  let mode = 'model';
  let selected = '';
  let checked = '';
  let renders = 0;
  const messages = [];

  const pass = await runByoCustomProbeFlow({
    apiFn: async (path, options) => ({valid: true, provider: 'anthropic', model: 'custom-pass'}),
    provider: 'anthropic',
    providerName: 'Claude',
    model: 'custom-pass',
    text,
    setMode: (next) => { mode = next; },
    selectModel: (model) => { selected = model; },
    markChecked: (model) => { checked = model; },
    renderFn: () => { renders += 1; },
    showStatus: (message, tone) => messages.push({message, tone}),
  });

  assert(pass.status === 'valid', 'custom pass status');
  assert(selected === 'custom-pass' && checked === 'custom-pass', 'custom pass should select and mark checked');
  assert(renders === 1, 'custom pass should render');
  assert(messages.at(-1).message === '✓ custom-pass answered — you can use it', 'custom pass copy');

  messages.length = 0;
  const missing = await runByoCustomProbeFlow({
    apiFn: async () => ({valid: false, reason_code: 'model_not_found'}),
    provider: 'anthropic',
    providerName: 'Claude',
    model: 'missing-model',
    text,
    setMode: (next) => { mode = next; },
    selectModel: (model) => { selected = model; },
    markChecked: (model) => { checked = model; },
    renderFn: () => { renders += 1; },
    showStatus: (message, tone) => messages.push({message, tone}),
  });

  assert(missing.status === 'invalid', 'custom missing status');
  assert(messages.at(-1).message === 'Claude doesn\\'t offer "missing-model" to this key.', 'custom not found copy');
  assert(!messages.at(-1).message.includes('model_not_found'), 'raw missing code should not render');

  messages.length = 0;
  mode = 'model';
  const keyMissing = await runByoCustomProbeFlow({
    apiFn: async () => ({valid: false, reason_code: 'key_missing'}),
    provider: 'anthropic',
    providerName: 'Claude',
    model: 'custom-pass',
    text,
    setMode: (next) => { mode = next; },
    selectModel: (model) => { selected = model; },
    markChecked: (model) => { checked = model; },
    renderFn: () => { renders += 1; },
    showStatus: (message, tone) => messages.push({message, tone}),
  });

  assert(keyMissing.status === 'key_missing', 'custom key missing status');
  assert(mode === 'paste', 'custom key missing should return to paste');
  assert(messages.at(-1).message.includes("Claude couldn't be checked"), 'custom key missing should map to unknown reason');
  assert(!messages.at(-1).message.includes('key_missing'), 'raw key missing code should not render');
  console.log('PASS');
}
main().catch((error) => { console.error(error.stack || error); process.exit(1); });
"""
        )
    )


def test_byo_probe_flows_use_real_api_wrapper_without_error_throw() -> None:
    _run_node(
        _node_api_script(
            """
async function main() {
  const payloads = [];
  const calls = [];
  globalThis.fetch = async (path, options = {}) => {
    calls.push({path, body: options.body ? JSON.parse(options.body) : null});
    const payload = payloads.shift();
    if (!payload) throw new Error(`no payload for ${path}`);
    return {
      ok: true,
      async json() {
        return payload;
      },
    };
  };

  let mode = 'model';
  let selected = '';
  let checked = '';
  let renders = 0;
  const messages = [];

  payloads.push({valid: false, reason_code: 'model_not_found', message: 'raw vendor missing'});
  const missing = await runByoCustomProbeFlow({
    apiFn: api,
    provider: 'anthropic',
    providerName: 'Claude',
    model: 'missing-real',
    text,
    setMode: (next) => { mode = next; },
    selectModel: (model) => { selected = model; },
    markChecked: (model) => { checked = model; },
    renderFn: () => { renders += 1; },
    showStatus: (message, tone) => messages.push({message, tone}),
  });

  assert(missing.status === 'invalid', 'custom missing should return invalid');
  assert(
    messages.at(-1).message === formatCopy(text.custom_not_found, {provider: 'Claude', model: 'missing-real'}),
    'custom not found copy should render',
  );
  assert(!messages.at(-1).message.includes('raw vendor missing'), 'custom missing should not render raw vendor text');

  messages.length = 0;
  payloads.push({valid: false, reason_code: 'provider_key_invalid', message: 'raw auth failure'});
  const authFail = await runByoModelSaveFlow({
    apiFn: api,
    applyProviders: () => { throw new Error('should not save providers'); },
    provider: 'anthropic',
    providerName: 'Claude',
    model: 'auth-model',
    text,
    setMode: (next) => { mode = next; },
    renderFn: () => { renders += 1; },
    showStatus: (message, tone) => messages.push({message, tone}),
  });

  assert(authFail.status === 'probe_failed', 'auth failure should be a probe failure');
  const rejectedReason = formatCopy(text.reason_rejected, {provider: 'Claude', model: 'auth-model'});
  assert(
    messages.at(-1).message === formatCopy(
      text.probe_failed_save,
      {provider: 'Claude', model: 'auth-model', reason: rejectedReason},
    ),
    'auth failure should use save probe copy plus rejected reason',
  );
  assert(!messages.at(-1).message.includes('raw auth failure'), 'auth failure should not render raw vendor text');

  messages.length = 0;
  mode = 'model';
  payloads.push({valid: false, reason_code: 'key_missing', message: 'No stored API key for provider.'});
  const keyMissing = await runByoModelSaveFlow({
    apiFn: api,
    applyProviders: () => { throw new Error('should not save providers'); },
    provider: 'anthropic',
    providerName: 'Claude',
    model: 'key-missing-model',
    text,
    setMode: (next) => { mode = next; },
    renderFn: () => { renders += 1; },
    showStatus: (message, tone) => messages.push({message, tone}),
  });

  assert(keyMissing.status === 'key_missing', 'key missing should return key_missing');
  assert(mode === 'paste', 'key missing should return to paste');
  const keyMissingReason = formatCopy(text.reason_unknown, {provider: 'Claude', model: 'key-missing-model'});
  assert(
    messages.at(-1).message === formatCopy(text.key_failed, {provider: 'Claude', reason: keyMissingReason}),
    'key missing should map to key failure copy',
  );
  assert(!messages.at(-1).message.includes('your key works'), 'key missing should not use model probe premise');
  assert(!calls.some((call) => call.path === 'api/providers'), 'failed probes should not write providers');
  console.log('PASS');
}
main().catch((error) => { console.error(error.stack || error); process.exit(1); });
"""
        )
    )


def test_byo_custom_preselection_requires_probe() -> None:
    _run_node(
        _node_script(
            """
const selected = 'remembered-custom-id';
const customText = byoCustomText(selected, true, '');

assert(customText === selected, 'custom preselection should hydrate the custom text');
assert(byoCustomShowsChecked(customText, '') === false, 'custom preselection should not show checked without a probe');
assert(byoSaveDisabled(selected, true, '') === true, 'custom preselection should keep save disabled until probe');
assert(byoSaveDisabled(selected, true, selected) === false, 'matching probe should enable save');
console.log('PASS');
"""
        )
    )


def test_byo_custom_editing_clears_probe_state() -> None:
    _run_node(
        _node_script(
            """
const selected = 'custom-pass';

assert(byoCustomShowsChecked('custom-pass', 'custom-pass') === true, 'matching probe should show checked');
assert(byoSaveDisabled(selected, true, 'custom-pass') === false, 'matching probe should enable save');

const cleared = byoCustomInputDraft('');
assert(cleared.customModel === '', 'cleared draft should clear custom text');
assert(cleared.checkedModel === '', 'cleared draft should clear probe flag');
assert(cleared.selectedModel === '', 'cleared draft should clear selected model');
assert(byoCustomText(cleared.selectedModel, true, cleared.customModel) === '', 'cleared draft should not rehydrate old text');
assert(byoCustomShowsChecked(cleared.customModel, cleared.checkedModel) === false, 'cleared draft should hide checked');
assert(byoSaveDisabled(cleared.selectedModel, true, cleared.checkedModel) === true, 'cleared draft should disable save');

const changed = byoCustomInputDraft('different-id');
assert(changed.customModel === 'different-id', 'changed draft should carry text');
assert(changed.checkedModel === '', 'changed draft should clear probe flag');
assert(changed.selectedModel === 'different-id', 'changed draft should select new candidate');
assert(byoCustomShowsChecked(changed.customModel, changed.checkedModel) === false, 'changed draft should hide checked');
assert(byoSaveDisabled(changed.selectedModel, true, changed.checkedModel) === true, 'changed draft should disable save until reprobe');
console.log('PASS');
"""
        )
    )


def test_byo_provider_change_resets_draft_state() -> None:
    _run_node(
        _node_script(
            """
const state = {
  selectedByoProvider: 'anthropic',
  byoSelectedModel: 'old-model',
  byoCustomOpen: true,
  byoCustomModel: 'old-custom',
  byoCustomCheckedModel: 'old-custom',
  byoMode: 'model',
  providers: {
    scout_enabled: false,
    byo_models: {openai: 'remembered-gpt'},
    active: {provider: 'anthropic', model: 'old-active'},
    model_tiers: {
      openai: [
        {tier: 'lite', label: 'Lite GPT', model: 'lite-gpt'},
      ],
    },
  },
  keys: {
    key_validation: {
      openai: {valid: true, timestamp: '2026-07-13T12:00:00Z'},
    },
  },
};
const nodes = {
  byoProvider: {value: 'anthropic'},
  byoKeyInput: {value: 'secret-key'},
  byoCustomModel: {value: 'old-custom'},
};
let renderByoCalls = 0;
let renderMainLanesCalls = 0;

function $(id) {
  return nodes[id] || null;
}
function defaultByoProvider() {
  return 'anthropic';
}
function setSelectedByoProvider(provider) {
  state.selectedByoProvider = provider || defaultByoProvider();
  const select = $('byoProvider');
  if (select) select.value = state.selectedByoProvider;
}
function resetByoDraft() {
  state.byoSelectedModel = '';
  state.byoCustomOpen = false;
  state.byoCustomModel = '';
  state.byoCustomCheckedModel = '';
}
function renderByo() {
  renderByoCalls += 1;
}
function renderMainLanes() {
  renderMainLanesCalls += 1;
}

changeByoProvider('openai');

assert(state.selectedByoProvider === 'openai', 'provider should update');
assert(nodes.byoProvider.value === 'openai', 'hidden select should update');
assert(state.byoSelectedModel !== 'old-model', 'selected model should not carry over');
assert(state.byoCustomOpen === false, 'custom row state should reset');
assert(state.byoCustomModel === '', 'custom text should reset');
assert(state.byoCustomCheckedModel === '', 'probe flag should reset');
assert(state.byoMode === 'model', 'cached-valid provider change should land on model mode');
assert(state.byoSelectedModel === 'remembered-gpt', 'provider change should preselect remembered model after reset');
assert(nodes.byoKeyInput.value === '', 'key input should clear');
assert(nodes.byoCustomModel.value === '', 'custom input should clear');
assert(renderByoCalls === 1, 'provider change should render byo');
assert(renderMainLanesCalls === 1, 'provider change should render main lanes');

state.byoSelectedModel = 'old-model';
state.byoCustomOpen = true;
state.byoCustomModel = 'old-custom';
state.byoCustomCheckedModel = 'old-custom';
nodes.byoKeyInput.value = 'secret-key';
nodes.byoCustomModel.value = 'old-custom';
state.keys.key_validation.openai = {valid: false, reason_code: 'provider_key_invalid'};
changeByoProvider('openai');

assert(state.byoSelectedModel === '', 'invalid provider change should clear selected model');
assert(state.byoCustomOpen === false, 'invalid provider change should reset custom row state');
assert(state.byoCustomModel === '', 'invalid provider change should reset custom text');
assert(state.byoCustomCheckedModel === '', 'invalid provider change should reset probe flag');
assert(state.byoMode === 'paste', 'cached-invalid provider change should land on paste mode');
assert(nodes.byoKeyInput.value === '', 'invalid provider change should clear key input');
assert(nodes.byoCustomModel.value === '', 'invalid provider change should clear custom input');
assert(renderByoCalls === 2, 'invalid provider change should render byo');
assert(renderMainLanesCalls === 2, 'invalid provider change should render main lanes');
console.log('PASS');
"""
        )
    )


def test_byo_entry_derivation_and_different_key_render_path() -> None:
    _run_node(
        _node_render_script(
            """
const window = {
  location: {hash: ''},
  history: {pushState() {}, replaceState() {}},
  addEventListener() {},
};
function defaultByoProvider() {
  return 'anthropic';
}
function renderMainLanes() {}

const state = {
  selectedByoProvider: 'anthropic',
  byoMode: 'paste',
  byoSelectedModel: 'old-model',
  byoCustomOpen: true,
  byoCustomModel: 'old-custom',
  byoCustomCheckedModel: 'old-custom',
  providers: {
    scout_enabled: false,
    byo_models: {google: 'remembered-google'},
    active: {provider: 'google', model: 'active-google'},
    model_tiers: {
      google: [
        {tier: 'top', label: 'Remembered Google', model: 'remembered-google'},
        {tier: 'mid', label: 'Active Google', model: 'active-google'},
        {tier: 'lite', label: 'Lite Google', model: 'google-lite'},
      ],
    },
  },
  keys: {
    api_keys: {google: true},
    key_validation: {
      google: {valid: true, timestamp: '2026-07-13T12:00:00Z'},
    },
  },
};

changeByoProvider('google');

assert(state.byoMode === 'model', 'cached-valid google should land on model with scout off');
assert(state.byoSelectedModel === 'remembered-google', 'remembered model should win preselection');
assert(state.byoCustomOpen === false, 'custom open state should not carry over');
assert(state.byoCustomModel === '', 'custom text should not carry over');
assert(state.byoCustomCheckedModel === '', 'checked custom state should not carry over');
assert($('byoKeyInput').value === '', 'provider switch should clear key input');
assert(lastHidden('byoModelPanel') === false, 'model panel should show for cached-valid google');
assert($('byoKeyCheckstripText').textContent.includes('Gemini'), 'checked-key strip should render provider');

state.byoSelectedModel = 'old-model';
state.byoCustomOpen = true;
state.byoCustomModel = 'old-custom';
state.byoCustomCheckedModel = 'old-custom';
delete state.providers.byo_models.google;
changeByoProvider('google');
assert(state.byoSelectedModel === 'active-google', 'active-effective model should win when no remembered model exists');

state.byoSelectedModel = 'old-model';
state.providers.active = {provider: 'anthropic', model: 'other-active'};
changeByoProvider('google');
assert(state.byoSelectedModel === 'google-lite', 'lite model should win when no remembered or active model exists');

state.keys.key_validation.google = {valid: false, reason_code: 'provider_key_invalid'};
changeByoProvider('google');
assert(state.byoMode === 'paste', 'cached-invalid google should land on paste');
const invalidGoogleReason = formatCopy(text.reason_rejected, {provider: 'Gemini'});
assert(
  $('byoKeyStatus').textContent === formatCopy(text.key_failed, {provider: 'Gemini', reason: invalidGoogleReason}),
  'cached-invalid google should render mapped failure',
);

state.keys.key_validation.google = {valid: true, timestamp: '2026-07-13T12:00:00Z'};
state.providers.scout_enabled = true;
changeByoProvider('google');
assert(state.byoMode === 'paste', 'scout google should never land on model');

state.providers.scout_enabled = false;
changeByoProvider('google');
assert(state.byoMode === 'model', 'valid google should return to model before choosing a different key');
$('byoKeyInput').value = 'just-checked-key';
bind();
nodes.get('byoDifferentKey').events.click();
assert(state.byoMode === 'paste', 'different key should force paste mode');
assert($('byoKeyInput').value === '', 'different key should clear key input');
renderByo();
assert(state.byoMode === 'paste', 'background render should not upgrade paste back to model');
assert(lastHidden('byoPastePanel') === false, 'paste panel should remain visible after render');
console.log('PASS');
"""
        )
    )


def test_byo_model_save_probes_before_writing() -> None:
    _run_node(
        _node_script(
            """
async function main() {
  let providersApplied = null;
  let renders = 0;
  let mode = 'model';
  const messages = [];
  const calls = [];
  const success = await runByoModelSaveFlow({
    apiFn: async (path, options) => {
      calls.push({path, body: JSON.parse(options.body)});
      if (path === 'api/validate-model') return {valid: true};
      if (path === 'api/providers') return {active_lane: {lane: 'byo'}};
      throw new Error(`unexpected path ${path}`);
    },
    applyProviders: (providers) => { providersApplied = providers; },
    provider: 'anthropic',
    providerName: 'Claude',
    model: 'model-one',
    text,
    setMode: (next) => { mode = next; },
    renderFn: () => { renders += 1; },
    showStatus: (message, tone) => messages.push({message, tone}),
  });

  assert(success.status === 'saved', 'save success status');
  assert(JSON.stringify(calls.map((call) => call.path)) === JSON.stringify(['api/validate-model', 'api/providers']), 'save should probe then write');
  assert(
    JSON.stringify(calls[1].body) === JSON.stringify({lane: 'byo', provider: 'anthropic', model: 'model-one'}),
    'provider write body',
  );
  assert(providersApplied.active_lane.lane === 'byo', 'providers should apply');
  assert(renders === 1, 'save should render after provider write');
  assert(mode === 'model', 'save flow should not switch mode itself');

  calls.length = 0;
  messages.length = 0;
  providersApplied = null;
  renders = 0;
  const restored = await runByoModelSaveFlow({
    apiFn: async (path, options) => {
      calls.push({path, body: JSON.parse(options.body)});
      if (path === 'api/validate-model') return {valid: true};
      if (path === 'api/providers') return {active_lane: {lane: 'confidential'}};
      throw new Error(`unexpected path ${path}`);
    },
    applyProviders: (providers) => { providersApplied = providers; },
    provider: 'google',
    providerName: 'Gemini',
    model: 'gemini-3.5-pro',
    modelLabel: 'Gemini 3.5 Pro',
    googleModelResolutionTargets: ['confidential_prior'],
    restoreOnly: true,
    text,
    setMode: (next) => { mode = next; },
    renderFn: () => { renders += 1; },
    showStatus: (message, tone) => messages.push({message, tone}),
  });

  assert(restored.status === 'restored', 'restore-only success status');
  assert(JSON.stringify(calls.map((call) => call.path)) === JSON.stringify(['api/validate-model', 'api/providers']), 'restore-only should probe then write');
  assert(
    JSON.stringify(calls[1].body) === JSON.stringify({
      lane: 'byo',
      provider: 'google',
      model: 'gemini-3.5-pro',
      google_model_resolution_targets: ['confidential_prior'],
    }),
    'restore-only provider write body should carry targets',
  );
  assert(providersApplied.active_lane.lane === 'confidential', 'restore-only providers should apply');
  assert(renders === 1, 'restore-only save should render after provider write');
  assert(messages.at(-1).message === 'remembered Gemini 3.5 Pro. sol keeps thinking with confidential processing now.', 'restore-only copy');
  assert(messages.at(-1).tone === 'ok', 'restore-only status tone');

  calls.length = 0;
  messages.length = 0;
  renders = 0;
  const probeFail = await runByoModelSaveFlow({
    apiFn: async (path, options) => {
      calls.push({path, body: JSON.parse(options.body)});
      return {valid: false, reason_code: 'provider_quota_exceeded'};
    },
    applyProviders: () => { throw new Error('should not apply providers'); },
    provider: 'anthropic',
    providerName: 'Claude',
    model: 'model-two',
    text,
    setMode: (next) => { mode = next; },
    renderFn: () => { renders += 1; },
    showStatus: (message, tone) => messages.push({message, tone}),
  });

  assert(probeFail.status === 'probe_failed', 'probe failure status');
  assert(calls.length === 1 && calls[0].path === 'api/validate-model', 'probe failure should not write providers');
  assert(messages.at(-1).message === "your key works, but model-two didn't answer — Claude says it's out of quota right now.", 'probe failure copy');

  calls.length = 0;
  messages.length = 0;
  renders = 0;
  const putFail = await runByoModelSaveFlow({
    apiFn: async (path, options) => {
      calls.push({path, body: JSON.parse(options.body)});
      if (path === 'api/validate-model') return {valid: true};
      throw new Error('write failed');
    },
    applyProviders: () => {},
    provider: 'anthropic',
    providerName: 'Claude',
    model: 'model-three',
    text,
    setMode: (next) => { mode = next; },
    renderFn: () => { renders += 1; },
    showStatus: (message, tone) => messages.push({message, tone}),
  });

  assert(putFail.status === 'save_failed', 'provider write failure status');
  assert(JSON.stringify(calls.map((call) => call.path)) === JSON.stringify(['api/validate-model', 'api/providers']), 'provider write failure should still probe first');
  assert(calls.filter((call) => call.path === 'api/providers').length === 1, 'provider write should happen once');
  assert(messages.at(-1).message === 'write failed', 'provider write error should render honestly');
  assert(renders === 1, 'provider write failure should stay on rendered model step');
  assert(mode === 'model', 'provider write failure should stay on model mode');
  console.log('PASS');
}
main().catch((error) => { console.error(error.stack || error); process.exit(1); });
"""
        )
    )
