# SPDX-License-Identifier: AGPL-3.0-only
# Copyright (c) 2026 sol pbc

"""Tests for the speaker differential harness.

Self-comparison reporting ``equal`` everywhere is only a sanity check that the
bundle can round-trip through the comparator.  It is not evidence that the
instrument works; the falsification tests below mutate copies of emitted
bundles and prove each compared component can fail independently.
"""

from __future__ import annotations

import json
import multiprocessing
import os
import shutil
import socket
import sys
import threading
import time
import uuid
from collections.abc import Callable
from pathlib import Path
from queue import Empty
from typing import Any

import numpy as np
import pytest

from solstone.observe.transcribe import diarize, overlap
from solstone.observe.vad import AudioReduction, SpeechSegment
from tests import verify_speaker_differential as harness
from tests._repo_inventory import assert_inventory_unchanged, repository_inventory
from tests._speaker_differential_fixtures import (
    COMPARATOR_THRESHOLDS,
    EMBEDDER_NAME,
    EMBEDDING_MAX_ABS_TOLERANCE,
    LOGPROB_MAX_ABS_TOLERANCE,
    SAMPLE_RATE,
    STATEMENT_DURATION_ABS_TOLERANCE,
    ModelFreeSpeakerCase,
    model_free_case,
    real_model_waveform,
)

CACHE_MUTATION_TIMEOUT_S = 2.0
CACHE_MUTATION_JOIN_TIMEOUT_S = 1.0


def _install_model_free_patches(
    monkeypatch: pytest.MonkeyPatch,
    case: ModelFreeSpeakerCase,
    *,
    overlap_fraction: float | None = None,
) -> Callable[[], None]:
    state = {"interval_index": 0}

    def fake_compute_overlap_and_logprobs(
        _audio: np.ndarray,
        _sample_rate: int = SAMPLE_RATE,
    ) -> overlap.OverlapInferenceResult:
        window_stats = (
            (overlap.SpeakerWindowStats(400, 1, 0),)
            if overlap_fraction == 0.0
            else tuple(case.window_stats)
        )
        return overlap.OverlapInferenceResult(
            case.overlap_fraction if overlap_fraction is None else overlap_fraction,
            case.avg_log_probs.copy(),
            window_stats,
        )

    def fake_embed_statements(
        _audio: np.ndarray,
        _statements: list[dict[str, Any]],
        _sample_rate: int,
    ) -> dict[str, np.ndarray]:
        return {
            "embeddings": case.statement_embeddings.copy(),
            "statement_ids": case.statement_ids.copy(),
            "durations_s": case.statement_durations_s.copy(),
            "encoder": np.array(EMBEDDER_NAME),
        }

    def fake_embed_interval(
        _audio: np.ndarray,
        _start_s: float,
        _end_s: float,
    ) -> np.ndarray:
        idx = state["interval_index"]
        if idx >= len(case.interval_embeddings):
            raise AssertionError("unexpected extra interval embedding request")
        state["interval_index"] += 1
        return case.interval_embeddings[idx].copy()

    def reset_interval_embeddings() -> None:
        state["interval_index"] = 0

    monkeypatch.setattr(
        overlap,
        "compute_overlap_and_logprobs",
        fake_compute_overlap_and_logprobs,
    )
    monkeypatch.setattr(
        harness.transcribe_main,
        "_embed_statements",
        fake_embed_statements,
    )
    monkeypatch.setattr(diarize, "_embed_interval", fake_embed_interval)
    return reset_interval_embeddings


def _emit_model_free_bundle(
    monkeypatch: pytest.MonkeyPatch,
    case: ModelFreeSpeakerCase | None = None,
    *,
    overlap_fraction: float | None = None,
) -> tuple[ModelFreeSpeakerCase, harness.Bundle, Callable[[], None]]:
    speaker_case = case if case is not None else model_free_case()
    reset = _install_model_free_patches(
        monkeypatch,
        speaker_case,
        overlap_fraction=overlap_fraction,
    )
    reset()
    bundle = harness.emit_speaker_bundle(
        audio_buffer=speaker_case.audio,
        statements=speaker_case.statements,
    )
    return speaker_case, bundle, reset


def _mutated(
    bundle: harness.Bundle,
    mutate: Callable[[harness.Bundle], None],
) -> harness.Bundle:
    copy = harness.copy_bundle(bundle)
    mutate(copy)
    return copy


def _component_classification(
    left: harness.Bundle,
    right: harness.Bundle,
    component: str,
) -> str:
    report = harness.compare_bundles(left, right)
    return str(report["components"][component]["classification"])


def _cache_mutation_worker(
    cache_dir: str,
    pyc_path: str,
    barrier: Any,
    messages: Any,
) -> None:
    messages.put({"event": "ready", "pid": os.getpid()})
    barrier.wait(timeout=CACHE_MUTATION_TIMEOUT_S)
    Path(cache_dir).mkdir()
    Path(pyc_path).write_bytes(f"cache mutation from {os.getpid()}\n".encode())
    messages.put({"event": "mutated", "pid": os.getpid(), "path": pyc_path})


def _cache_mutation_message(
    messages: Any,
    process: multiprocessing.Process,
    expected_event: str,
) -> dict[str, Any]:
    try:
        message = messages.get(timeout=CACHE_MUTATION_TIMEOUT_S)
    except Empty as exc:
        process.join(timeout=0)
        if process.exitcode is not None:
            raise AssertionError(
                "sibling cache mutation process exited before "
                f"{expected_event!r}: exitcode={process.exitcode}"
            ) from exc
        raise AssertionError(
            f"sibling cache mutation did not report {expected_event!r}"
        ) from exc
    assert message["event"] == expected_event, message
    return dict(message)


def _release_cache_mutation(barrier: Any) -> None:
    try:
        barrier.wait(timeout=CACHE_MUTATION_TIMEOUT_S)
    except threading.BrokenBarrierError as exc:
        raise AssertionError(
            "sibling cache mutation did not reach the barrier"
        ) from exc


def _mutate_logprob_below_tolerance(bundle: harness.Bundle) -> None:
    log_probs = harness._array(bundle, harness.PYANNOTE_LOGPROBS).copy()
    log_probs[0, 0] += LOGPROB_MAX_ABS_TOLERANCE / 2
    harness.replace_array(bundle, harness.PYANNOTE_LOGPROBS, log_probs)


def _mutate_logprob_above_tolerance(bundle: harness.Bundle) -> None:
    log_probs = harness._array(bundle, harness.PYANNOTE_LOGPROBS).copy()
    log_probs[0, 0] += LOGPROB_MAX_ABS_TOLERANCE * 10
    harness.replace_array(bundle, harness.PYANNOTE_LOGPROBS, log_probs)


def _mutate_evidence_outcome(bundle: harness.Bundle) -> None:
    harness.set_scalar_value(bundle, harness.EVIDENCE_SPEAKER, "single")


def _mutate_window_stats(bundle: harness.Bundle) -> None:
    stats = harness._array(bundle, harness.PYANNOTE_WINDOW_STATS).copy()
    stats[0, 0] += 1
    harness.replace_array(bundle, harness.PYANNOTE_WINDOW_STATS, stats)


def _mutate_remove_interval(bundle: harness.Bundle) -> None:
    intervals = harness._array(bundle, harness.DIARIZATION_INTERVALS).copy()
    harness.replace_array(bundle, harness.DIARIZATION_INTERVALS, intervals[:-1])


def _mutate_shift_interval_boundary(bundle: harness.Bundle) -> None:
    intervals = harness._array(bundle, harness.DIARIZATION_INTERVALS).copy()
    intervals[0, 0] += 0.01
    harness.replace_array(bundle, harness.DIARIZATION_INTERVALS, intervals)


def _mutate_embedding_below_tolerance(bundle: harness.Bundle) -> None:
    embeddings = harness._array(bundle, harness.DIARIZATION_INTERVAL_EMBEDDINGS).copy()
    embeddings[0, 0] += EMBEDDING_MAX_ABS_TOLERANCE / 2
    harness.replace_array(bundle, harness.DIARIZATION_INTERVAL_EMBEDDINGS, embeddings)


def _mutate_embedding_above_tolerance(bundle: harness.Bundle) -> None:
    embeddings = harness._array(bundle, harness.DIARIZATION_INTERVAL_EMBEDDINGS).copy()
    embeddings[0, 0] += EMBEDDING_MAX_ABS_TOLERANCE * 10
    harness.replace_array(bundle, harness.DIARIZATION_INTERVAL_EMBEDDINGS, embeddings)


def _mutate_statement_embedding_below_tolerance(bundle: harness.Bundle) -> None:
    embeddings = harness._array(bundle, harness.STATEMENT_EMBEDDINGS).copy()
    embeddings[0, 0] += EMBEDDING_MAX_ABS_TOLERANCE / 2
    harness.replace_array(bundle, harness.STATEMENT_EMBEDDINGS, embeddings)


def _mutate_statement_embedding_above_tolerance(bundle: harness.Bundle) -> None:
    embeddings = harness._array(bundle, harness.STATEMENT_EMBEDDINGS).copy()
    embeddings[0, 0] += EMBEDDING_MAX_ABS_TOLERANCE * 10
    harness.replace_array(bundle, harness.STATEMENT_EMBEDDINGS, embeddings)


def _mutate_statement_duration(bundle: harness.Bundle) -> None:
    durations = harness._array(bundle, harness.STATEMENT_DURATIONS).copy()
    durations[0] += STATEMENT_DURATION_ABS_TOLERANCE * 10
    harness.replace_array(bundle, harness.STATEMENT_DURATIONS, durations)


def _mutate_statement_encoder(bundle: harness.Bundle) -> None:
    harness.replace_array(bundle, harness.STATEMENT_ENCODER, np.array("other-encoder"))


def _mutate_valid_interval(bundle: harness.Bundle) -> None:
    intervals = harness._array(bundle, harness.DIARIZATION_VALID_INTERVALS).copy()
    intervals[0, 1] += 0.01
    harness.replace_array(bundle, harness.DIARIZATION_VALID_INTERVALS, intervals)


def _mutate_silhouette_k(bundle: harness.Bundle) -> None:
    current = harness._scalar(bundle, harness.DIARIZATION_SILHOUETTE_K)
    harness.set_scalar_value(
        bundle,
        harness.DIARIZATION_SILHOUETTE_K,
        1 if current != 1 else 2,
    )


def _mutate_effective_k(bundle: harness.Bundle) -> None:
    harness.set_scalar_value(bundle, harness.DIARIZATION_EFFECTIVE_K, 1)


def _mutate_statement_label(bundle: harness.Bundle) -> None:
    labels = harness._array(bundle, harness.DIARIZATION_STATEMENT_LABELS).copy()
    labels[0] = labels[0] + 1 if labels[0] != harness.LABEL_NULL_SENTINEL else 1
    harness.replace_array(bundle, harness.DIARIZATION_STATEMENT_LABELS, labels)


def _mutate_repermuted_cluster_labels(bundle: harness.Bundle) -> None:
    labels = harness._array(bundle, harness.DIARIZATION_CLUSTER_LABELS).copy()
    unique = sorted(set(labels.tolist()))
    assert len(unique) == 2
    swapped = np.where(labels == unique[0], unique[1], unique[0]).astype(np.int32)
    harness.replace_array(bundle, harness.DIARIZATION_CLUSTER_LABELS, swapped)


class _RunCountOnlySession:
    run_count = 7

    def get_providers(self) -> list[str]:
        return ["CPUExecutionProvider"]


def test_model_free_emitter_matches_independent_production_recording(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(overlap, "_overlap_session", _RunCountOnlySession())
    case, bundle, reset = _emit_model_free_bundle(monkeypatch)
    counters = bundle["manifest"]["counters"]
    assert counters["overlap_session_run_count_before_diarize"] == 7
    assert counters["overlap_session_run_count_after_diarize"] == 7

    overlap_result = overlap.compute_overlap_and_logprobs(case.audio, SAMPLE_RATE)
    evidence = overlap.decide_speaker_evidence(
        overlap_result.overlap_fraction,
        overlap_result.window_stats,
    )
    np.testing.assert_array_equal(
        harness._array(bundle, harness.PYANNOTE_LOGPROBS),
        overlap_result.avg_log_probs,
    )
    assert (
        harness._scalar(bundle, harness.EVIDENCE_SPEAKER) == evidence.speaker_evidence
    )
    assert harness._scalar(bundle, harness.EVIDENCE_OVERLAP_FRACTION) == pytest.approx(
        overlap_result.overlap_fraction
    )
    np.testing.assert_array_equal(
        harness._array(bundle, harness.PYANNOTE_WINDOW_STATS),
        np.asarray([tuple(row) for row in overlap_result.window_stats], dtype=np.int32),
    )

    reset()
    with harness.record_diarize_private_helpers() as records:
        direct_labels = diarize.diarize_auto_k(
            harness.PLACEHOLDER_WAV_PATH,
            [dict(statement) for statement in case.statements],
            avg_log_probs=case.avg_log_probs,
            audio=case.audio,
        )

    np.testing.assert_array_equal(
        harness._array(bundle, harness.DIARIZATION_INTERVALS),
        harness._intervals_array(records["intervals"]),
    )
    np.testing.assert_array_equal(
        harness._array(bundle, harness.DIARIZATION_VALID_INTERVALS),
        harness._intervals_array(records["valid_intervals"]),
    )
    np.testing.assert_array_equal(
        harness._array(bundle, harness.DIARIZATION_INTERVAL_EMBEDDINGS),
        records["interval_embeddings"],
    )
    np.testing.assert_array_equal(
        harness._array(bundle, harness.DIARIZATION_CLUSTER_LABELS),
        records["cluster_labels"],
    )
    assert (
        harness._scalar(bundle, harness.DIARIZATION_SILHOUETTE_K)
        == records["silhouette_k"]
    )
    assert (
        harness._scalar(bundle, harness.DIARIZATION_EFFECTIVE_K)
        == records["effective_k"]
    )
    np.testing.assert_array_equal(
        harness._array(bundle, harness.DIARIZATION_STATEMENT_LABELS),
        harness.encode_statement_labels(direct_labels),
    )


def test_report_names_thresholds_for_tolerance_judged_components(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _case, bundle, _reset = _emit_model_free_bundle(monkeypatch)
    report = harness.compare_bundles(bundle, harness.copy_bundle(bundle))
    components = report["components"]

    expected = {
        "pyannote_log_probs": {
            "max_abs_diff": "LOGPROB_MAX_ABS_TOLERANCE",
            "median_abs_diff": "LOGPROB_MEDIAN_ABS_TOLERANCE",
            "per_frame_argmax_agreement_fraction": "LOGPROB_ARGMAX_AGREEMENT_MIN",
        },
        "evidence": {
            harness.EVIDENCE_OVERLAP_FRACTION: "EVIDENCE_FLOAT_ABS_TOLERANCE",
            harness.EVIDENCE_MULTI_FRACTION: "EVIDENCE_FLOAT_ABS_TOLERANCE",
            harness.EVIDENCE_MEAN_OVERLAP: "EVIDENCE_FLOAT_ABS_TOLERANCE",
        },
        "statement_embeddings": {
            "max_abs_component_diff": "EMBEDDING_MAX_ABS_TOLERANCE",
            "min_cosine_similarity": "EMBEDDING_MIN_COSINE_SIMILARITY",
            "median_cosine_similarity": "EMBEDDING_MEDIAN_COSINE_SIMILARITY",
            "duration_max_abs_diff": "STATEMENT_DURATION_ABS_TOLERANCE",
        },
        "interval_embeddings": {
            "max_abs_component_diff": "EMBEDDING_MAX_ABS_TOLERANCE",
            "min_cosine_similarity": "EMBEDDING_MIN_COSINE_SIMILARITY",
            "median_cosine_similarity": "EMBEDDING_MEDIAN_COSINE_SIMILARITY",
        },
    }
    for component, thresholds in expected.items():
        reported = components[component]["thresholds"]
        for metric, constant_name in thresholds.items():
            assert reported[metric]["constant"] == constant_name
            assert reported[metric]["value"] == COMPARATOR_THRESHOLDS[constant_name]

    assert (
        components["statement_embeddings"]["thresholds"][harness.STATEMENT_ENCODER]
        == "exact"
    )
    assert (
        components["interval_embeddings"]["thresholds"][
            harness.DIARIZATION_VALID_INTERVALS
        ]
        == "exact"
    )
    assert components["evidence"]["thresholds"][harness.EVIDENCE_SPEAKER] == "exact"
    assert (
        components["evidence"]["thresholds"][harness.PYANNOTE_WINDOW_STATS] == "exact"
    )
    assert components["intervals"]["thresholds"] == "exact"
    assert components["clustering"]["thresholds"] == "exact"
    assert components["statement_labels"]["thresholds"] == "exact"


def test_bundle_round_trip_is_exact_and_manifest_validated(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _case, bundle, _reset = _emit_model_free_bundle(monkeypatch)
    bundle_path = tmp_path / "speaker-a.npz"
    original_load = harness.np.load
    load_kwargs: list[bool | None] = []

    def spy_load(*args: Any, **kwargs: Any) -> Any:
        load_kwargs.append(kwargs.get("allow_pickle"))
        return original_load(*args, **kwargs)

    harness.write_bundle(bundle, bundle_path)
    monkeypatch.setattr(harness.np, "load", spy_load)
    loaded = harness.load_bundle(bundle_path)

    assert load_kwargs == [False]
    for key, array in bundle["arrays"].items():
        np.testing.assert_array_equal(loaded["arrays"][key], array)
        field = loaded["manifest"]["fields"][key]
        assert field["dtype"] == str(array.dtype)
        assert field["shape"] == list(array.shape)

    assert loaded["arrays"][harness.PYANNOTE_LOGPROBS].dtype == np.float32
    assert loaded["arrays"][harness.INPUT_STATEMENT_SPANS].dtype == np.float64
    assert loaded["arrays"][harness.STATEMENT_EMBEDDING_IDS].dtype == np.int32
    assert loaded["arrays"][harness.DIARIZATION_INTERVAL_EMBEDDINGS].shape[1] == 256
    assert loaded["arrays"][harness.STATEMENT_ENCODER].shape == ()
    assert loaded["arrays"][harness.STATEMENT_ENCODER].dtype.kind == "U"

    bad = harness.copy_bundle(bundle)
    bad["manifest"]["fields"][harness.PYANNOTE_LOGPROBS]["shape"][0] += 1
    bad_path = tmp_path / "bad-shape.npz"
    harness.write_bundle(bad, bad_path)
    with pytest.raises(harness.HarnessError, match="shape mismatch"):
        harness.load_bundle(bad_path)


def test_duplicate_statement_ids_are_harness_error(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _case, bundle, _reset = _emit_model_free_bundle(monkeypatch)

    duplicate_embedding_ids = harness.copy_bundle(bundle)
    embedding_ids = harness._array(
        duplicate_embedding_ids, harness.STATEMENT_EMBEDDING_IDS
    ).copy()
    embedding_ids[1] = embedding_ids[0]
    harness.replace_array(
        duplicate_embedding_ids, harness.STATEMENT_EMBEDDING_IDS, embedding_ids
    )
    embedding_report = harness.compare_bundles(bundle, duplicate_embedding_ids)
    assert embedding_report["classification"] == harness.NOT_EVALUATED
    assert embedding_report["failure"]["class"] == harness.HARNESS_ERROR
    assert "duplicate statement ids" in embedding_report["failure"]["message"]

    duplicate_label_left = harness.copy_bundle(bundle)
    duplicate_label_right = harness.copy_bundle(bundle)
    label_ids = harness._array(
        duplicate_label_left, harness.INPUT_DIARIZATION_IDS
    ).copy()
    label_ids[1] = label_ids[0]
    harness.replace_array(
        duplicate_label_left, harness.INPUT_DIARIZATION_IDS, label_ids
    )
    harness.replace_array(
        duplicate_label_right, harness.INPUT_DIARIZATION_IDS, label_ids
    )
    label_report = harness.compare_bundles(duplicate_label_left, duplicate_label_right)
    assert label_report["classification"] == harness.NOT_EVALUATED
    assert label_report["failure"]["class"] == harness.HARNESS_ERROR
    assert "duplicate statement ids" in label_report["failure"]["message"]


def test_buffer_divergence_is_recorded_for_reduced_audio(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    full_audio = np.zeros(4 * SAMPLE_RATE, dtype=np.float32)
    reduced_audio = np.ones(1 * SAMPLE_RATE, dtype=np.float32) * 0.1
    statements = [{"id": 10, "start": 0.1, "end": 0.8, "text": "redacted"}]
    case = model_free_case()
    case = ModelFreeSpeakerCase(
        audio=full_audio,
        statements=statements,
        avg_log_probs=case.avg_log_probs,
        overlap_fraction=0.0,
        window_stats=case.window_stats,
        statement_embeddings=case.statement_embeddings[:1],
        statement_ids=np.array([10], dtype=np.int32),
        statement_durations_s=np.array([0.7], dtype=np.float32),
        interval_embeddings=case.interval_embeddings,
    )
    _install_model_free_patches(monkeypatch, case, overlap_fraction=0.0)
    reduction = AudioReduction(
        segments=[
            SpeechSegment(
                original_start=2.0,
                original_end=3.0,
                reduced_start=0.0,
                reduced_end=1.0,
            )
        ],
        original_duration=4.0,
        reduced_duration=1.0,
    )

    reduced_bundle = harness.emit_speaker_bundle(
        audio_buffer=full_audio,
        reduced_audio=reduced_audio,
        reduction=reduction,
        statements=statements,
    )
    inputs = reduced_bundle["manifest"]["inputs"]
    assert (
        inputs["statement_embedding"]["content_hash"]
        != inputs["diarization"]["content_hash"]
    )
    assert (
        inputs["statement_embedding"]["sample_count"]
        != inputs["diarization"]["sample_count"]
    )
    assert not np.array_equal(
        harness._array(reduced_bundle, harness.INPUT_STATEMENT_SPANS),
        harness._array(reduced_bundle, harness.INPUT_DIARIZATION_SPANS),
    )

    no_reduction_bundle = harness.emit_speaker_bundle(
        audio_buffer=full_audio,
        statements=statements,
    )
    no_reduction_inputs = no_reduction_bundle["manifest"]["inputs"]
    assert (
        no_reduction_inputs["statement_embedding"]["content_hash"]
        == no_reduction_inputs["diarization"]["content_hash"]
    )
    assert np.array_equal(
        harness._array(no_reduction_bundle, harness.INPUT_STATEMENT_SPANS),
        harness._array(no_reduction_bundle, harness.INPUT_DIARIZATION_SPANS),
    )


@pytest.mark.parametrize(
    ("mutate", "component", "expected"),
    [
        (
            _mutate_logprob_below_tolerance,
            "pyannote_log_probs",
            harness.FUNCTIONALLY_EQUAL,
        ),
        (
            _mutate_logprob_above_tolerance,
            "pyannote_log_probs",
            harness.UNEXPECTED_DIFFERS,
        ),
        (_mutate_evidence_outcome, "evidence", harness.UNEXPECTED_DIFFERS),
        (_mutate_window_stats, "evidence", harness.UNEXPECTED_DIFFERS),
        (_mutate_remove_interval, "intervals", harness.UNEXPECTED_DIFFERS),
        (_mutate_shift_interval_boundary, "intervals", harness.UNEXPECTED_DIFFERS),
        (
            _mutate_embedding_below_tolerance,
            "interval_embeddings",
            harness.FUNCTIONALLY_EQUAL,
        ),
        (
            _mutate_embedding_above_tolerance,
            "interval_embeddings",
            harness.UNEXPECTED_DIFFERS,
        ),
        (
            _mutate_statement_embedding_below_tolerance,
            "statement_embeddings",
            harness.FUNCTIONALLY_EQUAL,
        ),
        (
            _mutate_statement_embedding_above_tolerance,
            "statement_embeddings",
            harness.UNEXPECTED_DIFFERS,
        ),
        (
            _mutate_statement_duration,
            "statement_embeddings",
            harness.UNEXPECTED_DIFFERS,
        ),
        (_mutate_statement_encoder, "statement_embeddings", harness.UNEXPECTED_DIFFERS),
        (_mutate_valid_interval, "interval_embeddings", harness.UNEXPECTED_DIFFERS),
        (_mutate_silhouette_k, "clustering", harness.UNEXPECTED_DIFFERS),
        (_mutate_effective_k, "clustering", harness.UNEXPECTED_DIFFERS),
        (_mutate_statement_label, "statement_labels", harness.UNEXPECTED_DIFFERS),
        (_mutate_repermuted_cluster_labels, "clustering", harness.EQUAL),
    ],
)
def test_falsification_injections_report_expected_component(
    monkeypatch: pytest.MonkeyPatch,
    mutate: Callable[[harness.Bundle], None],
    component: str,
    expected: str,
) -> None:
    _case, bundle, _reset = _emit_model_free_bundle(monkeypatch)
    mutated = _mutated(bundle, mutate)

    assert _component_classification(bundle, mutated, component) == expected


def test_falsification_gate_declined_pair_is_not_evaluated_and_exits_nonzero(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    case, left, _reset = _emit_model_free_bundle(monkeypatch, overlap_fraction=0.0)
    right = harness.copy_bundle(left)
    report = harness.compare_bundles(left, right)

    assert harness._scalar(left, harness.EVIDENCE_SPEAKER) != "multi"
    assert report["classification"] == harness.NOT_EVALUATED

    left_path = tmp_path / "left.npz"
    right_path = tmp_path / "right.npz"
    report_path = tmp_path / "report.json"
    harness.write_bundle(left, left_path)
    harness.write_bundle(right, right_path)
    code = harness.main([str(left_path), str(right_path), "--report", str(report_path)])

    assert code == 1
    assert (
        json.loads(report_path.read_text())["classification"] == harness.NOT_EVALUATED
    )
    assert case.statements
    capsys.readouterr()


class _CountingSession:
    def __init__(self, wrapped: object):
        self._wrapped = wrapped
        self.run_count = 0

    def get_inputs(self) -> Any:
        return self._wrapped.get_inputs()

    def get_outputs(self) -> Any:
        return self._wrapped.get_outputs()

    def get_providers(self) -> Any:
        return self._wrapped.get_providers()

    def run(self, outputs: Any, inputs: Any) -> Any:
        self.run_count += 1
        return self._wrapped.run(outputs, inputs)


@pytest.mark.xdist_group("speaker_differential_real_onnx")
def test_real_model_emitter_wiring_records_provenance_and_reuses_pyannote(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    # Prep measured this path at about 0.1s warm and 0.25s cold on this host,
    # well under the 15s global pytest timeout; keep this unmarked by integration.
    monkeypatch.setattr(overlap, "_overlap_session", None)
    monkeypatch.setattr(harness.transcribe_main, "_embedder_session", None)
    monkeypatch.setattr(diarize, "_wespeaker_session", None)
    overlap_session = overlap._get_overlap_session()
    harness.transcribe_main._get_embedder_session()
    diarize._get_wespeaker_session()
    counting_session = _CountingSession(overlap_session)
    monkeypatch.setattr(overlap, "_overlap_session", counting_session)

    compute_call_count = 0
    original_compute = overlap.compute_overlap_and_logprobs

    def counted_compute(*args: Any, **kwargs: Any) -> Any:
        nonlocal compute_call_count
        compute_call_count += 1
        return original_compute(*args, **kwargs)

    run_pyannote_count = 0
    original_run_pyannote = diarize._run_pyannote

    def counted_run_pyannote(*args: Any, **kwargs: Any) -> Any:
        nonlocal run_pyannote_count
        run_pyannote_count += 1
        return original_run_pyannote(*args, **kwargs)

    monkeypatch.setattr(overlap, "compute_overlap_and_logprobs", counted_compute)
    monkeypatch.setattr(diarize, "_run_pyannote", counted_run_pyannote)
    audio = real_model_waveform(1.0)
    statements = [{"id": 1, "start": 0.0, "end": 1.0, "text": "redacted"}]

    start = time.perf_counter()
    bundle = harness.emit_speaker_bundle(audio_buffer=audio, statements=statements)
    wall_clock_s = time.perf_counter() - start

    assert wall_clock_s < 15.0
    assert compute_call_count == 1
    assert run_pyannote_count == 0
    counters = bundle["manifest"]["counters"]
    before = counters.get("overlap_session_run_count_before_diarize")
    after = counters.get("overlap_session_run_count_after_diarize")
    if before is not None and after is not None:
        assert after - before == 0
    providers = bundle["manifest"]["provenance"]["onnx_execution_providers"]
    assert providers["pyannote"] == ["CPUExecutionProvider"]
    assert providers["statement_encoder"] == ["CPUExecutionProvider"]
    assert providers["interval_encoder"] == ["CPUExecutionProvider"]
    assert bundle["manifest"]["provenance"]["versions"]["onnxruntime"]
    assert bundle["manifest"]["provenance"]["versions"]["kaldi_native_fbank"]


def test_cli_refuses_in_repo_report_destination(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    _case, bundle, _reset = _emit_model_free_bundle(monkeypatch)
    left = tmp_path / "left.npz"
    right = tmp_path / "right.npz"
    harness.write_bundle(bundle, left)
    harness.write_bundle(harness.copy_bundle(bundle), right)
    forbidden = harness.ROOT / "speaker-report.json"

    code = harness.main([str(left), str(right), "--report", str(forbidden)])

    output = json.loads(capsys.readouterr().out)
    assert code == 1
    assert output["failure"]["class"] == harness.HARNESS_ERROR
    assert "in-repo destination" in output["failure"]["message"]
    assert not forbidden.exists()


def test_harness_does_not_use_network_or_write_outside_output_dir(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    connect_calls: list[object] = []

    def fail_connect(self: socket.socket, address: object) -> None:
        connect_calls.append(address)
        raise AssertionError("network call attempted")

    monkeypatch.setattr(socket.socket, "connect", fail_connect)
    tmp_root = harness.ROOT / "tmp"
    created_tmp_root = not tmp_root.exists()
    run_root = (
        tmp_root / f"speaker-differential-cache-race-{os.getpid()}-{uuid.uuid4().hex}"
    )
    cache_dir = run_root / "__pycache__"
    cache_tag = sys.implementation.cache_tag or "cpython"
    pyc_path = cache_dir / f"l3rr7_{os.getpid()}_{uuid.uuid4().hex}.{cache_tag}.pyc"
    run_root.mkdir(parents=True)
    assert not cache_dir.exists()

    ctx = multiprocessing.get_context("spawn")
    barrier = ctx.Barrier(2)
    messages = ctx.Queue()
    process = ctx.Process(
        target=_cache_mutation_worker,
        args=(str(cache_dir), str(pyc_path), barrier, messages),
    )
    process_alive_after_join = False
    process_started = False
    cleanup_errors: list[str] = []
    try:
        process.start()
        process_started = True
        ready = _cache_mutation_message(messages, process, "ready")
        assert ready["pid"] != os.getpid(), ready
        before_inventory = repository_inventory(harness.ROOT)
        _release_cache_mutation(barrier)
        mutated = _cache_mutation_message(messages, process, "mutated")
        assert mutated["pid"] == ready["pid"], mutated
        assert mutated["pid"] != os.getpid(), mutated
        assert Path(str(mutated["path"])) == pyc_path
        assert pyc_path.exists()

        _case, bundle, _reset = _emit_model_free_bundle(monkeypatch)
        output_dir = tmp_path / "speaker-differential"
        left = output_dir / "left.npz"
        right = output_dir / "right.npz"
        report = output_dir / "report.json"

        harness.write_bundle(bundle, left)
        harness.write_bundle(harness.copy_bundle(bundle), right)
        code = harness.main([str(left), str(right), "--report", str(report)])

        after_inventory = repository_inventory(harness.ROOT)
    finally:
        if process_started:
            process.join(timeout=CACHE_MUTATION_JOIN_TIMEOUT_S)
            if process.is_alive():
                process_alive_after_join = True
                process.terminate()
                process.join(timeout=CACHE_MUTATION_JOIN_TIMEOUT_S)
        if run_root.exists():
            # Cleanup errors are captured per destructive call so they cannot
            # hide the primary no-write assertion. The repository traversal
            # itself remains tolerance-free.
            try:
                shutil.rmtree(run_root)
            except OSError as exc:
                cleanup_errors.append(f"failed to rmtree {run_root}: {exc}")
        if created_tmp_root and tmp_root.exists() and not any(tmp_root.iterdir()):
            try:
                tmp_root.rmdir()
            except OSError as exc:
                cleanup_errors.append(f"failed to rmdir {tmp_root}: {exc}")

    assert not process_alive_after_join
    assert process.exitcode == 0
    assert code == 0
    assert json.loads(report.read_text())["classification"] == harness.EQUAL
    assert connect_calls == []
    assert_inventory_unchanged(before_inventory, after_inventory)
    assert cleanup_errors == [], "\n".join(cleanup_errors)
    capsys.readouterr()
