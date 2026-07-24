# SPDX-License-Identifier: AGPL-3.0-only
# Copyright (c) 2026 sol pbc

from __future__ import annotations

import re
from html.parser import HTMLParser
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


def _css_rule(text: str, selector: str) -> str:
    match = re.search(
        rf"{re.escape(selector)}\s*(?=[,{{])[^{{]*\{{(.*?)\}}",
        text,
        re.DOTALL,
    )
    assert match, f"{selector} CSS rule not found"
    return match.group(1)


def _tag_by_id(text: str, tag: str, element_id: str) -> str:
    match = re.search(rf"<{tag}\b[^>]*\bid=\"{element_id}\"[^>]*>", text)
    assert match, f"{tag}#{element_id} not found"
    return match.group(0)


def _settings_field_block(text: str, tag_index: int) -> str:
    start = text.rfind('<div class="settings-field"', 0, tag_index)
    assert start != -1, "settings-field opening tag not found"

    depth = 0
    for match in re.finditer(r"</?div\b[^>]*>", text[start:]):
        tag = match.group(0)
        if tag.startswith("</"):
            depth -= 1
        else:
            depth += 1
        if depth == 0:
            end = start + match.end()
            return text[start:end]

    assert False, "settings-field closing tag not found"


class _SettingsFormButtonParser(HTMLParser):
    def __init__(self) -> None:
        super().__init__(convert_charrefs=True)
        self._settings_form_depth = 0
        self.non_button_buttons: list[str] = []

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        attr_map = dict(attrs)
        classes = set((attr_map.get("class") or "").split())
        starts_settings_form = tag == "form" and "settings-form" in classes
        in_settings_form = self._settings_form_depth > 0 or starts_settings_form

        if starts_settings_form:
            self._settings_form_depth += 1

        if tag == "button" and in_settings_form and attr_map.get("type") != "button":
            self.non_button_buttons.append(
                attr_map.get("id")
                or attr_map.get("class")
                or self.get_starttag_text()
                or "<button>"
            )

    def handle_startendtag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        self.handle_starttag(tag, attrs)
        self.handle_endtag(tag)

    def handle_endtag(self, tag: str) -> None:
        if tag == "form" and self._settings_form_depth > 0:
            self._settings_form_depth -= 1


def test_apikeys_inputs_are_masked_by_default():
    text = _workspace_text()
    keys = ("PLAUD_ACCESS_TOKEN",)

    for key in keys:
        match = re.search(rf'<input[^>]*\bdata-key="{key}"[^>]*>', text)
        assert match, f"{key} input not found"
        tag = match.group(0)
        assert 'type="password"' in tag, f"{key} input is not type=password"
        assert 'type="text"' not in tag, f"{key} input still has type=text"

    for moved_key in ("GOOGLE_API_KEY", "OPENAI_API_KEY", "ANTHROPIC_API_KEY"):
        assert f'data-key="{moved_key}"' not in text


def test_plaud_token_note_routes_to_apikeys_section():
    text = _workspace_text()

    assert 'id="plaudApiKeysLink"' in text
    assert "getElementById('plaudApiKeysLink')?.addEventListener('click'" in text


def test_password_toggle_does_not_steal_focus():
    text = _workspace_text()
    # Anchor on the querySelectorAll forEach, not the class="password-toggle" buttons.
    idx = text.index(".password-toggle')")
    block = text[idx : idx + 800]
    assert "mousedown" in block
    assert "preventDefault()" in block


def test_settings_form_buttons_are_non_submit_buttons():
    text = _workspace_text()
    parser = _SettingsFormButtonParser()
    parser.feed(text)

    assert parser.non_button_buttons == []


def test_settings_nav_reserves_app_bar_space():
    text = _workspace_text()

    nav_body = _css_rule(text, ".settings-nav")
    assert " ".join(nav_body.split()) == (
        "width: 180px; flex-shrink: 0; position: sticky; "
        "top: calc(var(--facet-bar-height, 60px) + 1em); "
        "align-self: flex-start; "
        "max-height: calc(100vh - var(--facet-bar-height, 60px) - "
        "var(--app-bar-height, 60px) - 3em); overflow-y: auto; "
        "border-right: 1px solid var(--facet-border, #e5e0db); "
        "padding-right: 0.75rem;"
    )
    assert "- 2em" not in nav_body

    content_body = _css_rule(text, ".settings-content")
    assert "padding-bottom: calc(var(--app-bar-height, 60px) + 2em);" in content_body


def test_settings_field_base_input_selector_excludes_bare_checks_and_radios():
    text = _workspace_text()
    base_selector = (
        '.settings-field input:where(:not([type="checkbox"]):not([type="radio"]))'
    )

    base_body = _css_rule(text, base_selector)
    assert "width: 100%;" in base_body
    assert "font-size: 0.95em;" in base_body

    form_fields = text[
        text.index("/* Form fields */") : text.index(".settings-field input:focus")
    ]
    assert base_selector in form_fields
    assert ".settings-field input,\n.settings-field textarea" not in form_fields

    media_block = text[text.index("@media (max-width: 480px)") : text.index("</style>")]
    media_body = _css_rule(
        media_block,
        ".settings-field input,\n  .settings-field select,\n  .settings-field textarea",
    )
    assert "font-size: 16px;" in media_body

    focus_selector = (
        ".settings-field input:focus,\n"
        ".settings-field textarea:focus,\n"
        ".settings-field select:focus"
    )
    focus_body = _css_rule(text, ".settings-field input:focus")
    assert focus_selector in text
    assert "border-color: var(--facet-color, #E8923A);" in focus_body

    hover_selector = (
        ".settings-field input:hover,\n"
        ".settings-field textarea:hover,\n"
        ".settings-field select:hover"
    )
    hover_body = _css_rule(text, ".settings-field input:hover")
    assert hover_selector in text
    assert "border-color: #bbb;" in hover_body


def test_settings_bare_controls_and_toggle_switch_widths_stay_scoped():
    text = _workspace_text()
    base_selector = (
        '.settings-field input:where(:not([type="checkbox"]):not([type="radio"]))'
    )

    toggle_body = _css_rule(text, ".toggle-switch input")
    assert text.index(".toggle-switch input") > text.index(base_selector)
    assert "width: 0;" in toggle_body
    assert "height: 0;" in toggle_body

    for element_id, input_type in (
        ("field-chat-thinking-on-tap", "radio"),
        ("field-chat-thinking-always", "radio"),
        ("field-chat-thinking-never", "radio"),
        ("retentionDontRetain", "checkbox"),
    ):
        tag = _tag_by_id(text, "input", element_id)
        assert f'type="{input_type}"' in tag
        assert "style=" not in tag
        assert "class=" not in tag
        tag_index = text.index(tag)
        block = _settings_field_block(text, tag_index)
        assert tag in block


def test_retention_custom_inputs_have_room_without_touching_cleanup_modals():
    text = _workspace_text()

    for element_id, width in (
        ("retentionDaysInput", "7em"),
        ("logRetentionDaysInput", "7em"),
        ("cleanupDaysInput", "5em"),
        ("cleanupLogsDaysInput", "5em"),
    ):
        tag = _tag_by_id(text, "input", element_id)
        assert f"width: {width};" in tag

    custom_tags = re.findall(r'<input\b[^>]*\bplaceholder="custom"[^>]*>', text)
    assert len(custom_tags) == 2
    assert {re.search(r'\bid="([^"]+)"', tag).group(1) for tag in custom_tags} == {
        "retentionDaysInput",
        "logRetentionDaysInput",
    }


def test_redact_add_enter_handler_is_explicit():
    text = _workspace_text()

    button = re.search(r'<button[^>]*\bid="redactAddBtn"[^>]*>', text)
    assert button, "redact add button not found"
    assert 'type="button"' in button.group(0)

    listener_idx = text.index("document.getElementById('redactAddInput')")
    block = text[listener_idx : listener_idx + 260]
    assert "keydown" in block
    assert "e.key === 'Enter'" in block
    assert "e.preventDefault()" in block
    assert "addRedactRule()" in block


def test_agent_name_enter_handler_blurs_instead_of_submitting():
    text = _workspace_text()

    button = re.search(r'<button[^>]*\bid="resetAgentName"[^>]*>', text)
    assert button, "agent name reset button not found"
    assert 'type="button"' in button.group(0)

    listener_idx = text.index("const agentNameInput = document.getElementById")
    block = text[listener_idx : listener_idx + 360]
    assert "keydown" in block
    assert "e.key === 'Enter'" in block
    assert "e.preventDefault()" in block
    assert "agentNameInput.blur()" in block


def test_workspace_has_diagnostic_reports_toggle():
    text = _workspace_text()

    assert 'id="field-reporting-enabled"' in text
    assert "diagnostic reports" in text


def test_workspace_has_log_retention_storage_controls():
    text = _workspace_text()

    assert 'id="logRetentionEnabled"' in text
    assert 'id="cleanupLogsBtn"' in text
    assert 'id="cleanupLogsModal"' in text


def test_workspace_stream_overrides_uses_render_mount():
    text = _workspace_text()

    assert 'id="streamOverridesMount"' in text
    assert 'id="streamOverridesContainer"' not in text
    assert 'id="streamOverridesLine"' not in text
    assert 'id="streamOverridesBody"' not in text


def test_workspace_log_cleanup_renderer_surfaces_preview_skips_and_errors():
    text = _workspace_text()

    assert "function renderLogCleanupResult(result, phase)" in text
    assert "would be deleted" in text
    assert "cleanup complete" in text
    assert "stats.skipped" in text
    assert "error.hint" in text
    assert "runLogCleanupPreview" in text
    assert "runLogCleanupExecute" in text


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
    assert '<a class="sapp" href="/app/network">' in guide
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


def test_document_level_listeners_reference_defined_handlers():
    """Guard against orphaned parse-time event registrations.

    A top-level ``document.addEventListener('<event>', <handler>)`` runs the
    moment the settings ``<script>`` is parsed. If ``<handler>`` is a bare
    identifier that is never declared, it throws a ReferenceError at parse time
    and aborts the entire script — so ``initSettings`` never registers, no
    config loads, and no auto-save listeners bind, leaving every setting
    silently un-saveable. This guards that class: every document-level listener
    registered against a bare named handler must have that handler declared
    somewhere in the script.
    """
    text = _workspace_text()

    # The settings app is a single inline <script> block.
    script_match = re.search(r"<script>(.*)</script>", text, re.DOTALL)
    assert script_match, "settings <script> block not found"
    script = script_match.group(1)

    # Match only `document.addEventListener('<event>', <bareIdentifier>)` — a
    # document-level registration whose handler is a named reference followed
    # immediately by ')'. This deliberately ignores inline function/arrow
    # literals (not followed by ')') and member-target registrations like
    # `el.addEventListener(...)`, which legitimately bind handlers declared in
    # nested scopes.
    registrations = re.findall(
        r"\bdocument\.addEventListener\(\s*'[^']+'\s*,\s*([A-Za-z_$][\w$]*)\s*\)",
        script,
    )
    assert registrations, (
        "no document.addEventListener(named-handler) registrations found"
    )

    for handler in registrations:
        declared = re.search(
            rf"(?:async\s+)?function\s+{re.escape(handler)}\b"
            rf"|\b(?:const|let|var)\s+{re.escape(handler)}\b",
            script,
        )
        assert declared, (
            f"document.addEventListener registers handler '{handler}', but it is "
            f"never declared in the settings script. A parse-time ReferenceError "
            f"would abort settings init and silently break all settings saving."
        )


def test_agent_name_enter_commits_via_blur():
    text = _workspace_text()
    start = text.index(
        "const agentNameInput = document.getElementById('field-agent-name');"
    )
    block = text[start : text.index("const resetAgentBtn", start)]
    # Enter in the lone agent-name input commits via blur (reusing the
    # change-save path); it must never fall through to implicit form submit.
    assert "agentNameInput.onkeydown" in block
    assert "e.key === 'Enter'" in block
    assert "e.preventDefault()" in block
    assert "agentNameInput.blur()" in block


def test_settings_form_buttons_declare_explicit_type():
    """Every <button> inside a <form class="settings-form"> must declare an
    explicit `type`. A type-less button defaults to type="submit"; Enter in a
    lone text input then implicitly submits the form and dispatches a click on
    it -- the sol-identity reset-to-"sol" footgun. Guarding the whole form
    class stops that bug class from re-appearing.

    Every settings-form button now declares an explicit type: req_eenxsko7 has
    landed and added `type` to its two buttons (createPersonalBtn, redactAddBtn),
    so the allowlist is empty. Shrink-only and self-checking -- a re-introduced
    type-less button turns this red.
    """
    text = _workspace_text()

    # req_eenxsko7 has landed and reworked its two buttons' Enter-to-add
    # behavior, giving each an explicit type; nothing left to allow. Never add.
    allowlist = set()

    forms = re.findall(r'<form class="settings-form".*?</form>', text, re.DOTALL)
    assert forms, "no settings-form blocks found"

    offenders = []
    for form in forms:
        for tag in re.findall(r"<button\b[^>]*>", form):
            if re.search(r"\btype\s*=", tag):
                continue
            id_match = re.search(r'\bid="([^"]+)"', tag)
            btn_id = id_match.group(1) if id_match else None
            if btn_id in allowlist:
                continue
            offenders.append(btn_id or tag)
    assert not offenders, f"settings-form buttons missing explicit type=: {offenders}"

    # Self-check: the allowlist may only shrink. Each id must still exist AND
    # still lack an explicit type -- otherwise drop it from the allowlist.
    for btn_id in allowlist:
        tag_match = re.search(rf'<button\b[^>]*\bid="{btn_id}"[^>]*>', text)
        assert tag_match, f"allowlisted button id {btn_id} no longer exists"
        assert not re.search(r"\btype\s*=", tag_match.group(0)), (
            f"allowlisted button {btn_id} now declares type= -- remove it "
            f"from the allowlist (req_eenxsko7)"
        )
