# SPDX-License-Identifier: AGPL-3.0-only
# Copyright (c) 2026 sol pbc

from __future__ import annotations

import shutil
import subprocess
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[3]
DEGRADED_SVG = ROOT / "solstone" / "convey" / "static" / "sol-status" / "degraded.svg"
WEBSOCKET_JS = ROOT / "solstone" / "convey" / "static" / "websocket.js"


def test_degraded_status_asset_uses_red_glyph_and_keeps_ring_rays():
    text = DEGRADED_SVG.read_text(encoding="utf-8")

    assert 'stroke="#dc2626"' in text
    assert '<circle cx="16.0" cy="18.6" r="0.7" fill="#dc2626"/>' in text
    assert "#d97706" not in text
    assert "#E8923A" in text
    assert "#FFCF33" in text


def test_websocket_status_mark_degraded_wiring():
    text = WEBSOCKET_JS.read_text(encoding="utf-8")

    assert "captureStatus === 'degraded'" in text
    assert "degraded: 'a device needs attention'" in text


def test_websocket_derive_status_mark_device_labels(tmp_path):
    node = shutil.which("node")
    if node is None:
        pytest.skip("node is not available")

    source = WEBSOCKET_JS.read_text(encoding="utf-8")
    start = source.index("  function deriveStatusMark")
    end = source.index("\n\n  function renderStatusMark", start)
    script = tmp_path / "derive-status-mark-test.js"
    script.write_text(
        """
function assert(condition, message) {
  if (!condition) throw new Error(message);
}

"""
        + source[start:end]
        + """

const cases = [
  ['connected', 'offline', false, 'error', false, 'devices offline'],
  ['connected', 'degraded', false, 'degraded', false, 'a device needs attention'],
  ['connected', 'stale', false, 'half', false, 'devices out of touch'],
  ['connected', 'no_observers', false, 'paused', false, 'no devices running sol'],
  ['connected', 'active', false, 'active', false, 'active'],
  ['disconnected', 'active', false, 'x', false, 'disconnected'],
  ['connected', 'unknown', false, 'question', false, 'status unknown'],
  ['connected', 'active', true, 'bang', false, 'attention'],
];

for (const [wsState, captureStatus, hasUnviewed, variant, spin, label] of cases) {
  const actual = deriveStatusMark(wsState, captureStatus, hasUnviewed);
  assert(actual.variant === variant, `${label}: wrong variant ${actual.variant}`);
  assert(actual.spin === spin, `${label}: wrong spin ${actual.spin}`);
  assert(actual.label === label, `${label}: wrong label ${actual.label}`);
}
""",
        encoding="utf-8",
    )

    subprocess.run([node, str(script)], check=True, text=True)
