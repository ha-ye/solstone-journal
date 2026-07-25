# SPDX-License-Identifier: AGPL-3.0-only
# Copyright (c) 2026 sol pbc

"""Synthetic inputs and tolerances for the speaker differential harness.

This module is intentionally self-contained.  `scripts/build_core_fixtures.py`
generates committed Rust drift-detector JSON under `core/fixtures/`; a re-pin
there must not silently change this instrument's inputs.  The differential also
needs materially different shapes: multi-window log-prob matrices and embeddings
that survive real AHC, not the monkeypatched clustering used by older unit tests.

DRY still binds for production constants: thresholds and frame geometry are
imported from their real homes.  Only comparator tolerances are declared here,
with enough room for float noise while staying far below branch margins.
"""

from __future__ import annotations

from dataclasses import dataclass

import numpy as np

from solstone.apps.speakers import encoder_config
from solstone.observe.transcribe.diarize import (
    FRAMES_PER_WINDOW,
    MAX_K,
    MIN_INTERVAL_S,
    SAMPLE_RATE,
    SILHOUETTE_IMPROVEMENT,
    SINGLE_SPEAKER_CLASSES,
    STRIDE_S,
    WINDOW_S,
)
from solstone.observe.transcribe.main import EMBEDDER_NAME, MIN_STATEMENT_DURATION
from solstone.observe.transcribe.overlap import OVERLAP_CLASSES, SpeakerWindowStats

# Comparator tolerances.  These are instrument tolerances, not production
# thresholds.  They allow small float drift while preserving all branch decisions.
LOGPROB_MAX_ABS_TOLERANCE = 1e-4
LOGPROB_MEDIAN_ABS_TOLERANCE = 1e-5
LOGPROB_ARGMAX_AGREEMENT_MIN = 0.999
EVIDENCE_FLOAT_ABS_TOLERANCE = 1e-6
EMBEDDING_MAX_ABS_TOLERANCE = 1e-4
EMBEDDING_MIN_COSINE_SIMILARITY = 0.9999
EMBEDDING_MEDIAN_COSINE_SIMILARITY = 0.99999

# Degenerate-norm floor for cosine math, not a comparison tolerance to tune.
COSINE_NORM_FLOOR = 1e-12

COMPARATOR_THRESHOLDS = {
    "LOGPROB_MAX_ABS_TOLERANCE": LOGPROB_MAX_ABS_TOLERANCE,
    "LOGPROB_MEDIAN_ABS_TOLERANCE": LOGPROB_MEDIAN_ABS_TOLERANCE,
    "LOGPROB_ARGMAX_AGREEMENT_MIN": LOGPROB_ARGMAX_AGREEMENT_MIN,
    "EVIDENCE_FLOAT_ABS_TOLERANCE": EVIDENCE_FLOAT_ABS_TOLERANCE,
    "EMBEDDING_MAX_ABS_TOLERANCE": EMBEDDING_MAX_ABS_TOLERANCE,
    "EMBEDDING_MIN_COSINE_SIMILARITY": EMBEDDING_MIN_COSINE_SIMILARITY,
    "EMBEDDING_MEDIAN_COSINE_SIMILARITY": EMBEDDING_MEDIAN_COSINE_SIMILARITY,
}


@dataclass(frozen=True)
class ModelFreeSpeakerCase:
    audio: np.ndarray
    statements: list[dict[str, object]]
    avg_log_probs: np.ndarray
    overlap_fraction: float
    window_stats: tuple[SpeakerWindowStats, ...]
    statement_embeddings: np.ndarray
    statement_ids: np.ndarray
    statement_durations_s: np.ndarray
    interval_embeddings: np.ndarray


def dominant_log_probs(classes: np.ndarray, *, seed: int = 20_260_724) -> np.ndarray:
    """Return log-probs with a seeded dominant pyannote class per frame."""
    class_count = max(max(SINGLE_SPEAKER_CLASSES), max(OVERLAP_CLASSES)) + 1
    rng = np.random.default_rng(seed)
    log_probs = rng.normal(
        loc=-3.0, scale=0.05, size=(len(classes), class_count)
    ).astype(np.float32)
    log_probs[np.arange(len(classes)), classes] = rng.normal(
        loc=2.0, scale=0.03, size=len(classes)
    ).astype(np.float32)
    return log_probs


def separated_interval_embeddings() -> np.ndarray:
    """Four interval embeddings in two well-separated clusters for real AHC."""
    embs = np.zeros((4, 256), dtype=np.float32)
    embs[0, 0] = 1.0
    embs[1, 1] = 1.0
    embs[2, 0] = 0.99
    embs[2, 2] = 0.03
    embs[3, 1] = 0.99
    embs[3, 3] = -0.03
    return embs


def statement_embeddings(count: int) -> np.ndarray:
    rng = np.random.default_rng(2_026_0724)
    rows = rng.normal(size=(count, 256)).astype(np.float32)
    norms = np.linalg.norm(rows, axis=1, keepdims=True)
    return rows / np.where(norms > 1e-9, norms, 1.0)


def model_free_case() -> ModelFreeSpeakerCase:
    """Build a fully populated, model-free speaker differential input."""
    single_a, single_b = sorted(SINGLE_SPEAKER_CLASSES)[:2]
    silence = 0
    classes = np.concatenate(
        [
            np.full(42, single_a, dtype=np.int64),
            np.full(12, silence, dtype=np.int64),
            np.full(42, single_b, dtype=np.int64),
            np.full(12, silence, dtype=np.int64),
            np.full(42, single_a, dtype=np.int64),
            np.full(12, silence, dtype=np.int64),
            np.full(42, single_b, dtype=np.int64),
        ]
    )
    avg_log_probs = dominant_log_probs(classes, seed=30_001)
    duration_s = float(len(classes) * WINDOW_S / FRAMES_PER_WINDOW + 0.2)
    audio = np.zeros(int(duration_s * SAMPLE_RATE), dtype=np.float32)

    frame_s = WINDOW_S / FRAMES_PER_WINDOW
    statements = [
        {"id": 1, "start": 2 * frame_s, "end": 34 * frame_s, "text": "redacted"},
        {"id": 2, "start": 58 * frame_s, "end": 88 * frame_s, "text": "redacted"},
        {"id": 3, "start": 112 * frame_s, "end": 142 * frame_s, "text": "redacted"},
        {"id": 4, "start": 166 * frame_s, "end": 196 * frame_s, "text": "redacted"},
    ]
    ids = np.array([int(stmt["id"]) for stmt in statements], dtype=np.int32)
    durations = np.array(
        [float(stmt["end"]) - float(stmt["start"]) for stmt in statements],
        dtype=np.float32,
    )
    return ModelFreeSpeakerCase(
        audio=audio,
        statements=statements,
        avg_log_probs=avg_log_probs,
        overlap_fraction=0.10,
        window_stats=(SpeakerWindowStats(400, 2, 40),),
        statement_embeddings=statement_embeddings(len(statements)),
        statement_ids=ids,
        statement_durations_s=durations,
        interval_embeddings=separated_interval_embeddings(),
    )


def real_model_waveform(duration_s: float = 1.0, *, seed: int = 50_001) -> np.ndarray:
    """Short synthetic waveform for real ONNX wiring tests."""
    rng = np.random.default_rng(seed)
    samples = int(duration_s * SAMPLE_RATE)
    t = np.arange(samples, dtype=np.float32) / SAMPLE_RATE
    voiced = (
        0.025 * np.sin(2 * np.pi * 180.0 * t)
        + 0.015 * np.sin(2 * np.pi * 310.0 * t)
        + 0.004 * rng.standard_normal(samples)
    )
    envelope = np.minimum(1.0, np.linspace(0.0, 1.0, samples, dtype=np.float32) * 8.0)
    return (voiced * envelope).astype(np.float32)


__all__ = [
    "COMPARATOR_THRESHOLDS",
    "COSINE_NORM_FLOOR",
    "EMBEDDING_MAX_ABS_TOLERANCE",
    "EMBEDDING_MEDIAN_COSINE_SIMILARITY",
    "EMBEDDING_MIN_COSINE_SIMILARITY",
    "EVIDENCE_FLOAT_ABS_TOLERANCE",
    "LOGPROB_ARGMAX_AGREEMENT_MIN",
    "LOGPROB_MAX_ABS_TOLERANCE",
    "LOGPROB_MEDIAN_ABS_TOLERANCE",
    "MAX_K",
    "MIN_INTERVAL_S",
    "MIN_STATEMENT_DURATION",
    "ModelFreeSpeakerCase",
    "SAMPLE_RATE",
    "SILHOUETTE_IMPROVEMENT",
    "STRIDE_S",
    "WINDOW_S",
    "encoder_config",
    "EMBEDDER_NAME",
    "model_free_case",
    "real_model_waveform",
]
