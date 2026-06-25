# SPDX-License-Identifier: AGPL-3.0-only
# Copyright (c) 2026 sol pbc

from pathlib import Path


def _function_body(text: str, name: str) -> str:
    start = text.index(f"function {name}(")
    nxt = text.index("\n  //", start + 1)
    return text[start:nxt]


def test_selected_pill_defers_to_css_for_contrast_safe_treatment():
    fn = _function_body(
        Path("solstone/convey/static/app.js").read_text(encoding="utf-8"),
        "applyPillStyle",
    )

    # The selected pill must not force white-on-color inline; the contrast-safe
    # .selected treatment (soft facet wash + dark ink + facet-hued border) lives
    # in app.css. Inline writes would override the stylesheet and re-break it.
    assert "pill.style.color = 'white';" not in fn
    assert "var(--status-inactive)" not in fn
    # It still marks selection via the class the CSS rule keys on.
    assert "pill.classList.add('selected');" in fn


def test_unselected_pill_reset_and_color_setters_unchanged():
    fn = _function_body(
        Path("solstone/convey/static/app.js").read_text(encoding="utf-8"),
        "applyPillStyle",
    )

    assert "pill.style.setProperty('--pill-color', facet.color);" in fn
    assert "pill.style.setProperty('--pill-bg', hexToRgba(facet.color, 0.2));" in fn
    assert (
        "pill.style.setProperty('--pill-bg-rest', hexToRgba(facet.color, 0.08));" in fn
    )
    assert "pill.style.background = '';" in fn
    assert "pill.style.color = '';" in fn
    assert "pill.style.borderColor = '';" in fn
