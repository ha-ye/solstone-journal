# SPDX-License-Identifier: AGPL-3.0-only
# Copyright (c) 2026 sol pbc

from __future__ import annotations

import re
from pathlib import Path

WORKSPACE = Path(__file__).resolve().parents[1] / "workspace.html"


def _workspace_text() -> str:
    return WORKSPACE.read_text(encoding="utf-8")


def test_apikeys_inputs_are_masked_by_default():
    text = _workspace_text()
    keys = (
        "REVAI_ACCESS_TOKEN",
        "PLAUD_ACCESS_TOKEN",
    )

    for key in keys:
        match = re.search(rf'<input[^>]*\bdata-key="{key}"[^>]*>', text)
        assert match, f"{key} input not found"
        tag = match.group(0)
        assert 'type="password"' in tag, f"{key} input is not type=password"
        assert 'type="text"' not in tag, f"{key} input still has type=text"

    for moved_key in ("GOOGLE_API_KEY", "OPENAI_API_KEY", "ANTHROPIC_API_KEY"):
        assert f'data-key="{moved_key}"' not in text


def test_password_toggle_does_not_steal_focus():
    text = _workspace_text()
    # Anchor on the querySelectorAll forEach, not the class="password-toggle" buttons.
    idx = text.index(".password-toggle')")
    block = text[idx : idx + 800]
    assert "mousedown" in block
    assert "preventDefault()" in block


def test_workspace_has_diagnostic_reports_toggle():
    text = _workspace_text()

    assert 'id="field-reporting-enabled"' in text
    assert "diagnostic reports" in text


def test_workspace_network_access_toggle_uses_dedicated_flow():
    text = _workspace_text()

    match = re.search(r'<input[^>]*\bid="field-network-access"[^>]*>', text)
    assert match, "network access toggle not found"
    toggle_tag = match.group(0)
    assert "data-section" not in toggle_tag
    assert "data-key" not in toggle_tag
    assert "{{ settings_copy.CONVEY_NETWORK_ACCESS_LABEL }}" in text
    assert "settings_copy.CONVEY_NETWORK_ACCESS_HINT" in text
    assert "api/convey/network-access/capability" in text
    assert "api/convey/network-access" in text
    assert "function handleNetworkAccessChange(el)" in text
    assert "const desired = el.checked" in text
    assert "el.checked = previous" in text
    assert "result?.restart_timeout" in text
    assert "saveConfigValue('convey', 'allow_network_access" not in text


def test_workspace_uses_global_convey_config_api():
    text = _workspace_text()

    assert "fetch('/api/config/convey')" in text
    assert "window.apiJson('/api/config/convey'" in text
    assert "'api/config/convey'" not in text


def test_workspace_transcription_resource_notice_and_info_line_present():
    text = _workspace_text()

    assert 'id="transcribeResourceNotice"' in text
    assert 'id="transcribeResourceNoticeText"' in text
    assert 'id="transcribeResourceInfo"' in text
    assert "function renderTranscribeResourceInfo(resource)" in text
    assert "function renderTranscribeResourceNotice(resource)" in text
    assert "transcribeResource = data.resource || null" in text
    assert "renderTranscribeResourceInfo(transcribeResource)" in text
    assert "renderTranscribeResourceNotice(transcribeResource)" in text


def test_workspace_cogitate_auth_control_removed():
    text = _workspace_text()

    assert 'id="field-cogitate-auth"' not in text
    assert "platform account" not in text
    assert "document.getElementById('field-cogitate-auth')" not in text


def test_workspace_security_network_mode_ui_removed_and_link_hint_present():
    text = _workspace_text()
    for removed in (
        'id="conveyNetworkButton"',
        'id="conveyNetworkMode"',
        'id="conveyNetworkDesc"',
        'id="conveyNetworkStatus"',
        'id="conveyPasswordDisclosure"',
        'id="conveyDisclosurePassword"',
        'id="conveyDisclosureConfirm"',
        'id="conveyDisclosureSubmit"',
        'id="conveyDisclosureError"',
        "conveyUiText",
        "renderConveyNetworkState",
        "setConveyNetworkStatus",
        "toggleConveyNetworkAccess",
        "showConveyPasswordDisclosure",
        "submitConveyPasswordDisclosure",
    ):
        assert removed not in text, removed

    assert 'id="conveyLanUrlDisplay"' not in text
    assert 'id="field-host-url"' not in text
    assert "function renderConveyHostFields(" in text
    assert 'id="field-password"' in text
    assert 'id="field-trust-localhost"' in text
    assert 'href="/app/link"' in text
    assert "{{ convey_copy.SETTINGS_SECURITY_REACH_HINT }}" in text
