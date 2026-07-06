# SPDX-License-Identifier: AGPL-3.0-only
# Copyright (c) 2026 sol pbc

from __future__ import annotations

import logging

import numpy as np
import pytest

from solstone.observe.transcribe import sound_tags
from solstone.observe.utils import SAMPLE_RATE

SINE_WAVE_JSON = (
    '[{"index": 501, "score": 0.906418, "label": "Sine wave"}, '
    '{"index": 503, "score": 0.027465, "label": "Chirp tone"}, '
    '{"index": 394, "score": 0.01928, "label": "Busy signal"}]'
)


@pytest.fixture(autouse=True)
def reset_sound_tagger(monkeypatch: pytest.MonkeyPatch):
    monkeypatch.setattr(sound_tags, "_disabled", False)
    monkeypatch.setattr(sound_tags, "_lib", None)
    monkeypatch.setattr(sound_tags, "_ctx", None)
    monkeypatch.setattr(sound_tags, "_np", None)
    monkeypatch.setattr(sound_tags, "_ctypes", None)


def _buffer(seconds: int | float) -> np.ndarray:
    return np.zeros(int(seconds * SAMPLE_RATE), dtype=np.float32)


def test_parse_classify_json_reads_verbatim_sine_wave_bytes() -> None:
    assert sound_tags.parse_classify_json(SINE_WAVE_JSON) == {
        "Sine wave": 0.906418,
        "Chirp tone": 0.027465,
        "Busy signal": 0.01928,
    }


def test_parse_classify_json_keeps_max_for_duplicate_labels() -> None:
    raw = (
        '[{"score": 0.1, "label": "Music"}, '
        '{"score": 0.4, "label": "Music"}, '
        '{"score": 0.2, "label": "Speech"}]'
    )

    assert sound_tags.parse_classify_json(raw) == {"Music": 0.4, "Speech": 0.2}


def test_window_spans_full_windows() -> None:
    assert sound_tags._window_spans(20 * SAMPLE_RATE, SAMPLE_RATE) == [
        (0, 10 * SAMPLE_RATE),
        (10 * SAMPLE_RATE, 20 * SAMPLE_RATE),
    ]


def test_window_spans_includes_tail_exactly_one_second() -> None:
    assert sound_tags._window_spans(21 * SAMPLE_RATE, SAMPLE_RATE) == [
        (0, 10 * SAMPLE_RATE),
        (10 * SAMPLE_RATE, 20 * SAMPLE_RATE),
        (20 * SAMPLE_RATE, 21 * SAMPLE_RATE),
    ]


def test_window_spans_excludes_tail_under_one_second() -> None:
    assert sound_tags._window_spans(int(20.5 * SAMPLE_RATE), SAMPLE_RATE) == [
        (0, 10 * SAMPLE_RATE),
        (10 * SAMPLE_RATE, 20 * SAMPLE_RATE),
    ]


def test_window_spans_short_audio_has_no_windows() -> None:
    assert sound_tags._window_spans(int(0.99 * SAMPLE_RATE), SAMPLE_RATE) == []


def test_aggregate_max_floor_rounding_and_order() -> None:
    result = sound_tags.aggregate(
        [
            {
                "Speech": 0.1999,
                "Music": 0.1004,
                "Inside, small room": 0.1,
                "Alpha": 0.3334,
            },
            {
                "Speech": 0.2,
                "Music": 0.099,
                "Beta": 0.3334,
            },
        ]
    )

    assert list(result.items()) == [
        ("Alpha", 0.333),
        ("Beta", 0.333),
        ("Speech", 0.2),
        ("Music", 0.1),
    ]


def test_salience_excludes_silence_family() -> None:
    assert not sound_tags.is_salient({"Silence": 0.9, "White noise": 0.8})


def test_salience_includes_exact_threshold_for_non_silence() -> None:
    assert sound_tags.is_salient({"Music": 0.2})


def test_salience_rejects_below_threshold() -> None:
    assert not sound_tags.is_salient({"Music": 0.199})


def test_tag_audio_success_header_with_stub_classifier() -> None:
    calls: list[tuple[int, int]] = []
    responses = [
        {"Speech": 0.872},
        {"Music": 0.201},
        {"Silence": 0.5},
    ]

    def classify(window: np.ndarray, sample_rate: int) -> dict[str, float]:
        calls.append((len(window), sample_rate))
        return responses[len(calls) - 1]

    result = sound_tags.tag_audio(_buffer(21), SAMPLE_RATE, classify=classify)

    assert result == {
        "engine": "ced.cpp v0.1.0",
        "model": "ced-tiny-q8_0",
        "threshold": 0.1,
        "window_s": 10,
        "agg": "max",
        "windows": 3,
        "tags": {"Speech": 0.872, "Silence": 0.5, "Music": 0.201},
    }
    assert calls == [
        (10 * SAMPLE_RATE, SAMPLE_RATE),
        (10 * SAMPLE_RATE, SAMPLE_RATE),
        (SAMPLE_RATE, SAMPLE_RATE),
    ]


def test_tag_audio_partial_window_failures_count_successes(
    caplog: pytest.LogCaptureFixture,
) -> None:
    calls = 0

    def classify(_window: np.ndarray, _sample_rate: int) -> dict[str, float]:
        nonlocal calls
        calls += 1
        if calls == 2:
            raise RuntimeError("bad window")
        return {"Music": 0.3 + calls / 100}

    caplog.set_level(logging.DEBUG, logger=sound_tags.LOG.name)

    result = sound_tags.tag_audio(_buffer(21), SAMPLE_RATE, classify=classify)

    assert result is not None
    assert result["windows"] == 2
    assert result["tags"] == {"Music": 0.33}
    assert any(
        "sound tagger window 1 failed" in record.message for record in caplog.records
    )
    assert not [
        record for record in caplog.records if record.levelno >= logging.WARNING
    ]


def test_tag_audio_all_windows_fail_returns_none_with_one_warning(
    caplog: pytest.LogCaptureFixture,
) -> None:
    def classify(_window: np.ndarray, _sample_rate: int) -> dict[str, float]:
        raise RuntimeError("ced classify failed")

    caplog.set_level(logging.WARNING, logger=sound_tags.LOG.name)

    assert sound_tags.tag_audio(_buffer(20), SAMPLE_RATE, classify=classify) is None

    warnings = [
        record
        for record in caplog.records
        if record.levelno == logging.WARNING
        and "sound tagger failed for all windows" in record.message
    ]
    assert len(warnings) == 1


def test_disable_latch_logs_once(caplog: pytest.LogCaptureFixture) -> None:
    caplog.set_level(logging.WARNING, logger=sound_tags.LOG.name)

    sound_tags._disable("missing assets")
    sound_tags._disable("lib load failed")

    warnings = [
        record
        for record in caplog.records
        if record.levelno == logging.WARNING
        and "sound tagger disabled" in record.message
    ]
    assert len(warnings) == 1
    assert warnings[0].message == "sound tagger disabled: missing assets"


def test_tag_audio_no_labels_over_floor_returns_none_without_warning(
    caplog: pytest.LogCaptureFixture,
) -> None:
    def classify(_window: np.ndarray, _sample_rate: int) -> dict[str, float]:
        return {"Inside, small room": 0.1, "Quiet": 0.099}

    caplog.set_level(logging.WARNING, logger=sound_tags.LOG.name)

    assert sound_tags.tag_audio(_buffer(10), SAMPLE_RATE, classify=classify) is None
    assert caplog.records == []
