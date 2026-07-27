# SPDX-License-Identifier: AGPL-3.0-only
# Copyright (c) 2026 sol pbc

"""Tests for the config-gated native speaker analysis seam."""

from __future__ import annotations

import json
import os
import stat
import subprocess
import time
from pathlib import Path
from typing import Any

import numpy as np

from solstone.apps.speakers.encoder_config import WESPEAKER_EMBEDDING_WIDTH
from solstone.observe.transcribe.speakers_analyze_seam import (
    CONFIG_KEY,
    EXIT_UNAVAILABLE,
    RESPONSE_SCHEMA,
    NativeSpeakerAnalysisResult,
    create_speakers_analyze_temp_dir,
    maybe_run_native_speaker_analysis,
    sweep_stale_speakers_analyze_dirs,
)
from solstone.think import speakers_analyze_runtime
from solstone.think.speakers_analyze_handshake import SpeakersAnalyzeHandshakeResult

CUTOVER_ARTIFACT_WALL_CLOCK_EXCLUSIONS = frozenset(
    {
        "_solstone_processing.attempted_at",
        "source_segments[].added_at",
        "merge_events[].merged_at",
        "consolidation_summary.last_merge.merged_at",
    }
)


class RuntimeRecorder:
    def __init__(self) -> None:
        self.failures: list[dict[str, Any]] = []
        self.successes = 0

    def success(self, **_kwargs: Any) -> dict[str, Any]:
        self.successes += 1
        return {}

    def failure(self, **kwargs: Any) -> dict[str, Any]:
        self.failures.append(kwargs)
        return {"consecutive_failures": len(self.failures)}


def _statements() -> list[dict[str, Any]]:
    return [
        {"id": 1, "start": 0.0, "end": 0.5, "text": "one"},
        {"id": 2, "start": 0.6, "end": 1.0, "text": "two"},
    ]


def _restored_statements() -> list[dict[str, Any]]:
    return [
        {"id": 1, "start": 10.0, "end": 10.5, "text": "one"},
        {"id": 2, "start": 11.0, "end": 11.4, "text": "two"},
    ]


def _temp_factory(tmp_path: Path):
    paths: list[Path] = []

    def factory(_raw_path: Path) -> Path:
        path = tmp_path / f"speakers-analyze-{len(paths)}"
        path.mkdir(mode=0o700)
        paths.append(path)
        return path

    return factory, paths


def _base_response(
    *,
    statement_ids: list[int] | None = None,
    labels: list[int | None] | None = [7, None],
    speaker_evidence: str = "multi",
) -> dict[str, Any]:
    if statement_ids is None:
        statement_ids = [1, 2]
    return {
        "schema": RESPONSE_SCHEMA,
        "statement_embeddings": {
            "statement_ids": statement_ids,
            "durations_s": [0.5 for _sid in statement_ids],
            "shape": [len(statement_ids), WESPEAKER_EMBEDDING_WIDTH],
        },
        "evidence": {
            "overlap_fraction": 0.125,
            "speaker_evidence": speaker_evidence,
            "multi_window_fraction": 0.5,
            "mean_window_overlap_share": 0.25,
        },
        "diarization": {"statement_labels": labels},
    }


def _payload_bytes(rows: int, *, truncate: bool = False) -> bytes:
    values = np.arange(rows * WESPEAKER_EMBEDDING_WIDTH, dtype="<f4")
    payload = values.tobytes()
    return payload[:-4] if truncate else payload


def _completed(
    request: dict[str, Any],
    *,
    returncode: int = 0,
    response: dict[str, Any] | None = None,
    stderr_reason: str | None = None,
    truncate_payload: bool = False,
) -> subprocess.CompletedProcess[str]:
    response = response or _base_response()
    if returncode == 0:
        statement_ids = response["statement_embeddings"]["statement_ids"]
        Path(request["output_payload_f32le_path"]).write_bytes(
            _payload_bytes(len(statement_ids), truncate=truncate_payload)
        )
    stderr = ""
    if stderr_reason is not None:
        stderr = json.dumps(
            {
                "schema": "solstone-speaker-analyze-error-v1",
                "reason": stderr_reason,
            }
        )
    return subprocess.CompletedProcess(
        args=["solstone-core-speakers-analyze"],
        returncode=returncode,
        stdout=json.dumps(response),
        stderr=stderr,
    )


def _run_native(
    tmp_path: Path,
    *,
    config: dict[str, Any] | None = None,
    runner=None,
    reduced_audio: np.ndarray | None = None,
    full_audio: np.ndarray | None = None,
    statement_audio: np.ndarray | None = None,
    statements_pre_restore: list[dict[str, Any]] | None = None,
    statements_restored: list[dict[str, Any]] | None = None,
    breaker_blocked=None,
    recorder: RuntimeRecorder | None = None,
) -> NativeSpeakerAnalysisResult:
    recorder = recorder or RuntimeRecorder()
    temp_factory, _paths = _temp_factory(tmp_path)
    if runner is None:

        def runner(_argv, **kwargs):
            return _completed(json.loads(kwargs["input"]))

    return maybe_run_native_speaker_analysis(
        journal=tmp_path,
        raw_path=tmp_path
        / "chronicle"
        / "20260101"
        / "mic"
        / "090000_300"
        / "mic_audio.flac",
        full_audio=(
            np.asarray(full_audio, dtype=np.float32)
            if full_audio is not None
            else np.ones(20, dtype=np.float32)
        ),
        statement_audio=(
            np.asarray(statement_audio, dtype=np.float32)
            if statement_audio is not None
            else np.ones(20, dtype=np.float32)
        ),
        reduced_audio=reduced_audio,
        statements_pre_restore=statements_pre_restore or _statements(),
        statements_restored=statements_restored or _statements(),
        sample_rate=10,
        min_statement_duration=0.3,
        config_reader=lambda _journal: (
            config if config is not None else {"core": {"speakers_analyze": "native"}}
        ),
        handshake_checker=lambda: SpeakersAnalyzeHandshakeResult("ok"),
        helper_locator=lambda: tmp_path / "solstone-core-speakers-analyze",
        native_runner=runner,
        model_path_resolver=lambda: (
            tmp_path / "wespeaker.onnx",
            tmp_path / "pyannote.onnx",
        ),
        temp_dir_factory=temp_factory,
        breaker_blocked=breaker_blocked
        or (lambda **_kwargs: (False, {"consecutive_failures": 0})),
        record_native_success=recorder.success,
        record_native_failure=recorder.failure,
    )


def test_absent_key_selects_python_without_running_helper(tmp_path: Path):
    result = _run_native(
        tmp_path,
        config={},
        runner=lambda *_args, **_kwargs: (_ for _ in ()).throw(
            AssertionError("native runner called")
        ),
    )

    assert result.status == "python"
    assert result.event_fields == {}


def test_explicit_python_selects_python_without_running_helper(tmp_path: Path):
    result = _run_native(
        tmp_path,
        config={"core": {"speakers_analyze": "python"}},
        runner=lambda *_args, **_kwargs: (_ for _ in ()).throw(
            AssertionError("native runner called")
        ),
    )

    assert result.status == "python"
    assert result.event_fields == {}


def test_invalid_config_value_fails_loudly(tmp_path: Path):
    result = _run_native(tmp_path, config={"core": {"speakers_analyze": "rust"}})

    assert result.status == "config_error"
    assert result.error_message is not None
    assert CONFIG_KEY in result.error_message
    assert "found 'rust'" in result.error_message
    assert "expected 'python' or 'native'" in result.error_message
    assert "Set core.speakers_analyze to 'python' to revert" in result.error_message


def test_native_success_maps_response_payload(tmp_path: Path):
    result = _run_native(tmp_path)

    assert result.status == "accepted"
    assert result.statements == [
        {"id": 1, "start": 0.0, "end": 0.5, "text": "one", "speaker": 7},
        {"id": 2, "start": 0.6, "end": 1.0, "text": "two"},
    ]
    assert result.embeddings_data is not None
    assert result.embeddings_data["embeddings"].shape == (
        2,
        WESPEAKER_EMBEDDING_WIDTH,
    )
    assert result.overlap_fraction == 0.125
    assert result.speaker_evidence is not None
    assert result.speaker_evidence.speaker_evidence == "multi"
    assert result.event_fields == {"speaker_analysis_path": "native"}


def test_native_gate_decline_accepts_no_speaker_labels(tmp_path: Path):
    def runner(_argv, **kwargs):
        request = json.loads(kwargs["input"])
        return _completed(
            request,
            response=_base_response(labels=None, speaker_evidence="single"),
        )

    result = _run_native(tmp_path, runner=runner)

    assert result.status == "accepted"
    assert result.statements == _statements()
    assert all("speaker" not in statement for statement in result.statements or [])
    assert result.event_fields == {
        "speaker_analysis_path": "native",
        "speaker_analysis_degradation": "gate_decline",
        "speaker_analysis_stage": "evidence_gate",
        "speaker_analysis_reason": "single",
    }


def test_request_contains_full_and_reduced_buffers_and_restored_spans(tmp_path: Path):
    captured: dict[str, Any] = {}
    full_audio = np.asarray([1.0, 2.0, 3.0, 4.0], dtype=np.float32)
    reduced_audio = np.arange(20, dtype=np.float32) + 10.0

    def runner(_argv, **kwargs):
        request = json.loads(kwargs["input"])
        captured["request"] = request
        captured["full_audio"] = np.fromfile(
            request["full_audio_f32le_path"], dtype="<f4"
        )
        captured["reduced_audio"] = np.fromfile(
            request["reduced_audio_f32le_path"], dtype="<f4"
        )
        return _completed(request)

    result = _run_native(
        tmp_path,
        runner=runner,
        full_audio=full_audio,
        statement_audio=reduced_audio,
        reduced_audio=reduced_audio,
        statements_restored=_restored_statements(),
    )

    assert result.status == "accepted"
    request = captured["request"]
    np.testing.assert_array_equal(captured["full_audio"], full_audio)
    np.testing.assert_array_equal(captured["reduced_audio"], reduced_audio)
    assert request["interval_embedding_payload_f32le_path"] is None
    assert [
        span["statement_id"] for span in request["statement_embedding"]["spans"]
    ] == [
        1,
        2,
    ]
    assert [span["statement_id"] for span in request["diarization"]["spans"]] == [1, 2]
    assert request["statement_embedding"]["spans"][0]["start_s"] == 0.0
    assert request["diarization"]["spans"][0]["start_s"] == 10.0


def test_request_omits_reduced_path_when_no_reduction(tmp_path: Path):
    captured: dict[str, Any] = {}

    def runner(_argv, **kwargs):
        request = json.loads(kwargs["input"])
        captured["request"] = request
        return _completed(request)

    result = _run_native(tmp_path, runner=runner, reduced_audio=None)

    assert result.status == "accepted"
    assert "reduced_audio_f32le_path" not in captured["request"]


def test_span_parity_mismatch_falls_back_before_invocation(tmp_path: Path):
    recorder = RuntimeRecorder()
    result = _run_native(
        tmp_path,
        statements_restored=[
            {"id": 1, "start": 0.0, "end": 0.5, "text": "one"},
            {"id": 99, "start": 0.6, "end": 1.0, "text": "two"},
        ],
        runner=lambda *_args, **_kwargs: (_ for _ in ()).throw(
            AssertionError("native runner called")
        ),
        recorder=recorder,
    )

    assert result.status == "fallback"
    assert result.event_fields["speaker_analysis_stage"] == "request"
    assert result.event_fields["speaker_analysis_reason"] == "span-parity-statement-id"
    assert recorder.failures[0]["stage"] == "request"


def test_truncated_payload_falls_back_without_reshape(tmp_path: Path):
    recorder = RuntimeRecorder()

    def runner(_argv, **kwargs):
        request = json.loads(kwargs["input"])
        return _completed(request, truncate_payload=True)

    result = _run_native(tmp_path, runner=runner, recorder=recorder)

    assert result.status == "fallback"
    assert result.event_fields["speaker_analysis_stage"] == "payload"
    assert (
        result.event_fields["speaker_analysis_reason"]
        == "embedding-payload-size-mismatch"
    )
    assert recorder.failures[0]["reason"] == "embedding-payload-size-mismatch"


def test_statement_id_divergence_falls_back(tmp_path: Path):
    recorder = RuntimeRecorder()

    def runner(_argv, **kwargs):
        request = json.loads(kwargs["input"])
        return _completed(request, response=_base_response(statement_ids=[2, 1]))

    result = _run_native(tmp_path, runner=runner, recorder=recorder)

    assert result.status == "fallback"
    assert result.event_fields["speaker_analysis_reason"] == "statement-id-divergence"
    assert recorder.failures[0]["stage"] == "payload"


def test_zero_row_payload_accepts_without_embedding_data(tmp_path: Path):
    short_statements = [
        {"id": 1, "start": 0.0, "end": 0.1, "text": "one"},
        {"id": 2, "start": 0.1, "end": 0.2, "text": "two"},
    ]

    def runner(_argv, **kwargs):
        request = json.loads(kwargs["input"])
        return _completed(
            request,
            response=_base_response(
                statement_ids=[],
                labels=None,
                speaker_evidence="single",
            ),
        )

    result = _run_native(
        tmp_path,
        runner=runner,
        statements_pre_restore=short_statements,
        statements_restored=short_statements,
    )

    assert result.status == "accepted"
    assert result.embeddings_data is None
    assert result.event_fields["speaker_analysis_degradation"] == "gate_decline"


def test_native_failure_paths_are_observable_and_do_not_reread_config(tmp_path: Path):
    config_reads = 0
    recorder = RuntimeRecorder()

    def config_reader(_journal):
        nonlocal config_reads
        config_reads += 1
        return {"core": {"speakers_analyze": "native"}}

    def runner(_argv, **kwargs):
        return _completed(
            json.loads(kwargs["input"]), returncode=75, stderr_reason="io"
        )

    result = maybe_run_native_speaker_analysis(
        journal=tmp_path,
        raw_path=tmp_path
        / "chronicle"
        / "20260101"
        / "mic"
        / "090000_300"
        / "mic_audio.flac",
        full_audio=np.ones(20, dtype=np.float32),
        statement_audio=np.ones(20, dtype=np.float32),
        reduced_audio=None,
        statements_pre_restore=_statements(),
        statements_restored=_statements(),
        sample_rate=10,
        min_statement_duration=0.3,
        config_reader=config_reader,
        handshake_checker=lambda: SpeakersAnalyzeHandshakeResult("ok"),
        helper_locator=lambda: tmp_path / "solstone-core-speakers-analyze",
        native_runner=runner,
        model_path_resolver=lambda: (
            tmp_path / "wespeaker.onnx",
            tmp_path / "pyannote.onnx",
        ),
        temp_dir_factory=_temp_factory(tmp_path)[0],
        breaker_blocked=lambda **_kwargs: (False, {}),
        record_native_success=recorder.success,
        record_native_failure=recorder.failure,
    )

    assert result.status == "fallback"
    assert config_reads == 1
    assert result.event_fields == {
        "speaker_analysis_path": "native_to_python",
        "speaker_analysis_degradation": "native_failure",
        "speaker_analysis_stage": "invoke",
        "speaker_analysis_reason": "io",
        "speaker_analysis_native_exit_code": 75,
    }


def test_native_exit_64_and_75_fall_back(tmp_path: Path):
    for code, reason in ((64, "invalid-args"), (75, "unavailable")):
        case_root = tmp_path / str(code)
        case_root.mkdir()
        result = _run_native(
            case_root,
            runner=lambda _argv, code=code, reason=reason, **kwargs: _completed(
                json.loads(kwargs["input"]),
                returncode=code,
                stderr_reason=reason,
            ),
        )

        assert result.status == "fallback"
        assert result.event_fields["speaker_analysis_stage"] == "invoke"
        assert result.event_fields["speaker_analysis_reason"] == reason
        assert result.event_fields["speaker_analysis_native_exit_code"] == code


def test_exit_69_is_configuration_error_not_fallback(tmp_path: Path):
    result = _run_native(
        tmp_path,
        runner=lambda _argv, **kwargs: _completed(
            json.loads(kwargs["input"]),
            returncode=EXIT_UNAVAILABLE,
            stderr_reason="model-missing",
        ),
    )

    assert result.status == "config_error"
    assert result.event_fields["speaker_analysis_degradation"] == "configuration_error"
    assert result.event_fields["speaker_analysis_reason"] == "model-missing"
    assert result.event_fields["speaker_analysis_native_exit_code"] == EXIT_UNAVAILABLE


def test_negative_returncode_is_signal_fallback(tmp_path: Path):
    result = _run_native(
        tmp_path,
        runner=lambda _argv, **kwargs: _completed(
            json.loads(kwargs["input"]),
            returncode=-9,
        ),
    )

    assert result.status == "fallback"
    assert result.event_fields["speaker_analysis_reason"] == "signal-9"
    assert result.event_fields["speaker_analysis_native_exit_code"] == -9


def test_circuit_breaker_blocks_native_invocation(tmp_path: Path):
    result = _run_native(
        tmp_path,
        breaker_blocked=lambda **_kwargs: (True, {"consecutive_failures": 3}),
        runner=lambda *_args, **_kwargs: (_ for _ in ()).throw(
            AssertionError("native runner called")
        ),
    )

    assert result.status == "fallback"
    assert result.event_fields["speaker_analysis_degradation"] == "breaker_open"
    assert result.event_fields["speaker_analysis_stage"] == "breaker"
    assert result.event_fields["speaker_analysis_consecutive_failures"] == 3


def test_circuit_breaker_stops_after_three_consecutive_native_failures(tmp_path: Path):
    for attempt in range(1, 4):
        speakers_analyze_runtime.record_native_failure(
            stage="invoke",
            reason=f"exit-{attempt}",
            native_exit_code=75,
            journal_path=tmp_path,
        )
        blocked, record = speakers_analyze_runtime.native_blocked(journal_path=tmp_path)
        assert blocked is (attempt >= 3)
        assert record["consecutive_failures"] == attempt

    speakers_analyze_runtime.record_native_success(journal_path=tmp_path)

    blocked, record = speakers_analyze_runtime.native_blocked(journal_path=tmp_path)
    assert not blocked
    assert record["consecutive_failures"] == 0


def test_temp_dir_and_files_are_owner_only(tmp_path: Path, monkeypatch):
    import solstone.observe.transcribe.speakers_analyze_seam as seam

    captured: dict[str, int] = {}
    monkeypatch.setattr(seam, "TEMP_ROOT", tmp_path)

    def runner(_argv, **kwargs):
        request = json.loads(kwargs["input"])
        full_path = Path(request["full_audio_f32le_path"])
        captured["dir_mode"] = stat.S_IMODE(full_path.parent.stat().st_mode)
        captured["full_file_mode"] = stat.S_IMODE(full_path.stat().st_mode)
        captured["reduced_file_mode"] = stat.S_IMODE(
            Path(request["reduced_audio_f32le_path"]).stat().st_mode
        )
        return _completed(request)

    result = maybe_run_native_speaker_analysis(
        journal=tmp_path,
        raw_path=tmp_path
        / "chronicle"
        / "20260101"
        / "mic"
        / "090000_300"
        / "mic_audio.flac",
        full_audio=np.ones(20, dtype=np.float32),
        statement_audio=np.ones(10, dtype=np.float32),
        reduced_audio=np.ones(10, dtype=np.float32),
        statements_pre_restore=_statements(),
        statements_restored=_statements(),
        sample_rate=10,
        min_statement_duration=0.3,
        config_reader=lambda _journal: {"core": {"speakers_analyze": "native"}},
        handshake_checker=lambda: SpeakersAnalyzeHandshakeResult("ok"),
        helper_locator=lambda: tmp_path / "solstone-core-speakers-analyze",
        native_runner=runner,
        model_path_resolver=lambda: (
            tmp_path / "wespeaker.onnx",
            tmp_path / "pyannote.onnx",
        ),
        breaker_blocked=lambda **_kwargs: (False, {}),
        record_native_success=lambda **_kwargs: {},
        record_native_failure=lambda **_kwargs: {},
    )

    assert result.status == "accepted"
    assert captured == {
        "dir_mode": 0o700,
        "full_file_mode": 0o600,
        "reduced_file_mode": 0o600,
    }
    assert list(tmp_path.glob("solstone-speakers-analyze-*")) == []


def test_concurrent_temp_dirs_for_same_segment_are_distinct(
    tmp_path: Path, monkeypatch
):
    import solstone.observe.transcribe.speakers_analyze_seam as seam

    monkeypatch.setattr(seam, "TEMP_ROOT", tmp_path)
    raw_path = (
        tmp_path / "chronicle" / "20260101" / "mic" / "090000_300" / "mic_audio.flac"
    )

    first = create_speakers_analyze_temp_dir(raw_path)
    second = create_speakers_analyze_temp_dir(raw_path)

    try:
        assert first != second
        assert first.name.startswith("solstone-speakers-analyze-")
        assert second.name.startswith("solstone-speakers-analyze-")
    finally:
        first.rmdir()
        second.rmdir()


def test_sweeps_stale_temp_dirs_only(tmp_path: Path, monkeypatch):
    import solstone.observe.transcribe.speakers_analyze_seam as seam

    monkeypatch.setattr(seam, "TEMP_ROOT", tmp_path)
    stale = tmp_path / "solstone-speakers-analyze-old"
    fresh = tmp_path / "solstone-speakers-analyze-fresh"
    unrelated = tmp_path / "other"
    stale.mkdir()
    fresh.mkdir()
    unrelated.mkdir()
    old = time.time() - 90000
    os.utime(stale, (old, old))

    swept = sweep_stale_speakers_analyze_dirs(max_age_seconds=86400)

    assert swept == 1
    assert not stale.exists()
    assert fresh.exists()
    assert unrelated.exists()


def test_default_config_keeps_speakers_analyze_key_absent():
    default = json.loads(
        Path("solstone/think/journal_default.json").read_text(encoding="utf-8")
    )

    assert "speakers_analyze" not in json.dumps(default)


def test_cutover_wall_clock_exclusion_list_is_literal_and_complete():
    assert CUTOVER_ARTIFACT_WALL_CLOCK_EXCLUSIONS == {
        "_solstone_processing.attempted_at",
        "source_segments[].added_at",
        "merge_events[].merged_at",
        "consolidation_summary.last_merge.merged_at",
    }
    assert "last_seen_ts" not in CUTOVER_ARTIFACT_WALL_CLOCK_EXCLUSIONS
