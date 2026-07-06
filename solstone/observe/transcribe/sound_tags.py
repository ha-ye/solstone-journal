# SPDX-License-Identifier: AGPL-3.0-only
# Copyright (c) 2026 sol pbc

"""Ambient sound tagging for transcribed audio segments.

ced.cpp v0.1.0 C API reference:

```c
int ced_capi_abi_version(void);
ced_ctx* ced_capi_load(const char* gguf_path);
void ced_capi_free(ced_ctx* ctx);
const char* ced_capi_last_error(const ced_ctx* ctx);
int         ced_capi_num_classes(const ced_ctx* ctx);
const char* ced_capi_label(const ced_ctx* ctx, int index);
int         ced_capi_sample_rate(const ced_ctx* ctx);
char* ced_capi_classify_pcm_json(ced_ctx* ctx, const float* samples, int n_samples,
                                 int sample_rate, int top_k);
char* ced_capi_classify_path_json(ced_ctx* ctx, const char* wav_path, int top_k);
int ced_capi_classify_pcm(ced_ctx* ctx, const float* samples, int n_samples,
                          int sample_rate, ced_tag* out, int max_tags);
void ced_capi_free_string(char* s);
```

Only the symbols used by this runtime path are bound.
"""

from __future__ import annotations

import atexit
import json
import logging
from collections.abc import Callable
from pathlib import Path
from typing import Any

LOG = logging.getLogger(__name__)

SCORE_FLOOR = 0.1
WINDOW_S = 10
MIN_TAIL_S = 1
SALIENCE_THRESHOLD = 0.2
SILENCE_FAMILY = frozenset({"Silence", "White noise"})
ENGINE = "ced.cpp v0.1.0"
MODEL = "ced-tiny-q8_0"
AGG = "max"
ABI_VERSION = 1
CLASSIFY_SAMPLE_RATE = 16000

_disabled = False
_lib: Any | None = None
_ctx: Any | None = None
_np: Any | None = None
_ctypes: Any | None = None
_cleanup_registered = False


def parse_classify_json(raw: str) -> dict[str, float]:
    data = json.loads(raw)
    if not isinstance(data, list):
        raise ValueError("ced classify JSON must be an array")

    tags: dict[str, float] = {}
    for item in data:
        if not isinstance(item, dict):
            raise ValueError("ced classify JSON entries must be objects")
        label = item.get("label")
        score = item.get("score")
        if not isinstance(label, str) or not label:
            raise ValueError("ced classify JSON entry label must be a non-empty string")
        if not isinstance(score, (int, float)) or isinstance(score, bool):
            raise ValueError("ced classify JSON entry score must be numeric")
        score_float = float(score)
        if label not in tags or score_float > tags[label]:
            tags[label] = score_float
    return tags


def _window_spans(n_samples: int, sample_rate: int) -> list[tuple[int, int]]:
    if sample_rate <= 0:
        raise ValueError("sample_rate must be positive")
    if n_samples <= 0:
        return []

    window_samples = WINDOW_S * sample_rate
    min_tail_samples = MIN_TAIL_S * sample_rate
    spans: list[tuple[int, int]] = []

    full_windows = n_samples // window_samples
    for i in range(full_windows):
        start = i * window_samples
        spans.append((start, start + window_samples))

    tail_start = full_windows * window_samples
    tail_samples = n_samples - tail_start
    if tail_samples >= min_tail_samples:
        spans.append((tail_start, n_samples))
    return spans


def aggregate(per_window: list[dict[str, float]]) -> dict[str, float]:
    max_scores: dict[str, float] = {}
    for tags in per_window:
        for label, score in tags.items():
            score_float = float(score)
            if label not in max_scores or score_float > max_scores[label]:
                max_scores[label] = score_float

    kept = [
        (label, score) for label, score in max_scores.items() if score > SCORE_FLOOR
    ]
    kept.sort(key=lambda item: (-item[1], item[0]))
    return {label: round(score, 3) for label, score in kept}


def is_salient(tags: dict[str, float]) -> bool:
    return any(
        label not in SILENCE_FAMILY and score >= SALIENCE_THRESHOLD
        for label, score in tags.items()
    )


def _disable(reason: str) -> None:
    global _disabled
    if _disabled:
        return
    _disabled = True
    LOG.warning("sound tagger disabled: %s", reason)


def _require_nonempty(path: Path, label: str) -> None:
    if not path.is_file():
        raise RuntimeError(f"{label} missing: {path}")
    if path.stat().st_size <= 0:
        raise RuntimeError(f"{label} is empty: {path}")


def _check_asset_sizes() -> None:
    from solstone.think.providers import ced_install

    model = ced_install.model_path()
    if not model.is_file():
        raise RuntimeError(f"ced model missing: {model}")
    actual_size = model.stat().st_size
    expected_size = ced_install.CED_MODEL_SPEC.size_bytes
    if actual_size != expected_size:
        raise RuntimeError(
            f"ced model size mismatch: expected {expected_size}, got {actual_size}"
        )

    _require_nonempty(ced_install.engine_lib_path(), "ced engine library")
    _require_nonempty(ced_install.engine_header_path(), "ced C API header")


def _last_error(lib: Any, ctx: Any | None) -> str | None:
    try:
        raw = lib.ced_capi_last_error(ctx)
    except Exception:
        return None
    if not raw:
        return None
    return raw.decode("utf-8", "replace")


def _cleanup_context() -> None:
    global _ctx
    if _lib is None or _ctx is None:
        return
    try:
        _lib.ced_capi_free(_ctx)
    except Exception:
        return
    _ctx = None


def _bind_symbols(lib: Any, ctypes: Any) -> None:
    lib.ced_capi_abi_version.restype = ctypes.c_int
    lib.ced_capi_abi_version.argtypes = []

    lib.ced_capi_load.restype = ctypes.c_void_p
    lib.ced_capi_load.argtypes = [ctypes.c_char_p]

    lib.ced_capi_free.restype = None
    lib.ced_capi_free.argtypes = [ctypes.c_void_p]

    lib.ced_capi_last_error.restype = ctypes.c_char_p
    lib.ced_capi_last_error.argtypes = [ctypes.c_void_p]

    lib.ced_capi_classify_pcm_json.restype = ctypes.c_void_p
    lib.ced_capi_classify_pcm_json.argtypes = [
        ctypes.c_void_p,
        ctypes.POINTER(ctypes.c_float),
        ctypes.c_int,
        ctypes.c_int,
        ctypes.c_int,
    ]

    lib.ced_capi_free_string.restype = None
    lib.ced_capi_free_string.argtypes = [ctypes.c_void_p]


def _load() -> tuple[Any, Any, Any, Any] | None:
    global _cleanup_registered, _ctx, _ctypes, _lib, _np

    if _disabled:
        return None
    if (
        _lib is not None
        and _ctx is not None
        and _np is not None
        and _ctypes is not None
    ):
        return _lib, _ctx, _np, _ctypes

    try:
        _check_asset_sizes()

        import ctypes

        import numpy as np

        from solstone.think.providers import ced_install

        lib_path = ced_install.engine_lib_path()
        model_path = ced_install.model_path()
        lib = ctypes.CDLL(str(lib_path))
        _bind_symbols(lib, ctypes)

        abi_version = lib.ced_capi_abi_version()
        if abi_version != ABI_VERSION:
            _disable(
                f"ced C API ABI mismatch: expected {ABI_VERSION}, got {abi_version}"
            )
            return None

        ctx = lib.ced_capi_load(str(model_path).encode("utf-8"))
        if not ctx:
            reason = _last_error(lib, None) or "ced_capi_load returned NULL"
            _disable(f"ced model load failed: {reason}")
            return None

        _lib = lib
        _ctx = ctx
        _np = np
        _ctypes = ctypes
        if not _cleanup_registered:
            atexit.register(_cleanup_context)
            _cleanup_registered = True
        return _lib, _ctx, _np, _ctypes
    except Exception as exc:
        _disable(str(exc))
        return None


def _ensure_loaded() -> tuple[Any, Any, Any, Any] | None:
    if _disabled:
        return None
    return _load()


def _classify_pcm_json(window: Any, _sample_rate: int) -> dict[str, float]:
    loaded = _ensure_loaded()
    if loaded is None:
        raise RuntimeError("sound tagger disabled")
    lib, ctx, np, ctypes = loaded

    contiguous = np.ascontiguousarray(window, dtype=np.float32)
    samples = contiguous.ctypes.data_as(ctypes.POINTER(ctypes.c_float))
    result_ptr = lib.ced_capi_classify_pcm_json(
        ctx,
        samples,
        int(contiguous.shape[0]),
        CLASSIFY_SAMPLE_RATE,
        0,
    )
    if not result_ptr:
        reason = _last_error(lib, ctx) or "ced_capi_classify_pcm_json returned NULL"
        raise RuntimeError(reason)

    try:
        raw = ctypes.cast(result_ptr, ctypes.c_char_p).value
        if raw is None:
            raise RuntimeError("ced_capi_classify_pcm_json returned no bytes")
        return parse_classify_json(raw.decode("utf-8"))
    finally:
        lib.ced_capi_free_string(result_ptr)


def tag_audio(
    buffer: Any,
    sample_rate: int,
    *,
    classify: Callable[[Any, int], dict[str, float]] | None = None,
) -> dict[str, Any] | None:
    if _disabled:
        return None

    try:
        spans = _window_spans(len(buffer), sample_rate)
        if not spans:
            return None

        classifier = classify
        if classifier is None:
            if _ensure_loaded() is None:
                return None
            classifier = _classify_pcm_json

        per_window: list[dict[str, float]] = []
        failures: list[Exception] = []
        for i, (start, end) in enumerate(spans):
            try:
                per_window.append(classifier(buffer[start:end], sample_rate))
            except Exception as exc:
                failures.append(exc)
                LOG.debug("sound tagger window %d failed: %s", i, exc)

        if not per_window:
            cause = failures[0] if failures else "no successful windows"
            LOG.warning("sound tagger failed for all windows: %s", cause)
            return None

        tags = aggregate(per_window)
        if not tags:
            return None

        return {
            "engine": ENGINE,
            "model": MODEL,
            "threshold": SCORE_FLOOR,
            "window_s": WINDOW_S,
            "agg": AGG,
            "windows": len(per_window),
            "tags": tags,
        }
    except Exception as exc:
        _disable(str(exc))
        return None
