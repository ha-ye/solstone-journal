# SPDX-License-Identifier: AGPL-3.0-only
# Copyright (c) 2026 sol pbc

"""Tests for local speaker diarization."""

from __future__ import annotations

from pathlib import Path

import numpy as np
import pytest


class _Input:
    def __init__(self, name: str):
        self.name = name


class _Output:
    def __init__(self, name: str):
        self.name = name


class _WeSpeakerStubSession:
    def __init__(self, embeddings: list[np.ndarray]):
        self._embeddings = [emb.astype(np.float32) for emb in embeddings]
        self._idx = 0

    def get_inputs(self):
        return [_Input("features")]

    def get_outputs(self):
        return [_Output("embedding")]

    def run(self, _outputs, _inputs):
        idx = min(self._idx, len(self._embeddings) - 1)
        self._idx += 1
        return [self._embeddings[idx][None, :]]


def _dominant_log_probs(classes: np.ndarray) -> np.ndarray:
    log_probs = np.full((classes.shape[0], 7), -10.0, dtype=np.float32)
    log_probs[np.arange(classes.shape[0]), classes] = 0.0
    return log_probs


def _dummy_features(_audio_slice: np.ndarray) -> np.ndarray:
    return np.zeros((10, 80), dtype=np.float32)


def test_diarize_precomputed_logprobs_emits_one_indexed_int(monkeypatch):
    from solstone.observe.transcribe import diarize

    def fail_pyannote():
        raise AssertionError("precomputed logprobs should skip pyannote")

    monkeypatch.setattr(diarize, "_get_pyannote_session", fail_pyannote)
    monkeypatch.setattr(
        diarize,
        "_get_wespeaker_session",
        lambda: _WeSpeakerStubSession([np.ones(256, dtype=np.float32)]),
    )
    monkeypatch.setattr(diarize, "_wespeaker_features", _dummy_features)

    avg_log_probs = _dominant_log_probs(np.ones(589, dtype=np.int64))
    audio = np.zeros(10 * diarize.SAMPLE_RATE, dtype=np.float32)

    labels = diarize.diarize(
        Path("unused.wav"),
        [{"start": 1.0, "end": 2.0, "text": "hello"}],
        avg_log_probs=avg_log_probs,
        audio=audio,
    )

    assert labels == [1]
    assert isinstance(labels[0], int)
    assert labels[0] > 0


def test_find_intervals_filters_to_confident_single_speaker_runs():
    from solstone.observe.transcribe import diarize

    classes = np.concatenate(
        [
            np.full(40, 1, dtype=np.int64),
            np.full(40, 4, dtype=np.int64),
            np.zeros(40, dtype=np.int64),
            np.full(10, 2, dtype=np.int64),
            np.full(40, 1, dtype=np.int64),
            np.full(45, 3, dtype=np.int64),
        ]
    )
    avg_log_probs = _dominant_log_probs(classes)
    low_conf_start = 130
    low_conf_end = 170
    avg_log_probs[low_conf_start:low_conf_end] = -0.1
    avg_log_probs[low_conf_start:low_conf_end, 1] = 0.0

    intervals = diarize._find_intervals(
        avg_log_probs,
        audio_len_samples=10 * diarize.SAMPLE_RATE,
    )

    assert [interval[2] for interval in intervals] == [1, 3]
    assert all(end - start >= diarize.MIN_INTERVAL_S for start, end, _ in intervals)
    assert intervals[0][0] == pytest.approx(0.0)
    assert intervals[0][1] == pytest.approx(40 * diarize.WINDOW_S / 589)


def test_auto_k_clusters_well_separated_embeddings_by_invariant(monkeypatch):
    from solstone.observe.transcribe import diarize

    embs = np.zeros((6, 256), dtype=np.float32)
    embs[0, 0] = 1.0
    embs[1, 0] = 0.98
    embs[1, 2] = 0.02
    embs[2, 0] = 1.02
    embs[2, 2] = -0.02
    embs[3, 1] = 1.0
    embs[4, 1] = 0.98
    embs[4, 3] = 0.02
    embs[5, 1] = 1.02
    embs[5, 3] = -0.02

    def fake_ahc(embs_n: np.ndarray, k: int) -> np.ndarray:
        if k == 2:
            return (embs_n[:, 1] > embs_n[:, 0]).astype(np.int32)
        return (np.arange(len(embs_n)) % k).astype(np.int32)

    def fake_silhouette(_embs_n: np.ndarray, labels: np.ndarray) -> float:
        return 0.95 if len(set(labels.tolist())) == 2 else 0.10

    monkeypatch.setattr(diarize, "_ahc", fake_ahc)
    monkeypatch.setattr(diarize, "_silhouette", fake_silhouette)

    normalized = diarize._normalize_rows(embs)
    assert diarize._pick_k_silhouette(normalized, diarize.MAX_K) == 2

    labels = diarize._cluster_intervals(embs, None)

    assert len(set(labels.tolist())) == 2
    assert labels[0] == labels[1] == labels[2]
    assert labels[3] == labels[4] == labels[5]
    assert labels[0] != labels[3]


def test_get_pyannote_session_missing_asset_raises_file_not_found(
    monkeypatch, tmp_path
):
    from solstone.observe.transcribe import diarize

    monkeypatch.setattr(diarize, "PYANNOTE_MODEL_PATH", tmp_path / "missing.onnx")
    monkeypatch.setattr(diarize, "_pyannote_session", None)

    with pytest.raises(FileNotFoundError):
        diarize._get_pyannote_session()
