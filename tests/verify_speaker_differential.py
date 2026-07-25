# SPDX-License-Identifier: AGPL-3.0-only
# Copyright (c) 2026 sol pbc

"""Differential harness for the local speaker pipeline.

The bundle schema says ``statements`` everywhere because
``solstone.observe.transcribe.main`` owns the production pipeline vocabulary.
At the single boundary into ``solstone.observe.transcribe.diarize``, those same
records are passed as that module's local ``sentences`` parameter.
"""

from __future__ import annotations

import argparse
import contextlib
import hashlib
import importlib
import json
import logging
import platform
import sys
from collections.abc import Iterator, Sequence
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import numpy as np

from solstone.observe.transcribe import diarize, overlap
from solstone.observe.vad import AudioReduction, restore_statement_timestamps
from solstone.think.utils import get_rev
from tests._speaker_differential_fixtures import (
    COMPARATOR_THRESHOLDS,
    COSINE_NORM_FLOOR,
    EMBEDDING_MAX_ABS_TOLERANCE,
    EMBEDDING_MEDIAN_COSINE_SIMILARITY,
    EMBEDDING_MIN_COSINE_SIMILARITY,
    EVIDENCE_FLOAT_ABS_TOLERANCE,
    LOGPROB_ARGMAX_AGREEMENT_MIN,
    LOGPROB_MAX_ABS_TOLERANCE,
    LOGPROB_MEDIAN_ABS_TOLERANCE,
    SAMPLE_RATE,
)

transcribe_main = importlib.import_module("solstone.observe.transcribe.main")

logger = logging.getLogger(__name__)

ROOT = Path(__file__).resolve().parent.parent
BUNDLE_SCHEMA = "solstone-speaker-differential-bundle"
REPORT_SCHEMA = "solstone-speaker-differential-report"
SCHEMA_VERSION = 1
MANIFEST_KEY = "__speaker_differential_manifest_json__"
PLACEHOLDER_WAV_PATH = Path("__speaker_differential_audio_supplied_no_wav_read.wav")

PRESENT = "present"
ABSENT_NONE = "absent-none"
NOT_EVALUATED = "not-evaluated"
FIELD_STATES = frozenset({PRESENT, ABSENT_NONE, NOT_EVALUATED})

EQUAL = "equal"
FUNCTIONALLY_EQUAL = "functionally-equal"
UNEXPECTED_DIFFERS = "unexpected-differs"
CLASSIFICATIONS = frozenset({EQUAL, FUNCTIONALLY_EQUAL, UNEXPECTED_DIFFERS})
HARNESS_ERROR = "harness_error"

COMPARE = "compare"
STATE_PAIR_VERDICT = {
    (PRESENT, PRESENT): COMPARE,
    (PRESENT, ABSENT_NONE): UNEXPECTED_DIFFERS,
    (PRESENT, NOT_EVALUATED): UNEXPECTED_DIFFERS,
    (ABSENT_NONE, PRESENT): UNEXPECTED_DIFFERS,
    (ABSENT_NONE, ABSENT_NONE): NOT_EVALUATED,
    (ABSENT_NONE, NOT_EVALUATED): UNEXPECTED_DIFFERS,
    (NOT_EVALUATED, PRESENT): UNEXPECTED_DIFFERS,
    (NOT_EVALUATED, ABSENT_NONE): UNEXPECTED_DIFFERS,
    (NOT_EVALUATED, NOT_EVALUATED): NOT_EVALUATED,
}

LABEL_NULL_SENTINEL = np.int32(-1)

INPUT_STATEMENT_IDS = "inputs.statement_embedding.statement_ids"
INPUT_STATEMENT_SPANS = "inputs.statement_embedding.spans_s"
INPUT_DIARIZATION_IDS = "inputs.diarization.statement_ids"
INPUT_DIARIZATION_SPANS = "inputs.diarization.spans_s"

PYANNOTE_LOGPROBS = "pyannote.avg_log_probs"
PYANNOTE_WINDOW_STATS = "pyannote.window_stats"

EVIDENCE_SPEAKER = "evidence.speaker_evidence"
EVIDENCE_MULTI_FRACTION = "evidence.multi_window_fraction"
EVIDENCE_MEAN_OVERLAP = "evidence.mean_window_overlap_share"
EVIDENCE_OVERLAP_FRACTION = "evidence.overlap_fraction"

STATEMENT_EMBEDDINGS = "statement_embeddings.embeddings"
STATEMENT_EMBEDDING_IDS = "statement_embeddings.statement_ids"
STATEMENT_DURATIONS = "statement_embeddings.durations_s"
STATEMENT_ENCODER = "statement_embeddings.encoder"

DIARIZATION_INTERVALS = "diarization.intervals"
DIARIZATION_VALID_INTERVALS = "diarization.valid_intervals"
DIARIZATION_INTERVAL_EMBEDDINGS = "diarization.interval_embeddings"
DIARIZATION_CLUSTER_LABELS = "diarization.cluster_labels"
DIARIZATION_STATEMENT_LABELS = "diarization.statement_labels"
DIARIZATION_SILHOUETTE_K = "diarization.silhouette_k"
DIARIZATION_EFFECTIVE_K = "diarization.effective_k"

GATE_DECLINED_FIELDS = frozenset(
    {
        DIARIZATION_INTERVALS,
        DIARIZATION_VALID_INTERVALS,
        DIARIZATION_INTERVAL_EMBEDDINGS,
        DIARIZATION_CLUSTER_LABELS,
        DIARIZATION_STATEMENT_LABELS,
        DIARIZATION_SILHOUETTE_K,
        DIARIZATION_EFFECTIVE_K,
    }
)

COMPONENT_FIELDS = {
    "pyannote_log_probs": (PYANNOTE_LOGPROBS,),
    "evidence": (
        EVIDENCE_SPEAKER,
        EVIDENCE_MULTI_FRACTION,
        EVIDENCE_MEAN_OVERLAP,
        EVIDENCE_OVERLAP_FRACTION,
        PYANNOTE_WINDOW_STATS,
    ),
    "intervals": (DIARIZATION_INTERVALS,),
    "interval_embeddings": (
        DIARIZATION_VALID_INTERVALS,
        DIARIZATION_INTERVAL_EMBEDDINGS,
    ),
    "statement_embeddings": (
        STATEMENT_EMBEDDINGS,
        STATEMENT_EMBEDDING_IDS,
        STATEMENT_DURATIONS,
        STATEMENT_ENCODER,
    ),
    "clustering": (
        DIARIZATION_CLUSTER_LABELS,
        DIARIZATION_SILHOUETTE_K,
        DIARIZATION_EFFECTIVE_K,
    ),
    "statement_labels": (DIARIZATION_STATEMENT_LABELS,),
}

COMPONENT_ORDER = (
    "pyannote_log_probs",
    "evidence",
    "statement_embeddings",
    "intervals",
    "interval_embeddings",
    "clustering",
    "statement_labels",
)

PYANNOTE_THRESHOLD_IDS = {
    "max_abs_diff": "LOGPROB_MAX_ABS_TOLERANCE",
    "median_abs_diff": "LOGPROB_MEDIAN_ABS_TOLERANCE",
    "per_frame_argmax_agreement_fraction": "LOGPROB_ARGMAX_AGREEMENT_MIN",
}

EVIDENCE_THRESHOLD_IDS = {
    EVIDENCE_OVERLAP_FRACTION: "EVIDENCE_FLOAT_ABS_TOLERANCE",
    EVIDENCE_MULTI_FRACTION: "EVIDENCE_FLOAT_ABS_TOLERANCE",
    EVIDENCE_MEAN_OVERLAP: "EVIDENCE_FLOAT_ABS_TOLERANCE",
    EVIDENCE_SPEAKER: "exact",
    PYANNOTE_WINDOW_STATS: "exact",
}

EMBEDDING_THRESHOLD_IDS = {
    "max_abs_component_diff": "EMBEDDING_MAX_ABS_TOLERANCE",
    "min_cosine_similarity": "EMBEDDING_MIN_COSINE_SIMILARITY",
    "median_cosine_similarity": "EMBEDDING_MEDIAN_COSINE_SIMILARITY",
}


class HarnessError(RuntimeError):
    """Raised when the harness cannot produce or compare trustworthy data."""


Bundle = dict[str, Any]


def _json_default(value: object) -> object:
    if isinstance(value, Path):
        return str(value)
    if isinstance(value, np.generic):
        return value.item()
    raise TypeError(f"Object of type {type(value).__name__} is not JSON serializable")


def _render_report(report: dict[str, Any]) -> str:
    return json.dumps(report, default=_json_default, indent=2, sort_keys=True) + "\n"


def _session_providers(session: object | None) -> list[str] | None:
    if session is None:
        return None
    get_providers = getattr(session, "get_providers", None)
    if get_providers is None:
        return None
    try:
        return list(get_providers())
    except Exception:
        logger.exception("failed to read ONNX execution providers")
        return None


def _module_version(module_name: str) -> str | None:
    try:
        module = importlib.import_module(module_name)
    except Exception:
        logger.exception("failed to import %s for provenance", module_name)
        return None
    version = getattr(module, "__version__", None)
    return str(version) if version is not None else None


def _provenance(producer: str) -> dict[str, Any]:
    return {
        "generated_at": datetime.now(UTC).isoformat().replace("+00:00", "Z"),
        "implementation": {"name": producer},
        "harness": {
            "name": "tests.verify_speaker_differential",
            "repo_commit": get_rev(),
            "schema_version": SCHEMA_VERSION,
        },
        "onnx_execution_providers": {
            "statement_encoder": _session_providers(
                getattr(transcribe_main, "_embedder_session", None)
            ),
            "interval_encoder": _session_providers(
                getattr(diarize, "_wespeaker_session", None)
            ),
            "pyannote": _session_providers(getattr(overlap, "_overlap_session", None)),
        },
        "versions": {
            "onnxruntime": _module_version("onnxruntime"),
            "kaldi_native_fbank": _module_version("kaldi_native_fbank"),
        },
        "host": {
            "platform": platform.platform(),
            "python": platform.python_version(),
        },
    }


def new_bundle(*, producer: str = "production-python") -> Bundle:
    return {
        "manifest": {
            "schema": BUNDLE_SCHEMA,
            "schema_version": SCHEMA_VERSION,
            "fields": {},
            "inputs": {},
            "counters": {},
            "stage_errors": [],
            "provenance": _provenance(producer),
            "label_null_sentinel": int(LABEL_NULL_SENTINEL),
            "gate_declined_fields": sorted(GATE_DECLINED_FIELDS),
        },
        "arrays": {},
    }


def _refresh_provenance(bundle: Bundle, producer: str) -> None:
    bundle["manifest"]["provenance"] = _provenance(producer)


def copy_bundle(bundle: Bundle) -> Bundle:
    return {
        "manifest": json.loads(json.dumps(bundle["manifest"], default=_json_default)),
        "arrays": {
            str(key): np.array(value, copy=True)
            for key, value in bundle["arrays"].items()
        },
    }


def _set_array(bundle: Bundle, key: str, value: np.ndarray, component: str) -> None:
    array = np.asarray(value)
    bundle["arrays"][key] = np.array(array, copy=True)
    bundle["manifest"]["fields"][key] = {
        "state": PRESENT,
        "kind": "array",
        "component": component,
        "dtype": str(bundle["arrays"][key].dtype),
        "shape": list(bundle["arrays"][key].shape),
    }


def _set_scalar(bundle: Bundle, key: str, value: Any, component: str) -> None:
    if isinstance(value, np.generic):
        value = value.item()
    bundle["manifest"]["fields"][key] = {
        "state": PRESENT,
        "kind": "scalar",
        "component": component,
        "value": value,
    }


def _set_state(bundle: Bundle, key: str, state: str, component: str) -> None:
    if state not in FIELD_STATES:
        raise HarnessError(f"invalid field state {state!r} for {key}")
    if state == PRESENT:
        raise HarnessError(f"{key} needs a value for present state")
    bundle["arrays"].pop(key, None)
    bundle["manifest"]["fields"][key] = {
        "state": state,
        "kind": "state",
        "component": component,
    }


def replace_array(bundle: Bundle, key: str, value: np.ndarray) -> None:
    field = _require_field(bundle, key)
    component = str(field["component"])
    _set_array(bundle, key, np.asarray(value), component)


def set_scalar_value(bundle: Bundle, key: str, value: Any) -> None:
    field = _require_field(bundle, key)
    component = str(field["component"])
    _set_scalar(bundle, key, value, component)


def _require_field(bundle: Bundle, key: str) -> dict[str, Any]:
    try:
        field = bundle["manifest"]["fields"][key]
    except KeyError as exc:
        raise HarnessError(f"bundle missing manifest field {key}") from exc
    state = field.get("state")
    if state not in FIELD_STATES:
        raise HarnessError(f"bundle field {key} has invalid state {state!r}")
    return field


def _field_state(bundle: Bundle, key: str) -> str:
    return str(_require_field(bundle, key)["state"])


def _array(bundle: Bundle, key: str) -> np.ndarray:
    field = _require_field(bundle, key)
    if field["state"] != PRESENT:
        raise HarnessError(f"bundle field {key} is {field['state']}, not present")
    if field.get("kind") != "array":
        raise HarnessError(f"bundle field {key} is not an array")
    try:
        return bundle["arrays"][key]
    except KeyError as exc:
        raise HarnessError(f"bundle missing payload array {key}") from exc


def _scalar(bundle: Bundle, key: str) -> Any:
    field = _require_field(bundle, key)
    if field["state"] != PRESENT:
        raise HarnessError(f"bundle field {key} is {field['state']}, not present")
    if field.get("kind") != "scalar":
        raise HarnessError(f"bundle field {key} is not a scalar")
    return field.get("value")


def _refuse_repo_destination(path: Path) -> Path:
    resolved = path.expanduser().resolve()
    if resolved == ROOT or resolved.is_relative_to(ROOT):
        raise HarnessError(
            f"speaker differential refuses in-repo destination: {resolved}"
        )
    return resolved


def write_bundle(bundle: Bundle, path: Path) -> None:
    destination = _refuse_repo_destination(path)
    destination.parent.mkdir(parents=True, exist_ok=True)
    manifest = bundle["manifest"]
    payload = {
        MANIFEST_KEY: np.array(
            json.dumps(manifest, default=_json_default, sort_keys=True)
        )
    }
    payload.update(bundle["arrays"])
    with destination.open("wb") as fh:
        np.savez(fh, **payload)


def load_bundle(path: Path) -> Bundle:
    try:
        with np.load(path, allow_pickle=False) as payload:
            if MANIFEST_KEY not in payload.files:
                raise HarnessError(f"bundle {path} missing {MANIFEST_KEY}")
            manifest_array = payload[MANIFEST_KEY]
            if manifest_array.shape != ():
                raise HarnessError(f"bundle {path} manifest is not a 0-d array")
            if manifest_array.dtype.kind not in {"U", "S"}:
                raise HarnessError(f"bundle {path} manifest is not a string array")
            manifest = json.loads(str(manifest_array.item()))
            _validate_manifest(manifest, payload, path)
            arrays = {
                key: np.array(payload[key], copy=True)
                for key in payload.files
                if key != MANIFEST_KEY
            }
    except HarnessError:
        raise
    except Exception as exc:
        raise HarnessError(f"failed to load speaker bundle {path}: {exc}") from exc
    return {"manifest": manifest, "arrays": arrays}


def _validate_manifest(manifest: dict[str, Any], payload: Any, path: Path) -> None:
    if manifest.get("schema") != BUNDLE_SCHEMA:
        raise HarnessError(
            f"bundle {path} has unsupported schema {manifest.get('schema')!r}"
        )
    if manifest.get("schema_version") != SCHEMA_VERSION:
        raise HarnessError(
            f"bundle {path} has unsupported schema_version "
            f"{manifest.get('schema_version')!r}"
        )
    fields = manifest.get("fields")
    if not isinstance(fields, dict):
        raise HarnessError(f"bundle {path} manifest fields must be an object")

    expected_payload_keys = {MANIFEST_KEY}
    for key, field in fields.items():
        state = field.get("state")
        if state not in FIELD_STATES:
            raise HarnessError(f"bundle {path} field {key} has invalid state {state!r}")
        if state != PRESENT:
            if key in payload.files:
                raise HarnessError(
                    f"bundle {path} stores absent field {key} as a payload array"
                )
            continue
        if field.get("kind") == "array":
            if key not in payload.files:
                raise HarnessError(f"bundle {path} missing payload array {key}")
            array = payload[key]
            declared_dtype = str(field.get("dtype"))
            declared_shape = list(field.get("shape", []))
            if str(array.dtype) != declared_dtype:
                raise HarnessError(
                    f"bundle {path} field {key} dtype mismatch: "
                    f"{declared_dtype} != {array.dtype}"
                )
            if list(array.shape) != declared_shape:
                raise HarnessError(
                    f"bundle {path} field {key} shape mismatch: "
                    f"{declared_shape} != {list(array.shape)}"
                )
            expected_payload_keys.add(key)
        elif field.get("kind") == "scalar":
            if "value" not in field:
                raise HarnessError(f"bundle {path} scalar field {key} lacks value")
        else:
            raise HarnessError(f"bundle {path} present field {key} has invalid kind")

    extras = set(payload.files) - expected_payload_keys
    if extras:
        raise HarnessError(
            f"bundle {path} has undeclared payload arrays: {sorted(extras)}"
        )


def _audio_identity(audio: np.ndarray, sample_rate: int) -> dict[str, Any]:
    samples = np.ascontiguousarray(np.asarray(audio, dtype="<f4"))
    digest = hashlib.sha256(samples.view(np.uint8).tobytes()).hexdigest()
    return {
        "sample_count": int(samples.shape[0]),
        "sample_rate": int(sample_rate),
        "dtype": "float32-le",
        "content_hash": f"sha256:{digest}",
    }


def _statement_ids_and_spans(
    statements: Sequence[dict[str, Any]],
) -> tuple[np.ndarray, np.ndarray]:
    ids: list[int] = []
    spans: list[tuple[float, float]] = []
    for index, statement in enumerate(statements):
        statement_id = statement.get("id", index)
        ids.append(int(statement_id))
        start = statement.get("start")
        end = statement.get("end")
        if start is None or end is None:
            spans.append((np.nan, np.nan))
        else:
            spans.append((float(start), float(end)))
    return np.asarray(ids, dtype=np.int32), np.asarray(spans, dtype=np.float64)


def _record_input_identity(
    bundle: Bundle,
    plane: str,
    audio: np.ndarray,
    sample_rate: int,
    statements: Sequence[dict[str, Any]],
) -> None:
    ids, spans = _statement_ids_and_spans(statements)
    ids_key = (
        INPUT_STATEMENT_IDS if plane == "statement_embedding" else INPUT_DIARIZATION_IDS
    )
    spans_key = (
        INPUT_STATEMENT_SPANS
        if plane == "statement_embedding"
        else INPUT_DIARIZATION_SPANS
    )
    component = f"inputs.{plane}"
    _set_array(bundle, ids_key, ids, component)
    _set_array(bundle, spans_key, spans, component)
    identity = _audio_identity(audio, sample_rate)
    identity["statement_ids_field"] = ids_key
    identity["statement_spans_field"] = spans_key
    bundle["manifest"]["inputs"][plane] = identity


def _window_stats_array(stats: Sequence[overlap.SpeakerWindowStats]) -> np.ndarray:
    return np.asarray(
        [
            (row.speech_frames, row.active_slot_count, row.overlap_frames)
            for row in stats
        ],
        dtype=np.int32,
    ).reshape((-1, 3))


def _intervals_array(intervals: Sequence[tuple[float, float, int]]) -> np.ndarray:
    if not intervals:
        return np.zeros((0, 3), dtype=np.float64)
    return np.asarray(
        [(float(start), float(end), int(local)) for start, end, local in intervals],
        dtype=np.float64,
    )


def encode_statement_labels(labels: Sequence[int | None]) -> np.ndarray:
    encoded: list[int] = []
    for label in labels:
        if label is None:
            encoded.append(int(LABEL_NULL_SENTINEL))
            continue
        label_int = int(label)
        if label_int <= 0:
            raise HarnessError(
                "production diarization label must be 1-indexed and positive; "
                f"got {label_int}"
            )
        encoded.append(label_int)
    return np.asarray(encoded, dtype=np.int32)


@contextlib.contextmanager
def record_diarize_private_helpers() -> Iterator[dict[str, Any]]:
    """Record private diarizer stage values without changing orchestration."""
    records: dict[str, Any] = {}
    original_find = diarize._find_intervals
    original_embed_all = diarize._embed_all_intervals
    original_pick_k = diarize._pick_k_silhouette
    original_cluster = diarize._cluster_intervals
    original_assign = diarize._assign_sentences

    def find_wrapper(avg_log_probs: np.ndarray, audio_len_samples: int) -> list[tuple]:
        result = original_find(avg_log_probs, audio_len_samples)
        records["intervals"] = list(result)
        return result

    def embed_all_wrapper(
        audio: np.ndarray,
        intervals: list[tuple[float, float, int]],
    ) -> tuple[list[tuple[float, float, int]], np.ndarray]:
        valid, embs = original_embed_all(audio, intervals)
        records["valid_intervals"] = list(valid)
        records["interval_embeddings"] = np.array(embs, copy=True)
        return valid, embs

    def pick_k_wrapper(embs_n: np.ndarray, max_k: int) -> int:
        result = int(original_pick_k(embs_n, max_k))
        records["silhouette_k"] = result
        return result

    def cluster_wrapper(embs: np.ndarray, n_speakers: int | None) -> np.ndarray:
        labels = original_cluster(embs, n_speakers)
        if n_speakers is None:
            selected = records.get("silhouette_k")
        else:
            selected = n_speakers
            records["silhouette_k"] = None
        if selected is None:
            records["effective_k"] = None
        else:
            upper = len(embs) - 1 if len(embs) > 1 else 1
            records["effective_k"] = int(max(1, min(int(selected), upper)))
        records["cluster_labels"] = np.asarray(labels, dtype=np.int32).copy()
        return labels

    def assign_wrapper(
        sentences: list[dict],
        intervals: list[tuple[float, float, int]],
        global_labels: np.ndarray,
    ) -> list[int | None]:
        result = original_assign(sentences, intervals, global_labels)
        records["statement_labels"] = list(result)
        return result

    diarize._find_intervals = find_wrapper
    diarize._embed_all_intervals = embed_all_wrapper
    diarize._pick_k_silhouette = pick_k_wrapper
    diarize._cluster_intervals = cluster_wrapper
    diarize._assign_sentences = assign_wrapper
    try:
        yield records
    finally:
        diarize._find_intervals = original_find
        diarize._embed_all_intervals = original_embed_all
        diarize._pick_k_silhouette = original_pick_k
        diarize._cluster_intervals = original_cluster
        diarize._assign_sentences = original_assign


def _record_statement_embedding(
    bundle: Bundle,
    embedding_result: dict[str, np.ndarray] | None,
) -> None:
    if embedding_result is None:
        for key in (
            STATEMENT_EMBEDDINGS,
            STATEMENT_EMBEDDING_IDS,
            STATEMENT_DURATIONS,
            STATEMENT_ENCODER,
        ):
            _set_state(bundle, key, ABSENT_NONE, "statement_embeddings")
        return

    _set_array(
        bundle,
        STATEMENT_EMBEDDINGS,
        np.asarray(embedding_result["embeddings"], dtype=np.float32),
        "statement_embeddings",
    )
    _set_array(
        bundle,
        STATEMENT_EMBEDDING_IDS,
        np.asarray(embedding_result["statement_ids"], dtype=np.int32),
        "statement_embeddings",
    )
    _set_array(
        bundle,
        STATEMENT_DURATIONS,
        np.asarray(embedding_result.get("durations_s", []), dtype=np.float32),
        "statement_embeddings",
    )
    _set_array(
        bundle,
        STATEMENT_ENCODER,
        np.asarray(embedding_result["encoder"]),
        "statement_embeddings",
    )


def _mark_fields(
    bundle: Bundle,
    fields: Sequence[str],
    state: str,
    component: str,
) -> None:
    for key in fields:
        _set_state(bundle, key, state, component)


def _record_pyannote_and_evidence(
    bundle: Bundle,
    result: overlap.OverlapInferenceResult,
    evidence: overlap.SpeakerEvidenceDecision,
) -> None:
    _set_array(
        bundle,
        PYANNOTE_LOGPROBS,
        np.asarray(result.avg_log_probs, dtype=np.float32),
        "pyannote_log_probs",
    )
    _set_array(
        bundle,
        PYANNOTE_WINDOW_STATS,
        _window_stats_array(result.window_stats),
        "evidence",
    )
    _set_scalar(
        bundle, EVIDENCE_OVERLAP_FRACTION, float(result.overlap_fraction), "evidence"
    )
    _set_scalar(bundle, EVIDENCE_SPEAKER, evidence.speaker_evidence, "evidence")
    _set_scalar(
        bundle,
        EVIDENCE_MULTI_FRACTION,
        float(evidence.multi_window_fraction),
        "evidence",
    )
    _set_scalar(
        bundle,
        EVIDENCE_MEAN_OVERLAP,
        float(evidence.mean_window_overlap_share),
        "evidence",
    )


def _record_diarization(
    bundle: Bundle,
    records: dict[str, Any],
    labels: Sequence[int | None],
) -> None:
    intervals = records.get("intervals")
    if intervals is None:
        _mark_fields(
            bundle,
            tuple(GATE_DECLINED_FIELDS),
            NOT_EVALUATED,
            "diarization",
        )
        return

    _set_array(
        bundle,
        DIARIZATION_INTERVALS,
        _intervals_array(intervals),
        "intervals",
    )
    if not intervals:
        _mark_fields(
            bundle,
            (
                DIARIZATION_VALID_INTERVALS,
                DIARIZATION_INTERVAL_EMBEDDINGS,
                DIARIZATION_CLUSTER_LABELS,
                DIARIZATION_STATEMENT_LABELS,
                DIARIZATION_SILHOUETTE_K,
                DIARIZATION_EFFECTIVE_K,
            ),
            NOT_EVALUATED,
            "diarization",
        )
        return

    valid_intervals = records.get("valid_intervals", [])
    interval_embeddings = np.asarray(
        records.get("interval_embeddings", np.zeros((0, 256), dtype=np.float32)),
        dtype=np.float32,
    )
    _set_array(
        bundle,
        DIARIZATION_VALID_INTERVALS,
        _intervals_array(valid_intervals),
        "interval_embeddings",
    )
    _set_array(
        bundle,
        DIARIZATION_INTERVAL_EMBEDDINGS,
        interval_embeddings,
        "interval_embeddings",
    )
    if len(interval_embeddings) == 0:
        _mark_fields(
            bundle,
            (
                DIARIZATION_CLUSTER_LABELS,
                DIARIZATION_STATEMENT_LABELS,
                DIARIZATION_SILHOUETTE_K,
                DIARIZATION_EFFECTIVE_K,
            ),
            NOT_EVALUATED,
            "diarization",
        )
        return

    _set_array(
        bundle,
        DIARIZATION_CLUSTER_LABELS,
        np.asarray(records["cluster_labels"], dtype=np.int32),
        "clustering",
    )
    _set_scalar(
        bundle,
        DIARIZATION_SILHOUETTE_K,
        records.get("silhouette_k"),
        "clustering",
    )
    _set_scalar(
        bundle,
        DIARIZATION_EFFECTIVE_K,
        records.get("effective_k"),
        "clustering",
    )
    _set_array(
        bundle,
        DIARIZATION_STATEMENT_LABELS,
        encode_statement_labels(labels),
        "statement_labels",
    )


def emit_speaker_bundle(
    *,
    audio_buffer: np.ndarray,
    statements: Sequence[dict[str, Any]],
    sample_rate: int = SAMPLE_RATE,
    reduced_audio: np.ndarray | None = None,
    reduction: AudioReduction | None = None,
    producer: str = "production-python",
) -> Bundle:
    """Run production speaker stages and return a versioned differential bundle."""
    if sample_rate != SAMPLE_RATE:
        raise HarnessError(f"speaker pipeline requires {SAMPLE_RATE} Hz audio")

    bundle = new_bundle(producer=producer)
    working_statements = [dict(statement) for statement in statements]
    stt_buffer = (
        np.asarray(reduced_audio, dtype=np.float32)
        if reduced_audio is not None
        else np.asarray(audio_buffer, dtype=np.float32)
    )
    full_buffer = np.asarray(audio_buffer, dtype=np.float32)

    _record_input_identity(
        bundle,
        "statement_embedding",
        stt_buffer,
        sample_rate,
        working_statements,
    )

    if not working_statements:
        _mark_fields(
            bundle,
            (
                STATEMENT_EMBEDDINGS,
                STATEMENT_EMBEDDING_IDS,
                STATEMENT_DURATIONS,
                STATEMENT_ENCODER,
            ),
            NOT_EVALUATED,
            "statement_embeddings",
        )
        for component, fields in COMPONENT_FIELDS.items():
            if component != "statement_embeddings":
                _mark_fields(bundle, fields, NOT_EVALUATED, component)
        _record_input_identity(
            bundle,
            "diarization",
            full_buffer,
            sample_rate,
            working_statements,
        )
        _refresh_provenance(bundle, producer)
        return bundle

    embedding_result = transcribe_main._embed_statements(
        stt_buffer,
        working_statements,
        sample_rate,
    )
    _record_statement_embedding(bundle, embedding_result)

    overlap_result = overlap.compute_overlap_and_logprobs(full_buffer, sample_rate)
    evidence = overlap.decide_speaker_evidence(
        overlap_result.overlap_fraction,
        overlap_result.window_stats,
    )
    _record_pyannote_and_evidence(bundle, overlap_result, evidence)

    restored_statements = (
        restore_statement_timestamps(working_statements, reduction)
        if reduction is not None
        else [dict(statement) for statement in working_statements]
    )
    _record_input_identity(
        bundle,
        "diarization",
        full_buffer,
        sample_rate,
        restored_statements,
    )

    if evidence.speaker_evidence != "multi":
        _mark_fields(bundle, tuple(GATE_DECLINED_FIELDS), NOT_EVALUATED, "diarization")
        _refresh_provenance(bundle, producer)
        return bundle

    bundle["manifest"]["counters"]["overlap_session_run_count_before_diarize"] = (
        _session_run_count(getattr(overlap, "_overlap_session", None))
    )
    try:
        with record_diarize_private_helpers() as records:
            # ``wav_path`` is inert when ``audio=`` is supplied; production reads
            # it only in the audio-is-None branch.
            labels = diarize.diarize_auto_k(
                PLACEHOLDER_WAV_PATH,
                restored_statements,
                avg_log_probs=overlap_result.avg_log_probs,
                audio=full_buffer,
            )
    except Exception as exc:
        logger.exception("production diarization failed during speaker differential")
        bundle["manifest"]["stage_errors"].append(
            {
                "stage": "diarization",
                "class": type(exc).__name__,
                "message": str(exc),
            }
        )
        _mark_fields(bundle, tuple(GATE_DECLINED_FIELDS), NOT_EVALUATED, "diarization")
        _refresh_provenance(bundle, producer)
        return bundle
    finally:
        bundle["manifest"]["counters"]["overlap_session_run_count_after_diarize"] = (
            _session_run_count(getattr(overlap, "_overlap_session", None))
        )

    _record_diarization(bundle, records, labels)
    _refresh_provenance(bundle, producer)
    return bundle


def _session_run_count(session: object | None) -> int | None:
    if session is None:
        return None
    count = getattr(session, "run_count", None)
    if count is None:
        return None
    return int(count)


def _component_state_verdict(
    left: Bundle,
    right: Bundle,
    fields: Sequence[str],
) -> str:
    verdict = COMPARE
    for key in fields:
        pair = (_field_state(left, key), _field_state(right, key))
        field_verdict = STATE_PAIR_VERDICT[pair]
        if field_verdict == UNEXPECTED_DIFFERS:
            return UNEXPECTED_DIFFERS
        if field_verdict == NOT_EVALUATED:
            verdict = NOT_EVALUATED
    return verdict


def _threshold_block(mapping: dict[str, str]) -> dict[str, Any]:
    thresholds: dict[str, Any] = {}
    for metric, constant_name in mapping.items():
        if constant_name == "exact":
            thresholds[metric] = "exact"
        else:
            thresholds[metric] = {
                "constant": constant_name,
                "value": COMPARATOR_THRESHOLDS[constant_name],
            }
    return thresholds


def _component_report(
    classification: str,
    *,
    thresholds: dict[str, Any] | str,
    **extra: Any,
) -> dict[str, Any]:
    report = {"classification": classification, "thresholds": thresholds}
    report.update(extra)
    return report


def _float_metrics(left_array: np.ndarray, right_array: np.ndarray) -> dict[str, Any]:
    if left_array.shape != right_array.shape:
        return {"shape_mismatch": [list(left_array.shape), list(right_array.shape)]}
    diff = np.abs(left_array.astype(np.float64) - right_array.astype(np.float64))
    if diff.size == 0:
        return {"shape": list(left_array.shape), "empty": True}
    return {
        "shape": list(left_array.shape),
        "max_abs_diff": float(diff.max()),
        "median_abs_diff": float(np.median(diff)),
    }


def _compare_pyannote(left: Bundle, right: Bundle) -> dict[str, Any]:
    thresholds = _threshold_block(PYANNOTE_THRESHOLD_IDS)
    state_verdict = _component_state_verdict(
        left, right, COMPONENT_FIELDS["pyannote_log_probs"]
    )
    if state_verdict != COMPARE:
        return _component_report(state_verdict, thresholds=thresholds)
    left_array = _array(left, PYANNOTE_LOGPROBS)
    right_array = _array(right, PYANNOTE_LOGPROBS)
    metrics = _float_metrics(left_array, right_array)
    if "shape_mismatch" in metrics:
        return _component_report(
            UNEXPECTED_DIFFERS, thresholds=thresholds, metrics=metrics
        )
    if left_array.size == 0 and right_array.size == 0:
        return _component_report(NOT_EVALUATED, thresholds=thresholds, metrics=metrics)
    argmax_agreement = float(
        (left_array.argmax(axis=-1) == right_array.argmax(axis=-1)).mean()
    )
    metrics["per_frame_argmax_agreement_fraction"] = argmax_agreement
    if np.array_equal(left_array, right_array):
        classification = EQUAL
    elif (
        metrics["max_abs_diff"] <= LOGPROB_MAX_ABS_TOLERANCE
        and metrics["median_abs_diff"] <= LOGPROB_MEDIAN_ABS_TOLERANCE
        and argmax_agreement >= LOGPROB_ARGMAX_AGREEMENT_MIN
    ):
        classification = FUNCTIONALLY_EQUAL
    else:
        classification = UNEXPECTED_DIFFERS
    return _component_report(classification, thresholds=thresholds, metrics=metrics)


def _compare_evidence(left: Bundle, right: Bundle) -> dict[str, Any]:
    thresholds = _threshold_block(EVIDENCE_THRESHOLD_IDS)
    state_verdict = _component_state_verdict(left, right, COMPONENT_FIELDS["evidence"])
    if state_verdict != COMPARE:
        return _component_report(state_verdict, thresholds=thresholds)
    diffs: list[dict[str, Any]] = []
    left_speaker = _scalar(left, EVIDENCE_SPEAKER)
    right_speaker = _scalar(right, EVIDENCE_SPEAKER)
    if left_speaker != right_speaker:
        diffs.append(
            {
                "field": EVIDENCE_SPEAKER,
                "left": left_speaker,
                "right": right_speaker,
            }
        )
    metrics: dict[str, Any] = {}
    for key in (
        EVIDENCE_OVERLAP_FRACTION,
        EVIDENCE_MULTI_FRACTION,
        EVIDENCE_MEAN_OVERLAP,
    ):
        delta = abs(float(_scalar(left, key)) - float(_scalar(right, key)))
        metrics[key] = {"abs_diff": delta}
        if delta > EVIDENCE_FLOAT_ABS_TOLERANCE:
            diffs.append({"field": key, "abs_diff": delta})
    left_stats = _array(left, PYANNOTE_WINDOW_STATS)
    right_stats = _array(right, PYANNOTE_WINDOW_STATS)
    if not np.array_equal(left_stats, right_stats):
        diffs.append(
            {
                "field": PYANNOTE_WINDOW_STATS,
                "left": left_stats.tolist(),
                "right": right_stats.tolist(),
            }
        )
    metrics["window_stats_equal"] = bool(np.array_equal(left_stats, right_stats))
    if diffs:
        return _component_report(
            UNEXPECTED_DIFFERS,
            thresholds=thresholds,
            metrics=metrics,
            differences=diffs,
        )
    exact = all(
        metrics[key]["abs_diff"] == 0.0
        for key in metrics
        if key != "window_stats_equal"
    )
    return _component_report(
        EQUAL if exact else FUNCTIONALLY_EQUAL,
        thresholds=thresholds,
        metrics=metrics,
    )


def _interval_set(array: np.ndarray) -> set[tuple[float, float, int]]:
    return {
        (float(row[0]), float(row[1]), int(row[2]))
        for row in np.asarray(array).reshape((-1, 3))
    }


def _compare_intervals(left: Bundle, right: Bundle) -> dict[str, Any]:
    state_verdict = _component_state_verdict(left, right, COMPONENT_FIELDS["intervals"])
    if state_verdict != COMPARE:
        return _component_report(state_verdict, thresholds="exact")
    left_intervals = _array(left, DIARIZATION_INTERVALS)
    right_intervals = _array(right, DIARIZATION_INTERVALS)
    if left_intervals.shape != right_intervals.shape:
        return _component_report(
            UNEXPECTED_DIFFERS,
            thresholds="exact",
            metrics={
                "shape_mismatch": [
                    list(left_intervals.shape),
                    list(right_intervals.shape),
                ]
            },
        )
    if len(left_intervals) == 0 and len(right_intervals) == 0:
        return _component_report(
            NOT_EVALUATED, thresholds="exact", metrics={"interval_count": 0}
        )
    left_set = _interval_set(left_intervals)
    right_set = _interval_set(right_intervals)
    if left_set == right_set:
        return _component_report(
            EQUAL, thresholds="exact", metrics={"interval_count": len(left_set)}
        )
    return _component_report(
        UNEXPECTED_DIFFERS,
        thresholds="exact",
        metrics={"left_count": len(left_set), "right_count": len(right_set)},
        differences={
            "added": sorted(right_set - left_set),
            "removed": sorted(left_set - right_set),
        },
    )


def _cosine_metrics(left_array: np.ndarray, right_array: np.ndarray) -> dict[str, Any]:
    diff = np.abs(left_array.astype(np.float64) - right_array.astype(np.float64))
    left_norm = np.linalg.norm(left_array, axis=1)
    right_norm = np.linalg.norm(right_array, axis=1)
    denom = np.maximum(left_norm * right_norm, COSINE_NORM_FLOOR)
    cosine = np.sum(left_array * right_array, axis=1) / denom
    return {
        "max_abs_component_diff": float(diff.max()) if diff.size else 0.0,
        "min_cosine_similarity": float(cosine.min()) if cosine.size else 1.0,
        "median_cosine_similarity": float(np.median(cosine)) if cosine.size else 1.0,
        "max_cosine_similarity": float(cosine.max()) if cosine.size else 1.0,
    }


def _embedding_classification(
    left_array: np.ndarray,
    right_array: np.ndarray,
    metrics: dict[str, Any],
) -> str:
    if np.array_equal(left_array, right_array):
        return EQUAL
    if (
        metrics["max_abs_component_diff"] <= EMBEDDING_MAX_ABS_TOLERANCE
        and metrics["min_cosine_similarity"] >= EMBEDDING_MIN_COSINE_SIMILARITY
        and metrics["median_cosine_similarity"] >= EMBEDDING_MEDIAN_COSINE_SIMILARITY
    ):
        return FUNCTIONALLY_EQUAL
    return UNEXPECTED_DIFFERS


def _compare_statement_embeddings(left: Bundle, right: Bundle) -> dict[str, Any]:
    thresholds = _threshold_block(EMBEDDING_THRESHOLD_IDS)
    state_verdict = _component_state_verdict(
        left, right, COMPONENT_FIELDS["statement_embeddings"]
    )
    if state_verdict != COMPARE:
        return _component_report(state_verdict, thresholds=thresholds)
    left_ids = _array(left, STATEMENT_EMBEDDING_IDS)
    right_ids = _array(right, STATEMENT_EMBEDDING_IDS)
    if set(left_ids.tolist()) != set(right_ids.tolist()):
        return _component_report(
            UNEXPECTED_DIFFERS,
            thresholds=thresholds,
            differences={
                "left_ids": left_ids.tolist(),
                "right_ids": right_ids.tolist(),
            },
        )
    left_embs = _array(left, STATEMENT_EMBEDDINGS)
    right_embs = _array(right, STATEMENT_EMBEDDINGS)
    if len(left_ids) == 0 and len(right_ids) == 0:
        return _component_report(
            NOT_EVALUATED, thresholds=thresholds, metrics={"statement_count": 0}
        )
    if left_embs.shape[1:] != right_embs.shape[1:]:
        return _component_report(
            UNEXPECTED_DIFFERS,
            thresholds=thresholds,
            metrics={"shape_mismatch": [list(left_embs.shape), list(right_embs.shape)]},
        )
    left_index = {int(statement_id): idx for idx, statement_id in enumerate(left_ids)}
    right_index = {int(statement_id): idx for idx, statement_id in enumerate(right_ids)}
    ordered_ids = sorted(left_index)
    left_ordered = np.stack(
        [left_embs[left_index[statement_id]] for statement_id in ordered_ids]
    )
    right_ordered = np.stack(
        [right_embs[right_index[statement_id]] for statement_id in ordered_ids]
    )
    metrics = _cosine_metrics(left_ordered, right_ordered)
    metrics["statement_count"] = len(ordered_ids)
    classification = _embedding_classification(left_ordered, right_ordered, metrics)
    return _component_report(classification, thresholds=thresholds, metrics=metrics)


def _compare_interval_embeddings(left: Bundle, right: Bundle) -> dict[str, Any]:
    thresholds = _threshold_block(EMBEDDING_THRESHOLD_IDS)
    state_verdict = _component_state_verdict(
        left, right, COMPONENT_FIELDS["interval_embeddings"]
    )
    if state_verdict != COMPARE:
        return _component_report(state_verdict, thresholds=thresholds)
    left_embs = _array(left, DIARIZATION_INTERVAL_EMBEDDINGS)
    right_embs = _array(right, DIARIZATION_INTERVAL_EMBEDDINGS)
    if left_embs.shape != right_embs.shape:
        return _component_report(
            UNEXPECTED_DIFFERS,
            thresholds=thresholds,
            metrics={"shape_mismatch": [list(left_embs.shape), list(right_embs.shape)]},
        )
    if left_embs.size == 0 and right_embs.size == 0:
        return _component_report(
            NOT_EVALUATED, thresholds=thresholds, metrics={"interval_count": 0}
        )
    metrics = _cosine_metrics(left_embs, right_embs)
    metrics["interval_count"] = int(left_embs.shape[0])
    classification = _embedding_classification(left_embs, right_embs, metrics)
    return _component_report(classification, thresholds=thresholds, metrics=metrics)


def _partition(labels: np.ndarray) -> set[frozenset[int]]:
    groups: dict[int, set[int]] = {}
    for index, label in enumerate(labels.tolist()):
        groups.setdefault(int(label), set()).add(index)
    return {frozenset(members) for members in groups.values()}


def _compare_clustering(left: Bundle, right: Bundle) -> dict[str, Any]:
    state_verdict = _component_state_verdict(
        left, right, COMPONENT_FIELDS["clustering"]
    )
    if state_verdict != COMPARE:
        return _component_report(state_verdict, thresholds="exact")
    left_labels = _array(left, DIARIZATION_CLUSTER_LABELS)
    right_labels = _array(right, DIARIZATION_CLUSTER_LABELS)
    if left_labels.shape != right_labels.shape:
        return _component_report(
            UNEXPECTED_DIFFERS,
            thresholds="exact",
            metrics={
                "shape_mismatch": [list(left_labels.shape), list(right_labels.shape)]
            },
        )
    if left_labels.size == 0 and right_labels.size == 0:
        return _component_report(
            NOT_EVALUATED, thresholds="exact", metrics={"cluster_label_count": 0}
        )
    scalar_diffs = []
    for key in (DIARIZATION_SILHOUETTE_K, DIARIZATION_EFFECTIVE_K):
        if _scalar(left, key) != _scalar(right, key):
            scalar_diffs.append(
                {"field": key, "left": _scalar(left, key), "right": _scalar(right, key)}
            )
    left_partition = _partition(left_labels)
    right_partition = _partition(right_labels)
    if scalar_diffs or left_partition != right_partition:
        return _component_report(
            UNEXPECTED_DIFFERS,
            thresholds="exact",
            metrics={
                "cluster_label_count": int(left_labels.size),
                "partition_equal_up_to_permutation": left_partition == right_partition,
            },
            differences={"scalars": scalar_diffs},
        )
    return _component_report(
        EQUAL,
        thresholds="exact",
        metrics={
            "cluster_label_count": int(left_labels.size),
            "partition_equal_up_to_permutation": True,
        },
    )


def _compare_statement_labels(left: Bundle, right: Bundle) -> dict[str, Any]:
    state_verdict = _component_state_verdict(
        left, right, COMPONENT_FIELDS["statement_labels"]
    )
    if state_verdict != COMPARE:
        return _component_report(state_verdict, thresholds="exact")
    left_ids = _array(left, INPUT_DIARIZATION_IDS)
    right_ids = _array(right, INPUT_DIARIZATION_IDS)
    left_labels = _array(left, DIARIZATION_STATEMENT_LABELS)
    right_labels = _array(right, DIARIZATION_STATEMENT_LABELS)
    if set(left_ids.tolist()) != set(right_ids.tolist()):
        return _component_report(
            UNEXPECTED_DIFFERS,
            thresholds="exact",
            differences={
                "left_ids": left_ids.tolist(),
                "right_ids": right_ids.tolist(),
            },
        )
    if left_labels.size == 0 and right_labels.size == 0:
        return _component_report(
            NOT_EVALUATED, thresholds="exact", metrics={"statement_count": 0}
        )
    if np.all(left_labels == LABEL_NULL_SENTINEL) and np.all(
        right_labels == LABEL_NULL_SENTINEL
    ):
        return _component_report(
            NOT_EVALUATED,
            thresholds="exact",
            metrics={"statement_count": int(left_labels.size), "all_null": True},
        )
    left_index = {int(statement_id): idx for idx, statement_id in enumerate(left_ids)}
    right_index = {int(statement_id): idx for idx, statement_id in enumerate(right_ids)}
    mismatches = []
    for statement_id in sorted(left_index):
        left_label = int(left_labels[left_index[statement_id]])
        right_label = int(right_labels[right_index[statement_id]])
        if left_label != right_label:
            mismatches.append(
                {
                    "statement_id": statement_id,
                    "left": None if left_label == LABEL_NULL_SENTINEL else left_label,
                    "right": None
                    if right_label == LABEL_NULL_SENTINEL
                    else right_label,
                }
            )
    if mismatches:
        return _component_report(
            UNEXPECTED_DIFFERS,
            thresholds="exact",
            metrics={
                "statement_count": int(left_labels.size),
                "agreement_fraction": 1.0 - len(mismatches) / max(1, len(left_index)),
            },
            differences=mismatches,
        )
    return _component_report(
        EQUAL,
        thresholds="exact",
        metrics={"statement_count": int(left_labels.size), "agreement_fraction": 1.0},
    )


def _validate_input_alignment(left: Bundle, right: Bundle) -> None:
    for plane in ("statement_embedding", "diarization"):
        if left["manifest"]["inputs"].get(plane) != right["manifest"]["inputs"].get(
            plane
        ):
            raise HarnessError(f"input identity mismatch for {plane} plane")
    for key in (
        INPUT_STATEMENT_IDS,
        INPUT_STATEMENT_SPANS,
        INPUT_DIARIZATION_IDS,
        INPUT_DIARIZATION_SPANS,
    ):
        if not np.array_equal(_array(left, key), _array(right, key), equal_nan=True):
            raise HarnessError(f"input array mismatch for {key}")


def _both_gate_declined(left: Bundle, right: Bundle) -> bool:
    try:
        return (
            _field_state(left, EVIDENCE_SPEAKER) == PRESENT
            and _field_state(right, EVIDENCE_SPEAKER) == PRESENT
            and _scalar(left, EVIDENCE_SPEAKER) != "multi"
            and _scalar(right, EVIDENCE_SPEAKER) != "multi"
        )
    except HarnessError:
        return False


def _rollup(left: Bundle, right: Bundle, components: dict[str, dict[str, Any]]) -> str:
    values = [component["classification"] for component in components.values()]
    if any(value == UNEXPECTED_DIFFERS for value in values):
        return UNEXPECTED_DIFFERS
    if _both_gate_declined(left, right):
        return NOT_EVALUATED
    if any(value == FUNCTIONALLY_EQUAL for value in values):
        return FUNCTIONALLY_EQUAL
    if any(value == EQUAL for value in values):
        return EQUAL
    return NOT_EVALUATED


def _base_report(
    left: Bundle | None = None, right: Bundle | None = None
) -> dict[str, Any]:
    return {
        "schema": REPORT_SCHEMA,
        "schema_version": SCHEMA_VERSION,
        "classification": NOT_EVALUATED,
        "failure": None,
        "provenance": {
            "generated_at": datetime.now(UTC).isoformat().replace("+00:00", "Z"),
            "harness": {
                "name": "tests.verify_speaker_differential",
                "repo_commit": get_rev(),
                "version": None,
            },
            "host": {
                "platform": platform.platform(),
                "python": platform.python_version(),
            },
            "bundles": {
                "left": left["manifest"]["provenance"] if left is not None else None,
                "right": right["manifest"]["provenance"] if right is not None else None,
            },
        },
        "components": {},
    }


def compare_bundles(left: Bundle, right: Bundle) -> dict[str, Any]:
    report = _base_report(left, right)
    try:
        if left["manifest"].get("stage_errors") or right["manifest"].get(
            "stage_errors"
        ):
            raise HarnessError("one or both bundles contain stage_errors")
        _validate_input_alignment(left, right)
        components = {
            "pyannote_log_probs": _compare_pyannote(left, right),
            "evidence": _compare_evidence(left, right),
            "statement_embeddings": _compare_statement_embeddings(left, right),
            "intervals": _compare_intervals(left, right),
            "interval_embeddings": _compare_interval_embeddings(left, right),
            "clustering": _compare_clustering(left, right),
            "statement_labels": _compare_statement_labels(left, right),
        }
        report["components"] = {key: components[key] for key in COMPONENT_ORDER}
        report["classification"] = _rollup(left, right, components)
    except HarnessError as exc:
        report["classification"] = NOT_EVALUATED
        report["failure"] = {"class": HARNESS_ERROR, "message": str(exc)}
    return report


def parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("left_bundle", help="Left speaker differential .npz bundle")
    parser.add_argument("right_bundle", help="Right speaker differential .npz bundle")
    parser.add_argument(
        "--report", help="JSON report destination outside the repository"
    )
    return parser.parse_args(argv)


def main(argv: list[str] | None = None) -> int:
    args = parse_args(argv)
    requested_report_path = Path(args.report).resolve() if args.report else None
    report_path: Path | None = None
    try:
        if requested_report_path is not None:
            report_path = _refuse_repo_destination(requested_report_path)
        left = load_bundle(Path(args.left_bundle))
        right = load_bundle(Path(args.right_bundle))
        report = compare_bundles(left, right)
    except Exception as exc:
        report = _base_report()
        report["failure"] = {"class": HARNESS_ERROR, "message": str(exc)}
        report["classification"] = NOT_EVALUATED

    rendered = _render_report(report)
    if report_path is not None:
        report_path.parent.mkdir(parents=True, exist_ok=True)
        report_path.write_text(rendered, encoding="utf-8")
    sys.stdout.write(rendered)
    return 0 if report.get("classification") in {EQUAL, FUNCTIONALLY_EQUAL} else 1


if __name__ == "__main__":
    raise SystemExit(main())
