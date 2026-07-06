# SPDX-License-Identifier: AGPL-3.0-only
# Copyright (c) 2026 sol pbc

"""Read-only local ONNX rerank scorer for journal search candidates."""

from __future__ import annotations

import logging
import os
from collections.abc import Sequence
from typing import Any

from solstone.think.providers import rerank_install

LOG = logging.getLogger(__name__)

_MODEL_FILE = "onnx/model.onnx"
_TOKENIZER_FILE = "tokenizer.json"
_KNOWN_INPUTS = frozenset({"input_ids", "attention_mask", "token_type_ids"})
_BATCH_SIZE = 16
_MAX_TOKENS = 512

_session: Any | None = None
_tokenizer: Any | None = None
_disabled = False
_np: Any | None = None


def _disable(reason: str) -> None:
    global _disabled
    if _disabled:
        return
    _disabled = True
    LOG.warning("rerank scorer disabled: %s", reason)


def _file_spec(
    spec: rerank_install.RerankModelSpec, path: str
) -> rerank_install.RerankFileSpec:
    for file_spec in spec.files:
        if file_spec.path == path:
            return file_spec
    raise RuntimeError(f"rerank spec missing required file: {path}")


def _check_asset_sizes(spec: rerank_install.RerankModelSpec) -> None:
    for file_spec in spec.files:
        path = rerank_install.asset_path(file_spec, spec=spec)
        if not path.is_file():
            raise RuntimeError(f"rerank asset missing: {path}")
        actual_size = path.stat().st_size
        if actual_size != file_spec.size_bytes:
            raise RuntimeError(
                f"rerank asset size mismatch for {path}: "
                f"expected {file_spec.size_bytes}, got {actual_size}"
            )


def _load() -> tuple[Any, Any, Any]:
    import numpy as np
    import onnxruntime as ort
    from tokenizers import Tokenizer

    spec = rerank_install.RERANK_MODEL_SPEC
    _check_asset_sizes(spec)
    model_path = rerank_install.asset_path(_file_spec(spec, _MODEL_FILE), spec=spec)
    tokenizer_path = rerank_install.asset_path(
        _file_spec(spec, _TOKENIZER_FILE), spec=spec
    )

    tokenizer = Tokenizer.from_file(str(tokenizer_path))
    tokenizer.enable_truncation(max_length=_MAX_TOKENS)
    tokenizer.enable_padding()

    session_options = ort.SessionOptions()
    session_options.intra_op_num_threads = min(8, os.cpu_count() or 1)
    session = ort.InferenceSession(
        str(model_path),
        sess_options=session_options,
        providers=["CPUExecutionProvider"],
    )
    return tokenizer, session, np


def _ensure_loaded() -> tuple[Any, Any, Any]:
    global _np, _session, _tokenizer
    if _session is None or _tokenizer is None or _np is None:
        _tokenizer, _session, _np = _load()
    return _tokenizer, _session, _np


def _score_batch(
    query: str,
    batch: Sequence[str],
    *,
    tokenizer: Any,
    session: Any,
    np: Any,
) -> list[float]:
    encodings = tokenizer.encode_batch([(query, text) for text in batch])
    arrays = {
        "input_ids": np.asarray(
            [encoding.ids for encoding in encodings], dtype=np.int64
        ),
        "attention_mask": np.asarray(
            [encoding.attention_mask for encoding in encodings], dtype=np.int64
        ),
        "token_type_ids": np.asarray(
            [encoding.type_ids for encoding in encodings], dtype=np.int64
        ),
    }
    declared = {input_info.name for input_info in session.get_inputs()}
    unknown = declared - _KNOWN_INPUTS
    if unknown:
        names = ", ".join(sorted(unknown))
        raise RuntimeError(f"rerank model declares unsupported inputs: {names}")
    feed = {name: arrays[name] for name in declared if name in arrays}
    if not feed:
        raise RuntimeError("rerank model declares no supported inputs")
    output = session.run(None, feed)[0]
    if output.shape != (len(batch), 1):
        raise RuntimeError(
            f"rerank model returned unexpected output shape: {output.shape}"
        )
    return [float(value) for value in output[:, 0]]


def score(query: str, texts: Sequence[str]) -> list[float] | None:
    if not texts:
        return []
    if _disabled:
        return None
    try:
        tokenizer, session, np = _ensure_loaded()
        scores: list[float] = []
        for start in range(0, len(texts), _BATCH_SIZE):
            batch = texts[start : start + _BATCH_SIZE]
            scores.extend(
                _score_batch(query, batch, tokenizer=tokenizer, session=session, np=np)
            )
        return scores
    except Exception as exc:
        _disable(str(exc))
        return None
