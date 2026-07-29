# SPDX-License-Identifier: AGPL-3.0-only
# Copyright (c) 2026 sol pbc

"""Frozen WeSpeaker statement-embedding oracle for speaker differential tests."""

from __future__ import annotations

import logging
import platform
import time
from collections.abc import Sequence
from typing import TYPE_CHECKING

from solstone.apps.speakers.encoder_config import ENCODER_ID, WESPEAKER_EMBEDDING_WIDTH
from solstone.observe.utils import SAMPLE_RATE
from solstone.think.model_assets import resolve_wespeaker_model

if TYPE_CHECKING:
    import numpy as np
    import onnxruntime as ort

_embedder_session: ort.InferenceSession | None = None
EMBEDDER_NAME = ENCODER_ID
MIN_STATEMENT_DURATION = 0.3


def _select_onnx_providers() -> list[str]:
    if platform.system() == "Darwin":
        return ["CoreMLExecutionProvider", "CPUExecutionProvider"]
    return ["CPUExecutionProvider"]


def _get_embedder_session() -> ort.InferenceSession:
    global _embedder_session

    if _embedder_session is None:
        import onnxruntime as ort

        wespeaker_model_path = resolve_wespeaker_model()
        if not wespeaker_model_path.is_file():
            raise FileNotFoundError(
                f"WeSpeaker model asset not found at {wespeaker_model_path}. "
                "Run `make install` to verify the bundled asset."
            )
        providers = _select_onnx_providers()
        start = time.monotonic()
        _embedder_session = ort.InferenceSession(
            str(wespeaker_model_path),
            providers=providers,
        )
        elapsed = time.monotonic() - start
        logging.info(
            "wespeaker oracle session loaded providers=%s elapsed=%.2fs",
            _embedder_session.get_providers(),
            elapsed,
        )

    return _embedder_session


def _compute_wespeaker_features(audio: np.ndarray, sample_rate: int) -> np.ndarray:
    import kaldi_native_fbank as knf
    import numpy as np

    if sample_rate != SAMPLE_RATE:
        raise ValueError(
            f"WeSpeaker embedder requires {SAMPLE_RATE} Hz audio, got {sample_rate}"
        )

    opts = knf.FbankOptions()
    opts.frame_opts.samp_freq = float(sample_rate)
    opts.frame_opts.dither = 0.0
    opts.frame_opts.snip_edges = True
    opts.frame_opts.frame_length_ms = 25.0
    opts.frame_opts.frame_shift_ms = 10.0
    opts.mel_opts.num_bins = 80
    opts.energy_floor = 0.0
    opts.use_energy = False

    fbank = knf.OnlineFbank(opts)
    scaled = (audio.astype(np.float32) * 32768.0).tolist()
    fbank.accept_waveform(float(sample_rate), scaled)
    fbank.input_finished()

    frames = [fbank.get_frame(i) for i in range(fbank.num_frames_ready)]
    if not frames:
        return np.zeros((0, 80), dtype=np.float32)

    feats = np.stack(frames, axis=0).astype(np.float32)
    return feats - feats.mean(axis=0, keepdims=True)


def _embed_statements(
    audio: np.ndarray,
    statements: Sequence[dict[str, object]],
    sample_rate: int,
) -> dict[str, np.ndarray] | None:
    import numpy as np

    if not statements:
        return None
    if sample_rate != SAMPLE_RATE:
        raise ValueError(
            f"WeSpeaker embedder requires {SAMPLE_RATE} Hz audio, got {sample_rate}"
        )

    session = _get_embedder_session()
    input_name = session.get_inputs()[0].name
    embeddings: list[np.ndarray] = []
    statement_ids: list[int] = []
    durations: list[float] = []

    for statement in statements:
        start = float(statement.get("start", 0.0))
        end = float(statement.get("end", start))
        duration = max(0.0, end - start)
        if duration < MIN_STATEMENT_DURATION:
            continue
        start_sample = max(0, int(round(start * sample_rate)))
        end_sample = min(len(audio), int(round(end * sample_rate)))
        if end_sample <= start_sample:
            continue
        segment = audio[start_sample:end_sample]
        feats = _compute_wespeaker_features(segment, sample_rate)
        if feats.shape[0] == 0:
            continue
        output = session.run(None, {input_name: feats[None, :, :]})[0]
        vector = np.asarray(output[0], dtype=np.float32)
        if vector.shape != (WESPEAKER_EMBEDDING_WIDTH,):
            raise ValueError(
                "WeSpeaker embedding shape mismatch: "
                f"{vector.shape} != ({WESPEAKER_EMBEDDING_WIDTH},)"
            )
        embeddings.append(vector)
        statement_ids.append(int(statement["id"]))
        durations.append(duration)

    if not embeddings:
        return None

    return {
        "embeddings": np.stack(embeddings).astype(np.float32),
        "statement_ids": np.asarray(statement_ids, dtype=np.int32),
        "durations_s": np.asarray(durations, dtype=np.float32),
        "encoder": np.array(EMBEDDER_NAME),
    }


__all__ = [
    "ENCODER_ID",
    "EMBEDDER_NAME",
    "MIN_STATEMENT_DURATION",
    "_compute_wespeaker_features",
    "_embed_statements",
    "_embedder_session",
    "_get_embedder_session",
    "_select_onnx_providers",
]
