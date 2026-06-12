# SPDX-License-Identifier: AGPL-3.0-only
# Copyright (c) 2026 sol pbc

from __future__ import annotations

import re
import shutil
import subprocess
from pathlib import Path

import pytest

INIT_HTML = Path(__file__).resolve().parents[1] / "templates" / "init.html"


def _extract_function(source: str, name: str) -> str:
    start = source.index(f"function {name}(")
    if source[start - 6 : start] == "async ":
        start -= 6
    brace_start = source.index("{", start)
    depth = 0
    in_string: str | None = None
    escaped = False
    for index in range(brace_start, len(source)):
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
    raise AssertionError(f"could not extract {name}")


def test_enable_scout_branches_on_reason_code() -> None:
    node = shutil.which("node")
    if node is None:
        pytest.skip("node is not available")

    source = INIT_HTML.read_text(encoding="utf-8")
    enable_scout = _extract_function(source, "enableScout")
    script = (
        """
let renderCalls = [];
function renderScoutState(state, opts = {}) { renderCalls.push([state, opts]); }
let portalUnreachableShown = 0;
function showPortalUnreachable() { portalUnreachableShown += 1; }
function hidePortalUnreachable() {}
function closeScoutStream() {}
function subscribeScoutStream() {}
const MANUAL_KEY_PRESENT_COPY = 'manual-key-copy-sentinel';
const button = { disabled: false, textContent: '' };
global.document = {
  getElementById(id) {
    return id === 'scout-enable' ? button : null;
  }
};
let thrown = null;
global.window = { apiJson: async () => { throw thrown; } };
"""
        + enable_scout
        + """

function assert(c, m) { if (!c) throw new Error(m); }
(async () => {
  // already_enabled: owner copy in serverMessage, slug only in reasonCode
  renderCalls = [];
  thrown = {
    status: 409,
    serverMessage: "I couldn't enable scout because it's already on.",
    reasonCode: 'already_enabled'
  };
  await enableScout();
  let last = renderCalls[renderCalls.length - 1];
  assert(
    last[0] === 'success',
    'already_enabled reason_code must render success, got ' + last[0]
  );

  // manual_key_present
  renderCalls = [];
  thrown = {
    status: 409,
    serverMessage:
      "I couldn't enable scout because a Gemini key is " +
      "already on this machine.",
    reasonCode: 'manual_key_present'
  };
  await enableScout();
  last = renderCalls[renderCalls.length - 1];
  assert(last[0] === 'error', 'manual_key_present must render error state');
  assert(
    last[1].reason === 'manual_key_present',
    'reason opt must be manual_key_present'
  );
  assert(last[1].message === MANUAL_KEY_PRESENT_COPY, 'must use manual-key copy');
  assert(last[1].retry === false, 'retry must be hidden');

  // adversarial: old shape (slug in serverMessage, no reasonCode) must NOT
  // be treated as a match
  renderCalls = [];
  portalUnreachableShown = 0;
  thrown = { status: 409, serverMessage: 'already_enabled', reasonCode: null };
  await enableScout();
  last = renderCalls[renderCalls.length - 1];
  assert(
    last[0] === 'idle',
    'slug-in-serverMessage must fall through to the generic path, not success'
  );
  assert(
    portalUnreachableShown === 1,
    'generic fallback must show portal-unreachable'
  );
})().catch(e => { console.error(e); process.exit(1); });
"""
    )
    subprocess.run([node, "-e", script], check=True, text=True)


def test_format_scout_since_matches_format_since() -> None:
    node = shutil.which("node")
    if node is None:
        pytest.skip("node is not available")

    source = INIT_HTML.read_text(encoding="utf-8")
    format_scout_since = _extract_function(source, "formatScoutSince")
    script = (
        format_scout_since
        + """

function assert(c, m) { if (!c) throw new Error(m); }
assert(
  formatScoutSince(1700000000000) === '2023-11-14',
  'epoch ms must format as UTC YYYY-MM-DD'
);
assert(formatScoutSince(null) === 'recently', 'null must be recently');
assert(formatScoutSince(0) === 'recently', 'zero must be recently');
assert(formatScoutSince('garbage') === 'recently', 'garbage must be recently');
"""
    )
    subprocess.run([node, "-e", script], check=True, text=True)


def test_subscribe_scout_stream_retries_then_renders_unreachable() -> None:
    node = shutil.which("node")
    if node is None:
        pytest.skip("node is not available")

    source = INIT_HTML.read_text(encoding="utf-8")
    close_scout_stream = _extract_function(source, "closeScoutStream")
    subscribe_scout_stream = _extract_function(source, "subscribeScoutStream")
    backoff_match = re.search(r"const SCOUT_RETRY_BACKOFF_MS = \[[^\]]*\];", source)
    assert backoff_match is not None
    backoff_line = backoff_match.group(0)
    script = (
        """
let renderCalls = [];
function renderScoutState(state, opts = {}) { renderCalls.push([state, opts]); }
let portalUnreachableShown = 0;
function showPortalUnreachable() { portalUnreachableShown += 1; }
function hidePortalUnreachable() {}
let scoutStream = null;
let scoutSubscribed = false;
let scoutRetryTimer = null;
"""
        + backoff_line
        + """
const SCOUT_UNREACHABLE_COPY = 'scout-unreachable-copy-sentinel';
let constructed = [];
global.EventSource = function EventSource(url) {
  this.closed = 0;
  constructed.push(this);
  this.addEventListener = function () {};
  this.close = function () { this.closed += 1; };
  Object.defineProperty(this, 'onerror', {
    configurable: true,
    set(fn) { fn(); }
  });
};
global.setTimeout = function (fn, _ms) { fn(); return 'scout-timer'; };
global.clearTimeout = function () {};
"""
        + close_scout_stream
        + subscribe_scout_stream
        + """

function assert(c, m) { if (!c) throw new Error(m); }
subscribeScoutStream('http://stub/subscribe', 'http://stub/portal');
assert(
  constructed.length === SCOUT_RETRY_BACKOFF_MS.length + 1,
  'construction count must equal backoff schedule plus terminal attempt'
);
assert(
  constructed.every(i => i.closed === 1),
  'each EventSource must be closed exactly once'
);
const last = renderCalls[renderCalls.length - 1];
assert(last[0] === 'error', 'terminal render must be error, got ' + last[0]);
assert(last[1].reason === 'timeout', 'terminal reason must be timeout');
assert(
  last[1].message === SCOUT_UNREACHABLE_COPY,
  'terminal message must route SCOUT_UNREACHABLE_COPY'
);
assert(
  portalUnreachableShown === 1,
  'portal-unreachable aside must be shown exactly once'
);
assert(
  renderCalls.every(c => c[0] !== 'waiting' && c[0] !== 'success'),
  'must never render waiting or success'
);
"""
    )
    subprocess.run([node, "-e", script], check=True, text=True)
