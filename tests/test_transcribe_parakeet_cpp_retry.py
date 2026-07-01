# SPDX-License-Identifier: AGPL-3.0-only
# Copyright (c) 2026 sol pbc

from __future__ import annotations

import argparse
from pathlib import Path
from unittest.mock import patch

import numpy as np
import pytest

from solstone.observe.utils import SAMPLE_RATE
from solstone.observe.vad import VadResult
from solstone.think.providers.parakeet_install import ParakeetProviderError
from solstone.think.providers.parakeet_server import ParakeetServerNotReady


@pytest.fixture
def raw_path(tmp_path: Path) -> Path:
    path = tmp_path / "chronicle" / "20260416" / "default" / "120000_300" / "audio.m4a"
    path.parent.mkdir(parents=True)
    path.write_bytes(b"audio")
    return path


@pytest.fixture
def audio_buffer() -> np.ndarray:
    return np.zeros(10 * SAMPLE_RATE, dtype=np.float32)


@pytest.fixture
def vad_result() -> VadResult:
    return VadResult(
        duration=10.0,
        speech_duration=5.0,
        has_speech=True,
        speech_segments=[(1.0, 6.0)],
    )


def test_process_audio_parakeet_server_not_ready_is_clean_retry(
    raw_path: Path, audio_buffer: np.ndarray, vad_result: VadResult
) -> None:
    from solstone.observe.transcribe.main import process_audio

    with (
        patch(
            "solstone.observe.transcribe.main.stt_transcribe",
            side_effect=ParakeetServerNotReady("no port"),
        ),
        patch("solstone.observe.transcribe.main.callosum_send") as mock_send,
    ):
        process_audio(raw_path, audio_buffer, vad_result, {}, backend="parakeet-cpp")

    assert raw_path.exists()
    assert not raw_path.with_suffix(".jsonl").exists()
    mock_send.assert_not_called()


def test_process_audio_parakeet_provider_error_uses_existing_failure_path(
    raw_path: Path, audio_buffer: np.ndarray, vad_result: VadResult
) -> None:
    from solstone.observe.transcribe.main import process_audio

    with (
        patch(
            "solstone.observe.transcribe.main.stt_transcribe",
            side_effect=ParakeetProviderError(
                "transcription_http_error", "HTTP 500: broken"
            ),
        ),
        patch(
            "solstone.observe.transcribe.main.get_journal",
            return_value=str(raw_path.parents[4]),
        ),
        patch("solstone.observe.transcribe.main.callosum_send") as mock_send,
    ):
        with pytest.raises(SystemExit) as exc_info:
            process_audio(
                raw_path, audio_buffer, vad_result, {}, backend="parakeet-cpp"
            )

    assert exc_info.value.code == 1
    assert raw_path.exists()
    assert not raw_path.with_suffix(".jsonl").exists()
    assert mock_send.call_args.args[:2] == ("observe", "transcribed")
    assert mock_send.call_args.kwargs["outcome"] == "failed"
    assert mock_send.call_args.kwargs["backend"] == "parakeet-cpp"
    assert "ParakeetProviderError" in mock_send.call_args.kwargs["error"]


@pytest.mark.parametrize(
    ("transcribe_config", "expected_backend_config"),
    [
        (
            {"backend": "parakeet-cpp", "parakeet-cpp": {"device": "cpu"}},
            {"device": "cpu"},
        ),
        ({"backend": "parakeet-cpp"}, {}),
    ],
)
def test_process_one_builds_parakeet_cpp_backend_config(
    tmp_path: Path,
    transcribe_config: dict,
    expected_backend_config: dict,
) -> None:
    from solstone.observe.transcribe.main import _process_one

    audio_path = (
        tmp_path / "chronicle" / "20260416" / "default" / "120000_300" / "audio.m4a"
    )
    audio_path.parent.mkdir(parents=True)
    audio_path.write_bytes(b"audio")
    args = argparse.Namespace(backend=None, cpu=False, model=None, redo=False)
    vad_result = VadResult(
        duration=10.0,
        speech_duration=5.0,
        has_speech=True,
        speech_segments=[(0.0, 5.0)],
    )
    captured = {}

    def fake_process_audio(
        _audio_path,
        _audio_buffer,
        _vad_result,
        backend_config,
        **kwargs,
    ):
        captured["backend_config"] = backend_config
        captured["backend"] = kwargs["backend"]

    with (
        patch(
            "solstone.observe.transcribe.main.load_audio",
            return_value=np.zeros(10 * SAMPLE_RATE, dtype=np.float32),
        ),
        patch("solstone.observe.vad.run_vad", return_value=vad_result),
        patch("solstone.observe.vad.reduce_audio", return_value=(None, None)),
        patch(
            "solstone.observe.transcribe.main.process_audio",
            side_effect=fake_process_audio,
        ),
    ):
        _process_one(audio_path, args, transcribe_config, "parakeet-cpp", [])

    assert captured == {
        "backend": "parakeet-cpp",
        "backend_config": expected_backend_config,
    }
