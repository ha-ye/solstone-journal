# SPDX-License-Identifier: AGPL-3.0-only
# Copyright (c) 2026 sol pbc

"""Config-gated native speaker analysis for transcribed audio segments.

This seam is bounded migration scaffolding, not a compatibility shim. It lets a
configured journal route speaker embeddings, evidence, and local diarization
through `solstone-core-speakers-analyze` while the absent key and explicit
`python` path remain byte-for-byte Python. The selection keys remain out of
journal_default.json because this is a two-release migration control: flip the
absent-key default in 1.0.19, then delete the Python orchestration path and key
in 1.0.20.

Only `core.speakers_analyze` exists. A second decline key would only spend a
subprocess and ONNX model load on solo recordings to reach the same not-multi
answer both implementations already produce. Native evidence-gate decline is
therefore fixed: accept the native embeddings/evidence and write no labels.

Real-helper end-to-end proof is VPE-direct post-ship. Unit tests use an injected
runner; helper presence in unit tests must not be redefined as a stubbed green
path.
"""

from __future__ import annotations

import json
import os
import shutil
import subprocess
import tempfile
import time
from collections.abc import Callable
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Literal

import numpy as np

from solstone.apps.speakers.encoder_config import (
    ENCODER_ID,
    WESPEAKER_EMBEDDING_WIDTH,
)
from solstone.observe.model_assets import (
    resolve_pyannote_segmentation_model,
    resolve_wespeaker_model,
)
from solstone.observe.transcribe.overlap import SpeakerEvidenceDecision
from solstone.think import speakers_analyze_runtime
from solstone.think.journal_config import read_journal_config
from solstone.think.speakers_analyze_handshake import (
    SpeakersAnalyzeHandshakeResult,
    check_speakers_analyze_handshake,
    speakers_analyze_path_for_executable,
)

REQUEST_SCHEMA = "solstone-speaker-analyze-request-v1"
RESPONSE_SCHEMA = "solstone-speaker-analyze-response-v1"
ERROR_SCHEMA = "solstone-speaker-analyze-error-v1"
CONFIG_KEY = "core.speakers_analyze"
PRODUCER_ID = "solstone-core-speakers-analyze-v1"
EXIT_CONFIG = 78
EXIT_UNAVAILABLE = 69
TEMP_ROOT = Path("/var/tmp")
TEMP_PREFIX = "solstone-speakers-analyze-"
TEMP_DIR_MODE = 0o700
TEMP_FILE_MODE = 0o600

INVALID_SPEAKERS_ANALYZE_MESSAGE = (
    "transcribe speakers analyze selected implementation 'invalid' from config key "
    "core.speakers_analyze; found {value!r}; expected 'python' or 'native'. "
    "Set core.speakers_analyze to 'python' to revert."
)

NativeStatus = Literal["python", "accepted", "fallback", "config_error"]
ConfigReader = Callable[[str | Path | None], dict[str, Any]]
HandshakeChecker = Callable[[], SpeakersAnalyzeHandshakeResult]
HelperLocator = Callable[[], Path]
NativeRunner = Callable[..., subprocess.CompletedProcess[Any]]
ModelPathResolver = Callable[[], tuple[Path, Path]]
TempDirFactory = Callable[[Path], Path]


def create_speakers_analyze_temp_dir(raw_path: Path) -> Path:
    day = _safe_temp_part(
        raw_path.parent.parent.parent.name if raw_path.parents else "x"
    )
    segment = _safe_temp_part(raw_path.parent.name)
    source = _safe_temp_part(raw_path.stem)
    prefix = f"{TEMP_PREFIX}{day}-{segment}-{source}-{os.getpid()}-"
    path = Path(tempfile.mkdtemp(prefix=prefix, dir=TEMP_ROOT))
    path.chmod(TEMP_DIR_MODE)
    return path


def _resolve_model_paths() -> tuple[Path, Path]:
    return resolve_wespeaker_model(), resolve_pyannote_segmentation_model()


@dataclass(frozen=True)
class NativeSpeakerAnalysisResult:
    status: NativeStatus
    statements: list[dict[str, Any]] | None = None
    embeddings_data: dict[str, np.ndarray] | None = None
    speaker_evidence: SpeakerEvidenceDecision | None = None
    overlap_fraction: float | None = None
    event_fields: dict[str, Any] = field(default_factory=dict)
    error_message: str | None = None


def maybe_run_native_speaker_analysis(
    *,
    journal: str | Path | None,
    raw_path: Path,
    full_audio: np.ndarray,
    statement_audio: np.ndarray,
    reduced_audio: np.ndarray | None,
    statements_pre_restore: list[dict[str, Any]],
    statements_restored: list[dict[str, Any]],
    sample_rate: int,
    min_statement_duration: float,
    config_reader: ConfigReader = read_journal_config,
    handshake_checker: HandshakeChecker = check_speakers_analyze_handshake,
    helper_locator: HelperLocator = speakers_analyze_path_for_executable,
    native_runner: NativeRunner = subprocess.run,
    model_path_resolver: ModelPathResolver = _resolve_model_paths,
    temp_dir_factory: TempDirFactory = create_speakers_analyze_temp_dir,
    breaker_blocked: Callable[..., tuple[bool, dict[str, Any]]] = (
        speakers_analyze_runtime.native_blocked
    ),
    record_native_success: Callable[..., dict[str, Any]] = (
        speakers_analyze_runtime.record_native_success
    ),
    record_native_failure: Callable[..., dict[str, Any]] = (
        speakers_analyze_runtime.record_native_failure
    ),
) -> NativeSpeakerAnalysisResult:
    """Run the native helper when selected, otherwise return the Python sentinel."""
    selected, error_message = _resolve_config(config_reader(journal))
    if error_message is not None:
        return _config_error(error_message, stage="config", reason="invalid-config")
    if selected == "python":
        return NativeSpeakerAnalysisResult(status="python")

    handshake = handshake_checker()
    if handshake.status != "ok":
        return _config_error(
            handshake.message or f"speakers-analyze handshake {handshake.status}",
            stage="handshake",
            reason=handshake.status,
        )

    blocked, breaker_record = breaker_blocked(journal_path=journal)
    if blocked:
        return _fallback(
            stage="breaker",
            reason="consecutive-native-failures",
            degradation="breaker_open",
            extra={
                "speaker_analysis_consecutive_failures": breaker_record.get(
                    "consecutive_failures"
                )
            },
        )

    try:
        wespeaker_model_path, pyannote_model_path = model_path_resolver()
        temp_dir = temp_dir_factory(raw_path)
    except Exception as exc:
        record_native_failure(
            stage="request",
            reason=type(exc).__name__,
            native_exit_code=None,
            journal_path=journal,
        )
        return _fallback(stage="request", reason=type(exc).__name__)

    try:
        try:
            request, payload_path = _build_request(
                temp_dir=temp_dir,
                full_audio=full_audio,
                statement_audio=statement_audio,
                reduced_audio=reduced_audio,
                statements_pre_restore=statements_pre_restore,
                statements_restored=statements_restored,
                sample_rate=sample_rate,
                wespeaker_model_path=wespeaker_model_path,
                pyannote_model_path=pyannote_model_path,
            )
            expected_statement_ids = _python_admitted_statement_ids(
                statement_audio,
                statements_pre_restore,
                sample_rate=sample_rate,
                min_statement_duration=min_statement_duration,
            )
            completed = native_runner(
                [str(helper_locator())],
                input=json.dumps(request, sort_keys=True),
                capture_output=True,
                text=True,
                check=False,
            )
        except OSError as exc:
            record_native_failure(
                stage="invoke",
                reason=type(exc).__name__,
                native_exit_code=None,
                journal_path=journal,
            )
            return _fallback(stage="invoke", reason=type(exc).__name__)
        except NativePayloadError as exc:
            record_native_failure(
                stage=exc.stage,
                reason=exc.reason,
                native_exit_code=None,
                journal_path=journal,
            )
            return _fallback(stage=exc.stage, reason=exc.reason)
        except Exception as exc:
            record_native_failure(
                stage="request",
                reason=type(exc).__name__,
                native_exit_code=None,
                journal_path=journal,
            )
            return _fallback(stage="request", reason=type(exc).__name__)

        if completed.returncode == EXIT_UNAVAILABLE:
            reason = _helper_reason(completed.stderr) or "native-exit-69"
            return _config_error(
                "solstone-core-speakers-analyze exited 69 "
                f"({reason}); set core.speakers_analyze to 'python' to revert.",
                stage="invoke",
                reason=reason,
                native_exit_code=completed.returncode,
            )
        if completed.returncode < 0:
            reason = f"signal-{abs(completed.returncode)}"
            record_native_failure(
                stage="invoke",
                reason=reason,
                native_exit_code=completed.returncode,
                journal_path=journal,
            )
            return _fallback(
                stage="invoke",
                reason=reason,
                native_exit_code=completed.returncode,
            )
        if completed.returncode != 0:
            reason = _helper_reason(completed.stderr) or f"exit-{completed.returncode}"
            record_native_failure(
                stage="invoke",
                reason=reason,
                native_exit_code=completed.returncode,
                journal_path=journal,
            )
            return _fallback(
                stage="invoke",
                reason=reason,
                native_exit_code=completed.returncode,
            )

        try:
            response = json.loads(completed.stdout)
        except json.JSONDecodeError:
            record_native_failure(
                stage="parse",
                reason="malformed-response",
                native_exit_code=completed.returncode,
                journal_path=journal,
            )
            return _fallback(stage="parse", reason="malformed-response")

        try:
            accepted = _accepted_result_from_response(
                response,
                payload_path=payload_path,
                statements_restored=statements_restored,
                expected_statement_ids=expected_statement_ids,
            )
        except NativePayloadError as exc:
            record_native_failure(
                stage=exc.stage,
                reason=exc.reason,
                native_exit_code=completed.returncode,
                journal_path=journal,
            )
            return _fallback(stage=exc.stage, reason=exc.reason)

        record_native_success(journal_path=journal)
        return accepted
    finally:
        shutil.rmtree(temp_dir, ignore_errors=True)


def sweep_stale_speakers_analyze_dirs(max_age_seconds: int = 86400) -> int:
    swept = 0
    now = time.time()
    for path in TEMP_ROOT.glob(f"{TEMP_PREFIX}*"):
        if not path.is_dir():
            continue
        try:
            age_seconds = now - path.stat().st_mtime
        except OSError:
            continue
        if age_seconds <= max_age_seconds:
            continue
        shutil.rmtree(path, ignore_errors=True)
        if not path.exists():
            swept += 1
    return swept


def _resolve_config(config: dict[str, Any]) -> tuple[str, str | None]:
    core = config.get("core", {})
    if not isinstance(core, dict):
        return "invalid", INVALID_SPEAKERS_ANALYZE_MESSAGE.format(value=core)
    selected = core.get("speakers_analyze", "python")
    if selected not in ("python", "native"):
        return "invalid", INVALID_SPEAKERS_ANALYZE_MESSAGE.format(value=selected)
    return str(selected), None


def _build_request(
    *,
    temp_dir: Path,
    full_audio: np.ndarray,
    statement_audio: np.ndarray,
    reduced_audio: np.ndarray | None,
    statements_pre_restore: list[dict[str, Any]],
    statements_restored: list[dict[str, Any]],
    sample_rate: int,
    wespeaker_model_path: Path,
    pyannote_model_path: Path,
) -> tuple[dict[str, Any], Path]:
    full_audio_path = temp_dir / "full-audio.f32le"
    _write_f32le(full_audio_path, full_audio)
    reduced_audio_path: Path | None = None
    if reduced_audio is not None:
        reduced_audio_path = temp_dir / "reduced-audio.f32le"
        _write_f32le(reduced_audio_path, reduced_audio)
    payload_path = temp_dir / "statement-embeddings.f32le"

    statement_spans = _spans_from_statements(statements_pre_restore)
    diarization_spans = _spans_from_statements(statements_restored)
    _ensure_span_parity(statement_spans, diarization_spans)

    request: dict[str, Any] = {
        "schema": REQUEST_SCHEMA,
        "sample_rate_hz": sample_rate,
        "full_audio_f32le_path": str(full_audio_path),
        "models": {
            "pyannote_segmentation_onnx_path": str(pyannote_model_path),
            "wespeaker_onnx_path": str(wespeaker_model_path),
        },
        "output_payload_f32le_path": str(payload_path),
        "interval_embedding_payload_f32le_path": None,
        "statement_embedding": {"spans": statement_spans},
        "diarization": {"spans": diarization_spans},
    }
    if reduced_audio_path is not None:
        request["reduced_audio_f32le_path"] = str(reduced_audio_path)
    return request, payload_path


def _write_f32le(path: Path, audio: np.ndarray) -> None:
    data = np.asarray(audio, dtype="<f4").tobytes()
    fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, TEMP_FILE_MODE)
    try:
        with os.fdopen(fd, "wb") as handle:
            handle.write(data)
            handle.flush()
            os.fsync(handle.fileno())
    except BaseException:
        path.unlink(missing_ok=True)
        raise


def _spans_from_statements(statements: list[dict[str, Any]]) -> list[dict[str, Any]]:
    spans: list[dict[str, Any]] = []
    for statement in statements:
        spans.append(
            {
                "statement_id": int(statement["id"]),
                "start_s": _optional_float(statement.get("start")),
                "end_s": _optional_float(statement.get("end")),
            }
        )
    return spans


def _optional_float(value: object) -> float | None:
    if isinstance(value, bool) or value is None:
        return None
    if not isinstance(value, int | float):
        return None
    return float(value)


def _ensure_span_parity(
    statement_spans: list[dict[str, Any]], diarization_spans: list[dict[str, Any]]
) -> None:
    if len(statement_spans) != len(diarization_spans):
        raise NativePayloadError(
            stage="request",
            reason="span-parity-length",
            message="native speaker request span lists differ in length",
        )
    for index, (left, right) in enumerate(zip(statement_spans, diarization_spans)):
        if left["statement_id"] != right["statement_id"]:
            raise NativePayloadError(
                stage="request",
                reason="span-parity-statement-id",
                message=f"native speaker request span id mismatch at index {index}",
            )


def _python_admitted_statement_ids(
    audio: np.ndarray,
    statements: list[dict[str, Any]],
    *,
    sample_rate: int,
    min_statement_duration: float,
) -> list[int]:
    audio_duration = len(audio) / sample_rate
    admitted: list[int] = []
    for statement in statements:
        start = statement.get("start")
        end = statement.get("end")
        if start is None or end is None:
            continue
        if not isinstance(start, int | float) or not isinstance(end, int | float):
            continue
        start = max(0.0, min(float(start), audio_duration))
        end = max(0.0, min(float(end), audio_duration))
        if end - start < min_statement_duration:
            continue
        start_sample = int(start * sample_rate)
        end_sample = int(end * sample_rate)
        if end_sample - start_sample < int(min_statement_duration * sample_rate):
            continue
        admitted.append(int(statement["id"]))
    return admitted


def _accepted_result_from_response(
    response: object,
    *,
    payload_path: Path,
    statements_restored: list[dict[str, Any]],
    expected_statement_ids: list[int],
) -> NativeSpeakerAnalysisResult:
    if not isinstance(response, dict):
        raise NativePayloadError("parse", "response-not-object")
    if response.get("schema") != RESPONSE_SCHEMA:
        raise NativePayloadError("parse", "unknown-schema")

    statement_embeddings = _required_object(response, "statement_embeddings")
    statement_ids = _required_int_list(statement_embeddings, "statement_ids")
    if statement_ids != expected_statement_ids:
        raise NativePayloadError("payload", "statement-id-divergence")
    durations_s = _required_float_list(statement_embeddings, "durations_s")
    rows = len(statement_ids)
    if len(durations_s) != rows:
        raise NativePayloadError("payload", "duration-count-mismatch")
    shape = statement_embeddings.get("shape")
    if shape != [rows, WESPEAKER_EMBEDDING_WIDTH]:
        raise NativePayloadError("payload", "embedding-shape-mismatch")
    payload_bytes = _read_payload_bytes(payload_path, rows)
    embeddings = np.frombuffer(payload_bytes, dtype="<f4").reshape(
        (rows, WESPEAKER_EMBEDDING_WIDTH)
    )

    evidence = _required_object(response, "evidence")
    speaker_evidence = SpeakerEvidenceDecision(
        speaker_evidence=_required_str(evidence, "speaker_evidence"),
        multi_window_fraction=_required_float(evidence, "multi_window_fraction"),
        mean_window_overlap_share=_required_float(
            evidence, "mean_window_overlap_share"
        ),
    )
    overlap_fraction = _required_float(evidence, "overlap_fraction")

    statements = [dict(statement) for statement in statements_restored]
    labels = _statement_labels(response)
    gate_declined = labels is None
    if labels is not None:
        if len(labels) != len(statements):
            raise NativePayloadError("payload", "statement-label-count-mismatch")
        for statement, label in zip(statements, labels):
            if label is not None:
                statement["speaker"] = int(label)

    embeddings_data = {
        "embeddings": embeddings.astype(np.float32, copy=False),
        "statement_ids": np.asarray(statement_ids, dtype=np.int32),
        "durations_s": np.asarray(durations_s, dtype=np.float32),
        "encoder": np.array(ENCODER_ID),
    }
    event_fields = {
        "speaker_analysis_path": "native",
    }
    if gate_declined:
        event_fields.update(
            {
                "speaker_analysis_degradation": "gate_decline",
                "speaker_analysis_stage": "evidence_gate",
                "speaker_analysis_reason": speaker_evidence.speaker_evidence,
            }
        )
    return NativeSpeakerAnalysisResult(
        status="accepted",
        statements=statements,
        embeddings_data=embeddings_data,
        speaker_evidence=speaker_evidence,
        overlap_fraction=overlap_fraction,
        event_fields=event_fields,
    )


def _required_object(container: dict[str, Any], key: str) -> dict[str, Any]:
    value = container.get(key)
    if not isinstance(value, dict):
        raise NativePayloadError("payload", f"missing-{key}")
    return value


def _required_int_list(container: dict[str, Any], key: str) -> list[int]:
    value = container.get(key)
    if not isinstance(value, list) or any(
        isinstance(item, bool) or not isinstance(item, int) for item in value
    ):
        raise NativePayloadError("payload", f"invalid-{key}")
    return [int(item) for item in value]


def _required_float_list(container: dict[str, Any], key: str) -> list[float]:
    value = container.get(key)
    if not isinstance(value, list) or any(
        isinstance(item, bool) or not isinstance(item, int | float) for item in value
    ):
        raise NativePayloadError("payload", f"invalid-{key}")
    return [float(item) for item in value]


def _required_float(container: dict[str, Any], key: str) -> float:
    value = container.get(key)
    if isinstance(value, bool) or not isinstance(value, int | float):
        raise NativePayloadError("payload", f"invalid-{key}")
    return float(value)


def _required_str(container: dict[str, Any], key: str) -> str:
    value = container.get(key)
    if not isinstance(value, str):
        raise NativePayloadError("payload", f"invalid-{key}")
    return value


def _read_payload_bytes(path: Path, rows: int) -> bytes:
    payload = path.read_bytes()
    expected_bytes = rows * WESPEAKER_EMBEDDING_WIDTH * 4
    if len(payload) != expected_bytes:
        raise NativePayloadError("payload", "embedding-payload-size-mismatch")
    return payload


def _statement_labels(response: dict[str, Any]) -> list[int | None] | None:
    diarization = _required_object(response, "diarization")
    value = diarization.get("statement_labels")
    if value is None:
        return None
    if not isinstance(value, list):
        raise NativePayloadError("payload", "invalid-statement-labels")
    labels: list[int | None] = []
    for item in value:
        if item is None:
            labels.append(None)
        elif isinstance(item, bool) or not isinstance(item, int):
            raise NativePayloadError("payload", "invalid-statement-labels")
        else:
            labels.append(int(item))
    return labels


def _helper_reason(stderr: str) -> str | None:
    for line in stderr.splitlines():
        try:
            payload = json.loads(line)
        except json.JSONDecodeError:
            continue
        if (
            isinstance(payload, dict)
            and payload.get("schema") == ERROR_SCHEMA
            and isinstance(payload.get("reason"), str)
        ):
            return str(payload["reason"])
    return None


def _fallback(
    *,
    stage: str,
    reason: str,
    degradation: str = "native_failure",
    native_exit_code: int | None = None,
    extra: dict[str, Any] | None = None,
) -> NativeSpeakerAnalysisResult:
    event_fields: dict[str, Any] = {
        "speaker_analysis_path": "native_to_python",
        "speaker_analysis_degradation": degradation,
        "speaker_analysis_stage": stage,
        "speaker_analysis_reason": reason,
    }
    if native_exit_code is not None:
        event_fields["speaker_analysis_native_exit_code"] = native_exit_code
    if extra:
        event_fields.update(extra)
    return NativeSpeakerAnalysisResult(status="fallback", event_fields=event_fields)


def _config_error(
    message: str,
    *,
    stage: str,
    reason: str,
    native_exit_code: int | None = None,
) -> NativeSpeakerAnalysisResult:
    event_fields = {
        "speaker_analysis_path": "native",
        "speaker_analysis_degradation": "configuration_error",
        "speaker_analysis_stage": stage,
        "speaker_analysis_reason": reason,
    }
    if native_exit_code is not None:
        event_fields["speaker_analysis_native_exit_code"] = native_exit_code
    return NativeSpeakerAnalysisResult(
        status="config_error",
        event_fields=event_fields,
        error_message=message,
    )


def _safe_temp_part(value: str) -> str:
    cleaned = "".join(ch if ch.isalnum() or ch in ("-", "_") else "_" for ch in value)
    return cleaned[:80] or "x"


class NativePayloadError(RuntimeError):
    def __init__(self, stage: str, reason: str, message: str | None = None) -> None:
        super().__init__(message or reason)
        self.stage = stage
        self.reason = reason


__all__ = [
    "CONFIG_KEY",
    "EXIT_CONFIG",
    "INVALID_SPEAKERS_ANALYZE_MESSAGE",
    "PRODUCER_ID",
    "NativeSpeakerAnalysisResult",
    "create_speakers_analyze_temp_dir",
    "maybe_run_native_speaker_analysis",
    "sweep_stale_speakers_analyze_dirs",
]
