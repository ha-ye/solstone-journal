# SPDX-License-Identifier: AGPL-3.0-only
# Copyright (c) 2026 sol pbc

from __future__ import annotations

import json
import shutil
import subprocess
from pathlib import Path

import pytest

from solstone.apps.thinking import copy as thinking_copy
from solstone.apps.thinking.install_copy import INSTALL_FAILED_NO_PROGRESS
from solstone.apps.thinking.tests.js_extract import (
    extract_js_const,
    extract_js_function,
)

STATIC = Path(__file__).resolve().parents[1] / "static" / "thinking.js"


def _node_script(body: str) -> str:
    source = STATIC.read_text(encoding="utf-8")
    parts = [
        extract_js_const(source, "installInFlightStates"),
        extract_js_const(source, "installTerminalStates"),
        extract_js_function(source, "installIsInFlight"),
        extract_js_function(source, "installIsTerminal"),
        extract_js_function(source, "formatInstallBytes"),
        extract_js_function(source, "installCopyForStatus"),
        extract_js_function(source, "pollLocalInstallUntilTerminal"),
        extract_js_function(source, "handleInstallPollError"),
        "const pollIntervalMs = 1500;",
        extract_js_function(source, "startInstallPoll"),
        "function assert(condition, message) { if (!condition) throw new Error(message); }",
        f"const text = {json.dumps(thinking_copy.LOCAL_INSTALL)};",
        f"const installFailedNoProgress = {json.dumps(INSTALL_FAILED_NO_PROGRESS)};",
        body,
    ]
    return "\n".join(parts)


def test_install_poll_applies_phases_and_stops_at_installed() -> None:
    node = shutil.which("node")
    if node is None:
        pytest.skip("node is not available")

    script = _node_script(
        """
async function main() {
  const gb = 1024 * 1024 * 1024;
  const statuses = [
    {
      install_state: 'downloading',
      progress_bytes_received: Math.round(2.1 * gb),
      progress_bytes_total: Math.round(4.7 * gb),
      install_error: null,
    },
    {
      install_state: 'installing',
      progress_bytes_received: null,
      progress_bytes_total: null,
      install_error: null,
    },
    {
      install_state: 'installed',
      progress_bytes_received: null,
      progress_bytes_total: null,
      install_error: null,
    },
  ];
  let fetchCalls = 0;
  const sleeps = [];
  const applied = [];
  async function fetchStatus() {
    if (fetchCalls >= statuses.length) {
      throw new Error('fetchStatus called after terminal status');
    }
    return statuses[fetchCalls++];
  }
  async function sleepFn(ms) {
    sleeps.push(ms);
  }
  function applyStatus(status) {
    applied.push({
      state: status.install_state,
      rendered: installCopyForStatus(status, text),
    });
  }
  const result = await pollLocalInstallUntilTerminal({
    fetchStatus,
    sleepFn,
    applyStatus,
    isCurrent: () => true,
    intervalMs: 1500,
  });

  assert(result.install_state === 'installed', 'terminal status should be returned');
  assert(fetchCalls === 3, 'poll should stop after installed');
  assert(sleeps.length === 2, 'poll should sleep only between in-flight states');
  assert(sleeps.every((ms) => ms === 1500), 'poll should use the requested interval');
  assert(
    JSON.stringify(applied.map((item) => item.state)) === JSON.stringify(['downloading', 'installing', 'installed']),
    'statuses should apply in order',
  );
  assert(formatInstallBytes(Math.round(2.1 * gb), Math.round(4.7 * gb)) === '2.1 GB of 4.7 GB', 'bytes should format as GB');
  assert(applied[0].rendered.sub === 'downloading', 'download phase should render');
  assert(applied[0].rendered.message === '2.1 GB of 4.7 GB', 'download bytes should render');
  assert(applied[1].rendered.sub === 'installing', 'install phase should render');
  assert(applied[1].rendered.message === 'installing', 'phase should render when bytes are absent');
  assert(applied[2].rendered === null, 'installed should leave the normal local render path');
  console.log('PASS');
}
main().catch((error) => { console.error(error.stack || error); process.exit(1); });
"""
    )

    result = subprocess.run(
        [node, "-e", script],
        check=True,
        text=True,
        capture_output=True,
    )
    assert "PASS" in result.stdout


def test_install_poll_with_primed_status_sleeps_before_next_fetch() -> None:
    node = shutil.which("node")
    if node is None:
        pytest.skip("node is not available")

    script = _node_script(
        """
async function main() {
  const statuses = [
    {
      install_state: 'installing',
      progress_bytes_received: null,
      progress_bytes_total: null,
      install_error: null,
    },
    {
      install_state: 'installed',
      progress_bytes_received: null,
      progress_bytes_total: null,
      install_error: null,
    },
  ];
  let fetchCalls = 0;
  const events = [];
  async function fetchStatus() {
    events.push(`fetch:${fetchCalls}`);
    return statuses[fetchCalls++];
  }
  async function sleepFn(ms) {
    events.push(`sleep:${ms}`);
  }
  function applyStatus(status) {
    events.push(`apply:${status.install_state}`);
  }
  const result = await pollLocalInstallUntilTerminal({
    fetchStatus,
    sleepFn,
    applyStatus,
    isCurrent: () => true,
    intervalMs: 1500,
    initialStatus: {
      install_state: 'downloading',
      progress_bytes_received: null,
      progress_bytes_total: null,
      install_error: null,
    },
  });

  assert(result.install_state === 'installed', 'terminal status should be returned');
  assert(fetchCalls === 2, 'primed poll should fetch only after sleeping');
  assert(
    JSON.stringify(events) === JSON.stringify([
      'sleep:1500',
      'fetch:0',
      'apply:installing',
      'sleep:1500',
      'fetch:1',
      'apply:installed',
    ]),
    `primed poll should sleep before first fetch, got ${JSON.stringify(events)}`,
  );
  console.log('PASS');
}
main().catch((error) => { console.error(error.stack || error); process.exit(1); });
"""
    )

    result = subprocess.run(
        [node, "-e", script],
        check=True,
        text=True,
        capture_output=True,
    )
    assert "PASS" in result.stdout


def test_install_poll_error_clears_inflight_phase() -> None:
    node = shutil.which("node")
    if node is None:
        pytest.skip("node is not available")

    script = _node_script(
        """
const state = {
  install: null,
  installPollGeneration: 0,
};
let fetchCalls = 0;
let shownError = '';
const sleeps = [];
const rendered = [];

function selectedLocalModelId() {
  return 'local/qwen3.5-4b';
}

function stopInstallPoll() {
  state.installPollGeneration += 1;
}

async function fetchInstallStatus() {
  fetchCalls += 1;
  if (fetchCalls === 1) {
    return {
      install_state: 'installing',
      progress_bytes_received: null,
      progress_bytes_total: null,
      install_error: null,
    };
  }
  throw new Error('status unavailable');
}

async function sleep(ms) {
  sleeps.push(ms);
}

function applyLocalInstallStatus(status, generation) {
  if (generation !== undefined && generation !== state.installPollGeneration) return false;
  state.install = status || null;
  rendered.push(installCopyForStatus(state.install, text));
  return true;
}

async function refreshProviders() {
  throw new Error('refreshProviders should not run');
}

async function refreshLocalAvailability() {
  throw new Error('refreshLocalAvailability should not run');
}

function setMessage(id, message, tone) {
  shownError = `${id}:${tone}:${message}`;
}

async function main() {
  await startInstallPoll();

  assert(fetchCalls === 2, 'poll should reject on the second fetch');
  assert(sleeps.length === 1 && sleeps[0] === 1500, 'poll should sleep once before the rejected fetch');
  assert(state.install === null, 'install status should be cleared after poll error');
  assert(rendered.length === 2, 'poll should render the in-flight phase and then the cleared state');
  assert(rendered[0].sub === 'installing', 'first render should show the in-flight phase');
  assert(rendered[1] === null, 'cleared status should not render an in-flight phase');
  assert(state.installPollGeneration === 2, 'poll generation should be stopped after error');
  assert(
    shownError === 'localSetupMessage:error:status unavailable',
    'poll error should surface to the owner',
  );
  console.log('PASS');
}
main().catch((error) => { console.error(error.stack || error); process.exit(1); });
"""
    )

    result = subprocess.run(
        [node, "-e", script],
        check=True,
        text=True,
        capture_output=True,
    )
    assert "PASS" in result.stdout


def test_install_poll_renders_failed_error_and_retry() -> None:
    node = shutil.which("node")
    if node is None:
        pytest.skip("node is not available")

    script = _node_script(
        """
async function main() {
  const statuses = [
    {
      install_state: 'installing',
      progress_bytes_received: null,
      progress_bytes_total: null,
      install_error: null,
    },
    {
      install_state: 'failed',
      progress_bytes_received: null,
      progress_bytes_total: null,
      install_error: installFailedNoProgress,
    },
  ];
  let fetchCalls = 0;
  const sleeps = [];
  const applied = [];
  async function fetchStatus() {
    if (fetchCalls >= statuses.length) {
      throw new Error('fetchStatus called after failed status');
    }
    return statuses[fetchCalls++];
  }
  async function sleepFn(ms) {
    sleeps.push(ms);
  }
  function applyStatus(status) {
    applied.push({
      state: status.install_state,
      rendered: installCopyForStatus(status, text),
    });
  }
  const result = await pollLocalInstallUntilTerminal({
    fetchStatus,
    sleepFn,
    applyStatus,
    isCurrent: () => true,
    intervalMs: 1500,
  });

  const failed = applied[1].rendered;
  assert(result.install_state === 'failed', 'failed status should be terminal');
  assert(fetchCalls === 2, 'poll should stop after failed');
  assert(sleeps.length === 1, 'poll should sleep only before failed');
  assert(failed.message === installFailedNoProgress, 'server install_error should render verbatim');
  assert(failed.bootstrap === true, 'failed install should offer retry');
  assert(failed.bootstrapLabel === text.retry, 'retry label should come from copy');
  assert(failed.tone === 'bad', 'failed install should render as an error');
  console.log('PASS');
}
main().catch((error) => { console.error(error.stack || error); process.exit(1); });
"""
    )

    result = subprocess.run(
        [node, "-e", script],
        check=True,
        text=True,
        capture_output=True,
    )
    assert "PASS" in result.stdout
