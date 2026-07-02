# SPDX-License-Identifier: AGPL-3.0-only
# Copyright (c) 2026 sol pbc

from __future__ import annotations

import io

import httpx
import numpy as np
import pytest
import soundfile as sf

from solstone.observe.transcribe import _parakeet_cpp as parakeet_cpp
from solstone.observe.transcribe import get_backend, get_backend_list
from solstone.think import parakeet_readiness
from solstone.think.providers.parakeet_install import ParakeetProviderError
from solstone.think.providers.parakeet_server import (
    STATE_READY,
    ParakeetServerInfo,
    ParakeetServerNotReady,
)


class _Response:
    def __init__(self, status_code: int = 200, payload=None, text: str = "") -> None:
        self.status_code = status_code
        self._payload = payload if payload is not None else {}
        self.text = text

    def json(self):
        if isinstance(self._payload, Exception):
            raise self._payload
        return self._payload


def _server() -> ParakeetServerInfo:
    return ParakeetServerInfo(
        model_id=parakeet_readiness.PARAKEET_CPP_MODEL_FILENAME,
        port=4567,
        base_url="http://127.0.0.1:4567",
        state=STATE_READY,
    )


def _payload(words=None, text: str = "") -> dict:
    return {
        "text": text,
        "words": words
        if words is not None
        else [{"word": "hello.", "start": 0.1, "end": 0.5, "conf": 0.75}],
    }


def _run_transcribe(monkeypatch: pytest.MonkeyPatch, response: _Response):
    monkeypatch.setattr(parakeet_cpp.parakeet_server, "connect", lambda: _server())
    monkeypatch.setattr(httpx, "post", lambda *_args, **_kwargs: response)
    return parakeet_cpp.transcribe(np.zeros(1600, dtype=np.float32), 16000, {})


def test_registry_exposes_parakeet_cpp_backend() -> None:
    assert get_backend("parakeet-cpp") is parakeet_cpp
    backend = next(
        item for item in get_backend_list() if item["name"] == "parakeet-cpp"
    )
    assert backend["settings"] == ["device"]


@pytest.mark.parametrize("device", ["auto", "cpu"])
def test_validate_config_accepts_supported_devices(device: str) -> None:
    assert parakeet_cpp._validate_config({"device": device}) == device


@pytest.mark.parametrize("device", ["cuda", "tpu"])
def test_validate_config_rejects_unsupported_devices(device: str) -> None:
    with pytest.raises(ValueError, match="auto, cpu"):
        parakeet_cpp._validate_config({"device": device})


def test_non_linux_transcribe_and_model_info_raise_provider_error(monkeypatch) -> None:
    monkeypatch.setattr(parakeet_cpp.sys, "platform", "darwin")

    with pytest.raises(ParakeetProviderError) as transcribe_exc:
        parakeet_cpp.transcribe(np.zeros(100, dtype=np.float32), 16000, {})
    with pytest.raises(ParakeetProviderError) as info_exc:
        parakeet_cpp.get_model_info({})

    assert transcribe_exc.value.reason_code == "unsupported_platform"
    assert info_exc.value.reason_code == "unsupported_platform"


def test_request_shape_and_wav_encoding(monkeypatch: pytest.MonkeyPatch) -> None:
    observed = {}

    def fake_post(url, **kwargs):
        observed["url"] = url
        observed.update(kwargs)
        return _Response(payload=_payload(text="hello."))

    monkeypatch.setattr(parakeet_cpp.parakeet_server, "connect", lambda: _server())
    monkeypatch.setattr(httpx, "post", fake_post)

    statements = parakeet_cpp.transcribe(
        np.linspace(-0.5, 0.5, 3200, dtype=np.float32),
        16000,
        {"device": "cpu"},
    )

    assert statements[0]["text"] == "hello."
    assert observed["url"] == "http://127.0.0.1:4567/v1/audio/transcriptions"
    assert observed["timeout"] == parakeet_cpp._DEFAULT_TIMEOUT_SEC
    assert observed["data"] == {
        "response_format": "verbose_json",
        "timestamp_granularities[]": "word",
    }
    filename, wav_bytes, content_type = observed["files"]["file"]
    assert filename == "audio.wav"
    assert content_type == "audio/wav"
    assert wav_bytes.startswith(b"RIFF")
    with sf.SoundFile(io.BytesIO(wav_bytes)) as wav_file:
        assert wav_file.samplerate == 16000
        assert wav_file.channels == 1
        assert wav_file.subtype == "PCM_16"


def test_real_multipart_encoder_encodes_form_fields(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    # Mocking httpx.post never runs the real multipart encoder, which is
    # where this bug lived (data passed as a list of tuples raised
    # TypeError). This test drives the genuine encoder via MockTransport so
    # a regression re-raises at encode time instead of shipping silently.
    captured = {}

    def real_encoder_post(url, **kwargs):
        def handler(request: httpx.Request) -> httpx.Response:
            captured["content"] = request.read()
            captured["content_type"] = request.headers["content-type"]
            return httpx.Response(200, json=_payload(text="hello."))

        with httpx.Client(transport=httpx.MockTransport(handler)) as client:
            return client.post(url, **kwargs)

    monkeypatch.setattr(parakeet_cpp.parakeet_server, "connect", lambda: _server())
    monkeypatch.setattr(httpx, "post", real_encoder_post)

    statements = parakeet_cpp.transcribe(
        np.linspace(-0.5, 0.5, 3200, dtype=np.float32),
        16000,
        {"device": "cpu"},
    )

    # Encoder ran and parse path still works end-to-end.
    assert statements[0]["text"] == "hello."
    assert captured["content_type"].startswith("multipart/form-data")
    body = captured["content"]
    assert b"response_format" in body
    assert b"verbose_json" in body
    assert b"timestamp_granularities[]" in body
    assert b"word" in body


def test_connect_not_ready_propagates(monkeypatch: pytest.MonkeyPatch) -> None:
    error = ParakeetServerNotReady("warming")
    monkeypatch.setattr(
        parakeet_cpp.parakeet_server,
        "connect",
        lambda: (_ for _ in ()).throw(error),
    )

    with pytest.raises(ParakeetServerNotReady) as exc_info:
        parakeet_cpp.transcribe(np.zeros(100, dtype=np.float32), 16000, {})

    assert exc_info.value is error


@pytest.mark.parametrize(
    "error",
    [httpx.ConnectError("refused"), httpx.TimeoutException("slow")],
)
def test_post_connection_errors_map_to_not_ready(
    monkeypatch: pytest.MonkeyPatch, error: Exception
) -> None:
    monkeypatch.setattr(parakeet_cpp.parakeet_server, "connect", lambda: _server())
    monkeypatch.setattr(
        httpx, "post", lambda *_args, **_kwargs: (_ for _ in ()).throw(error)
    )

    with pytest.raises(ParakeetServerNotReady) as exc_info:
        parakeet_cpp.transcribe(np.zeros(100, dtype=np.float32), 16000, {})

    assert exc_info.value.reason_code == "parakeet_server_not_ready"


def test_http_non_200_is_visible_provider_error(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    with pytest.raises(ParakeetProviderError) as exc_info:
        _run_transcribe(monkeypatch, _Response(status_code=500, text="broken"))

    assert exc_info.value.reason_code == "transcription_http_error"


@pytest.mark.parametrize(
    "payload",
    [ValueError("not json"), ["not", "object"]],
)
def test_invalid_json_is_visible_provider_error(
    monkeypatch: pytest.MonkeyPatch, payload
) -> None:
    with pytest.raises(ParakeetProviderError) as exc_info:
        _run_transcribe(monkeypatch, _Response(payload=payload))

    assert exc_info.value.reason_code == "invalid_json"


def test_missing_words_key_is_contract_violation(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    with pytest.raises(ParakeetProviderError) as exc_info:
        _run_transcribe(monkeypatch, _Response(payload={"text": "hello"}))

    assert exc_info.value.reason_code == "contract_violation"


def test_empty_words_and_empty_text_is_silence(monkeypatch: pytest.MonkeyPatch) -> None:
    assert _run_transcribe(monkeypatch, _Response(payload=_payload([], ""))) == []


def test_empty_words_and_text_is_contract_violation(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    with pytest.raises(ParakeetProviderError) as exc_info:
        _run_transcribe(monkeypatch, _Response(payload=_payload([], "hello")))

    assert exc_info.value.reason_code == "contract_violation"


def test_non_empty_words_build_statements_with_speaker_none(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    statements = _run_transcribe(
        monkeypatch,
        _Response(
            payload=_payload(
                [
                    {"word": "Hello", "start": 0.0, "end": 0.3, "conf": 0.5},
                    {"word": "world.", "start": 0.4, "end": 0.8},
                ],
                "Hello world.",
            )
        ),
    )

    assert len(statements) == 1
    statement = statements[0]
    assert statement["speaker"] is None
    assert statement["text"] == "Hello world."
    assert statement["words"] == [
        {"word": " Hello", "start": 0.0, "end": 0.3, "probability": 0.5},
        {"word": " world.", "start": 0.4, "end": 0.8, "probability": 1.0},
    ]


@pytest.mark.parametrize(
    "word_item",
    [
        "not a dict",
        {"start": 0.0, "end": 0.1},
        {"word": "bad", "start": "nope", "end": 0.1},
        {"word": "bad", "start": 0.0, "end": "nope"},
    ],
)
def test_bad_word_items_are_contract_violations(
    monkeypatch: pytest.MonkeyPatch, word_item
) -> None:
    with pytest.raises(ParakeetProviderError) as exc_info:
        _run_transcribe(monkeypatch, _Response(payload=_payload([word_item], "bad")))

    assert exc_info.value.reason_code == "contract_violation"


def test_get_model_info_does_not_connect(monkeypatch: pytest.MonkeyPatch) -> None:
    def fail_connect():
        raise AssertionError("connect should not be called")

    monkeypatch.setattr(parakeet_cpp.parakeet_server, "connect", fail_connect)

    assert parakeet_cpp.get_model_info({"device": "cpu"}) == {
        "model": parakeet_readiness.PARAKEET_CPP_MODEL_FILENAME,
        "device": "cpu",
        "compute_type": "q8_0",
        "per_word_confidence": True,
    }
