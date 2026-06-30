# SPDX-License-Identifier: AGPL-3.0-only
# Copyright (c) 2026 sol pbc

from __future__ import annotations

from pathlib import Path

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
    assert "degraded: 'observer needs attention'" in text
