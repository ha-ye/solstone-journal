# SPDX-License-Identifier: AGPL-3.0-only
# Copyright (c) 2026 sol pbc

from pathlib import Path


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
    assert "import.apple_health" in text

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
