# SPDX-License-Identifier: AGPL-3.0-only
# Copyright (c) 2026 sol pbc

"""Tests for observe.transcribe module."""

import json
import shutil
import subprocess
import tempfile
from datetime import datetime
from pathlib import Path
from unittest.mock import MagicMock, patch

import numpy as np
import pytest
import soundfile as sf

from solstone.observe import utils as observe_utils
from solstone.observe.transcribe import (
    DEFAULT_MIN_SPEECH_SECONDS,
    MIN_STATEMENT_DURATION,
    SENTENCE_ENDINGS,
    build_statement,
    build_statements_from_acoustic,
)
from solstone.observe.transcribe.main import EMBEDDER_NAME, _statements_to_jsonl
from solstone.observe.transcribe.overlap import (
    OverlapInferenceResult,
    SpeakerEvidenceDecision,
    SpeakerWindowStats,
)
from solstone.observe.utils import SAMPLE_RATE, AudioDecodeError, load_audio
from solstone.observe.vad import VadResult
from solstone.think.journal_io.errors import MalformedDataError
from solstone.think.journal_io.npz import load_npz
from solstone.think.media import AUDIO_EXTENSIONS

CLEAN_SINGLE_STATS = (SpeakerWindowStats(589, 1, 0),)
MULTI_STATS = (SpeakerWindowStats(589, 2, 300),)


def _overlap_result(
    overlap_fraction: float,
    avg_log_probs: np.ndarray | None = None,
    window_stats: tuple[SpeakerWindowStats, ...] = CLEAN_SINGLE_STATS,
) -> OverlapInferenceResult:
    if avg_log_probs is None:
        avg_log_probs = np.zeros((589, 7), dtype=np.float32)
    return OverlapInferenceResult(overlap_fraction, avg_log_probs, window_stats)


class TestBuildStatementsFromAcoustic:
    """Test building statements from acoustic segments."""

    def test_merges_fragments_into_statement(self):
        """Multiple acoustic segments forming one sentence should merge."""
        # Simulates Whisper splitting "I think I can do it." across 3 acoustic segments
        acoustic_segments = [
            {
                "id": 1,
                "start": 0.0,
                "end": 1.0,
                "text": "I think",
                "words": [
                    {"word": " I", "start": 0.0, "end": 0.3, "probability": 0.9},
                    {"word": " think", "start": 0.3, "end": 1.0, "probability": 0.9},
                ],
            },
            {
                "id": 2,
                "start": 1.5,
                "end": 2.5,
                "text": "I can",
                "words": [
                    {"word": " I", "start": 1.5, "end": 1.8, "probability": 0.9},
                    {"word": " can", "start": 1.8, "end": 2.5, "probability": 0.9},
                ],
            },
            {
                "id": 3,
                "start": 3.0,
                "end": 4.0,
                "text": "do it.",
                "words": [
                    {"word": " do", "start": 3.0, "end": 3.3, "probability": 0.9},
                    {"word": " it.", "start": 3.3, "end": 4.0, "probability": 0.9},
                ],
            },
        ]

        result = build_statements_from_acoustic(acoustic_segments)

        assert len(result) == 1
        stmt = result[0]
        assert stmt["id"] == 1
        assert stmt["start"] == 0.0
        assert stmt["end"] == 4.0
        assert stmt["text"] == "I think I can do it."
        assert len(stmt["words"]) == 6

    def test_splits_on_period(self):
        """Statements should split on period."""
        acoustic_segments = [
            {
                "id": 1,
                "start": 0.0,
                "end": 5.0,
                "text": "Hello. World.",
                "words": [
                    {"word": " Hello.", "start": 0.0, "end": 1.0, "probability": 0.9},
                    {"word": " World.", "start": 2.0, "end": 3.0, "probability": 0.9},
                ],
            },
        ]

        result = build_statements_from_acoustic(acoustic_segments)

        assert len(result) == 2
        assert result[0]["text"] == "Hello."
        assert result[1]["text"] == "World."

    def test_splits_on_question_mark(self):
        """Statements should split on question mark."""
        acoustic_segments = [
            {
                "id": 1,
                "start": 0.0,
                "end": 3.0,
                "text": "How are you? Good.",
                "words": [
                    {"word": " How", "start": 0.0, "end": 0.3, "probability": 0.9},
                    {"word": " are", "start": 0.3, "end": 0.6, "probability": 0.9},
                    {"word": " you?", "start": 0.6, "end": 1.0, "probability": 0.9},
                    {"word": " Good.", "start": 2.0, "end": 3.0, "probability": 0.9},
                ],
            },
        ]

        result = build_statements_from_acoustic(acoustic_segments)

        assert len(result) == 2
        assert result[0]["text"] == "How are you?"
        assert result[1]["text"] == "Good."

    def test_splits_on_exclamation(self):
        """Statements should split on exclamation mark."""
        acoustic_segments = [
            {
                "id": 1,
                "start": 0.0,
                "end": 2.0,
                "text": "Wow! Amazing.",
                "words": [
                    {"word": " Wow!", "start": 0.0, "end": 0.5, "probability": 0.9},
                    {"word": " Amazing.", "start": 1.0, "end": 2.0, "probability": 0.9},
                ],
            },
        ]

        result = build_statements_from_acoustic(acoustic_segments)

        assert len(result) == 2
        assert result[0]["text"] == "Wow!"
        assert result[1]["text"] == "Amazing."

    def test_handles_incomplete_final_sentence(self):
        """Final sentence without punctuation should still be captured."""
        acoustic_segments = [
            {
                "id": 1,
                "start": 0.0,
                "end": 3.0,
                "text": "First sentence. And then",
                "words": [
                    {"word": " First", "start": 0.0, "end": 0.3, "probability": 0.9},
                    {
                        "word": " sentence.",
                        "start": 0.3,
                        "end": 1.0,
                        "probability": 0.9,
                    },
                    {"word": " And", "start": 1.5, "end": 1.8, "probability": 0.9},
                    {"word": " then", "start": 1.8, "end": 2.0, "probability": 0.9},
                ],
            },
        ]

        result = build_statements_from_acoustic(acoustic_segments)

        assert len(result) == 2
        assert result[0]["text"] == "First sentence."
        assert result[1]["text"] == "And then"

    def test_empty_segments_returns_unchanged(self):
        """Empty acoustic segments should return unchanged."""
        acoustic_segments = []
        result = build_statements_from_acoustic(acoustic_segments)
        assert result == acoustic_segments

    def test_statement_timestamps_from_words(self):
        """Statement start/end should come from first/last word."""
        acoustic_segments = [
            {
                "id": 1,
                "start": 0.0,
                "end": 10.0,  # Original segment end
                "text": "Hello world.",
                "words": [
                    {"word": " Hello", "start": 2.5, "end": 3.0, "probability": 0.9},
                    {"word": " world.", "start": 3.5, "end": 4.2, "probability": 0.9},
                ],
            },
        ]

        result = build_statements_from_acoustic(acoustic_segments)

        stmt = result[0]
        assert stmt["start"] == 2.5  # From first word
        assert stmt["end"] == 4.2  # From last word


class TestBuildStatement:
    """Test statement building helper."""

    def test_builds_statement_from_words(self):
        """Should build statement with correct fields."""
        words = [
            {"word": " Hello", "start": 0.0, "end": 0.5, "probability": 0.9},
            {"word": " world", "start": 0.6, "end": 1.0, "probability": 0.8},
        ]

        stmt = build_statement(1, words)

        assert stmt["id"] == 1
        assert stmt["start"] == 0.0
        assert stmt["end"] == 1.0
        assert stmt["text"] == "Hello world"
        assert stmt["words"] == words


class TestConstants:
    """Test module constants."""

    def test_sentence_endings(self):
        """SENTENCE_ENDINGS should contain expected punctuation."""
        assert "." in SENTENCE_ENDINGS
        assert "?" in SENTENCE_ENDINGS
        assert "!" in SENTENCE_ENDINGS
        assert "," not in SENTENCE_ENDINGS

    def test_min_statement_duration(self):
        """MIN_STATEMENT_DURATION should be positive."""
        assert MIN_STATEMENT_DURATION > 0

    def test_default_transcription_settings(self):
        """Default transcription settings should be valid."""
        assert DEFAULT_MIN_SPEECH_SECONDS == 1.0


class TestLoadAudio:
    """Test the shared load_audio utility."""

    def test_flac_returns_numpy_array(self):
        """FLAC files should return a numpy array."""
        with tempfile.TemporaryDirectory() as tmpdir:
            flac_path = Path(tmpdir) / "test.flac"

            # Create a simple FLAC file
            sample_rate = 16000
            data = np.zeros(sample_rate, dtype=np.float32)
            sf.write(flac_path, data, sample_rate, format="FLAC")

            result = load_audio(flac_path)
            assert isinstance(result, np.ndarray)
            assert result.dtype == np.float32
            assert len(result) == sample_rate

    def test_m4a_returns_numpy_array(self):
        """M4A files should return a numpy array with audio content."""
        # Generated with:
        # ffmpeg -y -f lavfi \
        #   -i "sine=frequency=440:duration=0.5:sample_rate=16000" \
        #   -c:a aac -b:a 64k \
        #   tests/fixtures/audio/aac_single_track.m4a
        m4a_path = Path(__file__).parent / "fixtures" / "audio" / "aac_single_track.m4a"

        audio = load_audio(m4a_path)

        assert isinstance(audio, np.ndarray)
        assert audio.dtype == np.float32
        assert len(audio) > 0

    def test_multi_track_m4a_mixes_streams(self):
        """load_audio should mix multiple M4A audio streams together."""
        # Generated with:
        # ffmpeg -y -f lavfi -i "anullsrc=r=16000:cl=mono" \
        #   -f lavfi -i "sine=frequency=440:duration=1:sample_rate=16000,volume=4" \
        #   -map 0:a -map 1:a -c:a aac -b:a 64k -t 1 \
        #   tests/fixtures/audio/aac_multi_track.m4a
        m4a_path = Path(__file__).parent / "fixtures" / "audio" / "aac_multi_track.m4a"

        audio = load_audio(m4a_path)

        assert isinstance(audio, np.ndarray)
        assert audio.dtype == np.float32

        # The mixed audio should have content from track 1 (the sine wave)
        # AAC compression affects amplitude, so use loose threshold
        rms = np.sqrt(np.mean(audio**2))
        assert rms > 0.1, f"Mixed audio should contain signal, got RMS={rms}"

    @pytest.mark.integration
    @pytest.mark.skipif(not shutil.which("ffmpeg"), reason="ffmpeg not installed")
    def test_m4a_ffmpeg_round_trip_decodes(self, tmp_path):
        m4a_path = tmp_path / "round-trip.m4a"
        result = subprocess.run(
            [
                "ffmpeg",
                "-y",
                "-f",
                "lavfi",
                "-i",
                "sine=frequency=440:duration=0.5:sample_rate=16000",
                "-c:a",
                "aac",
                "-b:a",
                "64k",
                str(m4a_path),
            ],
            capture_output=True,
            text=True,
        )
        assert result.returncode == 0, result.stderr

        audio = load_audio(m4a_path)

        assert isinstance(audio, np.ndarray)
        assert audio.dtype == np.float32
        assert len(audio) > 0

    @pytest.mark.parametrize("suffix", sorted(AUDIO_EXTENSIONS - {".m4a"}))
    def test_load_audio_decodes_ext(self, tmp_path, suffix):
        sample_rate = 48000
        duration = 1.0
        t = np.arange(int(sample_rate * duration), dtype=np.float32) / sample_rate
        data = np.sin(2 * np.pi * 440 * t).astype(np.float32)
        path = tmp_path / f"test{suffix}"

        try:
            sf.write(path, data, sample_rate)
        except Exception as e:
            pytest.skip(f"libsndfile cannot encode {suffix}: {e}")

        audio = load_audio(path)

        assert isinstance(audio, np.ndarray)
        assert audio.dtype == np.float32
        assert audio.ndim == 1
        assert abs(len(audio) - 16000) <= 64

    def test_load_audio_sine_wave_resamples_correctly(self, tmp_path):
        input_rate = 48000
        output_rate = 16000
        t = np.arange(input_rate, dtype=np.float32) / input_rate
        data = np.sin(2 * np.pi * 440 * t).astype(np.float32)
        path = tmp_path / "test.wav"
        sf.write(path, data, input_rate, format="WAV", subtype="FLOAT")

        audio = load_audio(path, sample_rate=output_rate)
        reference = np.sin(
            2 * np.pi * 440 * np.arange(output_rate, dtype=np.float32) / output_rate
        ).astype(np.float32)

        errors = []
        for shift in range(-16, 17):
            if shift >= 0:
                actual = audio[shift:]
                expected = reference
            else:
                actual = audio
                expected = reference[-shift:]
            length = min(len(actual), len(expected))
            if length <= 200:
                continue
            actual_window = actual[:length][100:-100]
            expected_window = expected[:length][100:-100]
            errors.append(float(np.max(np.abs(actual_window - expected_window))))

        assert min(errors) <= 1e-2

    def test_load_audio_wraps_decode_failure(self, tmp_path):
        path = tmp_path / "not-audio.wav"
        path.write_bytes(b"not audio")

        with pytest.raises(RuntimeError) as excinfo:
            load_audio(path)

        message = str(excinfo.value)
        assert str(path) in message
        assert "(.wav)" in message
        assert isinstance(excinfo.value, AudioDecodeError)

    def test_load_audio_reports_worker_signal_exit(self, tmp_path, monkeypatch):
        path = tmp_path / "crash.m4a"
        path.write_bytes(b"truncated")

        def fake_decode(*args, **kwargs):
            raise AudioDecodeError("worker exited from signal 11")

        monkeypatch.setattr(observe_utils, "_decode_audio_in_worker", fake_decode)

        with pytest.raises(AudioDecodeError) as excinfo:
            load_audio(path)

        assert "worker exited from signal 11" in str(excinfo.value)

    def test_load_audio_reports_malformed_worker_payload(self, tmp_path, monkeypatch):
        path = tmp_path / "bad-payload.m4a"
        path.write_bytes(b"payload")

        def fake_decode(*args, **kwargs):
            return {"ok": True}

        monkeypatch.setattr(observe_utils, "_decode_audio_in_worker", fake_decode)

        with pytest.raises(AudioDecodeError) as excinfo:
            load_audio(path)

        assert "invalid worker sample rate" in str(excinfo.value)

    def test_load_audio_rejects_empty_decode(self, tmp_path):
        path = tmp_path / "not-audio.flac"
        path.write_bytes(b"not audio")

        with pytest.raises(RuntimeError) as excinfo:
            load_audio(path)

        message = str(excinfo.value)
        assert str(path) in message
        assert "(.flac)" in message
        assert "no audio data decoded" in message
        assert excinfo.value.__cause__ is None

    def test_load_audio_handles_very_short_clip(self, tmp_path):
        input_rate = 48000
        output_rate = 16000
        duration = 0.05
        t = np.arange(int(input_rate * duration), dtype=np.float32) / input_rate
        data = np.sin(2 * np.pi * 440 * t).astype(np.float32)
        path = tmp_path / "short.wav"
        sf.write(path, data, input_rate, format="WAV", subtype="FLOAT")

        audio = load_audio(path, sample_rate=output_rate)

        assert audio.dtype == np.float32
        assert len(audio) > 0
        assert abs(len(audio) - 800) <= 16


class TestEmbeddingsFormat:
    """Test embeddings.npz format validation."""

    def test_embeddings_arrays_shape(self):
        """Embeddings should have correct array shapes."""
        # Simulate 10 statements with 256-dim embeddings
        embeddings = np.random.randn(10, 256).astype(np.float32)
        statement_ids = np.array([1, 2, 3, 4, 5, 6, 7, 8, 9, 10], dtype=np.int32)

        assert embeddings.shape == (10, 256)
        assert statement_ids.shape == (10,)
        assert embeddings.dtype == np.float32
        assert statement_ids.dtype == np.int32

    def test_embeddings_npz_roundtrip(self):
        """Embeddings should survive save/load cycle."""
        with tempfile.TemporaryDirectory() as tmpdir:
            npz_path = Path(tmpdir) / "embeddings.npz"

            embeddings = np.random.randn(5, 256).astype(np.float32)
            statement_ids = np.array([1, 2, 3, 4, 5], dtype=np.int32)
            encoder = np.array(EMBEDDER_NAME)

            np.savez_compressed(
                npz_path,
                embeddings=embeddings,
                statement_ids=statement_ids,
                encoder=encoder,
            )

            loaded = np.load(npz_path)
            np.testing.assert_array_almost_equal(loaded["embeddings"], embeddings)
            np.testing.assert_array_equal(loaded["statement_ids"], statement_ids)
            assert loaded["encoder"].item() == EMBEDDER_NAME

    def test_statement_ids_are_unique(self):
        """Statement IDs should be unique."""
        statement_ids = np.array([1, 2, 3, 4, 5], dtype=np.int32)
        assert len(statement_ids) == len(np.unique(statement_ids))


def test_process_audio_failed_embeddings_write_emits_failed_event(tmp_path):
    from solstone.observe.transcribe.main import process_audio

    raw_path = (
        tmp_path / "chronicle" / "20260416" / "default" / "120000_300" / "audio.m4a"
    )
    raw_path.parent.mkdir(parents=True)
    raw_path.touch()
    audio_buffer = np.zeros(10 * SAMPLE_RATE, dtype=np.float32)
    vad_result = VadResult(
        duration=10.0,
        speech_duration=5.0,
        has_speech=True,
        speech_segments=[(1.0, 6.0)],
    )
    statements = [{"id": 0, "start": 0.0, "end": 1.0, "text": "hi"}]
    backend_module = MagicMock()
    backend_module.get_model_info.return_value = {
        "model": "medium.en",
        "device": "cpu",
        "compute_type": "int8",
    }
    embeddings_path = raw_path.with_suffix(".npz")
    embeddings_data = {
        "embeddings": np.zeros((1, 256), dtype=np.float32),
        "statement_ids": np.zeros((1,), dtype=np.int32),
        "durations_s": np.zeros((1,), dtype=np.float32),
        "encoder": np.array("test"),
    }

    with (
        patch(
            "solstone.observe.transcribe.main.get_journal",
            return_value=str(raw_path.parents[4]),
        ),
        patch(
            "solstone.observe.transcribe.main.get_config",
            return_value={"transcribe": {"preserve_all": False}},
        ),
        patch(
            "solstone.observe.transcribe.main.stt_transcribe", return_value=statements
        ),
        patch(
            "solstone.observe.transcribe.main.get_backend", return_value=backend_module
        ),
        patch(
            "solstone.observe.transcribe.main._embed_statements",
            return_value=embeddings_data,
        ),
        patch(
            "solstone.observe.transcribe.overlap.compute_overlap_and_logprobs",
            return_value=_overlap_result(0.0),
        ),
        patch("solstone.observe.transcribe.main.callosum_send") as mock_send,
        patch(
            "solstone.observe.transcribe.main.write_npz",
            side_effect=MalformedDataError(embeddings_path),
        ),
    ):
        with pytest.raises(SystemExit) as exc:
            process_audio(raw_path, audio_buffer, vad_result, {}, backend="parakeet")

    assert exc.value.code == 1
    assert mock_send.call_args.args[:2] == ("observe", "transcribed")
    assert mock_send.call_args.kwargs["outcome"] == "failed"
    assert "MalformedDataError" in mock_send.call_args.kwargs["error"]
    assert not embeddings_path.exists()


def test_process_audio_embeddings_write_round_trips_without_lock(tmp_path):
    from solstone.observe.transcribe.main import process_audio

    raw_path = (
        tmp_path / "chronicle" / "20260416" / "default" / "120000_300" / "audio.m4a"
    )
    raw_path.parent.mkdir(parents=True)
    raw_path.touch()
    audio_buffer = np.zeros(10 * SAMPLE_RATE, dtype=np.float32)
    vad_result = VadResult(
        duration=10.0,
        speech_duration=5.0,
        has_speech=True,
        speech_segments=[(1.0, 6.0)],
    )
    statements = [{"id": 0, "start": 0.0, "end": 1.0, "text": "hi"}]
    backend_module = MagicMock()
    backend_module.get_model_info.return_value = {
        "model": "medium.en",
        "device": "cpu",
        "compute_type": "int8",
    }
    embeddings_path = raw_path.with_suffix(".npz")
    embeddings_data = {
        "embeddings": np.zeros((1, 256), dtype=np.float32),
        "statement_ids": np.zeros((1,), dtype=np.int32),
        "durations_s": np.zeros((1,), dtype=np.float32),
        "encoder": np.array("test"),
    }

    with (
        patch(
            "solstone.observe.transcribe.main.get_journal",
            return_value=str(raw_path.parents[4]),
        ),
        patch(
            "solstone.observe.transcribe.main.get_config",
            return_value={"transcribe": {"preserve_all": False}},
        ),
        patch(
            "solstone.observe.transcribe.main.stt_transcribe", return_value=statements
        ),
        patch(
            "solstone.observe.transcribe.main.get_backend", return_value=backend_module
        ),
        patch(
            "solstone.observe.transcribe.main._embed_statements",
            return_value=embeddings_data,
        ),
        patch(
            "solstone.observe.transcribe.overlap.compute_overlap_and_logprobs",
            return_value=_overlap_result(0.0),
        ),
        patch("solstone.observe.transcribe.main.callosum_send") as mock_send,
    ):
        process_audio(raw_path, audio_buffer, vad_result, {}, backend="parakeet")

    loaded = load_npz(embeddings_path)
    assert embeddings_path.exists()
    assert loaded is not None
    assert set(loaded) == {"embeddings", "statement_ids", "durations_s", "encoder"}
    for key, expected in embeddings_data.items():
        np.testing.assert_array_equal(loaded[key], expected)
    assert mock_send.call_args.args[:2] == ("observe", "transcribed")
    assert mock_send.call_args.kwargs["outcome"] == "transcribed"
    assert list(embeddings_path.parent.glob("*.lock")) == []


def test_process_audio_native_failure_writes_python_identical_artifacts(tmp_path):
    from solstone.observe.transcribe.main import process_audio
    from solstone.observe.transcribe.speakers_analyze_seam import (
        NativeSpeakerAnalysisResult,
    )

    statements = [{"id": 0, "start": 0.0, "end": 1.0, "text": "hi"}]
    backend_module = MagicMock()
    backend_module.get_model_info.return_value = {
        "model": "medium.en",
        "device": "cpu",
        "compute_type": "int8",
    }
    embeddings_data = {
        "embeddings": np.zeros((1, 256), dtype=np.float32),
        "statement_ids": np.zeros((1,), dtype=np.int32),
        "durations_s": np.zeros((1,), dtype=np.float32),
        "encoder": np.array("test"),
    }

    def run_case(case: str, native_result: NativeSpeakerAnalysisResult):
        raw_path = (
            tmp_path
            / case
            / "chronicle"
            / "20260416"
            / "default"
            / "120000_300"
            / "audio.m4a"
        )
        raw_path.parent.mkdir(parents=True)
        raw_path.write_bytes(b"\x00" * 2048)
        audio_buffer = np.zeros(10 * SAMPLE_RATE, dtype=np.float32)
        vad_result = VadResult(
            duration=10.0,
            speech_duration=5.0,
            has_speech=True,
            speech_segments=[(1.0, 6.0)],
        )
        with (
            patch(
                "solstone.observe.transcribe.main.get_journal",
                return_value=str(raw_path.parents[4]),
            ),
            patch(
                "solstone.observe.transcribe.main.get_config",
                return_value={"transcribe": {"preserve_all": False}},
            ),
            patch(
                "solstone.observe.transcribe.main.stt_transcribe",
                return_value=statements,
            ),
            patch(
                "solstone.observe.transcribe.main.get_backend",
                return_value=backend_module,
            ),
            patch(
                "solstone.observe.transcribe.main._embed_statements",
                return_value=embeddings_data,
            ),
            patch(
                "solstone.observe.transcribe.overlap.compute_overlap_and_logprobs",
                return_value=_overlap_result(0.0),
            ),
            patch(
                "solstone.observe.processing_record.now_iso_utc",
                return_value="2026-06-30T12:00:00Z",
            ),
            patch(
                "solstone.observe.transcribe.speakers_analyze_seam."
                "maybe_run_native_speaker_analysis",
                return_value=native_result,
            ),
            patch("solstone.observe.transcribe.main.callosum_send"),
        ):
            process_audio(raw_path, audio_buffer, vad_result, {}, backend="parakeet")
        return raw_path.with_suffix(".jsonl").read_bytes(), load_npz(
            raw_path.with_suffix(".npz")
        )

    fallback_jsonl, fallback_npz = run_case(
        "fallback",
        NativeSpeakerAnalysisResult(
            status="fallback",
            event_fields={
                "speaker_analysis_path": "native_to_python",
                "speaker_analysis_degradation": "native_failure",
                "speaker_analysis_stage": "invoke",
                "speaker_analysis_reason": "unavailable",
                "speaker_analysis_native_exit_code": 75,
            },
        ),
    )
    python_jsonl, python_npz = run_case(
        "python",
        NativeSpeakerAnalysisResult(status="python"),
    )

    assert fallback_jsonl == python_jsonl
    assert fallback_npz is not None
    assert python_npz is not None
    assert set(fallback_npz) == set(python_npz)
    for key in fallback_npz:
        np.testing.assert_array_equal(fallback_npz[key], python_npz[key])


def test_process_audio_records_analyzed_processing(tmp_path):
    from solstone.observe.processing_record import (
        HANDLER_TRANSCRIBE,
        REASON_OK,
        SCHEMA,
        STATE_ANALYZED,
    )
    from solstone.observe.transcribe.main import process_audio

    raw_path = (
        tmp_path / "chronicle" / "20260416" / "default" / "120000_300" / "audio.m4a"
    )
    raw_path.parent.mkdir(parents=True)
    raw_path.write_bytes(b"\x00" * 2048)
    audio_buffer = np.zeros(10 * SAMPLE_RATE, dtype=np.float32)
    vad_result = VadResult(
        duration=10.0,
        speech_duration=5.0,
        has_speech=True,
        speech_segments=[(1.0, 6.0)],
    )
    statements = [{"id": 0, "start": 0.0, "end": 1.0, "text": "hi"}]
    backend_module = MagicMock()
    backend_module.get_model_info.return_value = {
        "model": "unit",
        "device": "cpu",
        "compute_type": "int8",
    }
    embeddings_data = {
        "embeddings": np.zeros((1, 256), dtype=np.float32),
        "statement_ids": np.zeros((1,), dtype=np.int32),
        "durations_s": np.zeros((1,), dtype=np.float32),
        "encoder": np.array("test"),
    }
    with (
        patch(
            "solstone.observe.transcribe.main.get_journal",
            return_value=str(raw_path.parents[4]),
        ),
        patch(
            "solstone.observe.transcribe.main.get_config",
            return_value={"transcribe": {"preserve_all": False}},
        ),
        patch(
            "solstone.observe.transcribe.main.stt_transcribe", return_value=statements
        ),
        patch(
            "solstone.observe.transcribe.main.get_backend", return_value=backend_module
        ),
        patch(
            "solstone.observe.transcribe.main._embed_statements",
            return_value=embeddings_data,
        ),
        patch(
            "solstone.observe.transcribe.overlap.compute_overlap_and_logprobs",
            return_value=_overlap_result(0.0),
        ),
        patch(
            "solstone.observe.processing_record.now_iso_utc",
            return_value="2026-06-30T12:00:00Z",
        ),
        patch("solstone.observe.transcribe.main.callosum_send"),
    ):
        process_audio(raw_path, audio_buffer, vad_result, {}, backend="parakeet")

    jsonl_path = raw_path.with_suffix(".jsonl")
    header = json.loads(jsonl_path.read_text().splitlines()[0])
    assert header["_solstone_processing"] == {
        "schema": SCHEMA,
        "state": STATE_ANALYZED,
        "reason_code": REASON_OK,
        "handler": HANDLER_TRANSCRIBE,
        "attempted_at": "2026-06-30T12:00:00Z",
        "input_size": 2048,
    }
    assert len(jsonl_path.read_text().splitlines()) >= 2


def test_process_audio_silent_filtered_writes_empty_record(tmp_path):
    from solstone.observe.processing_record import (
        HANDLER_TRANSCRIBE,
        REASON_NO_DECODABLE_AUDIO,
        STATE_EMPTY,
    )
    from solstone.observe.transcribe.main import process_audio

    raw_path = (
        tmp_path / "chronicle" / "20260416" / "default" / "120000_300" / "audio.m4a"
    )
    raw_path.parent.mkdir(parents=True)
    raw_path.write_bytes(b"\x00" * 1024)
    audio_buffer = np.zeros(10 * SAMPLE_RATE, dtype=np.float32)
    vad_result = VadResult(
        duration=10.0,
        speech_duration=5.0,
        has_speech=True,
        speech_segments=[(1.0, 6.0)],
    )
    backend_module = MagicMock()
    backend_module.get_model_info.return_value = {
        "model": "unit",
        "device": "cpu",
        "compute_type": "int8",
    }
    with (
        patch(
            "solstone.observe.transcribe.main.get_journal",
            return_value=str(raw_path.parents[4]),
        ),
        patch(
            "solstone.observe.transcribe.main.get_config",
            return_value={"transcribe": {"preserve_all": False}},
        ),
        patch("solstone.observe.transcribe.main.stt_transcribe", return_value=[]),
        patch(
            "solstone.observe.transcribe.main.get_backend", return_value=backend_module
        ),
        patch("solstone.observe.transcribe.main.callosum_send") as mock_send,
    ):
        process_audio(raw_path, audio_buffer, vad_result, {}, backend="parakeet")

    jsonl_path = raw_path.with_suffix(".jsonl")
    assert jsonl_path.exists()
    header = json.loads(jsonl_path.read_text(encoding="utf-8").splitlines()[0])
    record = header["_solstone_processing"]
    assert record["state"] == STATE_EMPTY
    assert record["reason_code"] == REASON_NO_DECODABLE_AUDIO
    assert record["handler"] == HANDLER_TRANSCRIBE
    assert record["input_size"] == 1024
    assert not raw_path.exists()
    assert mock_send.call_args.kwargs["outcome"] == "filtered"


def test_process_audio_diarizer_failure_is_fail_soft(tmp_path):
    from solstone.observe.transcribe.main import process_audio

    raw_path = (
        tmp_path / "chronicle" / "20260416" / "default" / "120000_300" / "audio.m4a"
    )
    raw_path.parent.mkdir(parents=True)
    raw_path.touch()
    audio_buffer = np.zeros(10 * SAMPLE_RATE, dtype=np.float32)
    vad_result = VadResult(
        duration=10.0,
        speech_duration=5.0,
        has_speech=True,
        speech_segments=[(1.0, 6.0)],
    )
    statements = [{"id": 0, "start": 0.0, "end": 1.0, "text": "hi"}]
    backend_module = MagicMock()
    backend_module.get_model_info.return_value = {
        "model": "medium.en",
        "device": "cpu",
        "compute_type": "int8",
    }

    with (
        patch(
            "solstone.observe.transcribe.main.get_journal",
            return_value=str(raw_path.parents[4]),
        ),
        patch(
            "solstone.observe.transcribe.main.get_config",
            return_value={"transcribe": {"preserve_all": False}},
        ),
        patch(
            "solstone.observe.transcribe.main.stt_transcribe", return_value=statements
        ),
        patch(
            "solstone.observe.transcribe.main.get_backend", return_value=backend_module
        ),
        patch("solstone.observe.transcribe.main._embed_statements", return_value=None),
        patch(
            "solstone.observe.transcribe.overlap.compute_overlap_and_logprobs",
            return_value=_overlap_result(0.5, window_stats=MULTI_STATS),
        ),
        patch(
            "solstone.observe.transcribe.diarize.diarize_auto_k",
            side_effect=RuntimeError("boom"),
        ),
        patch("solstone.observe.transcribe.main.callosum_send") as mock_send,
    ):
        process_audio(raw_path, audio_buffer, vad_result, {}, backend="parakeet")

    assert mock_send.call_args.args[:2] == ("observe", "transcribed")
    assert mock_send.call_args.kwargs["outcome"] == "transcribed"

    jsonl_path = raw_path.with_suffix(".jsonl")
    lines = jsonl_path.read_text(encoding="utf-8").splitlines()
    assert jsonl_path.exists()
    assert "speaker" not in json.loads(lines[1])


def test_process_audio_diarizes_parakeet_cpp_when_overlap_meets_threshold(tmp_path):
    from solstone.observe.transcribe.main import process_audio

    raw_path = (
        tmp_path / "chronicle" / "20260416" / "default" / "120000_300" / "audio.m4a"
    )
    raw_path.parent.mkdir(parents=True)
    raw_path.touch()
    audio_buffer = np.zeros(10 * SAMPLE_RATE, dtype=np.float32)
    vad_result = VadResult(
        duration=10.0,
        speech_duration=5.0,
        has_speech=True,
        speech_segments=[(1.0, 6.0)],
    )
    statements = [{"id": 0, "start": 0.0, "end": 1.0, "text": "hi"}]
    backend_module = MagicMock()
    backend_module.get_model_info.return_value = {
        "model": "unit",
        "device": "cpu",
        "compute_type": "int8",
    }
    logprobs = np.zeros((589, 7), dtype=np.float32)

    with (
        patch(
            "solstone.observe.transcribe.main.get_journal",
            return_value=str(raw_path.parents[4]),
        ),
        patch(
            "solstone.observe.transcribe.main.get_config",
            return_value={"transcribe": {"preserve_all": False}},
        ),
        patch(
            "solstone.observe.transcribe.main.stt_transcribe", return_value=statements
        ),
        patch(
            "solstone.observe.transcribe.main.get_backend", return_value=backend_module
        ),
        patch("solstone.observe.transcribe.main._embed_statements", return_value=None),
        patch(
            "solstone.observe.transcribe.overlap.compute_overlap_and_logprobs",
            return_value=_overlap_result(0.5, logprobs, CLEAN_SINGLE_STATS),
        ),
        patch(
            "solstone.observe.transcribe.diarize.diarize_auto_k",
            return_value=[2],
        ) as mock_diarize,
        patch("solstone.observe.transcribe.main.callosum_send"),
    ):
        process_audio(raw_path, audio_buffer, vad_result, {}, backend="parakeet-cpp")

    mock_diarize.assert_called_once()
    kwargs = mock_diarize.call_args.kwargs
    assert kwargs["avg_log_probs"] is logprobs
    assert kwargs["audio"] is audio_buffer

    jsonl_path = raw_path.with_suffix(".jsonl")
    lines = jsonl_path.read_text(encoding="utf-8").splitlines()
    assert json.loads(lines[1])["speaker"] == 2


def test_legacy_transcript_enrichment_fields_remain_reader_compatible(tmp_path):
    from solstone.observe.hear import load_transcript

    jsonl_path = (
        tmp_path / "chronicle" / "20260416" / "default" / "120000_300" / "audio.jsonl"
    )
    jsonl_path.parent.mkdir(parents=True)
    jsonl_path.write_text(
        "\n".join(
            [
                json.dumps(
                    {
                        "raw": "audio.m4a",
                        "topics": ["planning", "shipping"],
                        "setting": "office",
                    }
                ),
                json.dumps(
                    {
                        "start": "00:00:02",
                        "text": "raw transcript",
                        "corrected": "corrected transcript",
                        "emotion": "excited",
                    }
                ),
            ]
        )
        + "\n",
        encoding="utf-8",
    )

    metadata, entries, formatted_text = load_transcript(jsonl_path)

    assert metadata["topics"] == ["planning", "shipping"]
    assert metadata["setting"] == "office"
    assert entries == [
        {
            "start": "00:00:02",
            "text": "raw transcript",
            "corrected": "corrected transcript",
            "emotion": "excited",
        }
    ]
    assert "Topics: planning, shipping" in formatted_text
    assert "Setting: office" in formatted_text
    assert "[00:00:02] corrected transcript *(excited)*" in formatted_text
    assert "raw transcript" not in formatted_text


class TestJSONLFormat:
    """Test JSONL output format."""

    def test_statements_to_jsonl_includes_duration(self):
        """Audio metadata should include decode-derived duration."""
        lines = _statements_to_jsonl(
            [{"start": 1.0, "end": 2.0, "text": "Hello"}],
            "audio.m4a",
            datetime(2026, 5, 22, 9, 0, 0),
            {"model": "unit", "device": "cpu", "compute_type": "int8"},
            vad_result=VadResult(
                duration=12.34,
                speech_duration=1.0,
                has_speech=True,
            ),
        )

        metadata = json.loads(lines[0])

        assert metadata["duration"] == 12.34
        assert isinstance(metadata["duration"], float)

    def test_statements_to_jsonl_raw_is_producer_invariant(self):
        lines = _statements_to_jsonl(
            [{"start": 1.0, "end": 2.0, "text": "Hello"}],
            "audio.flac",
            datetime(2026, 5, 22, 9, 0, 0),
            {"model": "unit", "device": "cpu", "compute_type": "int8"},
        )

        metadata = json.loads(lines[0])

        # raw is the producer's invariant (relaxed from the shared floor), so the
        # transcriber must keep emitting it.
        assert metadata["raw"] == "audio.flac"

    def test_statements_to_jsonl_writes_speaker_evidence_decision_fields(self):
        lines = _statements_to_jsonl(
            [{"start": 1.0, "end": 2.0, "text": "Hello"}],
            "audio.flac",
            datetime(2026, 5, 22, 9, 0, 0),
            {"model": "unit", "device": "cpu", "compute_type": "int8"},
            speaker_evidence=SpeakerEvidenceDecision("none", 0.0, 0.25),
        )

        metadata = json.loads(lines[0])

        assert metadata["speaker_evidence"] == "none"
        assert metadata["speaker_evidence_multi_fraction"] == 0.0
        assert metadata["speaker_evidence_version"] == "windowed-slots-v1"
        assert "speaker_evidence_mean_window_overlap_share" not in metadata

    def test_statements_to_jsonl_speaker_analysis_producer_is_opt_in(self):
        python_lines = _statements_to_jsonl(
            [{"start": 1.0, "end": 2.0, "text": "Hello"}],
            "audio.flac",
            datetime(2026, 5, 22, 9, 0, 0),
            {"model": "unit", "device": "cpu", "compute_type": "int8"},
        )
        native_lines = _statements_to_jsonl(
            [{"start": 1.0, "end": 2.0, "text": "Hello"}],
            "audio.flac",
            datetime(2026, 5, 22, 9, 0, 0),
            {"model": "unit", "device": "cpu", "compute_type": "int8"},
            speaker_analysis_producer="solstone-core-speakers-analyze-v1",
        )

        python_metadata = json.loads(python_lines[0])
        native_metadata = json.loads(native_lines[0])

        assert "speaker_analysis_producer" not in python_metadata
        assert (
            native_metadata["speaker_analysis_producer"]
            == "solstone-core-speakers-analyze-v1"
        )

    def test_metadata_first_line(self):
        """First line should be metadata with 'raw' field."""
        lines = [
            json.dumps({"raw": "audio.flac"}),
            json.dumps({"start": "00:00:01", "text": "Hello"}),
        ]
        jsonl_content = "\n".join(lines) + "\n"

        parsed_lines = jsonl_content.strip().split("\n")
        assert len(parsed_lines) == 2

        metadata = json.loads(parsed_lines[0])
        assert "raw" in metadata
        assert metadata["raw"] == "audio.flac"

    def test_metadata_includes_transcription_config(self):
        """Metadata should include model, device, and compute_type fields."""
        # Example metadata as produced by _statements_to_jsonl()
        metadata = {
            "raw": "audio.flac",
            "model": "medium.en",
            "device": "cuda",
            "compute_type": "float16",
        }

        # Verify all config fields are present
        assert "model" in metadata
        assert "device" in metadata
        assert "compute_type" in metadata

        # Verify they have expected types
        assert isinstance(metadata["model"], str)
        assert isinstance(metadata["device"], str)
        assert isinstance(metadata["compute_type"], str)

    def test_entry_has_required_fields(self):
        """Transcript entries should have start and text."""
        entry = {"start": "00:00:01", "text": "Hello world"}

        assert "start" in entry
        assert "text" in entry

    def test_entry_source_is_optional(self):
        """Source field should be optional."""
        entry_with_source = {"start": "00:00:01", "text": "Hello", "source": "mic"}
        entry_without_source = {"start": "00:00:01", "text": "Hello"}

        # Both should be valid
        assert "text" in entry_with_source
        assert "text" in entry_without_source

    def test_speaker_not_required(self):
        """Speaker field is no longer required (no diarization)."""
        entry = {"start": "00:00:01", "text": "Hello world"}

        # Should be valid without speaker
        assert "start" in entry
        assert "text" in entry
        assert "speaker" not in entry
