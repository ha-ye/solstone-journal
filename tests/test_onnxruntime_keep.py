# SPDX-License-Identifier: AGPL-3.0-only
# Copyright (c) 2026 sol pbc

from __future__ import annotations

import importlib

import numpy as np


def test_bundled_onnxruntime_consumers_construct_and_run() -> None:
    import onnxruntime as ort

    main = importlib.import_module("solstone.observe.transcribe.main")
    diarize = importlib.import_module("solstone.observe.transcribe.diarize")
    overlap = importlib.import_module("solstone.observe.transcribe.overlap")
    silero = importlib.import_module("solstone.observe._silero_vad")

    assert ort.get_available_providers()

    wespeaker = main._get_embedder_session()
    wespeaker_input = wespeaker.get_inputs()[0].name
    wespeaker_output = wespeaker.run(
        None,
        {wespeaker_input: np.zeros((1, 20, 80), dtype=np.float32)},
    )[0]
    assert wespeaker_output.shape == (1, 256)

    vad_output = silero.get_vad_model()(np.zeros(512, dtype=np.float32))
    assert vad_output.shape == (1,)

    audio = np.zeros(16_000, dtype=np.float32)
    diarize_log_probs = diarize._run_pyannote(audio)
    assert diarize_log_probs.shape[1] == 7

    overlap_fraction = overlap.compute_overlap_fraction(audio)
    assert 0.0 <= overlap_fraction <= 1.0
