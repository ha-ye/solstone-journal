# SPDX-License-Identifier: AGPL-3.0-only
# Copyright (c) 2026 sol pbc

from __future__ import annotations

import re
from pathlib import Path

WORKSPACE = Path("solstone/apps/thinking/workspace.html")
STATIC = Path("solstone/apps/thinking/static/thinking.js")


def _workspace_text() -> str:
    return WORKSPACE.read_text(encoding="utf-8")


def _static_text() -> str:
    return STATIC.read_text(encoding="utf-8")


def _owner_surface_text() -> str:
    lines = (_workspace_text() + "\n" + _static_text()).splitlines()
    return "\n".join(
        line
        for line in lines
        if "SPDX-License-Identifier" not in line
        and "Copyright (c) 2026 sol pbc" not in line
    )


def test_thinking_workspace_exposes_providers_anchor_and_lanes():
    text = _workspace_text()

    assert 'id="providers"' in text
    assert 'id="thinkingActiveLane"' in text
    assert 'id="lane-scout"' in text
    assert 'id="lane-byo"' in text
    assert 'id="lane-local"' in text
    assert "window.THINKING = {{ thinking_initial | tojson }};" in text


def test_thinking_static_uses_moved_endpoints_and_local_reason():
    text = _static_text()

    for endpoint in (
        "api/providers",
        "api/keys",
        "api/validate-keys",
        "api/local/models",
        "api/local/availability",
        "api/local/bootstrap",
        "api/local/endpoint",
        "api/scout",
    ):
        assert endpoint in text
    assert "gpu_probe_failed" in text
    assert "gpu_unavailable" in text
    assert "/app/settings" not in text


def test_thinking_static_has_scout_orchestration_structures():
    text = _static_text()

    for name in (
        "refreshScout",
        "renderScout",
        "pollScoutUntilTerminal",
        "enableScout",
        "checkScout",
        "refreshScoutOp",
        "disableScout",
    ):
        assert f"function {name}(" in text
    assert "switchLane('scout')" in text
    assert "phase === 'repair_needed'" in text
    assert "api('api/scout/enable'" in text
    assert "api('api/scout/check'" in text
    assert "api('api/scout/refresh'" in text
    assert "api('api/scout/disable'" in text
    assert "$('scoutCheck')?.addEventListener" in text


def test_thinking_surface_avoids_forbidden_owner_terms():
    combined = _owner_surface_text()

    for term in (
        "account",
        "account_id",
        "sign in",
        "log in",
        "subscribe",
        "upgrade",
        "capture",
        "watch",
        "record",
        "monitor",
        "track",
        "collect",
    ):
        assert re.search(rf"\b{re.escape(term)}\b", combined, re.IGNORECASE) is None

    for phrase in ("sol pbc", "this machine", "this device"):
        assert re.search(rf"\b{re.escape(phrase)}\b", combined, re.IGNORECASE) is None
