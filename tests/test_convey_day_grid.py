# SPDX-License-Identifier: AGPL-3.0-only
# Copyright (c) 2026 sol pbc

from __future__ import annotations

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
