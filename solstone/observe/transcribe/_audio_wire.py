# SPDX-License-Identifier: AGPL-3.0-only
# Copyright (c) 2026 sol pbc

"""Shared audio wire helpers for OpenAI-compatible transcription endpoints."""

from __future__ import annotations

import io

import numpy as np


class AudioResponseContractError(ValueError):
    """Raised when a transcription response violates the expected word schema."""


def audio_to_wav_bytes(audio_array: np.ndarray, sample_rate: int) -> bytes:
    import soundfile as sf

    buf = io.BytesIO()
    sf.write(buf, audio_array, sample_rate, format="WAV", subtype="PCM_16")
    return buf.getvalue()


def parse_words(payload: dict) -> tuple[list[dict], str]:
    if "words" not in payload:
        raise AudioResponseContractError(
            "response missing top-level words[]; server did not honor "
            "timestamp_granularities"
        )
    raw_words = payload["words"]
    if not isinstance(raw_words, list):
        raise AudioResponseContractError("response words must be a list")

    text = str(payload.get("text", "")).strip()
    if not raw_words:
        if text:
            raise AudioResponseContractError("response has text but no word timings")
        return [], text

    words: list[dict] = []
    for item in raw_words:
        if not isinstance(item, dict):
            raise AudioResponseContractError("word timing item must be an object")
        try:
            token = str(item["word"]).strip()
            start = float(item["start"])
            end = float(item["end"])
            conf = item.get("conf")
            probability = float(conf) if conf is not None else 1.0
        except KeyError as exc:
            raise AudioResponseContractError(
                f"word timing missing key: {exc.args[0]}"
            ) from exc
        except (TypeError, ValueError) as exc:
            raise AudioResponseContractError(
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


__all__ = ["AudioResponseContractError", "audio_to_wav_bytes", "parse_words"]
