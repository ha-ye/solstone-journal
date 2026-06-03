# SPDX-License-Identifier: AGPL-3.0-only
# Copyright (c) 2026 sol pbc

from solstone.think.talents import _classify_degraded


def test_classify_degraded_marks_opted_in_low_output_tokens():
    assert _classify_degraded(
        {"output_tokens": 12},
        {"degradation_check": True},
    ) == {"reason": "near_empty", "output_tokens": 12}


def test_classify_degraded_marks_zero_output_tokens():
    assert _classify_degraded(
        {"output_tokens": 0},
        {"degradation_check": True},
    ) == {"reason": "near_empty", "output_tokens": 0}


def test_classify_degraded_ignores_high_output_tokens():
    assert (
        _classify_degraded(
            {"output_tokens": 5000},
            {"degradation_check": True},
        )
        is None
    )


def test_classify_degraded_ignores_unchecked_talent():
    assert _classify_degraded({"output_tokens": 12}, {}) is None


def test_classify_degraded_ignores_missing_usage():
    assert _classify_degraded(None, {"degradation_check": True}) is None


def test_classify_degraded_ignores_missing_output_tokens():
    assert _classify_degraded({"input_tokens": 10}, {"degradation_check": True}) is None


def test_classify_degraded_ignores_non_numeric_output_tokens():
    config = {"degradation_check": True}

    assert _classify_degraded({"output_tokens": "12"}, config) is None
    assert _classify_degraded({"output_tokens": None}, config) is None
    assert _classify_degraded({"output_tokens": True}, config) is None
