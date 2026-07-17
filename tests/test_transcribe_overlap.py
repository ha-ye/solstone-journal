# SPDX-License-Identifier: AGPL-3.0-only
# Copyright (c) 2026 sol pbc

"""Tests for pyannote overlap-fraction inference."""

from __future__ import annotations

import sys
import types

import numpy as np
import pytest

from solstone.observe.utils import SAMPLE_RATE


class _Input:
    def __init__(self, name: str):
        self.name = name


class _StubSession:
    def __init__(self, log_probs: np.ndarray | list[np.ndarray]):
        if isinstance(log_probs, list):
            self._log_probs = [item.astype(np.float32) for item in log_probs]
            self._repeat = False
        else:
            self._log_probs = [log_probs.astype(np.float32)]
            self._repeat = True
        self._idx = 0

    def get_inputs(self):
        return [_Input("input_values")]

    def run(self, _outputs, _inputs):
        if self._idx >= len(self._log_probs):
            if not self._repeat:
                raise AssertionError("unexpected pyannote run")
            idx = len(self._log_probs) - 1
        else:
            idx = self._idx
        self._idx += 1
        return [self._log_probs[idx][None, :, :]]


def _dominant_log_probs(classes: np.ndarray) -> np.ndarray:
    log_probs = np.full((classes.shape[0], 7), -10.0, dtype=np.float32)
    log_probs[np.arange(classes.shape[0]), classes] = 0.0
    return log_probs


def test_compute_overlap_fraction_silent_audio_returns_zero(monkeypatch):
    from solstone.observe.transcribe import overlap

    monkeypatch.setattr(
        overlap,
        "_get_overlap_session",
        lambda: _StubSession(_dominant_log_probs(np.zeros(589, dtype=np.int64))),
    )

    result = overlap.compute_overlap_fraction(
        np.zeros(12 * SAMPLE_RATE, dtype=np.float32)
    )

    assert result == 0.0


def test_compute_overlap_fraction_short_audio_padded(monkeypatch):
    from solstone.observe.transcribe import overlap

    monkeypatch.setattr(
        overlap,
        "_get_overlap_session",
        lambda: _StubSession(_dominant_log_probs(np.zeros(589, dtype=np.int64))),
    )

    result = overlap.compute_overlap_fraction(
        np.zeros(3 * SAMPLE_RATE, dtype=np.float32)
    )

    assert isinstance(result, float)
    assert result == 0.0


def test_compute_overlap_fraction_non_aligned_length(monkeypatch):
    from solstone.observe.transcribe import overlap

    monkeypatch.setattr(
        overlap,
        "_get_overlap_session",
        lambda: _StubSession(_dominant_log_probs(np.zeros(589, dtype=np.int64))),
    )

    audio = np.zeros(int(13.7 * SAMPLE_RATE), dtype=np.float32)
    result = overlap.compute_overlap_fraction(audio)

    assert result == 0.0


def test_compute_overlap_fraction_rejects_wrong_sample_rate():
    from solstone.observe.transcribe.overlap import compute_overlap_fraction

    with pytest.raises(ValueError, match="requires 16000 Hz audio"):
        compute_overlap_fraction(np.zeros(16000, dtype=np.float32), sample_rate=8000)


def test_get_overlap_session_loads_and_caches(monkeypatch, tmp_path):
    from solstone.observe import model_assets
    from solstone.observe.transcribe import overlap

    model = tmp_path / "seg.onnx"
    model.write_bytes(b"stub")
    constructions = []

    class _CountingSession:
        def __init__(self, *args, **kwargs):
            constructions.append((args, kwargs))

        def get_providers(self):
            return ["CPUExecutionProvider"]

    fake_ort = types.ModuleType("onnxruntime")
    fake_ort.InferenceSession = _CountingSession
    monkeypatch.setattr(overlap, "_overlap_session", None)
    monkeypatch.setattr(
        model_assets,
        "resolve_pyannote_segmentation_model",
        lambda: model,
    )
    monkeypatch.setitem(sys.modules, "onnxruntime", fake_ort)

    first = overlap._get_overlap_session()
    second = overlap._get_overlap_session()

    assert len(constructions) == 1
    assert first is second


def test_compute_overlap_fraction_uses_conditioned_formula(monkeypatch):
    from solstone.observe.transcribe import overlap

    classes = np.concatenate(
        [
            np.full(300, 1, dtype=np.int64),
            np.full(100, 4, dtype=np.int64),
            np.zeros(189, dtype=np.int64),
        ]
    )
    monkeypatch.setattr(
        overlap,
        "_get_overlap_session",
        lambda: _StubSession(_dominant_log_probs(classes)),
    )

    result = overlap.compute_overlap_fraction(
        np.zeros(10 * SAMPLE_RATE, dtype=np.float32)
    )

    assert result == pytest.approx(100 / 400)


def test_compute_overlap_and_logprobs_returns_fraction_and_logprobs(monkeypatch):
    from solstone.observe.transcribe import overlap

    classes = np.concatenate(
        [
            np.full(300, 1, dtype=np.int64),
            np.full(100, 4, dtype=np.int64),
            np.zeros(189, dtype=np.int64),
        ]
    )
    monkeypatch.setattr(
        overlap,
        "_get_overlap_session",
        lambda: _StubSession(_dominant_log_probs(classes)),
    )

    result = overlap.compute_overlap_and_logprobs(
        np.zeros(10 * SAMPLE_RATE, dtype=np.float32)
    )

    assert result.overlap_fraction == pytest.approx(100 / 400)
    assert result.avg_log_probs.shape == (589, 7)
    assert result.avg_log_probs.dtype == np.float32
    assert result.window_stats == (overlap.SpeakerWindowStats(400, 1, 100),)


def test_decide_speaker_evidence_solo_one_slot_returns_single():
    from solstone.observe.transcribe import overlap

    decision = overlap.decide_speaker_evidence(
        0.0,
        (overlap.SpeakerWindowStats(100, 1, 0),),
    )

    assert decision.speaker_evidence == "single"
    assert decision.multi_window_fraction == 0.0


def test_decide_speaker_evidence_slot_permuted_windows_return_single(monkeypatch):
    from solstone.observe.transcribe import overlap

    monkeypatch.setattr(
        overlap,
        "_get_overlap_session",
        lambda: _StubSession(
            [
                _dominant_log_probs(np.full(589, 1, dtype=np.int64)),
                _dominant_log_probs(np.full(589, 2, dtype=np.int64)),
            ]
        ),
    )

    result = overlap.compute_overlap_and_logprobs(
        np.zeros(12 * SAMPLE_RATE, dtype=np.float32)
    )
    decision = overlap.decide_speaker_evidence(
        result.overlap_fraction,
        result.window_stats,
    )

    assert len(result.window_stats) == 2
    assert {row.active_slot_count for row in result.window_stats} == {1}
    assert decision.speaker_evidence == "single"


def test_decide_speaker_evidence_turn_taking_returns_multi():
    from solstone.observe.transcribe import overlap

    decision = overlap.decide_speaker_evidence(
        0.0,
        (overlap.SpeakerWindowStats(100, 2, 0),),
    )

    assert decision.speaker_evidence == "multi"


def test_decide_speaker_evidence_overlap_heavy_returns_multi():
    from solstone.observe.transcribe import overlap

    decision = overlap.decide_speaker_evidence(
        0.8,
        (overlap.SpeakerWindowStats(100, 1, 80),),
    )

    assert decision.speaker_evidence == "multi"


def test_decide_speaker_evidence_all_silence_returns_none():
    from solstone.observe.transcribe import overlap

    decision = overlap.decide_speaker_evidence(
        0.0,
        (overlap.SpeakerWindowStats(0, 0, 0),),
    )

    assert decision.speaker_evidence == "none"
    assert decision.multi_window_fraction == 0.0


def test_decide_speaker_evidence_overlap_fraction_term_engages_multi():
    from solstone.observe.transcribe import overlap

    decision = overlap.decide_speaker_evidence(
        0.5,
        (overlap.SpeakerWindowStats(100, 1, 0),),
    )

    assert decision.speaker_evidence == "multi"


def test_decide_speaker_evidence_branch_four_overlap_ambiguity_returns_multi():
    from solstone.observe.transcribe import overlap

    decision = overlap.decide_speaker_evidence(
        0.0,
        (overlap.SpeakerWindowStats(100, 1, 5),),
    )

    assert decision.speaker_evidence == "multi"
