# SPDX-License-Identifier: AGPL-3.0-only
# Copyright (c) 2026 sol pbc

from __future__ import annotations

import re
from pathlib import Path

WORKSPACE = Path(__file__).resolve().parents[1] / "workspace.html"


def _workspace_text() -> str:
    return WORKSPACE.read_text(encoding="utf-8")


def _section_block(text: str, section_id: str) -> str:
    match = re.search(
        rf'<section class="settings-section(?: active)?" id="section-{section_id}"'
        r".*?</section>",
        text,
        re.DOTALL,
    )
    assert match, f"section-{section_id} not found"
    return match.group(0)


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


def test_workspace_vision_max_extractions_reads_server_value():
    text = _workspace_text()

    match = re.search(r'<input[^>]*\bid="field-max-extractions"[^>]*>', text)
    assert match, "max extractions input not found"
    tag = match.group(0)
    assert 'value="20"' not in tag
    assert 'placeholder="20"' in tag
    assert "function setMaxExtractionsInput(value)" in text
    assert "setMaxExtractionsInput(data.max_extractions)" in text
    assert "setMaxExtractionsInput(result.max_extractions)" in text
    assert "input.value = visionData?.max_extractions || 20" not in text


def test_workspace_network_access_toggle_removed():
    text = _workspace_text()

    assert 'id="field-network-access"' not in text
    assert 'id="network-access-status"' not in text
    assert "settings_copy.CONVEY_NETWORK_ACCESS_LABEL" not in text
    assert "settings_copy.CONVEY_NETWORK_ACCESS_HINT" not in text
    assert "api/convey/network-access/capability" not in text
    assert "api/convey/network-access" not in text
    assert "function handleNetworkAccessChange(el)" not in text
    assert "networkAccessCapability" not in text
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


def test_workspace_security_section_removed():
    text = _workspace_text()
    for removed in (
        '<option value="security">',
        'id="tab-security"',
        'id="section-security"',
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
        "function renderConveyHostFields(",
        'id="field-trust-localhost"',
    ):
        assert removed not in text, removed


def test_workspace_guide_is_default_static_section():
    text = _workspace_text()

    assert '<option value="guide" selected>guide</option>' in text
    assert '<option value="profile">profile</option>' in text
    assert (
        '<button class="settings-nav-item active" data-section="guide" id="tab-guide" '
        'role="tab" aria-selected="true" aria-controls="section-guide" tabindex="0">'
        "guide</button>"
    ) in text
    assert (
        '<button class="settings-nav-item" data-section="profile" id="tab-profile" '
        'role="tab" aria-selected="false" aria-controls="section-profile" '
        'tabindex="-1">profile</button>'
    ) in text

    guide = _section_block(text, "guide")
    profile = _section_block(text, "profile")
    assert guide.startswith('<section class="settings-section active"')
    assert profile.startswith('<section class="settings-section"')
    assert "VALID_SECTIONS = ['guide'," in text
    assert text.count("sectionId = 'guide';") == 2


def test_workspace_guide_copy_stays_in_bounds():
    text = _workspace_text()
    guide = _section_block(text, "guide")
    lowered = guide.lower()

    assert (
        "apps that have their own settings. "
        "open one to set it up or change how it works." in guide
    )
    # three live signposts route to their own app pages
    assert '<a class="sapp" href="/app/thinking">' in guide
    assert '<a class="sapp" href="/app/link">' in guide
    assert '<a class="sapp" href="/app/backup">' in guide
    # verbatim founder copy
    assert "manage what AI models your journal uses" in guide
    assert "reach your journal from your other devices" in guide
    assert "make an encrypted copy only you can read" in guide
    assert "how and when sol reaches you on any device" in guide
    # notifications is parked: present, but never a clickable dead link
    assert "notifications" in guide
    assert '<a class="sapp" href="/app/notifications"' not in guide
    assert 'href="#"' not in guide

    banned_terms = (
        "your services",
        "sign in",
        "account",
        "subscribe",
        "upgrade",
        "capture",
        "watch",
        "record",
        "monitor",
        "track",
        "collect",
    )
    for term in banned_terms:
        assert term not in lowered

    dynamic_terms = ("fetch(", "/api/", "setInterval", "enable", "disable", "poll")
    for term in dynamic_terms:
        assert term not in guide
