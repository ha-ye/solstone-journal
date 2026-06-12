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
        "GOOGLE_API_KEY",
        "OPENAI_API_KEY",
        "ANTHROPIC_API_KEY",
        "REVAI_ACCESS_TOKEN",
        "PLAUD_ACCESS_TOKEN",
    )

    for key in keys:
        match = re.search(rf'<input[^>]*\bdata-key="{key}"[^>]*>', text)
        assert match, f"{key} input not found"
        tag = match.group(0)
        assert 'type="password"' in tag, f"{key} input is not type=password"
        assert 'type="text"' not in tag, f"{key} input still has type=text"


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


def test_workspace_unified_provider_panel_replaces_install_regions():
    text = _workspace_text()

    assert 'id="providersPanel"' in text
    assert 'id="bundledProviders"' not in text
    assert 'id="mlxBootstrapRegion"' not in text
    assert 'id="localBootstrapRegion"' not in text
    assert "bundled-provider-grid" not in text
    assert "mlx-bootstrap-region" not in text
    assert "local-bootstrap-region" not in text
    assert "mlx-progress-shell" not in text
    assert "local-progress-shell" not in text
    assert "function startLocalBootstrap()" in text
    assert "function renderProvidersPanel(data)" in text
    assert "function renderAiReadinessSummary(aiReadiness)" in text
    assert "summary.dataset.aiReadinessSummary" in text
    assert (
        "function providerCardMeta(state, kind, availability, readiness = null)" in text
    )
    assert "function providerCardMetaLine(state, kind, availability)" in text
    assert "function runProviderAction(providerId, action)" in text
    assert "async function pollProvidersPanel()" in text
    assert "function providerCardOverflow(state, kind)" in text
    assert "const PROVIDER_NAMES = ['anthropic', 'openai', 'local']" in text


def test_workspace_unified_provider_panel_keeps_bootstrap_endpoints_and_polling():
    text = _workspace_text()

    assert "let localBootstrapPostStarted = false" in text
    assert "api/local/bootstrap?model=${model}" in text
    assert "api/providers?local_model=${model}" in text
    assert "api/local/availability?model=${model}" in text
    assert "setInterval(pollProvidersPanel, 1000)" in text
    assert "clearInterval(providersPanelPollTimer)" in text
    assert "IN_FLIGHT_INSTALL_STATES.includes(state.install_state)" in text
    assert "providersPanelActionPending" in text


def test_workspace_local_issue_copy_includes_gpu_unavailable():
    text = _workspace_text()

    assert "gpu_unavailable" in text
    assert "This computer has no GPU acceleration, which local models require." in text


def test_workspace_provider_names_excludes_openhands():
    text = _workspace_text()

    assert "const PROVIDER_NAMES = ['anthropic', 'openai', 'local']" in text
    assert "'openhands'" not in text


def test_workspace_cloud_cards_have_no_install_affordances():
    text = _workspace_text()

    assert "postProviderAction" not in text
    assert "api/providers/${providerId}" not in text
    assert "cloudInstalledMeta" not in text
    assert "CLI: installed at" not in text


def test_workspace_unified_provider_panel_has_byte_and_blocked_state_paths():
    text = _workspace_text()

    assert "formatMlxBytes(receivedBytes)" in text
    assert "formatMlxBytes(totalBytes)" in text
    assert "totalBytes <= 0" in text
    assert "receivedBytes > totalBytes" in text
    assert "function localMlxBlockedReason(state, availability)" in text
    assert "providerCardMetaLine(state, kind, availability)" in text
    assert "INSTALL_COPY.LOCAL_REQUIREMENTS_TEMPLATE" in text
    assert "INSTALL_COPY.LOCAL_DETECTED_MEMORY_TEMPLATE" in text
    assert "INSTALL_COPY.LOCAL_DETECTED_MEMORY_UNKNOWN" in text
    assert "INSTALL_COPY.LOCAL_PATHS_FRAMING" in text
    assert "INSTALL_COPY.LOCAL_EXPERIMENTAL_NOTE" in text
    assert "INSTALL_COPY.LOCAL_RECOVERY_HOSTED_KEY_SET" in text
    assert "INSTALL_COPY.LOCAL_RECOVERY_NO_HOSTED_KEY" in text
    assert (
        "!!(configData?.env?.GOOGLE_API_KEY || configData?.runtime_env?.GOOGLE_API_KEY)"
    ) in text
    assert "'local runtime is not installed'" in text
    assert "'local model files are not installed'" in text
    match = re.search(
        r"const installableReasons = \[(?P<body>.*?)\];",
        text,
        re.DOTALL,
    )
    assert match is not None
    assert "insufficient RAM" not in match.group("body")
    assert "insufficient disk" not in match.group("body")


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


def test_workspace_cogitate_key_guidance_strings_present():
    text = _workspace_text()

    assert (
        "This provider needs an API key to run agents. Get one at "
        "aistudio.google.com, then add it in API keys below."
    ) in text
    assert (
        "This provider needs an API key to run agents. Get one at "
        "console.anthropic.com, then add it in API keys below."
    ) in text
    assert (
        "This provider needs an API key to run agents. Get one at "
        "platform.openai.com, then add it in API keys below."
    ) in text


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


def test_workspace_local_cogitate_status_block_and_unified_panel():
    text = _workspace_text()

    warning_idx = text.index('id="cogitateProviderKeyWarning"')
    status_idx = text.index('id="localCogitateStatus"')
    provider_status_idx = text.index('id="providerStatus"')
    assert warning_idx < status_idx < provider_status_idx
    assert 'id="localCogitateStatus-indicator"' in text
    assert 'id="providersPanel"' in text
    assert 'id="localBootstrapRegion"' not in text
    assert "api/providers/local/status" in text
    assert "api/local/bootstrap?model=${model}" in text
    assert "api/local/availability?model=${model}" in text
    assert "tool-using agents" not in text


def test_workspace_local_model_row_uses_shared_local_install_path():
    text = _workspace_text()

    assert 'id="localModelRow"' in text
    assert 'id="field-local-active-model"' in text
    assert 'id="mlxModelRow"' not in text
    assert 'id="field-mlx-active-model"' not in text
    assert "data?.local_backend === \"mlx\" ? 'local-mlx' : 'local'" in text
    assert "kind === 'local-mlx'" in text
    assert "kind === 'local'" in text
    assert "function isLocalProviderSelected()" in text
