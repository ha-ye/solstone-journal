# SPDX-License-Identifier: AGPL-3.0-only
# Copyright (c) 2026 sol pbc

from solstone.observe import extract as extract_module
from solstone.observe.extract import (
    _apply_category_caps,
    _fallback_select_frames,
    select_frames_for_extraction,
)


def _frame(frame_id: int, category: str | None, timestamp: float | None = None) -> dict:
    frame = {
        "frame_id": frame_id,
        "timestamp": float(frame_id) if timestamp is None else timestamp,
        "analysis": {},
    }
    if category is not None:
        frame["analysis"]["primary"] = category
    return frame


def _frames(count: int) -> list[dict]:
    return [_frame(frame_id, "code") for frame_id in range(1, count + 1)]


def test_apply_category_caps_semantics():
    categorized_frames = [
        _frame(1, "ignored"),
        _frame(2, "ignored"),
        _frame(3, "low_priority"),
        _frame(4, "low_priority"),
        _frame(5, "low_priority"),
        _frame(6, "normal_priority"),
        _frame(7, "high_priority"),
        _frame(8, "unknown"),
        _frame(9, None),
    ]
    selected_ids = [9, 8, 7, 6, 5, 4, 3, 2, 1]
    config_overrides = {
        "ignored": {"importance": "ignore"},
        "low_priority": {"importance": "low"},
        "normal_priority": {"importance": "normal"},
        "high_priority": {"importance": "high"},
    }

    assert _apply_category_caps(selected_ids, categorized_frames, config_overrides) == [
        3,
        4,
        6,
        7,
        8,
        9,
    ]


def test_fallback_select_frames_is_deterministic_and_spread():
    categorized_frames = _frames(30)

    result1 = _fallback_select_frames(categorized_frames, max_extractions=5)
    result2 = _fallback_select_frames(categorized_frames, max_extractions=5)

    assert result1 == result2
    assert len(result1) == 5
    assert 1 in result1
    assert 30 in result1

    timestamps = {frame["frame_id"]: frame["timestamp"] for frame in categorized_frames}
    selected_timestamps = sorted(timestamps[frame_id] for frame_id in result1)
    adjacent_gaps = [
        right - left
        for left, right in zip(selected_timestamps, selected_timestamps[1:])
    ]
    assert min(adjacent_gaps) >= 5


def test_fallback_select_frames_returns_all_when_under_max_and_empty():
    categorized_frames = _frames(3)

    assert _fallback_select_frames(categorized_frames, max_extractions=5) == [1, 2, 3]
    assert _fallback_select_frames([], max_extractions=5) == []


def test_select_frames_readds_first_frame_when_ignore_capped(monkeypatch):
    monkeypatch.setattr(
        extract_module,
        "_get_category_config",
        lambda: {"private": {"importance": "ignore"}},
    )
    categorized_frames = [
        _frame(1, "private"),
        _frame(2, "private"),
    ]

    result = select_frames_for_extraction(
        categorized_frames, max_extractions=5, categories=None
    )

    assert result == [1]


def test_select_frames_applies_caps_with_fallback_and_sorts(monkeypatch):
    monkeypatch.setattr(
        extract_module,
        "_get_category_config",
        lambda: {
            "private": {"importance": "ignore"},
            "low_priority": {"importance": "low"},
        },
    )
    categorized_frames = [
        _frame(1, "private"),
        _frame(2, "low_priority"),
        _frame(3, "low_priority"),
        _frame(4, "low_priority"),
        _frame(5, "normal_priority"),
        _frame(6, "high_priority"),
        _frame(7, "private"),
    ]

    result = select_frames_for_extraction(
        categorized_frames, max_extractions=10, categories=None
    )

    assert result == [1, 2, 3, 5, 6]
