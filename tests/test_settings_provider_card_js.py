# SPDX-License-Identifier: AGPL-3.0-only
# Copyright (c) 2026 sol pbc

from __future__ import annotations

from pathlib import Path

WORKSPACE = Path("solstone/apps/thinking/workspace.html")
STATIC = Path("solstone/apps/thinking/static/thinking.js")


def _workspace_text() -> str:
    return WORKSPACE.read_text(encoding="utf-8")


def _static_text() -> str:
    return STATIC.read_text(encoding="utf-8")


def test_thinking_workspace_exposes_providers_anchor_and_lanes():
    text = _workspace_text()

    assert 'id="providers"' in text
    assert 'id="thinkingActiveLane"' in text
    assert 'id="lane-confidential"' in text
    assert 'id="lane-byo"' in text
    assert 'id="lane-local"' in text
    assert "window.THINKING =" not in text
    assert "/app/thinking/api/state" in _static_text()


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
    ):
        assert endpoint in text
    assert "gpu_probe_failed" in text
    assert "gpu_unavailable" in text
    for moved in (
        "/app/settings/api/providers",
        "/app/settings/api/keys",
        "/app/settings/api/validate-keys",
        "/app/settings/api/local",
    ):
        assert moved not in text
    # The confidential audio switch writes transcribe config, which the settings
    # app owns: this is the one settings endpoint thinking may call.
    assert "/app/settings/api/config" in text
    assert text.count("/app/settings") == 1
