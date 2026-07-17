# SPDX-License-Identifier: AGPL-3.0-only
# Copyright (c) 2026 sol pbc

from __future__ import annotations

from solstone.convey.date_nav import build_date_nav_index


def test_date_nav_index_accumulates_int_counts():
    payload = build_date_nav_index(
        {
            "20400101": 2,
            "20400103": 5,
            "20400201": 7,
        }
    )

    assert payload == {
        "coverage": {"start": "20400101", "end": "20400201"},
        "months": {"204001": 7, "204002": 7},
    }


def test_date_nav_index_preserves_float_counts():
    payload = build_date_nav_index(
        {
            "20400301": 0.014,
            "20400302": 0.014,
            "20400401": 1.5,
        }
    )

    assert payload == {
        "coverage": {"start": "20400301", "end": "20400401"},
        "months": {"204003": 0.028, "204004": 1.5},
    }


def test_date_nav_index_skips_zero_totals():
    payload = build_date_nav_index(
        {
            "20400501": 0,
            "20400502": 3,
            "20400503": 0,
        }
    )

    assert payload == {
        "coverage": {"start": "20400502", "end": "20400502"},
        "months": {"204005": 3},
    }


def test_date_nav_index_skips_negative_totals():
    payload = build_date_nav_index(
        {
            "20400601": -5,
            "20400602": 4,
            "20400603": -1,
        }
    )

    assert payload == {
        "coverage": {"start": "20400602", "end": "20400602"},
        "months": {"204006": 4},
    }


def test_date_nav_index_empty_input_has_no_coverage():
    payload = build_date_nav_index({})

    assert payload == {"coverage": None, "months": {}}


def test_date_nav_index_single_day_input():
    payload = build_date_nav_index({"20400704": 9})

    assert payload == {
        "coverage": {"start": "20400704", "end": "20400704"},
        "months": {"204007": 9},
    }


def test_date_nav_index_spans_month_boundary():
    payload = build_date_nav_index(
        {
            "20400831": 2,
            "20400901": 3,
        }
    )

    assert payload == {
        "coverage": {"start": "20400831", "end": "20400901"},
        "months": {"204008": 2, "204009": 3},
    }


def test_date_nav_index_spans_year_boundary():
    payload = build_date_nav_index(
        {
            "20401231": 6,
            "20410101": 8,
        }
    )

    assert payload == {
        "coverage": {"start": "20401231", "end": "20410101"},
        "months": {"204012": 6, "204101": 8},
    }


def test_date_nav_index_coverage_is_independent_of_insertion_order():
    payload = build_date_nav_index(
        {
            "20420301": 1,
            "20420101": 1,
            "20420201": 1,
        }
    )

    assert payload["coverage"] == {"start": "20420101", "end": "20420301"}
