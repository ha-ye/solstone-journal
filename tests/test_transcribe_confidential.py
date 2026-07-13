# SPDX-License-Identifier: AGPL-3.0-only
# Copyright (c) 2026 sol pbc

from __future__ import annotations

import argparse
import io
from collections.abc import Callable
from pathlib import Path
from unittest.mock import Mock, patch

import httpx
import numpy as np
import pytest
import soundfile as sf

from solstone.observe.exit_codes import EXIT_PROVIDER_BLOCKED
from solstone.observe.transcribe import ConfidentialTranscribeDeferral, confidential
from solstone.observe.utils import SAMPLE_RATE
from solstone.observe.vad import VadResult
from solstone.think.models import AttestationFailedError, AttestationStaleError
from solstone.think.providers.local_endpoint import LocalEndpoint
from solstone.think.services import spp, spp_transport


def _block() -> dict[str, str]:
    return {
        "endpoint_url": "https://spp.example.test",
        spp.CREDENTIAL_FINGERPRINT_FIELD: "device-fingerprint",
    }


def _local_endpoint() -> LocalEndpoint:
    return LocalEndpoint(
        base_url="https://configured-endpoint.example/v1",
        served_model_id="configured-model",
        credential="confidential-token",
        is_bundled=False,
    )


def _install_verified_lane(
    monkeypatch: pytest.MonkeyPatch,
    *,
    base_url: str = "http://127.0.0.1:4567",
) -> None:
    monkeypatch.setattr(confidential.spp, "confidential_provenance", lambda: _block())
    monkeypatch.setattr(
        confidential.spp_transport,
        "confidential_forwarder_base_url",
        lambda: base_url,
    )
    monkeypatch.setattr(confidential, "resolve_local_endpoint", _local_endpoint)


def _audio(seconds: float = 0.2) -> np.ndarray:
    return np.linspace(-0.5, 0.5, int(SAMPLE_RATE * seconds), dtype=np.float32)


def test_confidential_transcribe_posts_canonical_wav_to_forwarder(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _install_verified_lane(monkeypatch)
    captured: dict = {}

    def fake_post(url: str, **kwargs):
        captured["url"] = url
        captured.update(kwargs)
        return httpx.Response(
            200,
            json={
                "text": "hello.",
                "words": [
                    {
                        "word": "hello.",
                        "start": 0.0,
                        "end": 0.2,
                        "conf": None,
                    }
                ],
            },
        )

    monkeypatch.setattr(confidential.httpx, "post", fake_post)

    statements = confidential.transcribe(_audio(), SAMPLE_RATE, {})

    assert captured["url"] == "http://127.0.0.1:4567/v1/audio/transcriptions"
    assert "configured-endpoint.example" not in captured["url"]
    assert captured["timeout"] == 30.0
    assert captured["data"] == {
        "response_format": "verbose_json",
        "timestamp_granularities[]": "word",
    }
    assert captured["headers"] == {
        "Authorization": "Bearer confidential-token",
        "x-sol-device": "device-fingerprint",
    }
    filename, wav_bytes, content_type = captured["files"]["file"]
    assert filename == "audio.wav"
    assert content_type == "audio/wav"
    with sf.SoundFile(io.BytesIO(wav_bytes)) as wav_file:
        assert wav_file.samplerate == SAMPLE_RATE
        assert wav_file.channels == 1
        assert wav_file.subtype == "PCM_16"

    assert len(statements) == 1
    assert statements[0]["speaker"] is None
    assert set(statements[0]) == {"id", "start", "end", "text", "words", "speaker"}
    assert statements[0]["words"][0]["probability"] == 1.0


def test_confidential_direct_backend_call_respects_disabled_setting(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _install_verified_lane(monkeypatch)
    monkeypatch.setattr(confidential, "confidential_audio_enabled", lambda: False)
    post = Mock(side_effect=AssertionError("audio egress attempted"))
    monkeypatch.setattr(confidential.httpx, "post", post)

    with pytest.raises(ConfidentialTranscribeDeferral) as exc_info:
        confidential.transcribe(_audio(), SAMPLE_RATE, {})

    assert exc_info.value.reason_code == "confidential_audio_disabled"
    post.assert_not_called()


@pytest.mark.parametrize(
    ("install_failure", "expected_reason"),
    [
        (
            lambda monkeypatch: monkeypatch.setattr(
                confidential.spp, "confidential_provenance", lambda: None
            ),
            "confidential_lane_inactive",
        ),
        (
            lambda monkeypatch: (
                monkeypatch.setattr(
                    confidential.spp,
                    "confidential_provenance",
                    lambda: _block(),
                ),
                monkeypatch.setattr(
                    confidential.spp_transport,
                    "confidential_forwarder_base_url",
                    Mock(
                        side_effect=spp_transport.ConfidentialLaneInactiveError(
                            "lane off"
                        )
                    ),
                ),
            ),
            "confidential_lane_inactive",
        ),
        (
            lambda monkeypatch: (
                monkeypatch.setattr(
                    confidential.spp,
                    "confidential_provenance",
                    lambda: _block(),
                ),
                monkeypatch.setattr(
                    confidential.spp_transport,
                    "confidential_forwarder_base_url",
                    Mock(side_effect=AttestationFailedError("gateway down")),
                ),
                monkeypatch.setattr(
                    confidential.spp_transport,
                    "confidential_probe_status",
                    lambda: (False, "attestation_unreachable"),
                ),
            ),
            "attestation_unreachable",
        ),
        (
            lambda monkeypatch: (
                monkeypatch.setattr(
                    confidential.spp,
                    "confidential_provenance",
                    lambda: _block(),
                ),
                monkeypatch.setattr(
                    confidential.spp_transport,
                    "confidential_forwarder_base_url",
                    Mock(side_effect=AttestationStaleError("stale")),
                ),
                monkeypatch.setattr(
                    confidential.spp_transport,
                    "confidential_probe_status",
                    lambda: (False, "attestation_stale"),
                ),
            ),
            "attestation_stale",
        ),
    ],
)
def test_confidential_pre_post_failures_map_to_deferral_reasons(
    monkeypatch: pytest.MonkeyPatch,
    install_failure: Callable[[pytest.MonkeyPatch], object],
    expected_reason: str,
) -> None:
    install_failure(monkeypatch)
    post = Mock(side_effect=AssertionError("egress attempted"))
    monkeypatch.setattr(confidential.httpx, "post", post)

    with pytest.raises(ConfidentialTranscribeDeferral) as exc_info:
        confidential.transcribe(_audio(), SAMPLE_RATE, {})

    assert exc_info.value.reason_code == expected_reason
    post.assert_not_called()


@pytest.mark.parametrize(
    ("response_or_error", "expected_reason"),
    [
        (httpx.TimeoutException("slow"), "hosted_transcribe_unreachable"),
        (httpx.ConnectError("down"), "hosted_transcribe_unreachable"),
        (httpx.Response(400), "hosted_transcribe_rejected"),
        (httpx.Response(413), "hosted_transcribe_rejected"),
        (httpx.Response(429), "hosted_transcribe_backpressure"),
        (httpx.Response(503), "hosted_transcribe_backpressure"),
        (httpx.Response(504), "hosted_transcribe_backpressure"),
        (httpx.Response(401), "hosted_transcribe_unexpected_status"),
        (httpx.Response(500), "hosted_transcribe_unexpected_status"),
        (
            httpx.Response(200, content=b"not-json"),
            "hosted_transcribe_contract_failed",
        ),
        (
            httpx.Response(200, json=["not", "an", "object"]),
            "hosted_transcribe_contract_failed",
        ),
        (
            httpx.Response(200, json={"text": "missing timings"}),
            "hosted_transcribe_contract_failed",
        ),
    ],
)
def test_confidential_post_results_map_to_deferral_reasons(
    monkeypatch: pytest.MonkeyPatch,
    response_or_error: httpx.Response | Exception,
    expected_reason: str,
) -> None:
    _install_verified_lane(monkeypatch)
    if isinstance(response_or_error, Exception):
        post = Mock(side_effect=response_or_error)
    else:
        post = Mock(return_value=response_or_error)
    monkeypatch.setattr(confidential.httpx, "post", post)

    with pytest.raises(ConfidentialTranscribeDeferral) as exc_info:
        confidential.transcribe(_audio(), SAMPLE_RATE, {})

    assert exc_info.value.reason_code == expected_reason
    post.assert_called_once()


@pytest.mark.parametrize(
    ("audio", "sample_rate"),
    [
        (np.zeros(10, dtype=np.float32), SAMPLE_RATE + 1),
        (np.zeros(10, dtype=np.float64), SAMPLE_RATE),
        (np.zeros((10, 1), dtype=np.float32), SAMPLE_RATE),
    ],
)
def test_confidential_input_contract_violations_are_hard_failures(
    monkeypatch: pytest.MonkeyPatch,
    audio: np.ndarray,
    sample_rate: int,
) -> None:
    _install_verified_lane(monkeypatch)
    post = Mock(side_effect=AssertionError("egress attempted"))
    monkeypatch.setattr(confidential.httpx, "post", post)

    with pytest.raises(ValueError):
        confidential.transcribe(audio, sample_rate, {})

    post.assert_not_called()


def test_confidential_get_model_info_caches_checked_identity(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _install_verified_lane(monkeypatch)
    monkeypatch.setattr(confidential, "_MODEL_CACHE", None)
    get = Mock(return_value=httpx.Response(200, json={"model": "checked-model"}))
    monkeypatch.setattr(confidential.httpx, "get", get)

    first = confidential.get_model_info({})
    second = confidential.get_model_info({})

    assert first == second
    assert first["model"] == "checked-model"
    assert first["per_word_confidence"] is False
    assert get.call_count == 1


def test_confidential_get_model_info_parses_openai_list_envelope(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _install_verified_lane(monkeypatch)
    monkeypatch.setattr(confidential, "_MODEL_CACHE", None)
    get = Mock(
        return_value=httpx.Response(
            200,
            json={
                "object": "list",
                "data": [
                    {
                        "id": "nvidia/parakeet-tdt-0.6b-v3",
                        "object": "model",
                        "owned_by": "sol pbc",
                    }
                ],
            },
        )
    )
    monkeypatch.setattr(confidential.httpx, "get", get)

    first = confidential.get_model_info({})
    second = confidential.get_model_info({})

    expected = {
        "model": "nvidia/parakeet-tdt-0.6b-v3",
        "device": "confidential",
        "per_word_confidence": False,
    }
    assert first == expected
    assert second == expected
    assert get.call_count == 1


def test_confidential_get_model_info_reports_unknown_for_empty_model_envelope(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _install_verified_lane(monkeypatch)
    monkeypatch.setattr(confidential, "_MODEL_CACHE", None)
    get = Mock(return_value=httpx.Response(200, json={"object": "list", "data": []}))
    monkeypatch.setattr(confidential.httpx, "get", get)

    info = confidential.get_model_info({})

    assert info["model"] == "unknown"
    assert info["per_word_confidence"] is False
    assert get.call_count == 1


def test_confidential_get_model_info_reports_unknown_on_failed_check(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _install_verified_lane(monkeypatch)
    monkeypatch.setattr(confidential, "_MODEL_CACHE", None)
    get = Mock(side_effect=httpx.ConnectError("down"))
    monkeypatch.setattr(confidential.httpx, "get", get)

    info = confidential.get_model_info({})

    assert info["model"] == "unknown"
    assert info["per_word_confidence"] is False
    assert get.call_count == 1


def _raw_audio_path(tmp_path: Path) -> Path:
    path = tmp_path / "chronicle" / "20260416" / "default" / "120000_300" / "audio.m4a"
    path.parent.mkdir(parents=True)
    path.write_bytes(b"audio")
    return path


def _vad_result(duration: float = 10.0) -> VadResult:
    return VadResult(
        duration=duration,
        speech_duration=5.0,
        has_speech=True,
        speech_segments=[(0.0, 5.0)],
    )


def test_process_audio_confidential_deferral_uses_backend_reason(
    tmp_path: Path,
) -> None:
    from solstone.observe.transcribe.main import process_audio

    raw_path = _raw_audio_path(tmp_path)
    with (
        patch(
            "solstone.observe.transcribe.main.stt_transcribe",
            side_effect=ConfidentialTranscribeDeferral("hosted_transcribe_unreachable"),
        ),
        patch("solstone.observe.transcribe.main.callosum_send") as mock_send,
    ):
        with pytest.raises(SystemExit) as exc_info:
            process_audio(
                raw_path,
                np.zeros(10 * SAMPLE_RATE, dtype=np.float32),
                _vad_result(),
                {},
                backend="confidential",
            )

    assert exc_info.value.code == EXIT_PROVIDER_BLOCKED
    assert raw_path.exists()
    assert not raw_path.with_suffix(".jsonl").exists()

    kwargs = mock_send.call_args.kwargs
    assert kwargs["outcome"] == "deferred"
    assert kwargs["reason"] == "hosted_transcribe_unreachable"
    assert kwargs["backend"] == "confidential"


def test_process_one_builds_confidential_backend_config(tmp_path: Path) -> None:
    from solstone.observe.transcribe.main import _process_one

    audio_path = _raw_audio_path(tmp_path)
    captured = {}

    def fake_process_audio(
        _audio_path,
        _audio_buffer,
        _vad_result_value,
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
        patch("solstone.observe.vad.run_vad", return_value=_vad_result()),
        patch("solstone.observe.vad.reduce_audio", return_value=(None, None)),
        patch(
            "solstone.observe.transcribe.main.process_audio",
            side_effect=fake_process_audio,
        ),
    ):
        _process_one(
            audio_path,
            argparse.Namespace(backend=None, cpu=False, model=None, redo=False),
            {"backend": "confidential"},
            "confidential",
            [],
        )

    assert captured == {"backend": "confidential", "backend_config": {}}


def test_process_one_routes_over_cap_confidential_audio_to_local(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from solstone.observe.transcribe.main import _process_one
    from solstone.observe.transcribe.resource import CONFIDENTIAL_STT_MAX_AUDIO_SECONDS

    audio_path = _raw_audio_path(tmp_path)
    captured = {}
    audio = np.zeros(
        int((CONFIDENTIAL_STT_MAX_AUDIO_SECONDS + 1) * SAMPLE_RATE),
        dtype=np.float32,
    )
    monkeypatch.setenv("GOOGLE_API_KEY", "test-key")

    def fake_process_audio(
        _audio_path,
        _audio_buffer,
        _vad_result_value,
        backend_config,
        **kwargs,
    ):
        captured["backend_config"] = backend_config
        captured["backend"] = kwargs["backend"]

    with (
        patch("solstone.observe.transcribe.main.load_audio", return_value=audio),
        patch(
            "solstone.observe.vad.run_vad",
            return_value=_vad_result(CONFIDENTIAL_STT_MAX_AUDIO_SECONDS + 1),
        ),
        patch("solstone.observe.vad.reduce_audio", return_value=(None, None)),
        patch(
            "solstone.observe.transcribe.main.local_stt_backend",
            return_value="parakeet",
        ),
        patch(
            "solstone.observe.transcribe.main.process_audio",
            side_effect=fake_process_audio,
        ),
    ):
        _process_one(
            audio_path,
            argparse.Namespace(backend=None, cpu=False, model=None, redo=False),
            {},
            "confidential",
            [],
        )

    assert captured == {"backend": "parakeet", "backend_config": {}}
