# SPDX-License-Identifier: AGPL-3.0-only
# Copyright (c) 2026 sol pbc

"""Confidential hosted STT backend over the verified SPP forwarder."""

from __future__ import annotations

import logging
import time
from typing import Any

import httpx
import numpy as np

from solstone.observe.transcribe import ConfidentialTranscribeDeferral
from solstone.observe.transcribe._audio_wire import (
    AudioResponseContractError,
    audio_to_wav_bytes,
    parse_words,
)
from solstone.observe.transcribe.config import confidential_audio_enabled
from solstone.observe.transcribe.utils import build_statements_from_acoustic
from solstone.observe.utils import SAMPLE_RATE
from solstone.think.models import AttestationFailedError, AttestationStaleError
from solstone.think.providers.local_endpoint import resolve_local_endpoint
from solstone.think.services import spp, spp_transport

logger = logging.getLogger(__name__)

_TRANSCRIBE_TIMEOUT_SEC = 30.0
_MODEL_INFO_TIMEOUT_SEC = 5.0
_MODEL_CACHE: str | None = None


def _attestation_reason(exc: AttestationFailedError | AttestationStaleError) -> str:
    status = spp_transport.confidential_probe_status()
    if status is not None:
        _ready, reason = status
        if reason:
            return reason
    return getattr(exc, "reason_code", "attestation_failed")


def _forwarder_base_url() -> str:
    try:
        return spp_transport.confidential_forwarder_base_url()
    except spp_transport.ConfidentialLaneInactiveError as exc:
        raise ConfidentialTranscribeDeferral("confidential_lane_inactive") from exc
    except AttestationStaleError as exc:
        raise ConfidentialTranscribeDeferral(_attestation_reason(exc)) from exc
    except AttestationFailedError as exc:
        raise ConfidentialTranscribeDeferral(_attestation_reason(exc)) from exc


def _headers(block: dict[str, Any]) -> dict[str, str]:
    endpoint = resolve_local_endpoint()
    credential = endpoint.credential
    device_id = str(block.get(spp.CREDENTIAL_FINGERPRINT_FIELD) or "").strip()
    if not credential or not device_id:
        raise ConfidentialTranscribeDeferral("hosted_transcribe_unreachable")
    return {
        "Authorization": f"Bearer {credential}",
        "x-sol-device": device_id,
    }


def _deferral_for_status(status_code: int) -> str:
    if status_code in {400, 413}:
        return "hosted_transcribe_rejected"
    if status_code in {429, 503, 504}:
        return "hosted_transcribe_backpressure"
    return "hosted_transcribe_unexpected_status"


def _parse_payload(response: httpx.Response) -> dict:
    try:
        payload = response.json()
    except ValueError as exc:
        raise ConfidentialTranscribeDeferral(
            "hosted_transcribe_contract_failed"
        ) from exc
    if not isinstance(payload, dict):
        raise ConfidentialTranscribeDeferral("hosted_transcribe_contract_failed")
    return payload


def transcribe(audio: np.ndarray, sample_rate: int, config: dict) -> list[dict]:
    """Transcribe audio with the operated confidential ASR engine."""
    block = spp.confidential_provenance()
    if block is None:
        raise ConfidentialTranscribeDeferral("confidential_lane_inactive")
    if not confidential_audio_enabled():
        raise ConfidentialTranscribeDeferral("confidential_audio_disabled")
    if sample_rate != SAMPLE_RATE:
        raise ValueError(f"audio sample rate must be {SAMPLE_RATE}")
    if not isinstance(audio, np.ndarray):
        raise ValueError("audio must be a numpy ndarray")
    if audio.dtype != np.float32:
        raise ValueError("audio must be float32")
    if audio.ndim != 1:
        raise ValueError("audio must be a 1-D mono ndarray")

    base_url = _forwarder_base_url()
    wav_bytes = audio_to_wav_bytes(audio, sample_rate)
    files = {"file": ("audio.wav", wav_bytes, "audio/wav")}
    data = {
        "response_format": "verbose_json",
        "timestamp_granularities[]": "word",
    }

    started = time.perf_counter()
    try:
        response = httpx.post(
            f"{base_url}/v1/audio/transcriptions",
            files=files,
            data=data,
            headers=_headers(block),
            timeout=_TRANSCRIBE_TIMEOUT_SEC,
        )
    except httpx.TimeoutException as exc:
        raise ConfidentialTranscribeDeferral("hosted_transcribe_unreachable") from exc
    except httpx.TransportError as exc:
        raise ConfidentialTranscribeDeferral("hosted_transcribe_unreachable") from exc

    if response.status_code != 200:
        raise ConfidentialTranscribeDeferral(_deferral_for_status(response.status_code))

    payload = _parse_payload(response)
    try:
        words, _text = parse_words(payload)
    except AudioResponseContractError as exc:
        raise ConfidentialTranscribeDeferral(
            "hosted_transcribe_contract_failed"
        ) from exc
    if not words:
        return []

    audio_sec = len(audio) / sample_rate
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
    logger.info(
        "  Confidential STT returned %d statements for %.2fs audio in %.2fs",
        len(statements),
        audio_sec,
        elapsed,
    )
    return statements


def _model_from_response(response: httpx.Response) -> str | None:
    if response.status_code != 200:
        return None
    try:
        payload = response.json()
    except ValueError:
        text = response.text.strip().strip('"')
        return text or None
    if isinstance(payload, str):
        return payload.strip() or None
    if isinstance(payload, dict):
        model = payload.get("model") or payload.get("id")
        if model:
            return str(model).strip() or None
        data = payload.get("data")
        if not isinstance(data, list) or not data:
            return None
        item = data[0]
        if not isinstance(item, dict):
            return None
        model = item.get("id")
        if not isinstance(model, str):
            return None
        return model.strip() or None
    return None


def get_model_info(config: dict) -> dict:
    """Return hosted model metadata; never fail transcription over this probe."""
    global _MODEL_CACHE
    if _MODEL_CACHE:
        model = _MODEL_CACHE
    else:
        model = "unknown"
        try:
            block = spp.confidential_provenance()
            if block is not None:
                base_url = spp_transport.confidential_forwarder_base_url()
                response = httpx.get(
                    f"{base_url}/v1/audio/models",
                    headers=_headers(block),
                    timeout=_MODEL_INFO_TIMEOUT_SEC,
                )
                checked = _model_from_response(response)
                if checked:
                    _MODEL_CACHE = checked
                    model = checked
        except Exception:
            logger.info("Confidential STT model identity check failed", exc_info=True)
            model = "unknown"
    return {
        "model": model,
        "device": "confidential",
        "per_word_confidence": False,
    }


__all__ = ["get_model_info", "transcribe"]
