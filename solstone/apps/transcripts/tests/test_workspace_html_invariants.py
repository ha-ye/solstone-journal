# SPDX-License-Identifier: AGPL-3.0-only
# Copyright (c) 2026 sol pbc

import re
from pathlib import Path

from solstone.apps.transcripts.copy import (
    TR_SPEAKER_SOMEONE_ELSE,
    transcripts_copy_payload,
)
from solstone.think.importers import health_schema


def _apple_health_card_stream() -> str:
    return health_schema.health_card_stream(health_schema.SOURCE_APPLE_HEALTH)


_MEDIA_OPEN = re.compile(r"@media\s*(?P<condition>[^{}]+)\{")


def _slice_between(text: str, start: str, end: str) -> str:
    """Return the text between two anchors, failing if either is missing."""
    assert text.count(start) == 1, f"expected exactly one start anchor: {start}"
    start_idx = text.index(start) + len(start)
    assert end in text[start_idx:], f"missing end anchor after {start}: {end}"
    end_idx = text.index(end, start_idx)
    return text[start_idx:end_idx]


def _css_rule(text: str, selector: str) -> str:
    """Return the declaration body of a top-level CSS rule.

    Newline-anchored so a variant rule (``body.presentation-mode .tr-tab``) can
    never satisfy the anchor.
    """
    return _slice_between(text, f"\n{selector} {{", "}")


def _js_const_expr(text: str, name: str) -> str:
    """Return the right-hand-side expression of a single ``const NAME = ...;``."""
    prefix = f"const {name} = "
    lines = [
        line.strip() for line in text.splitlines() if line.strip().startswith(prefix)
    ]
    assert len(lines) == 1, f"expected exactly one const declaration for {name}"
    return lines[0].split(prefix, 1)[1].split(";", 1)[0].strip()


def _norm(text: str) -> str:
    return re.sub(r"\s+", " ", text).strip()


def _media_conditions(text: str) -> list[str]:
    return [match.group("condition").strip() for match in _MEDIA_OPEN.finditer(text)]


def _mobile_media_block(text: str) -> tuple[int, str]:
    mobile_blocks: list[tuple[int, str]] = []
    for match in _MEDIA_OPEN.finditer(text):
        depth = 1
        index = match.end()
        while index < len(text) and depth > 0:
            char = text[index]
            if char == "{":
                depth += 1
            elif char == "}":
                depth -= 1
            index += 1
        assert depth == 0, "unterminated @media block in workspace.html"
        if _norm(match.group("condition")) == "(max-width: 768px)":
            mobile_blocks.append((match.start(), text[match.end() : index - 1]))
    assert len(mobile_blocks) == 1, "expected exactly one mobile media block"
    return mobile_blocks[0]


def test_workspace_html_single_purge_notice_emission():
    workspace_html = Path(__file__).resolve().parents[1] / "workspace.html"

    text = workspace_html.read_text()

    assert text.count('<div class="tr-purge-notice"') == 1, (
        "expected exactly one retention-banner emit site in workspace.html"
    )


def test_workspace_html_renders_awaiting_thinking_state():
    workspace_html = Path(__file__).resolve().parents[1] / "workspace.html"

    text = workspace_html.read_text()

    assert text.count("awaiting thinking") >= 1
    assert "tr-seg-awaiting" in text
    assert "tr-zoom-pill-awaiting" in text
    assert "seg.think" in text
    assert "rg.think" in text


def test_workspace_html_failed_final_reuses_failed_affordance():
    workspace_html = Path(__file__).resolve().parents[1] / "workspace.html"

    text = workspace_html.read_text()
    failed_renderer = text.split("function renderFailedDataState", 1)[1].split(
        "function renderDataStateAffordance", 1
    )[0]
    affordance = text.split("function renderDataStateAffordance", 1)[1].split(
        "function tabExists", 1
    )[0]

    assert "function renderFailedDataState(modality)" in text
    assert "dataStateCopy(modality, 'failed')" in failed_renderer
    assert "state === 'failed' || state === 'failed_final'" in affordance
    assert "return renderFailedDataState(modality);" in affordance
    assert "dataState[modality] === 'failed_final'" in text


def test_workspace_html_wires_body_window_panel_and_strip():
    workspace_html = Path(__file__).resolve().parents[1] / "workspace.html"

    text = workspace_html.read_text()

    assert 'id="trBodyPanel"' in text
    assert 'id="trBodyEvents"' in text
    assert "/app/body/api/window" in text
    assert "fetchSegmentBodyWindow" in text
    assert "fetchDayBodyWindow" in text
    assert "renderBodyStrip" in text
    assert "renderBodyContextCard" in text
    assert "renderBodyEvents" in text
    assert "renderBodyRhythm" in text
    assert "renderBodyHourList" in text
    assert "open-body-hour" in text
    assert "tr-body-strip-separator" in text
    assert "tr-body-rhythm-grid" in text
    assert "segment = null" in text
    assert "selectSegment(segment)" in text
    assert "seg.think, seg" in text
    assert ".join('\\n')" in text
    assert "Open full Body day" in text
    assert _apple_health_card_stream() in text

    body_events_css = text.split(".tr-body-events {", 1)[1].split("}", 1)[0]
    assert "left: 104px" not in body_events_css
    assert "left: 168px" in body_events_css

    body_card = text.split("function renderBodyContextCard", 1)[1].split(
        "function attachBodyWindowButtons", 1
    )[0]
    assert "chunk.markdown" not in body_card
    assert "Body context unavailable for this day window." in body_card
    assert "tr-body-context-facts" in body_card
    assert "renderBodyRhythm(payload)" in body_card

    iso_builder = text.split("function isoForDayMinute", 1)[1].split(
        "function minuteForIso", 1
    )[0]
    assert "localIsoString" in iso_builder
    assert "toISOString" not in iso_builder


def test_workspace_html_body_display_audit_fixes():
    workspace_html = Path(__file__).resolve().parents[1] / "workspace.html"

    text = workspace_html.read_text()

    # Signals and sources rows always carry a noun after the count — a bare
    # "Heart rate: 183" reads as a physiological value.
    assert "function bodyCountText" in text
    assert "${escapeHtml(signal.label)} · ${escapeHtml(bodyCountText(signal))}" in text
    assert "signal.label)}: " not in text
    assert "${escapeHtml(source.name)} · ${escapeHtml(bodyCountText(source))}" in text
    count_text = text.split("function bodyCountText", 1)[1].split(
        "function renderBodyPanelFooter", 1
    )[0]
    assert "'entry'" in count_text
    assert "'entries'" in count_text

    # Sources chips reuse the app chip idiom instead of the sparse pill.
    assert 'class="tr-signal-chip">${escapeHtml(source.name)}' in text
    assert 'class="tr-body-chip"' not in text

    # "Open full Body day" renders as a styled pill link, not a default
    # blue-underline anchor, and sits inside a styled actions row.
    assert 'class="tr-body-link"' in text
    pill_block = text.split(".tr-body-strip-button,", 1)[1].split("}", 1)[0]
    assert ".tr-body-link {" in pill_block
    link_block = text.split(".tr-body-link {", 2)[2].split("}", 1)[0]
    assert "text-decoration: none" in link_block
    assert ".tr-body-link:focus-visible" in text
    assert "function renderBodyPanelFooter" in text
    footer = text.split("function renderBodyPanelFooter", 1)[1].split(
        "function renderBodyPanel(", 1
    )[0]
    assert "tr-body-context-actions" in footer
    assert "tr-body-link" in footer

    # Truncated signals surface the remainder as a muted link into Body.
    assert "more in Body" in text
    assert "tr-body-panel-more" in text

    # The closed docked panel removes focusable children from the tab order.
    # (The first ".tr-body-panel {" occurrence is the shared surface selector
    # list; the standalone panel block is the second.)
    panel_css = text.split(".tr-body-panel {", 2)[2].split("}", 1)[0]
    assert "visibility: hidden" in panel_css
    assert "visibility" in panel_css.split("transition:", 1)[1]
    visible_css = text.split(".tr-body-panel.visible {", 1)[1].split("}", 1)[0]
    assert "visibility: visible" in visible_css

    # Body lane blocks stay inside the 200px day column (168 + 24 = 192).
    body_events_css = text.split(".tr-body-events {", 1)[1].split("}", 1)[0]
    assert "width: 24px" in body_events_css
    body_event_css = text.split(".tr-body-event {", 1)[1].split("}", 1)[0]
    assert "width: 24px" in body_event_css
    assert "width: 40px" not in body_event_css

    # Legend names the two body block kinds rather than one ambiguous "body"
    # dot, and the import dot is distinguishable from sleep-blue.
    legend = text.split('class="tr-timeline-legend"', 1)[1].split("</div>", 1)[0]
    assert ">sleep</span>" in legend
    assert ">workout</span>" in legend
    assert ">body</span>" not in legend
    assert ">import</span>" in legend
    assert "#93c5fd" not in legend

    # Events-only windows still render a minimal strip from event counts.
    assert "function bodyEventParts" in text
    strip = text.split("function renderBodyStrip", 1)[1].split(
        "function renderBodyContextCard", 1
    )[0]
    assert "bodyEventParts(payload)" in strip
    assert "payload?.has_data" in strip
    event_parts = text.split("function bodyEventParts", 1)[1].split(
        "function renderBodyStrip", 1
    )[0]
    assert "sleep session" in event_parts


def test_workspace_html_body_panel_short_window_fixes():
    workspace_html = Path(__file__).resolve().parents[1] / "workspace.html"

    text = workspace_html.read_text()

    # One heading for the day-rhythm block: the panel section supplies the
    # "Day rhythm" heading and suppresses the block's inner title — no
    # RHYTHM + DAY RHYTHM stutter.
    panel = text.split("function renderBodyPanel(", 1)[1].split(
        "function closeBodyPanel", 1
    )[0]
    assert "renderBodyPanelSection('Day rhythm', rhythm)" in panel
    assert "renderBodyPanelSection('Rhythm'" not in text
    assert "title: false" in panel
    rhythm_fn = text.split("function renderBodyRhythm", 1)[1].split(
        "function renderBodyPanelSection", 1
    )[0]
    assert "options.title === false" in rhythm_fn

    # Span gates: the hourly strip renders only for day-scale windows, and
    # hourly rows only when the window would produce at least two rows.
    assert "BODY_RHYTHM_STRIP_MIN_HOURS = 6" in text
    assert "BODY_HOURLY_ROWS_MIN_HOURS = 1" in text
    span_fn = text.split("function bodyWindowSpanHours", 1)[1].split(
        "function renderBodyRhythm", 1
    )[0]
    assert "payload?.from" in span_fn
    assert "payload?.to" in span_fn
    assert "spanHours >= BODY_RHYTHM_STRIP_MIN_HOURS" in rhythm_fn
    assert "spanHours > BODY_HOURLY_ROWS_MIN_HOURS" in rhythm_fn

    # A fully suppressed rhythm returns '' so no empty section heading can
    # render (renderBodyPanelSection drops sections with empty content).
    assert "if (!showStrip && !showRows) return '';" in rhythm_fn
    section_fn = text.split("function renderBodyPanelSection", 1)[1].split(
        "function bodyCountText", 1
    )[0]
    assert "if (!innerHtml) return '';" in section_fn

    # Signals never repeat rows already shown as events: filtered by event
    # label at render time, case-insensitive, no hardcoded workout names.
    assert "const eventLabels = new Set((payload.events || [])" in panel
    assert "!eventLabels.has(String(signal.label || '').trim().toLowerCase())" in panel

    # Panel sections read Brief -> Day rhythm -> Events -> Sources -> Signals.
    positions = [
        panel.index("renderBodyPanelSection('Brief'"),
        panel.index("renderBodyPanelSection('Day rhythm'"),
        panel.index("renderBodyPanelSection('Events'"),
        panel.index("renderBodyPanelSection('Sources'"),
        panel.index("renderBodyPanelSection('Signals'"),
    ]
    assert positions == sorted(positions)


def test_workspace_html_day_lane_hit_target_and_selection_rail_sync():
    workspace_html = Path(__file__).resolve().parents[1] / "workspace.html"

    text = workspace_html.read_text()

    # Tiny day-lane segments (a 5-minute import renders ~2px tall) keep a
    # minimum pointer target via a transparent ::after extension, so the
    # rendered timeline geometry itself is not distorted.
    hit_css = text.split(".tr-seg::after {", 1)[1].split("}", 1)[0]
    assert "content: ''" in hit_css
    assert "min-height: 14px" in hit_css
    assert "height: 100%" in hit_css

    # Selecting a segment (pointer or keyboard) recenters the detail zoom
    # range to contain it — the rail can never describe a different window
    # than the selected segment.
    select_fn = text.split("function selectSegment", 1)[1].split(
        "function navigateSegment", 1
    )[0]
    assert "segStart < range.start || segEnd > range.end" in select_fn
    assert "renderTimeline();" in select_fn
    assert "updateZoom();" in select_fn

    # The containment logic lives in selectSegment alone — callers do not
    # carry duplicated recenter blocks.
    assert text.count("segStart < range.start || segEnd > range.end") == 1


def test_workspace_html_body_panel_focus_management():
    workspace_html = Path(__file__).resolve().parents[1] / "workspace.html"

    text = workspace_html.read_text()

    # Opening the docked Body panel places keyboard focus on its close
    # button (mirroring the screenshot modal), after capturing the trigger.
    open_fn = text.split("function openBodyPanelSurface", 1)[1].split(
        "function renderBodyPanel(", 1
    )[0]
    assert "bodyPanelTrigger = document.activeElement" in open_fn
    assert "bodyPanelClose.focus()" in open_fn

    # Every open path routes through the focus-managing helper: the two
    # renderBodyPanel branches and the openBodyWindowFromIso loading state.
    assert text.count("openBodyPanelSurface();") == 3
    assert text.count("bodyPanel.classList.add('visible')") == 1

    # Closing returns focus to the triggering element when still attached.
    close_fn = text.split("function closeBodyPanel", 1)[1].split(
        "bodyPanelClose.addEventListener", 1
    )[0]
    assert "document.contains(bodyPanelTrigger)" in close_fn
    assert "bodyPanelTrigger.focus()" in close_fn

    # The close button activates on explicit Enter/Space keydown (belt and
    # braces over native button activation, which some automation skips),
    # and Escape closes a visible panel unless the image modal is open.
    assert text.count("bodyPanelClose.addEventListener('keydown'") == 1
    keydown_handler = text.split("bodyPanelClose.addEventListener('keydown'", 1)[
        1
    ].split("});", 1)[0]
    assert "e.key === 'Enter' || e.key === ' '" in keydown_handler
    assert "closeBodyPanel()" in keydown_handler
    escape_branch = text.split(
        "e.key === 'Escape' && bodyPanel.classList.contains('visible')", 1
    )[1].split("}", 1)[0]
    assert "closeBodyPanel();" in escape_branch
    assert "trImageModal" in escape_branch


def test_workspace_html_owner_copy_folds_named_transcript_literals():
    workspace_html = Path(__file__).resolve().parents[1] / "workspace.html"

    text = workspace_html.read_text()

    expected_literals = (
        "window.SurfaceState.loading({ text: 'loading body context…' })",
        "window.SurfaceState.loading({ text: 'loading segment…' })",
        "renderSignalSection(\n        'location',",
        "renderSignalSection(\n        'glasses battery',",
        "renderSignalSection('photos and button presses', photoRows)",
        "renderSignalSection(\n        'device capabilities',",
        "renderSignalSection(\n        'calendar snapshot',",
        '<span class="sr-only">audio: </span>',
        '<span class="sr-only">screen: </span>',
        "current raw media size: ${formatSize(totalBytes)}",
    )
    for literal in expected_literals:
        assert literal in text

    retired_literals = (
        "Loading body context...",
        "Loading segment...",
        "'Location'",
        "'Glasses Battery'",
        "'Photo And Button Events'",
        "'Device Capabilities'",
        "'Calendar Snapshot'",
        '<span class="sr-only">Audio: </span>',
        '<span class="sr-only">Screen: </span>',
        "Current raw media size:",
    )
    for literal in retired_literals:
        assert literal not in text

    assert "Loading screen entries..." in text
    assert "loading screen entries..." in text
    assert "Current time" in text


def test_workspace_html_speaker_copy_identifiers_are_referenced():
    workspace_html = Path(__file__).resolve().parents[1] / "workspace.html"

    text = workspace_html.read_text()

    assert "let TR_COPY = {};" in text
    assert "TR_COPY = data.transcripts_copy || {};" in text
    for name in transcripts_copy_payload():
        assert f"TR_COPY.{name} || ''" in text


def test_workspace_html_speaker_picker_markup_and_data_contract():
    workspace_html = Path(__file__).resolve().parents[1] / "workspace.html"

    text = workspace_html.read_text()

    render_slot = text.split("function renderSpeakerSlot", 1)[1].split(
        "function loadKnownSpeakers",
        1,
    )[0]
    render_timeline = text.split("function renderSegmentTimeline", 1)[1].split(
        "targetEl.innerHTML = html;",
        1,
    )[0]
    assert render_slot.count('data-action="speaker-picker"') == 1
    assert render_timeline.count("data-speaker-slot") == 1
    for attr in (
        "data-sentence-id",
        "data-speaker-source",
        "data-current-speaker",
        "data-segment-key",
        "data-stream",
    ):
        assert attr in text
    assert "Number(trigger.dataset.sentenceId)" in text
    assert "source: trigger.dataset.speakerSource || ''" in text
    assert "'/app/speakers/api/speakers/known'" in text
    assert "payload?.speakers" in text
    assert "payload.success" not in text
    assert "voices.length > 7" in text
    assert 'data-speaker-handoff="statement"' in text
    assert "function speakerStatementHandoffUrl(trigger)" in text
    assert "return `/app/speakers?${query}`;" in text
    for param in (
        "'voice_day'",
        "'voice_stream'",
        "'voice_segment_key'",
        "'voice_source'",
        "'voice_sentence_id'",
    ):
        assert param in text
    assert "encodeURIComponent(value)" in text
    handoff_branch = text.split(
        "if (option.dataset.speakerHandoff === 'statement')",
        1,
    )[1].split("const target = {", 1)[0]
    assert "navigateSpeakerStatementHandoff(trigger);" in handoff_branch
    for mutating_route in (
        "assign-attribution",
        "correct-attribution",
        "owner/tag-cli",
        "propagate-correction",
    ):
        assert mutating_route not in handoff_branch
    assert TR_SPEAKER_SOMEONE_ELSE == "someone else…"
    assert "TR_SPEAKER_SOMEONE_ELSE: TR_COPY.TR_SPEAKER_SOMEONE_ELSE || ''" in text
    assert "someone_else" in text
    assert "speaker-picker-create" not in text
    assert "create_new" not in text


def test_workspace_html_speaker_picker_focus_and_touch_targets():
    workspace_html = Path(__file__).resolve().parents[1] / "workspace.html"

    text = workspace_html.read_text()

    assert ".tr-speaker-button {" in text
    assert ".tr-speaker-picker-option {" in text
    assert text.count("min-height: var(--touch-min)") >= 5
    assert text.count("min-width: var(--touch-min)") >= 5
    assert "function handleSpeakerPickerKeydown" in text
    assert "if (e.key === 'Escape')" in text
    assert "function trapSpeakerPickerTab" in text
    assert "speakerPickerFocusable(popover)" in text
    assert "trigger.focus();" in text
    assert (
        "document.addEventListener('pointerdown', handleSpeakerPickerOutsidePointer, true)"
        in text
    )
    assert (
        "document.removeEventListener('pointerdown', handleSpeakerPickerOutsidePointer, true)"
        in text
    )


def test_workspace_html_speaker_dispatch_and_local_rerender_contract():
    workspace_html = Path(__file__).resolve().parents[1] / "workspace.html"

    text = workspace_html.read_text()

    assert "'/app/speakers/api/correct-attribution'" in text
    assert "'/app/speakers/api/assign-attribution'" in text
    assert "'/app/speakers/api/owner/tag-cli'" in text
    assert "body.new_speaker = target.entityId;" in text
    assert "body.speaker = target.entityId;" in text
    assert "target.isOwner" in text
    owner_branch = text.split("if (target.isOwner)", 1)[1].split(
        "} else if (hasCurrentSpeaker)",
        1,
    )[0]
    assert "body.speaker" not in owner_branch
    assert "slot.innerHTML = renderSpeakerSlot(chunk" in text
    assert "renderLoadedSegmentData(data, activeTab)" not in text
    assert "loadSegmentContent(selectedSegment" not in text
    assert "item.speaker_actionable !== true" in text
    assert "result?.status === 'already_correct'" in text
    assert "err.reasonCode === 'speaker_voiceprint_busy'" in text
    assert "err.reasonCode === 'speaker_labels_busy'" in text
    assert "err.reasonCode === 'speaker_owner_voice_too_close'" in text
    assert "err.reasonCode === 'speaker_owner_identity_required'" in text


def test_workspace_html_speaker_propagation_strip_uses_response_offer():
    workspace_html = Path(__file__).resolve().parents[1] / "workspace.html"

    text = workspace_html.read_text()

    assert "function renderSpeakerPropagationOffer" in text
    propagation_fn = text.split("function renderSpeakerPropagationOffer", 1)[1].split(
        "function applySpeakerSuccess",
        1,
    )[0]
    assert "result?.propagation_offer" in propagation_fn
    assert "offer.request" in propagation_fn
    assert "commit: true" in propagation_fn
    assert "'/app/speakers/api/propagate-correction'" in propagation_fn
    assert "preview" not in propagation_fn
    assert "modal" not in propagation_fn.lower()


def test_workspace_html_speaker_rendering_states():
    workspace_html = Path(__file__).resolve().parents[1] / "workspace.html"

    text = workspace_html.read_text()

    assert "label.confidence_state === 'unknown'" in text
    assert "label.confidence_state === 'high'" in text
    assert "TR_COPY.TR_SPEAKER_HEDGE_PROBABLE || ''" in text
    assert "TR_COPY.TR_SPEAKER_HEDGE_MAYBE || ''" in text
    assert "tr-speaker-dot-high" in text
    assert "tr-speaker-dot-medium" in text
    assert "const displayName = sl.is_owner ? 'You'" not in text
    assert "Speaker 1:" not in text


def test_workspace_html_timeline_rail_vertical_math_contract():
    workspace_html = Path(__file__).resolve().parents[1] / "workspace.html"

    text = workspace_html.read_text()

    header_value = int(_js_const_expr(text, "HEADER_HEIGHT"))
    legend_height_expr = _js_const_expr(text, "LEGEND_HEIGHT")
    assert legend_height_expr == "30"
    legend_height = int(legend_height_expr)
    zoom_bottom = int(_js_const_expr(text, "ZOOM_BOTTOM"))
    day_padding_expr = _js_const_expr(text, "DAY_PADDING")
    zoom_padding_expr = _js_const_expr(text, "ZOOM_PADDING")

    assert [token.strip() for token in day_padding_expr.split("+")] == [
        "HEADER_HEIGHT",
        "LEGEND_HEIGHT",
    ]
    assert [token.strip() for token in zoom_padding_expr.split("+")] == [
        "HEADER_HEIGHT",
        "ZOOM_BOTTOM",
    ]
    assert re.search(r"\bPADDING\b", text) is None

    timeline_css = _css_rule(text, ".tr-timeline")
    legend_var_lines = [
        line.strip()
        for line in timeline_css.splitlines()
        if line.strip().startswith("--tr-legend-h:")
    ]
    assert len(legend_var_lines) == 1
    legend_css_value = legend_var_lines[0].split(":", 1)[1].split(";", 1)[0].strip()
    assert legend_css_value.endswith("px")
    assert int(legend_css_value.removesuffix("px")) == legend_height

    render_timeline = _slice_between(
        text, "function renderTimeline", "function buildZoomGrid"
    )
    assert (
        "selWrap.style.top = (HEADER_HEIGHT + y(range.start)) + 'px';"
        in render_timeline
    )

    now_marker = _slice_between(
        text, "updateNowPosition = function()", "setInterval(updateNowPosition"
    )
    assert "marker.style.top = (HEADER_HEIGHT + y(nowMin)) + 'px';" in now_marker

    click_handler = _slice_between(text, "timeline.addEventListener('click'", "});")
    assert "const py = e.clientY - box.top - HEADER_HEIGHT;" in click_handler

    day_rail_selectors = (
        ".tr-grid",
        ".tr-labels",
        ".tr-segments",
        ".tr-body-events",
    )
    zoom_rail_selectors = (
        ".tr-zoom-labels",
        ".tr-zoom-grid",
        ".tr-zoom-segments",
    )

    for selector in day_rail_selectors + zoom_rail_selectors:
        css_block = _css_rule(text, selector)
        top_lines = [
            line.strip()
            for line in css_block.splitlines()
            if line.strip().startswith("top:")
        ]
        assert len(top_lines) == 1, f"{selector} must define exactly one top inset"
        top_inset = int(top_lines[0].split("top:", 1)[1].split("px", 1)[0])
        assert top_inset == header_value, (
            f"{selector} top inset {top_inset}px != HEADER_HEIGHT {header_value}px"
        )

    for selector in day_rail_selectors:
        css_block = _css_rule(text, selector)
        bottom_lines = [
            line.strip()
            for line in css_block.splitlines()
            if line.strip().startswith("bottom:")
        ]
        assert len(bottom_lines) == 1, (
            f"{selector} must define exactly one bottom inset"
        )
        assert bottom_lines[0] == "bottom: var(--tr-legend-h, 12px);"

    for selector in zoom_rail_selectors:
        css_block = _css_rule(text, selector)
        bottom_lines = [
            line.strip()
            for line in css_block.splitlines()
            if line.strip().startswith("bottom:")
        ]
        assert len(bottom_lines) == 1, (
            f"{selector} must define exactly one bottom inset"
        )
        bottom = int(bottom_lines[0].split("bottom:", 1)[1].split("px", 1)[0])
        assert bottom == zoom_bottom

    update_zoom = _slice_between(text, "function updateZoom", "const rangeLen")
    load_day_height = _slice_between(
        text,
        "// Recalculate pixels-per-minute with new bounds",
        "// Set initial selection range within bounds",
    )
    init_transcripts = _slice_between(
        text, "function initTranscripts", "// Handle browser back/forward"
    )
    assert update_zoom.count("zoom.clientHeight - ZOOM_PADDING") == 1
    assert load_day_height.count("timeline.clientHeight - DAY_PADDING") == 1
    assert init_transcripts.count("timeline.clientHeight - DAY_PADDING") == 1
    assert "zoom.clientHeight - DAY_PADDING" not in text
    assert "timeline.clientHeight - ZOOM_PADDING" not in text


def test_workspace_html_timeline_legend_band_contract():
    workspace_html = Path(__file__).resolve().parents[1] / "workspace.html"

    text = workspace_html.read_text()

    legend_css = _css_rule(text, ".tr-timeline-legend")
    assert "height: var(--tr-legend-h);" in legend_css
    assert "display: flex;" in legend_css
    assert "flex-wrap: wrap;" in legend_css
    assert "align-content: center;" in legend_css
    assert "box-sizing: border-box;" in legend_css
    assert "column-gap: 8px;" in legend_css
    assert "row-gap: 0;" in legend_css
    assert "padding: 0 4px;" in legend_css
    assert "line-height: 14px;" in legend_css
    assert "background: #fff;" in legend_css
    assert "height: 12px;" not in legend_css
    assert "align-items: center;" not in legend_css
    assert not any(line.strip().startswith("gap:") for line in legend_css.splitlines())
    legend_z_index_lines = [
        line.strip()
        for line in legend_css.splitlines()
        if line.strip().startswith("z-index:")
    ]
    assert legend_z_index_lines == ["z-index: 3;"]

    label_css = _css_rule(text, ".tr-labels")
    assert not any(
        line.strip().startswith("z-index:") for line in label_css.splitlines()
    )

    dot_css = _css_rule(text, ".tr-legend-dot")
    assert "vertical-align: baseline;" in dot_css
    assert "position: relative;" in dot_css
    assert "top: 1px;" in dot_css
    assert "vertical-align: middle;" not in dot_css

    font_bump = _slice_between(text, "\nbody.presentation-mode .tr-timeline-label", "}")
    assert "body.presentation-mode .tr-timeline-legend" not in text
    assert ".tr-timeline-legend" not in font_bump
    assert "body.presentation-mode .tr-zoom-timeline-label" in font_bump

    selection_css = _css_rule(text, ".tr-sel-wrap")
    z_index_lines = [
        line.strip()
        for line in selection_css.splitlines()
        if line.strip().startswith("z-index:")
    ]
    assert z_index_lines == ["z-index: 4;"]


def test_workspace_html_last_timeline_label_clearance_is_one_property():
    workspace_html = Path(__file__).resolve().parents[1] / "workspace.html"

    text = workspace_html.read_text()

    label_last_css = _css_rule(text, ".tr-labels .tr-label:last-child")
    declarations = [
        line.strip() for line in label_last_css.splitlines() if line.strip()
    ]
    assert declarations == ["transform: translateY(-100%);"], (
        "last timeline label clearance must stay a one-property transform-only fix"
    )


def test_workspace_html_base_timeline_label_transform_stays_centered():
    workspace_html = Path(__file__).resolve().parents[1] / "workspace.html"

    text = workspace_html.read_text()

    label_css = _css_rule(text, ".tr-label")
    transform_lines = [
        line.strip()
        for line in label_css.splitlines()
        if line.strip().startswith("transform:")
    ]
    assert transform_lines == ["transform: translateY(-50%);"]


def test_workspace_html_bottom_label_last_child_population_contract():
    workspace_html = Path(__file__).resolve().parents[1] / "workspace.html"

    text = workspace_html.read_text()

    failure_message = (
        "buildGrid() must remain the sole #trLabels populator that appends in "
        "ascending hour order; if anything is ever appended after it, the "
        ":last-child rule silently retargets and an arbitrary mid-axis label "
        "gets offset from its neighbors - this is the change's only "
        "silent-failure surface"
    )
    assert text.count("labels.appendChild(") == 1, failure_message
    assert text.count("labels.innerHTML = '';") == 1, failure_message


def test_workspace_html_last_label_clearance_depends_on_hour_snapping():
    workspace_html = Path(__file__).resolve().parents[1] / "workspace.html"

    text = workspace_html.read_text()

    timeline_bounds = _slice_between(
        text, "function computeTimelineBounds", "function markdownOnlySegments"
    )
    assert "Math.floor((minTime - BUFFER) / 60) * 60" in timeline_bounds
    assert "Math.ceil((maxTime + BUFFER) / 60) * 60" in timeline_bounds


def test_workspace_html_presentation_label_block_keeps_transform_offset():
    workspace_html = Path(__file__).resolve().parents[1] / "workspace.html"

    text = workspace_html.read_text()

    presentation_label_block = _slice_between(
        text, "\nbody.presentation-mode .tr-label,", "}"
    )
    assert "transform:" not in presentation_label_block


def test_workspace_html_zoom_label_has_no_last_child_clearance_variant():
    workspace_html = Path(__file__).resolve().parents[1] / "workspace.html"

    text = workspace_html.read_text()

    assert ":last-child" not in _css_rule(text, ".tr-zoom-label")
    assert ".tr-zoom-label:last-child" not in text


def test_workspace_html_card_grid_and_panel_overflow_contract():
    workspace_html = Path(__file__).resolve().parents[1] / "workspace.html"

    text = workspace_html.read_text()

    card_css = _css_rule(text, ".tr-card")
    assert (
        "grid-template-columns: var(--tr-day-col) var(--tr-zoom-col) minmax(0, 1fr);"
    ) in card_css
    assert (
        "grid-template-columns: var(--tr-day-col) var(--tr-zoom-col) 1fr;"
        not in card_css
    )

    panel_css = _css_rule(text, ".tr-panel")
    assert "overflow-x: auto;" in panel_css
    assert "overflow-x: hidden;" not in panel_css


def test_workspace_html_mobile_media_condition_contract():
    workspace_html = Path(__file__).resolve().parents[1] / "workspace.html"

    text = workspace_html.read_text()

    width_conditions = [
        _norm(condition)
        for condition in _media_conditions(text)
        if re.search(r"\b(?:min|max)-width\s*:", condition)
    ]
    assert width_conditions == ["(max-width: 768px)"]


def test_workspace_html_mobile_card_content_grid_contract():
    workspace_html = Path(__file__).resolve().parents[1] / "workspace.html"

    text = workspace_html.read_text()
    _media_start, media_css = _mobile_media_block(text)
    normalized_media_css = _norm(media_css)

    assert (
        _norm(
            """
            .tr-card,
            body.has-app-bar .tr-card {
              grid-template-columns: var(--tr-day-col) minmax(0, 1fr);
              grid-template-rows: auto auto;
              min-height: 0;
            }
            """
        )
        in normalized_media_css
    )
    assert (
        _norm(
            """
            .tr-content {
              grid-column: 1 / -1;
              grid-row: 2;
            }
            """
        )
        in normalized_media_css
    )
    assert re.search(r"--tr-day-col\s*:", media_css) is None


def test_workspace_html_mobile_timeline_zoom_position_contract():
    workspace_html = Path(__file__).resolve().parents[1] / "workspace.html"

    text = workspace_html.read_text()
    _media_start, media_css = _mobile_media_block(text)
    normalized_media_css = _norm(media_css)

    assert (
        _norm(
            """
            .tr-timeline,
            body.has-app-bar .tr-timeline {
              grid-column: 1;
              grid-row: 1;
              position: relative;
              top: auto;
            """
        )
        in normalized_media_css
    )
    assert (
        _norm(
            """
            .tr-zoom,
            body.has-app-bar .tr-zoom {
              grid-column: 2;
              grid-row: 1;
              position: relative;
              top: auto;
            """
        )
        in normalized_media_css
    )
    assert "position: static" not in text


def test_workspace_html_mobile_app_bar_selector_contract():
    workspace_html = Path(__file__).resolve().parents[1] / "workspace.html"

    text = workspace_html.read_text()
    _media_start, media_css = _mobile_media_block(text)
    normalized_media_css = _norm(media_css)

    assert _norm(".tr-card, body.has-app-bar .tr-card {") in normalized_media_css
    assert (
        _norm(".tr-timeline, body.has-app-bar .tr-timeline {") in normalized_media_css
    )
    assert _norm(".tr-zoom, body.has-app-bar .tr-zoom {") in normalized_media_css


def test_workspace_html_mobile_media_order_contract():
    workspace_html = Path(__file__).resolve().parents[1] / "workspace.html"

    text = workspace_html.read_text()
    media_start, _media_css = _mobile_media_block(text)

    assert media_start > text.index("body.has-app-bar .tr-timeline,")
    assert media_start > text.index("body.presentation-mode .tr-card")


def test_workspace_html_tab_pill_spacing_contract():
    workspace_html = Path(__file__).resolve().parents[1] / "workspace.html"

    text = workspace_html.read_text()

    tabs_css = _css_rule(text, ".tr-tabs")
    assert "gap: 8px;" in tabs_css

    tab_css = _css_rule(text, ".tr-tab")
    assert "padding: 8px 12px;" in tab_css
    assert "margin: 0 -8px;" not in tab_css
    assert not any(line.strip().startswith("margin:") for line in tab_css.splitlines())


def test_workspace_html_zoom_rail_empty_state_contract():
    workspace_html = Path(__file__).resolve().parents[1] / "workspace.html"
    copy_py = Path(__file__).resolve().parents[1] / "copy.py"

    text = workspace_html.read_text()
    copy_text = copy_py.read_text()

    zoom_empty_css = _css_rule(text, ".tr-zoom-empty")
    assert "position: absolute;" in zoom_empty_css
    assert "inset: 0;" in zoom_empty_css
    assert "padding: 0 6px;" in zoom_empty_css
    assert "display: flex;" in zoom_empty_css
    assert "pointer-events: none;" in zoom_empty_css
    assert text.count('class="tr-zoom-empty"') == 1

    build_zoom_segments = _slice_between(
        text,
        "function buildZoomSegments",
        "function selectSegment(seg, updateHash = true, requestedTabId = null)",
    )
    empty_branch = _slice_between(
        build_zoom_segments, "if (filtered.length === 0) {", "return;"
    )
    assert "window.SurfaceState.empty" not in empty_branch
    emitted_text = _slice_between(empty_branch, 'class="tr-zoom-empty">', "</div>")
    assert emitted_text.strip()
    assert "TR_COPY" not in emitted_text
    assert "${" not in emitted_text
    assert "TR_COPY" not in build_zoom_segments

    retired_copy = "widen the time range or pick a different day"
    assert retired_copy not in text
    assert retired_copy not in copy_text
    assert ".tr-zoom-segments > .surface-state" not in text
