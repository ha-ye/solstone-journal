# SPDX-License-Identifier: AGPL-3.0-only
# Copyright (c) 2026 sol pbc

from __future__ import annotations

from pathlib import Path

from solstone.convey.day_grid import build_day_grid_payload


def test_day_grid_payload_splits_on_watermark_cut_line():
    counts = {
        "20400101": 2,
        "20400103": 5,
        "20400104": 7,
    }

    payload = build_day_grid_payload(counts, "20400103")

    assert set(payload) == {"coverage", "days", "pending"}
    assert payload["days"] == {
        "20400101": 2,
        "20400103": 5,
    }
    assert payload["pending"] == {"20400104": 7}
    assert payload["coverage"] == {"start": "20400101", "end": "20400104"}


def test_day_grid_payload_uses_watermark_not_rollup_membership():
    counts = {
        "20400201": 4,
        "20400205": 9,
    }

    payload = build_day_grid_payload(counts, "20400210")

    assert payload["days"] == counts
    assert payload["pending"] == {}
    assert payload["coverage"] == {"start": "20400201", "end": "20400205"}


def test_day_grid_payload_without_watermark_marks_everything_pending():
    counts = {
        "20400302": 8,
        "20400309": 1,
    }

    payload = build_day_grid_payload(counts, None)

    assert payload["days"] == {}
    assert payload["pending"] == counts
    assert payload["coverage"] == {"start": "20400302", "end": "20400309"}


def test_day_grid_payload_empty_counts_has_no_coverage():
    payload = build_day_grid_payload({}, "20400401")

    assert payload == {"coverage": None, "days": {}, "pending": {}}


def test_day_grid_payload_generalizes_to_apps_without_pending_days():
    counts = {
        "20400507": 3,
        "20400511": 6,
    }

    payload = build_day_grid_payload(counts, max(counts))

    assert payload["days"] == counts
    assert payload["pending"] == {}
    assert payload["coverage"] == {"start": "20400507", "end": "20400511"}


def test_day_grid_payload_uses_explicit_coverage_override():
    counts = {
        "20400610": 4,
        "20400612": 2,
    }

    payload = build_day_grid_payload(
        counts,
        max(counts),
        coverage={"start": "20400101", "end": "20401231"},
    )

    assert set(payload) == {"coverage", "days", "pending"}
    assert payload["coverage"] == {"start": "20400101", "end": "20401231"}
    assert payload["days"] == counts
    assert payload["pending"] == {}


def test_day_grid_payload_omits_activity_by_default():
    payload = build_day_grid_payload({"20400610": 4}, "20400610")

    assert set(payload) == {"coverage", "days", "pending"}


def test_day_grid_payload_includes_activity_when_supplied():
    payload = build_day_grid_payload(
        {"20400610": 4},
        "20400610",
        coverage={"start": "20400601", "end": "20400630"},
        activity={"20400610": "8"},
    )

    assert payload == {
        "coverage": {"start": "20400601", "end": "20400630"},
        "days": {"20400610": 4},
        "pending": {},
        "activity": {"20400610": 8},
    }


def test_day_grid_payload_activity_does_not_expand_inferred_coverage():
    payload = build_day_grid_payload(
        {"20400610": 4},
        "20400610",
        activity={"20400601": 2, "20400630": 3},
    )

    assert payload["coverage"] == {"start": "20400610", "end": "20400610"}
    assert payload["activity"] == {"20400601": 2, "20400630": 3}


def test_day_grid_payload_empty_counts_accepts_empty_corpus_coverage_override():
    payload = build_day_grid_payload(
        {},
        None,
        coverage={"start": "20400701", "end": "20400731"},
    )

    assert payload == {
        "coverage": {"start": "20400701", "end": "20400731"},
        "days": {},
        "pending": {},
    }


def test_day_grid_css_uses_neutral_pending_marker():
    css_path = (
        Path(__file__).resolve().parents[1]
        / "solstone"
        / "convey"
        / "static"
        / "day-grid.css"
    )

    assert "--warn" not in css_path.read_text(encoding="utf-8")


def test_day_grid_css_pins_presence_tone():
    css_path = (
        Path(__file__).resolve().parents[1]
        / "solstone"
        / "convey"
        / "static"
        / "day-grid.css"
    )
    css = css_path.read_text(encoding="utf-8")

    assert ".daygrid-cell--presence" in css
    assert ".daygrid-legend-swatch--presence" in css
    assert "color-mix(in srgb, var(--orange) 58%, var(--hairline))" in css
    assert "color-mix(in srgb, var(--orange) 72%, var(--orange-wash))" in css
