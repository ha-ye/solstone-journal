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


def _node_script(body: str) -> str:
    source = STATIC.read_text(encoding="utf-8")
    parts = [
        extract_js_const(source, "confidentialTerminalPhases"),
        extract_js_function(source, "formatCopy"),
        extract_js_function(source, "confidentialOperationIsTerminal"),
        extract_js_function(source, "confidentialOperationRender"),
        extract_js_function(source, "confidentialSetupMetaLine"),
        extract_js_function(source, "confidentialNoticeLine"),
        extract_js_function(source, "confidentialSetupOperationLines"),
        extract_js_function(source, "confidentialAttestationRender"),
        extract_js_function(source, "clearConfidentialInProgressOperation"),
        extract_js_function(source, "confidentialGlanceForAttestation"),
        extract_js_function(source, "pollConfidentialUntilTerminal"),
        extract_js_function(source, "handleConfidentialPollError"),
        extract_js_function(source, "requestBrainCheck"),
        extract_js_function(source, "recheckConfidential"),
        "function assert(condition, message) { if (!condition) throw new Error(message); }",
        f"const copy = {json.dumps(thinking_copy.thinking_copy_payload())};",
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


def test_confidential_render_mappings_are_pure() -> None:
    _run_node(
        _node_script(
            """
const confidentialCopy = copy.confidential;
const verifiedLine = 'confidential hardware verified · checked just now';
const verified = confidentialAttestationRender({
  state: 'verified',
  observed_at: '2026-07-12T12:00:00+00:00',
}, confidentialCopy, 'just now');
assert(verified.pill === 'active', 'verified pill');
assert(verified.tone === 'hot', 'verified tone');
assert(verified.message === verifiedLine, 'verified message');
assert(verified.recheck === false, 'verified no recheck');

const inactive = confidentialAttestationRender({state: 'inactive'}, confidentialCopy);
assert(inactive.pill === 'available', 'inactive pill');
assert(inactive.tone === '', 'inactive tone');
assert(inactive.recheck === false, 'inactive no recheck');
assert(inactive.message === 'confidential processing is available.', 'inactive message');

const failed = confidentialAttestationRender({state: 'failed'}, confidentialCopy);
assert(failed.tone === 'bad', 'failed tone');
assert(failed.recheck === true, 'failed recheck');
assert(failed.message === "couldn't verify the service — sol isn't sending.", 'failed message');

const starting = confidentialOperationRender({phase: 'starting'}, confidentialCopy);
assert(starting.message === 'opening your browser to confirm…', 'starting message');
const early = confidentialOperationRender({phase: 'early_access'}, confidentialCopy);
assert(early.message === 'confidential processing is coming — scouts get it first.', 'early access copy');
assert(confidentialOperationIsTerminal({phase: 'early_access'}), 'early access terminal');

const glance = confidentialGlanceForAttestation(
  {state: 'verified', observed_at: '2026-07-12T12:00:00+00:00'},
  copy,
  'just now',
);
assert(glance.label === 'sol is thinking with', 'verified glance label');
assert(glance.detail === verifiedLine, 'verified glance detail');
const available = confidentialGlanceForAttestation({state: 'inactive'}, copy);
assert(available.label === 'available', 'inactive glance label');
assert(available.detail === 'confidential processing is available.', 'inactive glance detail');
const off = confidentialGlanceForAttestation({state: 'off'}, copy);
assert(off.label === '', 'off glance label');
assert(off.value === '', 'off glance value');
assert(off.detail === '', 'off glance detail');
const blocked = confidentialGlanceForAttestation({state: 'unreachable'}, copy);
assert(blocked.label === 'sol is holding', 'blocked glance label');
assert(blocked.detail === "can't reach confidential processing right now — sol isn't sending.", 'blocked glance detail');
console.log('PASS');
"""
        )
    )


def test_confidential_verified_render_and_setup_lines() -> None:
    _run_node(
        _node_script(
            """
const confidentialCopy = copy.confidential;
const expectedLine = 'confidential hardware verified · checked 1 min ago';
const verifiedAttestation = {state: 'verified', observed_at: '2026-07-12T12:00:00+00:00'};
const verified = confidentialAttestationRender(verifiedAttestation, confidentialCopy, '1 min ago');
const verifiedGlance = confidentialGlanceForAttestation(verifiedAttestation, copy, '1 min ago');
assert(verified.message === expectedLine, 'verified setup and lane-card line');
assert(verifiedGlance.detail === expectedLine, 'verified glance line');
assert(verified.message === verifiedGlance.detail, 'verified surfaces share formatter');
assert(confidentialSetupMetaLine(verifiedAttestation, 'just now') === '', 'verified meta hidden');

for (const state of ['inactive', 'verifying', 'off', '']) {
  assert(confidentialSetupMetaLine({state}, 'just now') === '', `${state || 'empty'} meta hidden`);
}

for (const state of ['failed', 'stale', 'unreachable']) {
  assert(
    confidentialSetupMetaLine({state}, 'just now') === 'last checked just now',
    `${state} meta line`,
  );
  assert(confidentialSetupMetaLine({state}, '') === '', `${state} meta hidden without time`);
}

const earlyNotice = confidentialNoticeLine({phase: 'early_access'}, confidentialCopy);
assert(earlyNotice.hidden === false, 'early notice visible');
assert(
  earlyNotice.text === 'confidential processing is coming — scouts get it first.',
  'early notice text',
);
for (const operation of [
  null,
  {phase: 'starting'},
  {phase: 'waiting'},
  {phase: 'repair_needed'},
  {phase: 'not_verified'},
]) {
  const notice = confidentialNoticeLine(operation, confidentialCopy);
  assert(notice.text === '', `notice text hidden for ${operation?.phase || 'none'}`);
  assert(notice.hidden === true, `notice hidden for ${operation?.phase || 'none'}`);
}

const earlyLines = confidentialSetupOperationLines(
  {phase: 'early_access'},
  confidentialCopy,
  '',
);
const earlySentence = 'confidential processing is coming — scouts get it first.';
assert(earlyLines.state === '', 'early access absent from state line');
assert(earlyLines.operation === '', 'early access absent from operation line');
assert(earlyLines.notice.text === earlySentence, 'early access assigned to notice');
assert(
  [earlyLines.state, earlyLines.operation, earlyLines.notice.text]
    .filter((line) => line === earlySentence).length === 1,
  'early access visible exactly once',
);

for (const [phase, message] of [
  ['starting', 'opening your browser to confirm…'],
  ['waiting', 'finish turning it on in your browser'],
]) {
  const lines = confidentialSetupOperationLines({phase}, confidentialCopy, 'attestation');
  assert(lines.state === message, `${phase} state unchanged`);
  assert(lines.operation === message, `${phase} operation unchanged`);
  assert(lines.notice.hidden === true, `${phase} notice hidden`);
}

console.log('PASS');
"""
        )
    )


def test_confidential_poll_stops_at_early_access_terminal() -> None:
    _run_node(
        _node_script(
            """
async function main() {
  const statuses = ['starting', 'waiting', 'early_access'].map((phase) => ({
    active_lane: {confidential_operation: {phase}},
  }));
  let fetchCalls = 0;
  const sleeps = [];
  const applied = [];
  async function fetchStatus() {
    return statuses[fetchCalls++];
  }
  async function sleepFn(ms) {
    sleeps.push(ms);
  }
  function applyStatus(status) {
    applied.push(status.active_lane.confidential_operation.phase);
  }
  const result = await pollConfidentialUntilTerminal({
    fetchStatus,
    sleepFn,
    applyStatus,
    isCurrent: () => true,
    intervalMs: 1500,
    maxElapsedMs: 15000,
  });
  assert(result.active_lane.confidential_operation.phase === 'early_access', 'terminal result');
  assert(fetchCalls === 3, 'fetch count');
  assert(JSON.stringify(applied) === JSON.stringify(['starting', 'waiting', 'early_access']), 'applied phases');
  assert(sleeps.length === 2, 'sleep count');
  console.log('PASS');
}
main().catch((error) => { console.error(error.stack || error); process.exit(1); });
"""
        )
    )


def test_confidential_recheck_posts_brain_check_then_rereads_providers() -> None:
    _run_node(
        _node_script(
            """
const calls = [];
const messages = [];
const state = {providers: {brain: {state: 'unhealthy'}, sentinel: true}};
async function api(path, options) {
  calls.push({path, options});
  assert(path === 'api/brain/check', 'posts brain check');
  return {ok: true, brain: {state: 'checking'}};
}
function renderGlance() {
  calls.push({path: 'renderGlance'});
}
async function refreshProviders() {
  assert(state.providers.sentinel === true, 'post did not replace providers');
  assert(state.providers.brain.state === 'checking', 'brain response merged first');
  calls.push({path: 'refreshProviders'});
  state.providers = {refreshed: true};
}
function setMessage(id, message, tone = '') {
  messages.push({id, message, tone});
}

async function main() {
  await recheckConfidential();

  assert(messages.length === 1, 'message cleared once');
  assert(messages[0].message === '', 'no optimistic verifying paint');
  assert(JSON.stringify(calls.map((call) => call.path)) === JSON.stringify([
    'api/brain/check',
    'renderGlance',
    'refreshProviders',
  ]), 'call order');
  assert(state.providers.refreshed === true, 'providers reread updates card state');
  console.log('PASS');
}
main().catch((error) => { console.error(error.stack || error); process.exit(1); });
"""
        )
    )


def test_confidential_poll_timeout_and_error_handler_clear_current_generation() -> None:
    _run_node(
        _node_script(
            """
async function main() {
  let now = 0;
  let fetchCalls = 0;
  let cleared = false;
  let stopped = false;
  let errorMessage = '';
  try {
    await pollConfidentialUntilTerminal({
      fetchStatus: async () => {
        fetchCalls += 1;
        return {active_lane: {confidential_operation: {phase: 'waiting'}}};
      },
      sleepFn: async (ms) => { now += ms; },
      applyStatus: () => {},
      isCurrent: () => true,
      intervalMs: 1500,
      maxElapsedMs: 2000,
      nowFn: () => now,
    });
    throw new Error('poll should have timed out');
  } catch (error) {
    const handled = handleConfidentialPollError({
      generation: 1,
      currentGeneration: () => 1,
      clearOperation: () => { cleared = true; },
      stopPoll: () => { stopped = true; },
      showError: (message) => { errorMessage = message; },
      error,
    });
    assert(handled === true, 'current generation handled');
  }
  assert(fetchCalls === 2, 'bounded fetches');
  assert(cleared === true, 'cleared');
  assert(stopped === true, 'stopped');
  assert(errorMessage === 'confidential setup timed out', 'timeout message');

  const stale = handleConfidentialPollError({
    generation: 1,
    currentGeneration: () => 2,
    clearOperation: () => { throw new Error('stale clear'); },
    stopPoll: () => { throw new Error('stale stop'); },
    showError: () => { throw new Error('stale error'); },
    error: new Error('ignored'),
  });
  assert(stale === false, 'stale generation ignored');
  console.log('PASS');
}
main().catch((error) => { console.error(error.stack || error); process.exit(1); });
"""
        )
    )


def test_confidential_poll_cancellation_clears_only_in_progress_operation() -> None:
    _run_node(
        _node_script(
            """
const activeLane = {confidential_operation: {phase: 'waiting'}};
assert(clearConfidentialInProgressOperation(activeLane) === true, 'waiting cleared');
assert(activeLane.confidential_operation === null, 'operation nulled');

const terminalLane = {confidential_operation: {phase: 'early_access'}};
assert(clearConfidentialInProgressOperation(terminalLane) === false, 'terminal kept');
assert(terminalLane.confidential_operation.phase === 'early_access', 'terminal preserved');

const emptyLane = {};
assert(clearConfidentialInProgressOperation(emptyLane) === false, 'empty ignored');
console.log('PASS');
"""
        )
    )
