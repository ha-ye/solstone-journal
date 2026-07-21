# SPDX-License-Identifier: AGPL-3.0-only
# Copyright (c) 2026 sol pbc

from pathlib import Path

from solstone.apps.transcripts.copy import (
    TR_SPEAKER_SOMEONE_ELSE,
    transcripts_copy_payload,
)
from solstone.think.importers import health_schema


def _apple_health_card_stream() -> str:
    return health_schema.health_card_stream(health_schema.SOURCE_APPLE_HEALTH)


def _slice_between(text: str, start: str, end: str) -> str:
    """Return the text between two anchors, failing if either is missing."""
    assert text.count(start) == 1, f"expected exactly one start anchor: {start}"
    start_idx = text.index(start) + len(start)
    assert end in text[start_idx:], f"missing end anchor after {start}: {end}"
    end_idx = text.index(end, start_idx)
    return text[start_idx:end_idx]


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

    header_lines = [
        line.strip()
        for line in text.splitlines()
        if line.strip().startswith("const HEADER_HEIGHT = ")
    ]
    assert len(header_lines) == 1
    header_value = int(
        header_lines[0].split("const HEADER_HEIGHT = ", 1)[1].split(";", 1)[0]
    )

    padding_lines = [
        line.strip()
        for line in text.splitlines()
        if line.strip().startswith("const PADDING = ")
    ]
    assert len(padding_lines) == 1
    padding_expr = padding_lines[0].split("const PADDING = ", 1)[1].split(";", 1)[0]
    padding_prefix = "HEADER_HEIGHT + "
    assert padding_expr.startswith(padding_prefix), (
        "PADDING must derive from HEADER_HEIGHT"
    )
    bottom_inset = int(padding_expr.split(padding_prefix, 1)[1])
    assert "const PADDING = 24" not in text

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

    rail_selectors = (
        ".tr-grid",
        ".tr-labels",
        ".tr-segments",
        ".tr-body-events",
        ".tr-zoom-labels",
        ".tr-zoom-grid",
        ".tr-zoom-segments",
    )

    for selector in rail_selectors:
        css_block = _slice_between(text, f"{selector} {{", "}")
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

        bottom_lines = [
            line.strip()
            for line in css_block.splitlines()
            if line.strip().startswith("bottom:")
        ]
        assert len(bottom_lines) == 1, (
            f"{selector} must define exactly one bottom inset"
        )
        bottom = int(bottom_lines[0].split("bottom:", 1)[1].split("px", 1)[0])
        assert bottom == bottom_inset, (
            f"{selector} bottom inset {bottom}px != PADDING - HEADER_HEIGHT "
            f"{bottom_inset}px"
        )

    assert "zoom.clientHeight - 24" not in text
