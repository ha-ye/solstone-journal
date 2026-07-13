# SPDX-License-Identifier: AGPL-3.0-only
# Copyright (c) 2026 sol pbc

"""Speech-to-text backend registry and shared utilities.

This package provides a pluggable STT backend system with:
- Backend registry for dispatch to different STT providers
- Shared utilities for statement building from word-level data
- Normalized statement format for all backends

Terminology:
- "statement" = individual transcript entry (sentence or speaker turn)
- "segment" = journal directory (HHMMSS_LEN/ time window) - NOT used here

Available backends:
- parakeet: Default local backend via Apple Silicon helper or Linux parakeet.cpp
- confidential: Operated attested STT over the verified confidential forwarder
- revai: Rev.ai cloud API (speaker diarization)
- gemini: Google Gemini API (speaker diarization)

Backend Interface:
    Each backend module must export a transcribe() function:

    def transcribe(
        audio: np.ndarray,      # float32, mono, sample_rate Hz
        sample_rate: int,       # typically 16000
        config: dict,           # backend-specific config
    ) -> list[dict]:
        '''Return statements with word-level timestamps (if available).'''

    Statement format:
    {
        "id": int,              # sequential, starting from 1
        "start": float,         # seconds
        "end": float,           # seconds
        "text": str,            # transcribed text
        "words": list[dict] | None,  # word-level data if available
        "speaker": int | None,  # speaker ID (revai/gemini, 1-indexed)
    }

    Word format (when available):
    {
        "word": str,
        "start": float,
        "end": float,
        "probability": float,
    }
"""

from __future__ import annotations

import logging
from importlib import import_module
from types import ModuleType
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    import numpy as np

# ---------------------------------------------------------------------------
# Backend Registry
# ---------------------------------------------------------------------------

BACKEND_REGISTRY: dict[str, str] = {
    "revai": "solstone.observe.transcribe.revai",
    "gemini": "solstone.observe.transcribe.gemini",
    "parakeet": "solstone.observe.transcribe.parakeet",
    "parakeet-cpp": "solstone.observe.transcribe._parakeet_cpp",
    "confidential": "solstone.observe.transcribe.confidential",
}

# ---------------------------------------------------------------------------
# Backend Metadata
# ---------------------------------------------------------------------------
# Display labels, descriptions, and settings schemas for each backend.
# Used by settings UI to dynamically build backend dropdowns and forms.
# ---------------------------------------------------------------------------

BACKEND_METADATA: dict[str, dict] = {
    "revai": {
        "label": "Rev.ai - Cloud with speaker diarization",
        "description": "Cloud-based transcription with speaker identification",
        "env_key": "REVAI_ACCESS_TOKEN",
        "settings": ["model"],
        "local": False,
        "selectable": True,
    },
    "gemini": {
        "label": "Gemini - Cloud with speaker diarization",
        "description": "Cloud-based transcription with speaker identification",
        "env_key": "GOOGLE_API_KEY",
        "settings": [],
        "local": False,
        "selectable": True,
    },
    "parakeet": {
        "label": "Parakeet - Local processing (Apple Silicon CoreML or Linux parakeet.cpp)",
        "description": "On-device speech recognition via Parakeet TDT; macOS uses a FluidAudio/CoreML helper, Linux uses the supervised parakeet.cpp server. Requires `make install`.",
        "env_key": None,
        "settings": ["model_version", "device", "timeout_sec"],
        "local": True,
        "selectable": True,
    },
    "parakeet-cpp": {
        "label": "Parakeet.cpp - Local processing (Linux)",
        "description": "On-device speech recognition via a supervised parakeet.cpp server (mudler/parakeet.cpp). Linux only; install with `journal install-provider parakeet`.",
        "env_key": None,
        "settings": ["device"],
        "local": True,
        "selectable": True,
    },
    "confidential": {
        "label": "Confidential - Operated attested transcription",
        "description": "Hosted speech recognition over the verified confidential processing lane",
        "env_key": None,
        "settings": [],
        "local": False,
        "selectable": False,
    },
}


_PUBLIC_METADATA_FIELDS = ("label", "description", "env_key", "settings")


def _validate_backend_metadata() -> None:
    missing = sorted(set(BACKEND_REGISTRY) - set(BACKEND_METADATA))
    stale = sorted(set(BACKEND_METADATA) - set(BACKEND_REGISTRY))
    errors: list[str] = []
    if missing:
        errors.append(f"missing metadata for: {', '.join(missing)}")
    if stale:
        errors.append(f"metadata for unregistered backends: {', '.join(stale)}")

    for name in sorted(set(BACKEND_REGISTRY) & set(BACKEND_METADATA)):
        meta = BACKEND_METADATA[name]
        for marker in ("local", "selectable"):
            if marker not in meta:
                errors.append(f"{name!r} metadata missing {marker!r} marker")
                continue
            if not isinstance(meta[marker], bool):
                errors.append(f"{name!r} metadata marker {marker!r} must be bool")

    if errors:
        raise RuntimeError("Invalid STT backend metadata: " + "; ".join(errors))


_validate_backend_metadata()


def get_backend(name: str) -> ModuleType:
    """Get STT backend module by name.

    Args:
        name: Backend name (e.g., "parakeet")

    Returns:
        Backend module with transcribe() function

    Raises:
        ValueError: If backend name is not registered
    """
    if name not in BACKEND_REGISTRY:
        valid = ", ".join(sorted(BACKEND_REGISTRY.keys()))
        raise ValueError(f"Unknown STT backend: {name!r}. Valid backends: {valid}")

    return import_module(BACKEND_REGISTRY[name])


def get_backend_list() -> list[dict]:
    """Get list of backends with metadata for UI display.

    Returns:
        List of backend info dicts, each containing:
        - name: Backend identifier (e.g., "parakeet")
        - label: Display label
        - description: Short description
        - env_key: Environment variable for API key (None for local backends)
        - settings: List of configurable field names
    """
    backends = []
    for name in BACKEND_REGISTRY:
        meta = BACKEND_METADATA[name]
        if not meta["selectable"]:
            continue
        backends.append(
            {"name": name, **{field: meta[field] for field in _PUBLIC_METADATA_FIELDS}}
        )
    return backends


class ConfidentialAudioEgressError(Exception):
    """Raised when confidential processing refuses a cloud STT backend."""


class ConfidentialTranscribeDeferral(Exception):
    """Raised when confidential transcription must defer without sending audio."""

    def __init__(self, reason_code: str) -> None:
        super().__init__(reason_code)
        self.reason_code = reason_code


def transcribe(
    backend: str,
    audio: "np.ndarray",
    sample_rate: int,
    config: dict,
    speech_segments: list[tuple[float, float]] | None = None,
) -> list[dict]:
    """Dispatch transcription to the specified backend.

    Args:
        backend: Backend name (e.g., "parakeet")
        audio: Audio buffer (float32, mono)
        sample_rate: Sample rate in Hz (typically 16000)
        config: Backend-specific configuration dict
        speech_segments: Optional VAD speech segments for chunk-based transcription.
            Currently only used by the Gemini backend for timestamp anchoring.

    Returns:
        List of statement dicts with id, start, end, text, and optionally words
    """
    from solstone.think.services import spp

    confidential_lane_active = spp.confidential_provenance() is not None
    if confidential_lane_active:
        meta = BACKEND_METADATA.get(backend)
        if meta is None:
            logging.warning(
                "Confidential lane refused unregistered STT backend %s; raw audio must stay local",
                backend,
            )
            raise ConfidentialAudioEgressError(
                f"confidential lane blocks unregistered STT backend {backend!r}; "
                "raw audio must stay local"
            )
        if meta["local"]:
            pass
        elif backend == "confidential":
            from solstone.observe.transcribe.config import confidential_audio_enabled

            if not confidential_audio_enabled():
                raise ConfidentialTranscribeDeferral("confidential_audio_disabled")
        else:
            logging.warning(
                "Confidential lane refused cloud STT backend %s; raw audio must stay local",
                backend,
            )
            raise ConfidentialAudioEgressError(
                f"confidential lane blocks cloud STT backend {backend!r}; "
                "raw audio must stay local"
            )
    elif backend == "confidential":
        raise ConfidentialTranscribeDeferral("confidential_lane_inactive")

    backend_mod = get_backend(backend)

    # Pass speech_segments to backends that support it (currently only Gemini)
    if backend == "gemini" and speech_segments is not None:
        return backend_mod.transcribe(audio, sample_rate, config, speech_segments)

    return backend_mod.transcribe(audio, sample_rate, config)


# ---------------------------------------------------------------------------
# Re-exports (utilities from utils.py, main entry point from main.py)
# ---------------------------------------------------------------------------

from solstone.observe.transcribe.main import (
    DEFAULT_MIN_SPEECH_SECONDS,
    MIN_STATEMENT_DURATION,
    main,
    process_audio,
)
from solstone.observe.transcribe.utils import (
    SENTENCE_ENDINGS,
    build_statement,
    build_statements_from_acoustic,
    is_apple_silicon,
)

__all__ = [
    # Registry
    "BACKEND_REGISTRY",
    "BACKEND_METADATA",
    "get_backend",
    "get_backend_list",
    "ConfidentialAudioEgressError",
    "ConfidentialTranscribeDeferral",
    "transcribe",
    # Utilities
    "SENTENCE_ENDINGS",
    "is_apple_silicon",
    "build_statement",
    "build_statements_from_acoustic",
    # Main entry point
    "main",
    "process_audio",
    "DEFAULT_MIN_SPEECH_SECONDS",
    "MIN_STATEMENT_DURATION",
]
