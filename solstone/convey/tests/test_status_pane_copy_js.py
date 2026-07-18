# SPDX-License-Identifier: AGPL-3.0-only
# Copyright (c) 2026 sol pbc

from __future__ import annotations

import shutil
import subprocess
from pathlib import Path

import pytest

STATUS_PANE_JS = Path(__file__).resolve().parents[1] / "static" / "status_pane.js"


def test_render_capture_section_device_copy(tmp_path):
    node = shutil.which("node")
    if node is None:
        pytest.skip("node is not available")

    source = STATUS_PANE_JS.read_text(encoding="utf-8")
    start = source.index("  function formatObserverLastReported")
    end = source.index("\n\n  function renderVersionSection", start)
    script = tmp_path / "status-pane-copy-test.js"
    script.write_text(
        """
function assert(condition, message) {
  if (!condition) throw new Error(message);
}

class FakeElement {
  constructor(tagName = 'div') {
    this.tagName = tagName.toUpperCase();
    this.children = [];
    this.style = {};
    this.attributes = {};
    this.listeners = {};
    this.href = '';
    this.type = '';
    this.disabled = false;
    this._text = '';
  }

  set textContent(value) {
    this._text = String(value ?? '');
    this.children = [];
  }

  get textContent() {
    return this._text;
  }

  appendChild(child) {
    this.children.push(child);
    return child;
  }

  setAttribute(name, value) {
    this.attributes[name] = String(value);
  }

  addEventListener(type, handler) {
    this.listeners[type] = handler;
  }
}

const section = new FakeElement('section');
const text = new FakeElement('div');

global.document = {
  getElementById(id) {
    if (id === 'capture-status-section') return section;
    if (id === 'capture-status-text') return text;
    return null;
  },
  createElement(tagName) {
    return new FakeElement(tagName);
  }
};
global.window = {
  CONVEY_COPY: { ACTION_RECONNECT: 'Reconnect' },
  apiJson() {
    return Promise.resolve({});
  }
};

const now = Date.UTC(2026, 5, 22, 12, 5, 0);
Date.now = () => now;

function relativeTime(ms) {
  return Math.round(ms / 60000) + 'm';
}

"""
        + source[start:end]
        + """

function reset() {
  section.style = {};
  text.style = {};
  text.textContent = '';
}

function allText(element) {
  return [element.textContent, ...element.children.map(allText)]
    .filter(Boolean)
    .join('|');
}

function render(capture) {
  reset();
  renderCaptureSection(capture);
  return allText(text);
}

const firstTs = Date.UTC(2026, 5, 22, 12, 0, 0);

const noDetail = render({ status: 'degraded', observers: [] });
assert(noDetail.includes('a device needs attention'), 'degraded no-detail title missing');
assert(noDetail.includes("a device isn't reaching your journal."), 'degraded no-detail body missing');
assert(noDetail.includes('view device health →'), 'device health link missing');

const consequenceCases = [
  [
    { first_ts: firstTs, active_count: 2 },
    "what it sensed hasn't reached your journal since jun 22, 2 uploads turned away."
  ],
  [
    { active_count: 2 },
    "what it senses isn't reaching your journal, 2 uploads turned away."
  ],
  [
    { first_ts: firstTs },
    "what it sensed hasn't reached your journal since jun 22."
  ],
  [
    {},
    "what it senses isn't reaching your journal."
  ],
];

for (const [rejection, expected] of consequenceCases) {
  const rendered = render({
    status: 'degraded',
    observers: [{
      name: 'phone',
      status: 'degraded',
      ingest_rejection: rejection
    }]
  });
  assert(rendered.includes(expected), `missing consequence: ${expected}`);
}

const versionRecovery = render({
  status: 'degraded',
  observers: [{
    name: '',
    status: 'degraded',
    ingest_rejection: { version: '0.3.1' }
  }]
});
assert(
  versionRecovery.includes('this device is running sol v0.3.1. update or restart sol on that device, then the next upload clears this.'),
  'version recovery copy missing'
);

const staleOne = render({
  status: 'stale',
  observers: [{ name: 'phone', status: 'stale', last_seen: now - 300000 }]
});
assert(staleOne.includes('device phone last reported 5m ago'), 'singular stale copy missing');

const staleMany = render({
  status: 'stale',
  observers: [
    { name: 'phone', status: 'stale', last_seen: now - 300000 },
    { name: 'tablet', status: 'stale', last_seen: now - 600000 }
  ]
});
assert(staleMany.includes('devices phone, tablet last reported 5m ago'), 'plural stale copy missing');

assert(
  render({ status: 'no_observers', observers: [] }) === 'no devices are running sol yet. set one up to start your journal.',
  'no-observers tail copy missing'
);
assert(render({ status: 'active', observers: [] }) === 'device active', 'active tail copy missing');
assert(
  render({ status: 'garbled', observers: [] }) === "i don't know the status of your devices right now.",
  'unknown tail copy missing'
);
""",
        encoding="utf-8",
    )

    subprocess.run([node, str(script)], check=True, text=True)
