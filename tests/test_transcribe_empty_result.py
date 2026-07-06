# SPDX-License-Identifier: AGPL-3.0-only
# Copyright (c) 2026 sol pbc

"""Tests for empty-result handling in process_audio."""

import argparse
import json
import logging
from unittest.mock import MagicMock, patch

import numpy as np
import pytest

from solstone.observe.processing_record import REASON_NO_DECODABLE_AUDIO, STATE_EMPTY
from solstone.observe.utils import SAMPLE_RATE
from solstone.observe.vad import VadResult

SOUND_TAGS = {
    "engine": "ced.cpp v0.1.0",
    "model": "ced-tiny-q8_0",
    "threshold": 0.1,
    "window_s": 10,
    "agg": "max",
    "windows": 1,
    "tags": {"Music": 0.201, "Silence": 0.5},
}

SILENCE_SOUND_TAGS = {
    "engine": "ced.cpp v0.1.0",
    "model": "ced-tiny-q8_0",
    "threshold": 0.1,
    "window_s": 10,
    "agg": "max",
    "windows": 1,
    "tags": {"Silence": 0.7, "White noise": 0.3},
}


@pytest.fixture
def raw_path(tmp_path):
    path = tmp_path / "chronicle" / "20260416" / "default" / "120000_300" / "audio.m4a"
    path.parent.mkdir(parents=True)
    path.touch()
    return path


@pytest.fixture
def audio_buffer():
    return np.zeros(10 * SAMPLE_RATE, dtype=np.float32)


@pytest.fixture
def vad_result():
    return VadResult(
        duration=10.0,
        speech_duration=5.0,
        has_speech=True,
        speech_segments=[(1.0, 6.0)],
    )


@pytest.fixture
def no_speech_vad_result():
    return VadResult(
        duration=10.0,
        speech_duration=0.0,
        has_speech=False,
        speech_segments=[],
    )


def _backend_module() -> MagicMock:
    backend_module = MagicMock()
    backend_module.get_model_info.return_value = {
        "model": "medium.en",
        "device": "cpu",
        "compute_type": "int8",
    }
    return backend_module


def _read_header(jsonl_path):
    return json.loads(jsonl_path.read_text(encoding="utf-8").splitlines()[0])


def test_process_audio_speech_writes_sound_tags_and_keeps_audio(
    raw_path,
    audio_buffer,
    vad_result,
):
    from solstone.observe.transcribe.main import process_audio

    statements = [{"id": 0, "start": 0.0, "end": 1.0, "text": "hi"}]

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
        process_audio(
            raw_path,
            audio_buffer,
            vad_result,
            {},
            backend="parakeet",
            sound_tags=SOUND_TAGS,
        )

    assert raw_path.exists()
    header = _read_header(raw_path.with_suffix(".jsonl"))
    assert header["sound_tags"] == SOUND_TAGS
    assert mock_send.call_args.kwargs["outcome"] == "transcribed"


def test_empty_statements_filter_path(raw_path, audio_buffer, vad_result):
    from solstone.observe.transcribe.main import process_audio

    with (
        patch(
            "solstone.observe.transcribe.main.get_config",
            return_value={"transcribe": {"preserve_all": False}},
        ),
        patch(
            "solstone.observe.transcribe.main.get_journal",
            return_value=str(raw_path.parents[4]),
        ),
        patch("solstone.observe.transcribe.main.stt_transcribe", return_value=[]),
        patch(
            "solstone.observe.transcribe.main.get_backend",
            return_value=_backend_module(),
        ),
        patch("solstone.observe.transcribe.main.callosum_send") as mock_send,
    ):
        process_audio(raw_path, audio_buffer, vad_result, {}, backend="parakeet")

    assert not raw_path.exists()
    assert not raw_path.with_suffix(".jsonl").exists()
    assert mock_send.call_args.args[:2] == ("observe", "transcribed")
    assert mock_send.call_args.kwargs["outcome"] == "filtered"


def test_empty_statements_preserve_path(raw_path, audio_buffer, vad_result):
    from solstone.observe.transcribe.main import process_audio

    with (
        patch(
            "solstone.observe.transcribe.main.get_config",
            return_value={"transcribe": {"preserve_all": True}},
        ),
        patch(
            "solstone.observe.transcribe.main.get_journal",
            return_value=str(raw_path.parents[4]),
        ),
        patch("solstone.observe.transcribe.main.stt_transcribe", return_value=[]),
        patch(
            "solstone.observe.transcribe.main.get_backend",
            return_value=_backend_module(),
        ),
        patch("solstone.observe.transcribe.main.callosum_send") as mock_send,
    ):
        process_audio(
            raw_path,
            audio_buffer,
            vad_result,
            {},
            backend="parakeet",
            sound_tags=SOUND_TAGS,
        )

    assert raw_path.exists()
    jsonl_path = raw_path.with_suffix(".jsonl")
    lines = jsonl_path.read_text(encoding="utf-8").splitlines()
    header = json.loads(lines[0])
    record = header["_solstone_processing"]
    assert len(lines) == 1
    assert header["sound_tags"] == SOUND_TAGS
    assert record["state"] == STATE_EMPTY
    assert record["reason_code"] == REASON_NO_DECODABLE_AUDIO
    assert mock_send.call_args.args[:2] == ("observe", "transcribed")
    assert mock_send.call_args.kwargs["outcome"] == "preserved"


def test_empty_statements_salient_writes_empty_jsonl_then_deletes_audio(
    raw_path,
    audio_buffer,
    vad_result,
):
    from solstone.observe.transcribe.main import process_audio

    with (
        patch(
            "solstone.observe.transcribe.main.get_config",
            return_value={"transcribe": {"preserve_all": False}},
        ),
        patch(
            "solstone.observe.transcribe.main.get_journal",
            return_value=str(raw_path.parents[4]),
        ),
        patch("solstone.observe.transcribe.main.stt_transcribe", return_value=[]),
        patch(
            "solstone.observe.transcribe.main.get_backend",
            return_value=_backend_module(),
        ),
        patch("solstone.observe.transcribe.main.callosum_send") as mock_send,
    ):
        process_audio(
            raw_path,
            audio_buffer,
            vad_result,
            {},
            backend="parakeet",
            sound_tags=SOUND_TAGS,
        )

    jsonl_path = raw_path.with_suffix(".jsonl")
    assert not raw_path.exists()
    assert jsonl_path.exists()
    assert _read_header(jsonl_path)["sound_tags"] == SOUND_TAGS
    assert mock_send.call_args.kwargs["outcome"] == "filtered"


def test_empty_statements_non_salient_deletes_without_jsonl(
    raw_path,
    audio_buffer,
    vad_result,
):
    from solstone.observe.transcribe.main import process_audio

    with (
        patch(
            "solstone.observe.transcribe.main.get_config",
            return_value={"transcribe": {"preserve_all": False}},
        ),
        patch(
            "solstone.observe.transcribe.main.get_journal",
            return_value=str(raw_path.parents[4]),
        ),
        patch("solstone.observe.transcribe.main.stt_transcribe", return_value=[]),
        patch(
            "solstone.observe.transcribe.main.get_backend",
            return_value=_backend_module(),
        ),
        patch("solstone.observe.transcribe.main.callosum_send") as mock_send,
    ):
        process_audio(
            raw_path,
            audio_buffer,
            vad_result,
            {},
            backend="parakeet",
            sound_tags=SILENCE_SOUND_TAGS,
        )

    assert not raw_path.exists()
    assert not raw_path.with_suffix(".jsonl").exists()
    assert mock_send.call_args.kwargs["outcome"] == "filtered"


def test_empty_statements_write_failure_preserves_audio(
    raw_path,
    audio_buffer,
    vad_result,
):
    from solstone.observe.transcribe.main import process_audio

    with (
        patch(
            "solstone.observe.transcribe.main.get_config",
            return_value={"transcribe": {"preserve_all": False}},
        ),
        patch(
            "solstone.observe.transcribe.main.get_journal",
            return_value=str(raw_path.parents[4]),
        ),
        patch("solstone.observe.transcribe.main.stt_transcribe", return_value=[]),
        patch(
            "solstone.observe.transcribe.main.get_backend",
            return_value=_backend_module(),
        ),
        patch(
            "solstone.observe.transcribe.main.write_text",
            side_effect=RuntimeError("disk full"),
        ),
        patch("solstone.observe.transcribe.main.callosum_send") as mock_send,
    ):
        with pytest.raises(SystemExit):
            process_audio(
                raw_path,
                audio_buffer,
                vad_result,
                {},
                backend="parakeet",
                sound_tags=SOUND_TAGS,
            )

    assert raw_path.exists()
    assert not raw_path.with_suffix(".jsonl").exists()
    assert mock_send.call_args.kwargs["outcome"] == "failed"


def test_vad_no_speech_preserve_path_writes_empty_record(
    raw_path,
    no_speech_vad_result,
):
    from solstone.observe.transcribe.main import _process_one

    args = argparse.Namespace(backend=None, cpu=False, model=None, redo=False)

    with (
        patch(
            "solstone.observe.transcribe.main.load_audio",
            return_value=np.zeros(10 * SAMPLE_RATE, dtype=np.float32),
        ),
        patch("solstone.observe.vad.run_vad", return_value=no_speech_vad_result),
        patch("solstone.observe.transcribe.main.tag_audio", return_value=SOUND_TAGS),
        patch("solstone.observe.transcribe.main.callosum_send") as mock_send,
    ):
        _process_one(
            raw_path,
            args,
            {"preserve_all": True},
            "parakeet",
            [],
        )

    assert raw_path.exists()
    jsonl_path = raw_path.with_suffix(".jsonl")
    lines = jsonl_path.read_text(encoding="utf-8").splitlines()
    header = json.loads(lines[0])
    record = header["_solstone_processing"]
    assert len(lines) == 1
    assert header["sound_tags"] == SOUND_TAGS
    assert record["state"] == STATE_EMPTY
    assert record["reason_code"] == REASON_NO_DECODABLE_AUDIO
    assert header["backend"] == "unknown"
    assert mock_send.call_args.args[:2] == ("observe", "transcribed")
    assert mock_send.call_args.kwargs["outcome"] == "preserved"


def test_vad_no_speech_salient_writes_empty_jsonl_then_deletes_audio(
    raw_path,
    no_speech_vad_result,
):
    from solstone.observe.transcribe.main import _process_one

    args = argparse.Namespace(backend=None, cpu=False, model=None, redo=False)

    with (
        patch(
            "solstone.observe.transcribe.main.load_audio",
            return_value=np.zeros(10 * SAMPLE_RATE, dtype=np.float32),
        ),
        patch("solstone.observe.vad.run_vad", return_value=no_speech_vad_result),
        patch("solstone.observe.transcribe.main.tag_audio", return_value=SOUND_TAGS),
        patch("solstone.observe.transcribe.main.callosum_send") as mock_send,
    ):
        _process_one(
            raw_path,
            args,
            {"preserve_all": False},
            "parakeet",
            [],
        )

    jsonl_path = raw_path.with_suffix(".jsonl")
    assert not raw_path.exists()
    assert jsonl_path.exists()
    assert _read_header(jsonl_path)["sound_tags"] == SOUND_TAGS
    assert mock_send.call_args.kwargs["outcome"] == "filtered"


def test_vad_no_speech_non_salient_deletes_without_jsonl(
    raw_path,
    no_speech_vad_result,
):
    from solstone.observe.transcribe.main import _process_one

    args = argparse.Namespace(backend=None, cpu=False, model=None, redo=False)

    with (
        patch(
            "solstone.observe.transcribe.main.load_audio",
            return_value=np.zeros(10 * SAMPLE_RATE, dtype=np.float32),
        ),
        patch("solstone.observe.vad.run_vad", return_value=no_speech_vad_result),
        patch(
            "solstone.observe.transcribe.main.tag_audio",
            return_value=SILENCE_SOUND_TAGS,
        ),
        patch("solstone.observe.transcribe.main.callosum_send") as mock_send,
    ):
        _process_one(
            raw_path,
            args,
            {"preserve_all": False},
            "parakeet",
            [],
        )

    assert not raw_path.exists()
    assert not raw_path.with_suffix(".jsonl").exists()
    assert mock_send.call_args.kwargs["outcome"] == "filtered"


def test_vad_no_speech_write_failure_preserves_audio(
    raw_path,
    no_speech_vad_result,
):
    from solstone.observe.transcribe.main import _process_one

    args = argparse.Namespace(backend=None, cpu=False, model=None, redo=False)

    with (
        patch(
            "solstone.observe.transcribe.main.load_audio",
            return_value=np.zeros(10 * SAMPLE_RATE, dtype=np.float32),
        ),
        patch("solstone.observe.vad.run_vad", return_value=no_speech_vad_result),
        patch("solstone.observe.transcribe.main.tag_audio", return_value=SOUND_TAGS),
        patch(
            "solstone.observe.transcribe.main.write_text",
            side_effect=RuntimeError("disk full"),
        ),
        patch("solstone.observe.transcribe.main.callosum_send"),
    ):
        with pytest.raises(RuntimeError, match="disk full"):
            _process_one(
                raw_path,
                args,
                {"preserve_all": False},
                "parakeet",
                [],
            )

    assert raw_path.exists()
    assert not raw_path.with_suffix(".jsonl").exists()


def test_vad_no_speech_tagger_raise_degrades_to_existing_filter_path(
    raw_path,
    no_speech_vad_result,
    caplog: pytest.LogCaptureFixture,
):
    from solstone.observe.transcribe.main import _process_one

    args = argparse.Namespace(backend=None, cpu=False, model=None, redo=False)
    caplog.set_level(logging.WARNING)

    with (
        patch(
            "solstone.observe.transcribe.main.load_audio",
            return_value=np.zeros(10 * SAMPLE_RATE, dtype=np.float32),
        ),
        patch("solstone.observe.vad.run_vad", return_value=no_speech_vad_result),
        patch(
            "solstone.observe.transcribe.main.tag_audio",
            side_effect=RuntimeError("tagger bug"),
        ),
        patch("solstone.observe.transcribe.main.callosum_send") as mock_send,
    ):
        _process_one(
            raw_path,
            args,
            {"preserve_all": False},
            "parakeet",
            [],
        )

    assert not raw_path.exists()
    assert not raw_path.with_suffix(".jsonl").exists()
    assert mock_send.call_args.kwargs["outcome"] == "filtered"
    warnings = [
        record
        for record in caplog.records
        if record.levelno == logging.WARNING
        and "sound tagging failed" in record.message
    ]
    assert len(warnings) == 1


def test_backend_raise_propagates(raw_path, audio_buffer, vad_result):
    from solstone.observe.transcribe.main import process_audio

    with (
        patch(
            "solstone.observe.transcribe.main.stt_transcribe",
            side_effect=RuntimeError("rev.ai 502"),
        ),
        patch(
            "solstone.observe.transcribe.main.get_journal",
            return_value=str(raw_path.parents[4]),
        ),
        patch("solstone.observe.transcribe.main.callosum_send") as mock_send,
    ):
        with pytest.raises(SystemExit) as exc_info:
            process_audio(raw_path, audio_buffer, vad_result, {}, backend="parakeet")

    assert exc_info.value.code == 1
    assert raw_path.exists()
    assert mock_send.call_args.args[:2] == ("observe", "transcribed")
    assert mock_send.call_args.kwargs["outcome"] == "failed"
    assert mock_send.call_args.kwargs["backend"] == "parakeet"
    assert (
        mock_send.call_args.kwargs["input"] == "20260416/default/120000_300/audio.m4a"
    )
    assert mock_send.call_args.kwargs["error"] == "RuntimeError: rev.ai 502"
