# SPDX-License-Identifier: AGPL-3.0-only
# Copyright (c) 2026 sol pbc

from __future__ import annotations

import re
from pathlib import Path

WORKSPACE_PATH = Path(__file__).resolve().parents[1] / "workspace.html"
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


def _css_rule(source: str, selector: str) -> str:
    match = re.search(rf"{re.escape(selector)}\s*\{{(?P<body>[^}}]*)\}}", source)
    assert match is not None, f"missing CSS rule for {selector}"
    return match.group("body")


def _hex_to_rgb(color: str) -> tuple[float, float, float]:
    value = color.removeprefix("#")
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


def test_level_colors_meet_wcag_contrast():
    for level, foreground in LEVEL_FOREGROUNDS.items():
        assert _contrast_ratio(foreground, VIEWPORT_BACKGROUND) >= 4.5, level


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
    gutter = _css_rule(source, ".logs-line[data-hhmmss]::before")

    assert re.search(r"content:\s*attr\(data-hhmmss\)\s+\" \"\s*;", gutter)
    assert re.search(r"color:\s*#6b7280\s*;", gutter)
    assert re.search(r"font-family:\s*inherit\s*;", gutter)
    assert "logs-ts-gutter" not in source
    assert "line.dataset.hhmmss = formatLogTime(record.ts);" in source


def test_state_only_adds_today_cost_usd():
    source = _workspace_source()
    match = re.search(r"const state = \{(?P<body>.*?)\n\s*\};", source, re.DOTALL)
    assert match is not None

    keys = re.findall(
        r"^\s*([A-Za-z_][A-Za-z0-9_]*):", match.group("body"), re.MULTILINE
    )

    assert keys == EXPECTED_STATE_KEYS
    assert len(keys) == 32


def test_filter_handlers_preserve_collapsed_services():
    source = _workspace_source()

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
    source = _workspace_source()

    assert "if (state.logLevelFilter === 'error') return level === 'error';" in source
    assert (
        "if (state.logLevelFilter === 'warning') return level === 'error' || level === 'warning';"
        in source
    )
    assert (
        "if (state.logLevelFilter === 'info') return level === 'error' || level === 'warning' || level === 'info';"
        in source
    )
