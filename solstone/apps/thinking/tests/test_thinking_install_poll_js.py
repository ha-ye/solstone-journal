# SPDX-License-Identifier: AGPL-3.0-only
# Copyright (c) 2026 sol pbc

from __future__ import annotations

import json
import re
import shutil
import subprocess
from pathlib import Path

import pytest

from solstone.apps.thinking import copy as thinking_copy
from solstone.apps.thinking.install_copy import INSTALL_FAILED_NO_PROGRESS

STATIC = Path(__file__).resolve().parents[1] / "static" / "thinking.js"


def _extract_js_const(source: str, const_name: str) -> str:
    match = re.search(
        rf"  const {re.escape(const_name)} = new Set\(\[[^\]]+\]\);",
        source,
    )
    if match is None:
        raise AssertionError(f"could not extract {const_name}")
    return match.group(0).strip()


def _extract_js_function(source: str, function_name: str) -> str:
    markers = [
        f"  function {function_name}",
        f"  async function {function_name}",
    ]
    starts = [source.index(marker) for marker in markers if marker in source]
    if not starts:
        raise AssertionError(f"could not extract {function_name}")
    start = min(starts) + 2
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


def _node_script(body: str) -> str:
    source = STATIC.read_text(encoding="utf-8")
    parts = [
        _extract_js_const(source, "installInFlightStates"),
        _extract_js_const(source, "installTerminalStates"),
        _extract_js_function(source, "installIsInFlight"),
        _extract_js_function(source, "installIsTerminal"),
        _extract_js_function(source, "formatInstallBytes"),
        _extract_js_function(source, "installCopyForStatus"),
        _extract_js_function(source, "pollLocalInstallUntilTerminal"),
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
