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


def _run_node(body: str) -> None:
    node = shutil.which("node")
    if node is None:
        pytest.skip("node is not available")
    source = STATIC.read_text(encoding="utf-8")
    script = "\n".join(
        [
            extract_js_function(source, "localRuntimeCopy"),
            extract_js_function(source, "pollLocalRuntimeUntilStable"),
            extract_js_function(source, "applyLocalRuntime"),
            extract_js_function(source, "stopRuntimePoll"),
            extract_js_function(source, "markLocalRuntimeStale"),
            "function assert(condition, message) { if (!condition) throw new Error(message); }",
            f"const text = {json.dumps(thinking_copy.LOCAL_RECOVERY)};",
            body,
        ]
    )
    subprocess.run(
        [node, "-e", script],
        check=True,
        text=True,
        capture_output=True,
    )


def test_runtime_copy_has_one_honest_action_for_terminal_failure() -> None:
    _run_node(
        """
const failed = localRuntimeCopy({
  status: 'ok',
  phase: 'failed',
  reason_code: 'launch-budget-exhausted',
  can_retry: true,
}, true, text);
assert(failed.pill === 'needs attention', 'failed pill');
assert(failed.sub === "local thinking couldn't start", 'failed verdict');
assert(failed.retryRuntime === true, 'failed state should expose retry');
assert(failed.retryRuntimeLabel === 'try starting local again', 'component action');
assert(!failed.bootstrap && !failed.activate, 'failure should have one action');

const requested = localRuntimeCopy({
  status: 'ok',
  phase: 'retry-requested',
  can_retry: false,
}, true, text);
assert(requested.pill === 'retrying', 'retry request should be visible');
assert(requested.retryRuntime === false, 'retry request cannot double spend');
"""
    )


def test_runtime_copy_distinguishes_transient_ready_and_fail_closed_states() -> None:
    _run_node(
        """
const starting = localRuntimeCopy({status: 'ok', phase: 'warming'}, true, text);
assert(starting.pill === 'starting', 'warming should say starting');
assert(starting.retryRuntime === false, 'warming is non-actionable');

const waiting = localRuntimeCopy({
  status: 'ok',
  phase: 'host-blocked',
  reason_code: 'ram-insufficient',
}, true, text);
assert(waiting.pill === 'waiting', 'capacity pressure is recheckable');

const unsupported = localRuntimeCopy({
  status: 'ok',
  phase: 'host-blocked',
  reason_code: 'platform-unsupported',
}, true, text);
assert(unsupported.pill === 'unavailable', 'permanent host block is distinct');

const ready = localRuntimeCopy({status: 'ok', phase: 'ready'}, true, text);
assert(ready.pill === 'on', 'ready is earned');
assert(ready.sub === 'local thinking is ready', 'ready verdict');

const degraded = localRuntimeCopy({
  status: 'ok',
  phase: 'ready-proof-unavailable',
}, true, text);
assert(degraded.pill === 'on, needs a check', 'live process stays on');
assert(degraded.retryRuntime === false, 'degraded ready must not replace');

const corrupt = localRuntimeCopy({
  status: 'corrupt',
  phase: 'state-corrupt',
}, true, text);
assert(corrupt.pill === "can't verify", 'corrupt state fails closed');
assert(corrupt.retryRuntime === false, 'corrupt state cannot retry');

const stale = localRuntimeCopy({
  status: 'stale',
  phase: 'state-stale',
}, true, text);
assert(stale.sub === "local status couldn't be refreshed", 'stale read is explicit');
assert(stale.retryRuntime === false, 'stale read must hide mutation');
"""
    )


def test_runtime_copy_does_not_override_inactive_artifact_setup() -> None:
    _run_node(
        """
assert(
  localRuntimeCopy({status: 'ok', phase: 'stopped'}, false, text) === null,
  'inactive stopped runtime should leave artifact/activation rendering alone',
);
assert(
  localRuntimeCopy({status: 'ok', phase: 'artifact-not-ready'}, true, text) === null,
  'artifact owner should render artifact recovery',
);
"""
    )


def test_runtime_poll_stops_at_truthful_terminal_state_and_fences_navigation() -> None:
    _run_node(
        """
async function main() {
  const statuses = [
    {phase: 'warming', poll: true},
    {phase: 'ready', poll: false},
  ];
  let index = 0;
  const applied = [];
  const result = await pollLocalRuntimeUntilStable({
    fetchStatus: async () => statuses[index++],
    sleepFn: async () => {},
    applyStatus: (status) => applied.push(status.phase),
    isCurrent: () => true,
    intervalMs: 1500,
    initialStatus: {phase: 'starting', poll: true},
  });
  assert(result.phase === 'ready', 'poll should return ready');
  assert(JSON.stringify(applied) === JSON.stringify(['warming', 'ready']), 'phase order');

  let fetched = false;
  const cancelled = await pollLocalRuntimeUntilStable({
    fetchStatus: async () => { fetched = true; return {phase: 'ready', poll: false}; },
    sleepFn: async () => {},
    applyStatus: () => {},
    isCurrent: () => false,
    intervalMs: 1500,
    initialStatus: {phase: 'warming', poll: true},
  });
  assert(cancelled.phase === 'warming', 'cancelled poll keeps its last state');
  assert(fetched === false, 'cancelled poll must not fetch');
}
main().catch((error) => { console.error(error.stack || error); process.exit(1); });
"""
    )


def test_runtime_apply_fences_health_and_retry_revision_races() -> None:
    _run_node(
        """
const state = {
  providers: {
    local_runtime: {
      health_revision: 8,
      retry_revision: 4,
      phase: 'retry-requested',
    },
  },
  runtimePollGeneration: 3,
};
let renders = 0;
function renderAll() { renders += 1; }

assert(
  applyLocalRuntime({
    health_revision: 8,
    retry_revision: 3,
    phase: 'failed',
  }) === false,
  'older retry truth must not restore a spent retry action',
);
assert(state.providers.local_runtime.phase === 'retry-requested', 'retry state preserved');
assert(renders === 0, 'stale retry truth must not render');

assert(
  applyLocalRuntime({
    health_revision: 7,
    retry_revision: 5,
    phase: 'failed',
  }) === false,
  'older health truth must stay fenced',
);
assert(
  applyLocalRuntime({
    health_revision: 9,
    retry_revision: 5,
    phase: 'starting',
  }, 2) === false,
  'an obsolete poll generation must stay fenced',
);
assert(
  applyLocalRuntime({
    health_revision: 9,
    retry_revision: 5,
    phase: 'starting',
  }, 3) === true,
  'current monotonic truth should apply',
);
assert(state.providers.local_runtime.phase === 'starting', 'fresh truth applied');
assert(renders === 1, 'fresh truth renders once');
"""
    )


def test_runtime_refresh_failure_marks_prior_truth_stale_and_fail_closed() -> None:
    _run_node(
        """
const state = {
  providers: {
    local_runtime: {
      status: 'ok',
      phase: 'failed',
      health_revision: 8,
      retry_revision: 4,
      can_retry: true,
    },
  },
  runtimePollGeneration: 3,
};
let renders = 0;
function renderAll() { renders += 1; }

markLocalRuntimeStale();

assert(state.runtimePollGeneration === 4, 'stale transition cancels polling');
assert(state.providers.local_runtime.status === 'stale', 'prior truth marked stale');
assert(state.providers.local_runtime.can_retry === false, 'stale truth disables retry');
assert(state.providers.local_runtime.health_revision === null, 'stale CAS is discarded');
assert(renders === 1, 'stale state renders immediately');
"""
    )
