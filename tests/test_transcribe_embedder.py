# SPDX-License-Identifier: AGPL-3.0-only
# Copyright (c) 2026 sol pbc

"""Tests for the bundled WeSpeaker embedder."""

import importlib
import platform

import numpy as np
import pytest

from tests.speaker_oracle import embedder
from tests.speaker_oracle.embedder import (
    ENCODER_ID,
    _compute_wespeaker_features,
    _embed_statements,
    _select_onnx_providers,
)

oracle_embedder = importlib.import_module("tests.speaker_oracle.embedder")


class _Input:
    def __init__(self, name: str):
        self.name = name


class _Output:
    def __init__(self, name: str):
        self.name = name


class _WeSpeakerStubSession:
    def get_inputs(self):
        return [_Input("feats")]

    def get_outputs(self):
        return [_Output("embs")]

    def run(self, _outputs, _inputs):
        return [np.zeros((1, 256), dtype=np.float32)]


def test_embed_synthetic_shape_and_provenance(monkeypatch) -> None:
    monkeypatch.setattr(embedder, "_embedder_session", None)
    monkeypatch.setattr(
        oracle_embedder,
        "_get_embedder_session",
        lambda: _WeSpeakerStubSession(),
    )
    monkeypatch.setattr(
        oracle_embedder,
        "_compute_wespeaker_features",
        lambda _audio, _sr: np.zeros((10, 80), dtype=np.float32),
    )
    audio = np.zeros(3 * 16000, dtype=np.float32)
    result = _embed_statements(
        audio,
        [{"id": 1, "start": 0.0, "end": 3.0, "text": "x"}],
        16000,
    )

    assert result is not None
    assert result["embeddings"].shape == (1, 256)
    assert result["embeddings"].dtype == np.float32
    assert result["statement_ids"].tolist() == [1]
    assert result["encoder"].item() == ENCODER_ID


def test_compute_wespeaker_features_applies_cmn() -> None:
    rng = np.random.default_rng(7)
    audio = (rng.normal(0.0, 0.01, 3 * 16000) + 0.05).astype(np.float32)

    feats = _compute_wespeaker_features(audio, 16000)

    assert feats.shape[0] > 0
    assert feats.shape[1] == 80
    np.testing.assert_allclose(
        feats.mean(axis=0),
        np.zeros(feats.shape[1], dtype=np.float32),
        atol=1e-4,
    )


@pytest.mark.parametrize(
    ("system_name", "expected"),
    [
        ("Darwin", ["CoreMLExecutionProvider", "CPUExecutionProvider"]),
        ("Linux", ["CPUExecutionProvider"]),
    ],
)
def test_provider_selection(
    monkeypatch: pytest.MonkeyPatch,
    system_name: str,
    expected: list[str],
) -> None:
    monkeypatch.setattr(platform, "system", lambda: system_name)
    assert _select_onnx_providers() == expected
