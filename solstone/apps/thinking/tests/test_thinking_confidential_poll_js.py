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
        extract_js_function(source, "confidentialAttestationRender"),
        extract_js_function(source, "confidentialGlanceForAttestation"),
        extract_js_function(source, "pollConfidentialUntilTerminal"),
        extract_js_function(source, "handleConfidentialPollError"),
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
const verified = confidentialAttestationRender({
  state: 'verified',
  provenance: {checked_at: '2026-07-12T12:00:00+00:00'},
}, confidentialCopy, 'just now');
assert(verified.pill === 'active', 'verified pill');
assert(verified.tone === 'hot', 'verified tone');
assert(verified.message === 'checked just now', 'verified message');

const failed = confidentialAttestationRender({state: 'failed'}, confidentialCopy);
assert(failed.tone === 'bad', 'failed tone');
assert(failed.recheck === true, 'failed recheck');
assert(failed.message === "couldn't verify the service — sol isn't sending.", 'failed message');

const starting = confidentialOperationRender({phase: 'starting'}, confidentialCopy);
assert(starting.message === 'opening your browser to confirm…', 'starting message');
assert(starting.active === true, 'starting active');
const early = confidentialOperationRender({phase: 'early_access'}, confidentialCopy);
assert(early.message === 'confidential processing is coming — scouts get it first.', 'early access copy');
assert(confidentialOperationIsTerminal({phase: 'early_access'}), 'early access terminal');

const glance = confidentialGlanceForAttestation({state: 'verified', provenance: {}}, copy, '1 min ago');
assert(glance.label === 'sol is thinking with', 'verified glance label');
assert(glance.detail === 'checked 1 min ago', 'verified glance detail');
const blocked = confidentialGlanceForAttestation({state: 'unreachable'}, copy);
assert(blocked.label === 'sol is holding', 'blocked glance label');
assert(blocked.detail === "can't reach confidential processing right now — sol isn't sending.", 'blocked glance detail');
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
