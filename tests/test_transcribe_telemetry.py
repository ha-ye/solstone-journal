# SPDX-License-Identifier: AGPL-3.0-only
# Copyright (c) 2026 sol pbc

"""Content-free stage telemetry on the observe.transcribed event.

See solstone/observe/transcribe/failure-and-telemetry.md for the field contract.
"""

from __future__ import annotations

import importlib
import json
from pathlib import Path
from unittest.mock import MagicMock, patch

import numpy as np
import pytest

from solstone.observe.utils import SAMPLE_RATE
from solstone.observe.vad import VadResult

# A string that exists nowhere but in the (mocked) transcript. If it shows up in a
# serialized event, transcript content leaked into telemetry.
TRANSCRIPT_SENTINEL = "zzq-secret-utterance-do-not-leak"


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


def _backend_module() -> MagicMock:
    backend_module = MagicMock()
    backend_module.get_model_info.return_value = {
        "model": "parakeet-v3-q8_0.gguf",
        "device": "auto",
        "compute_type": "q8_0",
    }
    return backend_module


def _run_success(raw_path: Path, audio_buffer, vad_result) -> dict:
    """Run a successful process_audio and return the emitted event kwargs."""
    from solstone.observe.transcribe.main import process_audio

    statements = [
        {"id": 0, "start": 0.0, "end": 1.0, "text": f"{TRANSCRIPT_SENTINEL} hello"}
    ]

    with (
        patch(
            "solstone.observe.transcribe.main.get_config",
            return_value={"transcribe": {"preserve_all": False, "enrich": False}},
        ),
        patch(
            "solstone.observe.transcribe.main.get_journal",
            return_value=str(raw_path.parents[4]),
        ),
        patch(
            "solstone.observe.transcribe.main.stt_transcribe", return_value=statements
        ),
        patch(
            "solstone.observe.transcribe.main.get_backend",
            return_value=_backend_module(),
        ),
        patch("solstone.observe.transcribe.main._embed_statements", return_value=None),
        patch(
            "solstone.observe.transcribe.overlap.compute_overlap_and_logprobs",
            return_value=(0.0, np.zeros((589, 7), dtype=np.float32)),
        ),
        patch("solstone.observe.transcribe.main.callosum_send") as mock_send,
    ):
        process_audio(raw_path, audio_buffer, vad_result, {}, backend="parakeet-cpp")

    assert mock_send.call_args.args[:2] == ("observe", "transcribed")
    return mock_send.call_args.kwargs


def test_success_event_carries_stage_timings_and_envelope(
    raw_path: Path, audio_buffer: np.ndarray, vad_result: VadResult
) -> None:
    kwargs = _run_success(raw_path, audio_buffer, vad_result)

    assert kwargs["outcome"] == "transcribed"
    timings = kwargs["timings"]
    # Stages that ran inside process_audio. decode/vad/reduce are measured in
    # _process_one, which this test calls past; enrich is disabled by config and
    # diarization is skipped for parakeet-cpp -- so neither may appear.
    assert {"asr_ms", "embed_ms", "overlap_ms", "write_ms"} <= set(timings)
    assert "enrich_ms" not in timings
    assert "diarize_ms" not in timings
    assert all(isinstance(v, int) and v >= 0 for v in timings.values())

    assert kwargs["backend"] == "parakeet-cpp"
    assert kwargs["device"] == "auto"
    assert kwargs["model"] == "parakeet-v3-q8_0.gguf"
    assert kwargs["audio_seconds"] == 10.0
    assert isinstance(kwargs["peak_rss_mib"], int)
    assert kwargs["peak_rss_mib"] > 0


def test_success_event_is_content_free(
    raw_path: Path, audio_buffer: np.ndarray, vad_result: VadResult
) -> None:
    """No transcript text may appear anywhere in the serialized event."""
    kwargs = _run_success(raw_path, audio_buffer, vad_result)

    serialized = json.dumps(kwargs, default=str)
    assert TRANSCRIPT_SENTINEL not in serialized

    # And none of the enrichment-derived content fields ride along either.
    for banned in ("text", "words", "statements", "topics", "setting", "emotions"):
        assert banned not in kwargs


def test_failed_event_is_content_free_even_when_the_exception_message_is_not(
    raw_path: Path, audio_buffer: np.ndarray, vad_result: VadResult
) -> None:
    """The failed path must not put an exception *message* on the bus.

    Real exception messages can embed model output: SchemaValidationError carries a
    preview of the raw response, and transcribe/gemini.py interpolates it into its
    own message. So the event carries the exception *type*, never the message.
    """
    from solstone.observe.transcribe.main import process_audio

    leaky = RuntimeError(
        f"Gemini response failed schema validation: preview={TRANSCRIPT_SENTINEL!r}"
    )

    with (
        patch("solstone.observe.transcribe.main.stt_transcribe", side_effect=leaky),
        patch(
            "solstone.observe.transcribe.main.get_journal",
            return_value=str(raw_path.parents[4]),
        ),
        patch("solstone.observe.transcribe.main.callosum_send") as mock_send,
    ):
        with pytest.raises(SystemExit) as exc_info:
            process_audio(raw_path, audio_buffer, vad_result, {}, backend="gemini")

    assert exc_info.value.code == 1
    kwargs = mock_send.call_args.kwargs
    assert kwargs["outcome"] == "failed"
    assert kwargs["reason"] == "RuntimeError"
    assert kwargs["error"] == "RuntimeError"
    assert TRANSCRIPT_SENTINEL not in json.dumps(kwargs, default=str)


def test_rtfx_derived_from_asr_time(
    raw_path: Path, audio_buffer: np.ndarray, vad_result: VadResult
) -> None:
    kwargs = _run_success(raw_path, audio_buffer, vad_result)

    asr_ms = kwargs["timings"]["asr_ms"]
    if asr_ms:
        assert kwargs["rtfx"] == pytest.approx(
            kwargs["audio_seconds"] / (asr_ms / 1000), rel=0.01
        )
    else:
        # A sub-millisecond mocked ASR cannot produce an honest ratio, so none is
        # fabricated.
        assert "rtfx" not in kwargs


def test_queue_wait_is_read_from_env(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    from solstone.observe.transcribe.main import _read_queue_wait_ms

    monkeypatch.setenv("SOL_QUEUE_WAIT_MS", "4200")
    assert _read_queue_wait_ms() == 4200

    monkeypatch.setenv("SOL_QUEUE_WAIT_MS", "not-a-number")
    assert _read_queue_wait_ms() is None

    monkeypatch.delenv("SOL_QUEUE_WAIT_MS")
    assert _read_queue_wait_ms() is None


def test_stage_timings_accumulate_repeated_stages(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """write_ms covers the jsonl AND the npz, so a second entry must sum, not clobber."""
    # NB: `solstone.observe.transcribe.main` as an attribute path resolves to the
    # re-exported main() *function*, not the module -- import it explicitly.
    transcribe_main = importlib.import_module("solstone.observe.transcribe.main")

    # Drive perf_counter so the two blocks have distinct, known durations.
    # Values are binary-exact so the int() truncation is not off by a millisecond.
    ticks = iter([0.0, 0.25, 1.0, 1.5])
    monkeypatch.setattr(transcribe_main.time, "perf_counter", lambda: next(ticks))

    timings = transcribe_main._StageTimings()
    assert timings.as_dict() == {}

    with timings.time("write"):  # 250 ms
        pass
    assert timings.get_ms("write") == 250

    with timings.time("write"):  # 500 ms
        pass
    assert timings.get_ms("write") == 750  # summed, not clobbered to 500
    assert set(timings.as_dict()) == {"write_ms"}


def test_stage_timing_recorded_even_when_the_stage_raises() -> None:
    """A server that dies mid-ASR must still report how long ASR ran before dying."""
    from solstone.observe.transcribe.main import _StageTimings

    timings = _StageTimings()
    with pytest.raises(RuntimeError):
        with timings.time("asr"):
            raise RuntimeError("server died")

    assert timings.get_ms("asr") is not None
