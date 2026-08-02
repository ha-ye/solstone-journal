# SPDX-License-Identifier: AGPL-3.0-only
# Copyright (c) 2026 sol pbc

from __future__ import annotations

import re
from pathlib import Path

WORKSPACE_PATH = Path(__file__).resolve().parents[1] / "workspace.html"
HEALTH_JS_PATH = Path(__file__).resolve().parents[1] / "static" / "health.js"
VIEWPORT_BACKGROUND = "#1e1e1e"
LEVEL_FOREGROUNDS = {
    "error": "#fca5a5",
    "warning": "#fcd34d",
    "info": "#d1d5db",
    "debug": "#9ca3af",
}
LEVEL_BORDERS = {
    "error": "3px solid #dc2626",
    "warning": "2px solid #d97706",
}
EXPECTED_STATE_KEYS = [
    "services",
    "connected",
    "crashed",
    "tasks",
    "health",
    "queues",
    "schedules",
    "agents",
    "agentCount",
    "imports",
    "think",
    "thinkActive",
    "sync",
    "serviceLogs",
    "logFollow",
    "logsCollapsed",
    "logLevelFilter",
    "logCollapsedServices",
    "logErrorCount",
    "logTotalCount",
    "lastLogTs",
    "lastAgentFinishTs",
    "todayCostUSD",
    "observers",
    "recentErrors",
    "agentErrorsOk",
    "recentErrorsFilter",
    "pendingRecentErrorsFocus",
    "pendingLogAnchor",
    "localHost",
    "deepLinkMode",
    "lastLogFilter",
    "lastEventTs",
]


def _workspace_source() -> str:
    return WORKSPACE_PATH.read_text(encoding="utf-8")


def _health_js_source() -> str:
    return HEALTH_JS_PATH.read_text(encoding="utf-8")


def _health_surface_source() -> str:
    return _workspace_source() + "\n" + _health_js_source()


def _css_rule(source: str, selector: str) -> str:
    match = re.search(
        rf"^{re.escape(selector)}\s*\{{(?P<body>[^}}]*)\}}",
        source,
        re.MULTILINE,
    )
    assert match is not None, f"no rule for {selector!r}"
    return match.group("body")


def _declared_color(body: str) -> str:
    match = re.search(r"color:\s*(#(?:[0-9a-fA-F]{6}|[0-9a-fA-F]{3}))\s*;", body)
    assert match is not None, f"no color declaration in {body!r}"
    return match.group(1)


def _hex_to_rgb(color: str) -> tuple[float, float, float]:
    value = color.removeprefix("#")
    if len(value) == 3:
        value = "".join(channel * 2 for channel in value)
    return tuple(int(value[index : index + 2], 16) / 255 for index in (0, 2, 4))


def _linearize(channel: float) -> float:
    if channel <= 0.03928:
        return channel / 12.92
    return ((channel + 0.055) / 1.055) ** 2.4


def _relative_luminance(color: str) -> float:
    red, green, blue = (_linearize(channel) for channel in _hex_to_rgb(color))
    return 0.2126 * red + 0.7152 * green + 0.0722 * blue


def _contrast_ratio(foreground: str, background: str) -> float:
    fg = _relative_luminance(foreground)
    bg = _relative_luminance(background)
    lighter = max(fg, bg)
    darker = min(fg, bg)
    return (lighter + 0.05) / (darker + 0.05)


def test_logs_header_wraps_and_collapsed_override_intact():
    source = _workspace_source()
    header = _css_rule(source, ".logs-header")
    collapsed = _css_rule(source, ".logs-card.logs-collapsed .logs-header")

    assert re.search(r"display:\s*flex\s*;", header)
    assert re.search(r"flex-wrap:\s*wrap\s*;", header)
    assert re.search(r"row-gap:\s*0\.75em\s*;", header)
    assert re.search(r"margin-bottom:\s*0\s*;", collapsed)


def test_logs_controls_wrap_and_collapsed_override_intact():
    source = _workspace_source()
    controls = _css_rule(source, ".logs-controls")
    collapsed = _css_rule(
        source,
        ".logs-card.logs-collapsed .logs-viewport,\n"
        ".logs-card.logs-collapsed .logs-controls",
    )

    assert re.search(r"display:\s*flex\s*;", controls)
    assert re.search(r"flex-wrap:\s*wrap\s*;", controls)
    assert re.search(r"row-gap:\s*0\.75em\s*;", controls)
    assert re.search(r"display:\s*none\s*;", collapsed)


def test_severity_label_ink_matches_status_pill():
    source = _workspace_source()
    body = _css_rule(source, ".severity-label")

    assert re.search(r"color:\s*#fff\s*;", body)
    assert "#6b7280" not in body
    for background in ("#16a34a", "#d97706", "#dc2626"):
        assert _contrast_ratio("#fff", background) >= 3.0
    for declaration in (
        "font-size: 0.75em",
        "letter-spacing: 0.05em",
        "margin-left: 0.5em",
    ):
        assert re.search(rf"{re.escape(declaration)}\s*;", body)


def test_h2_card_title_resets_user_agent_margin():
    source = _workspace_source()
    body = _css_rule(source, "h2.card-title")

    assert re.search(r"margin:\s*0\s*;", body)


def test_vitals_chip_constrains_width_and_breaks_long_identifiers():
    source = _workspace_source()
    body = _css_rule(source, ".vitals-chip")

    assert re.search(r"max-width:\s*100%\s*;", body)
    assert re.search(r"overflow-wrap:\s*anywhere\s*;", body)


def test_level_colors_meet_wcag_contrast():
    for level, foreground in LEVEL_FOREGROUNDS.items():
        assert _contrast_ratio(foreground, VIEWPORT_BACKGROUND) >= 4.5, level


def test_log_empty_placeholder_and_expanded_service_header_colors_meet_contrast():
    source = _workspace_source()

    for selector in (".logs-viewport:empty::before", ".logs-service-header"):
        body = _css_rule(source, selector)
        foreground = _declared_color(body)
        assert _contrast_ratio(foreground, VIEWPORT_BACKGROUND) >= 4.5, selector


def test_log_empty_placeholder_and_service_header_color_rules_are_surgical():
    source = _workspace_source()

    for selector in (".logs-viewport:empty::before", ".logs-service-header"):
        body = _css_rule(source, selector)
        assert re.search(r"color:\s*#9ca3af\s*;", body)
        assert "#6b7280" not in body

    assert source.count("#6b7280") >= 26
    collapsed = _css_rule(source, '.logs-service-header[aria-expanded="false"]')
    assert re.search(r"color:\s*#d1d5db\s*;", collapsed)


def test_level_foreground_colors_are_pinned():
    source = _workspace_source()

    for level, color in LEVEL_FOREGROUNDS.items():
        body = _css_rule(source, f".logs-line.logs-level-{level}")
        assert re.search(rf"color:\s*{re.escape(color)}\s*;", body)


def test_level_border_colors_are_pinned():
    source = _workspace_source()

    for level, border in LEVEL_BORDERS.items():
        body = _css_rule(source, f".logs-line.logs-level-{level}")
        assert re.search(rf"border-left:\s*{re.escape(border)}\s*;", body)

    for level in ("info", "debug"):
        body = _css_rule(source, f".logs-line.logs-level-{level}")
        assert "border-left" not in body


def test_logs_spacing_css_migrated():
    source = _workspace_source()
    viewport = _css_rule(source, ".logs-viewport")
    line = _css_rule(source, ".logs-line")

    assert re.search(r"font-size:\s*13px\s*;", viewport)
    assert re.search(r"background:\s*#1e1e1e\s*;", viewport)
    assert re.search(r"padding:\s*3px\s*;", line)
    assert re.search(r"line-height:\s*1\.6\s*;", line)


def test_timestamp_gutter_uses_pseudo_element():
    source = _workspace_source()
    js_source = _health_js_source()
    gutter = _css_rule(source, ".logs-line[data-hhmmss]::before")

    assert re.search(r"content:\s*attr\(data-hhmmss\)\s+\" \"\s*;", gutter)
    assert re.search(r"color:\s*#6b7280\s*;", gutter)
    assert re.search(r"font-family:\s*inherit\s*;", gutter)
    assert "logs-ts-gutter" not in _health_surface_source()
    assert "line.dataset.hhmmss = formatLogTime(record.ts);" in js_source


def test_state_only_adds_today_cost_usd():
    source = _health_js_source()
    match = re.search(r"const state = \{(?P<body>.*?)\n\s*\};", source, re.DOTALL)
    assert match is not None

    keys = re.findall(
        r"^\s*([A-Za-z_][A-Za-z0-9_]*):", match.group("body"), re.MULTILINE
    )

    assert keys == EXPECTED_STATE_KEYS
    assert len(keys) == 33


def test_filter_handlers_preserve_collapsed_services():
    source = _health_js_source()

    for marker in (
        "elements.logServiceFilter.addEventListener('change'",
        "elements.logLevelFilter.addEventListener('change'",
        "elements.logStreamFilter.addEventListener('change'",
    ):
        start = source.index(marker)
        handler = source[start : source.index("});", start) + len("});")]
        assert "logCollapsedServices.clear()" not in handler
        assert "logCollapsedServices =" not in handler


def test_level_filter_is_nested_severity_ladder():
    source = _health_js_source()

    assert "if (state.logLevelFilter === 'error') return level === 'error';" in source
    assert (
        "if (state.logLevelFilter === 'warning') return level === 'error' || level === 'warning';"
        in source
    )
    assert (
        "if (state.logLevelFilter === 'info') return level === 'error' || level === 'warning' || level === 'info';"
        in source
    )


def test_observe_best_available_fallback_is_labeled_and_finite():
    source = _health_surface_source()
    start = source.index("function updateObserve()")
    end = source.index("  // Update observers", start)
    body = source[start:end]

    assert "observeSourceNote" in source
    assert ".filter(([stream]) => !stream.endsWith('.tmux'))" in body
    assert (
        "const tmux = displayedStream ? state.observers.get(displayedStream + '.tmux') : null;"
        in body
    )
    assert "this host's stream isn't reporting yet — showing" in body
    assert "this host is unknown — showing" in body
    assert "updateObserveMode(primary);" in body
    assert "if (!state.localHost || !primary)" not in body
    assert "ch.statusEl.textContent = 'waiting...';" not in body


def test_log_follow_scroll_pause_and_gated_autoscroll_are_wired():
    source = _health_js_source()
    start = source.index("function renderLogs(newService, newRecord)")
    end = source.index("  // Event handlers by tract", start)
    render_logs = source[start:end]

    assert "let programmaticScroll = false;" in source
    assert "function isAtBottom(viewport, tol = 50)" in source
    assert "function scrollLogsToBottom(viewport = elements.logsViewport)" in source
    assert "elements.logsViewport.addEventListener('scroll'" in source
    assert "if (programmaticScroll) return;" in source
    assert "state.logFollow = false;" in source
    assert "elements.logFollowBtn.classList.remove('active');" in source
    assert "const atBottom = isAtBottom(viewport);" in render_logs
    assert "if (state.logFollow && atBottom)" in render_logs
    assert "const wasAtBottom = isAtBottom(viewport);" in render_logs
    assert "if (state.logFollow && wasAtBottom)" in render_logs
    assert "wasAtBottom || state.logFollow" not in render_logs


def test_log_stream_delay_notice_is_visible_near_logs():
    source = _health_surface_source()

    assert 'id="logsConnectionNote"' in source
    assert "log updates may be delayed" in source
    assert "elements.logsConnectionNote.classList.remove('hidden');" in source
