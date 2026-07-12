# SPDX-License-Identifier: AGPL-3.0-only
# Copyright (c) 2026 sol pbc

"""Linux Parakeet backend via supervised parakeet.cpp HTTP server."""

from __future__ import annotations

import io
import logging
import sys
import time

import numpy as np

from solstone.think import parakeet_readiness
from solstone.think.providers import parakeet_server
from solstone.think.providers.parakeet_install import ParakeetProviderError
from solstone.think.providers.parakeet_server import ParakeetServerNotReady

from .utils import build_statements_from_acoustic

logger = logging.getLogger(__name__)

_DEFAULT_TIMEOUT_SEC = 300.0
_DEFAULT_DEVICE = "auto"
_COMPUTE_TYPE = "q8_0"


def _require_linux() -> None:
    if not sys.platform.startswith("linux"):
        raise ParakeetProviderError(
            "unsupported_platform", "parakeet-cpp is only supported on Linux"
        )


def _validate_config(config: dict) -> str:
    device = config.get("device", _DEFAULT_DEVICE)
    if device not in {"auto", "cpu"}:
        raise ValueError("device must be one of: auto, cpu")
    return device


def resolve_serving_device(config: dict, *, default: str | None = None) -> str | None:
    """Return supervisor placement when recorded, else the configured device."""
    placement = parakeet_server.read_parakeet_placement()
    if placement is not None:
        return placement
    return config.get("device", default)


def _audio_to_wav_bytes(audio_array: np.ndarray, sample_rate: int) -> bytes:
    import soundfile as sf

    buf = io.BytesIO()
    sf.write(buf, audio_array, sample_rate, format="WAV", subtype="PCM_16")
    return buf.getvalue()


def _is_retryable_transport_error(exc: Exception) -> bool:
    """Whether a transport failure is worth deferring for, or is a bug to surface.

    LocalProtocolError (we built a malformed request) and UnsupportedProtocol (the
    URL scheme is wrong) are defects on our side of the wire: no amount of retrying
    fixes them, and deferring on them would hide the bug behind a daily retry
    forever -- exactly the silent failure this module exists to prevent.
    """
    import httpx

    return not isinstance(exc, (httpx.LocalProtocolError, httpx.UnsupportedProtocol))


def _transport_retry_reason(exc: Exception) -> str:
    """Classify a retryable httpx transport failure into a machine-readable reason.

    This function is the single source of truth for the transport reason strings.
    Checks run subclass-before-base per the httpx hierarchy: every class below is a
    TransportError, ConnectError is a NetworkError, and RemoteProtocolError is a
    ProtocolError (server death mid-response) but *not* a NetworkError -- which is
    why catching NetworkError alone used to miss a crashed server entirely.

    "read_timeout" covers every TimeoutException, including ConnectTimeout and
    PoolTimeout (which are timeouts, not connect/network errors, in httpx's tree).
    """
    import httpx

    if isinstance(exc, httpx.TimeoutException):
        return "read_timeout"
    if isinstance(exc, httpx.RemoteProtocolError):
        return "server_disconnected"
    if isinstance(exc, httpx.ConnectError):
        return "connect_error"
    if isinstance(exc, httpx.NetworkError):
        return "network_error"
    return "transport_error"


def _invalid_contract(message: str) -> ParakeetProviderError:
    return ParakeetProviderError("contract_violation", message)


def _parse_words(payload: dict) -> tuple[list[dict], str]:
    if "words" not in payload:
        raise _invalid_contract(
            "response missing top-level words[]; server did not honor "
            "timestamp_granularities"
        )
    raw_words = payload["words"]
    if not isinstance(raw_words, list):
        raise _invalid_contract("response words must be a list")

    text = str(payload.get("text", "")).strip()
    if not raw_words:
        if text:
            raise _invalid_contract("response has text but no word timings")
        return [], text

    words: list[dict] = []
    for item in raw_words:
        if not isinstance(item, dict):
            raise _invalid_contract("word timing item must be an object")
        try:
            token = str(item["word"]).strip()
            start = float(item["start"])
            end = float(item["end"])
            conf = item.get("conf")
            probability = float(conf) if conf is not None else 1.0
        except KeyError as exc:
            raise _invalid_contract(f"word timing missing key: {exc.args[0]}") from exc
        except (TypeError, ValueError) as exc:
            raise _invalid_contract(
                "word timing contains invalid numeric value"
            ) from exc
        words.append(
            {
                "word": f" {token}",
                "start": start,
                "end": end,
                "probability": probability,
            }
        )
    return words, text


def transcribe(audio: np.ndarray, sample_rate: int, config: dict) -> list[dict]:
    """Transcribe audio using a supervised parakeet.cpp server."""
    _require_linux()
    audio_array = np.asarray(audio, dtype=np.float32)
    if audio_array.ndim != 1:
        raise ValueError("audio must be a 1-D mono ndarray")

    device = _validate_config(config)
    wav_bytes = _audio_to_wav_bytes(audio_array, sample_rate)
    server = parakeet_server.connect()

    import httpx

    started = time.perf_counter()
    files = {"file": ("audio.wav", wav_bytes, "audio/wav")}
    data = {
        "response_format": "verbose_json",
        "timestamp_granularities[]": "word",
    }
    try:
        response = httpx.post(
            f"{server.base_url}/v1/audio/transcriptions",
            files=files,
            data=data,
            timeout=_DEFAULT_TIMEOUT_SEC,
        )
    except httpx.TransportError as exc:
        # TransportError is the common base for connect/timeout/network *and*
        # protocol failures.  A server that dies mid-request drops the connection
        # and raises RemoteProtocolError, which is not a NetworkError -- catching
        # the base class is what makes that death a retryable deferral rather than
        # a hard failure.  DecodingError/TooManyRedirects/HTTPStatusError are not
        # TransportErrors and deliberately stay uncaught here.
        if not _is_retryable_transport_error(exc):
            raise
        raise ParakeetServerNotReady(
            f"parakeet-server unreachable during transcription: {exc}",
            retry_reason=_transport_retry_reason(exc),
        ) from exc

    if response.status_code != 200:
        raise ParakeetProviderError(
            "transcription_http_error",
            f"HTTP {response.status_code}: {response.text[:200]}",
        )

    try:
        payload = response.json()
    except Exception as exc:
        raise ParakeetProviderError("invalid_json", str(exc)) from exc
    if not isinstance(payload, dict):
        raise ParakeetProviderError("invalid_json", "response JSON was not an object")

    words, _text = _parse_words(payload)
    if not words:
        return []

    audio_sec = len(audio_array) / sample_rate
    acoustic_segments = [
        {
            "id": 1,
            "start": 0.0,
            "end": audio_sec,
            "text": "".join(word["word"] for word in words).strip(),
            "words": words,
        }
    ]
    statements = build_statements_from_acoustic(acoustic_segments)
    for statement in statements:
        statement["speaker"] = None

    elapsed = time.perf_counter() - started
    rtfx = audio_sec / max(elapsed, 0.001)
    logger.info(
        f"  Transcribed {len(statements)} statements, {audio_sec:.2f}s speech "
        f"in {elapsed:.2f}s (RTFx: {rtfx:.2f}) "
        f"[model={parakeet_readiness.PARAKEET_CPP_MODEL_FILENAME} "
        f"device={device} quant={_COMPUTE_TYPE}]"
    )
    return statements


def get_model_info(config: dict) -> dict:
    """Return parakeet.cpp model metadata for transcript JSONL headers."""
    _require_linux()
    device = _validate_config(config)
    resolved_device = resolve_serving_device(config, default=device)
    return {
        "model": parakeet_readiness.PARAKEET_CPP_MODEL_FILENAME,
        "device": resolved_device,
        "compute_type": _COMPUTE_TYPE,
        "per_word_confidence": True,
    }
