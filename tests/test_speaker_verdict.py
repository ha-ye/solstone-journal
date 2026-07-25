# SPDX-License-Identifier: AGPL-3.0-only
# Copyright (c) 2026 sol pbc

"""Tests for the speaker verdict replay harness."""

from __future__ import annotations

import json
import socket
from pathlib import Path
from typing import Any

import numpy as np
import pytest

from solstone.apps.speakers import encoder_config
from solstone.observe.transcribe import diarize
from tests import verify_speaker_differential as harness
from tests import verify_speaker_verdict as verdict
from tests._repo_inventory import assert_inventory_unchanged, repository_inventory
from tests._speaker_differential_fixtures import EMBEDDING_MAX_ABS_TOLERANCE
from tests.test_speaker_differential import _emit_model_free_bundle


def _eps(*values: float) -> float:
    positives = [abs(float(value)) for value in values if float(value) > 0]
    return min(positives) / 10


def _unit_vector(components: list[float]) -> np.ndarray:
    remainder = 1.0 - sum(float(value) * float(value) for value in components)
    assert remainder > 0
    return np.array([*components, np.sqrt(remainder)], dtype=np.float32)


def _axis(index: int, width: int) -> np.ndarray:
    vector = np.zeros(width, dtype=np.float32)
    vector[index] = 1.0
    return vector


def _statement_bundle(
    rows: list[np.ndarray], ids: list[int] | None = None
) -> harness.Bundle:
    bundle = harness.new_bundle(producer="test")
    statement_ids = np.array(
        ids if ids is not None else list(range(1, len(rows) + 1)),
        dtype=np.int32,
    )
    harness._set_array(
        bundle,
        harness.STATEMENT_EMBEDDING_IDS,
        statement_ids,
        "statement_embeddings",
    )
    harness._set_array(
        bundle,
        harness.STATEMENT_EMBEDDINGS,
        np.asarray(rows, dtype=np.float32),
        "statement_embeddings",
    )
    return bundle


def _prediction_bundle(
    rows: list[tuple[int, float, float, int | np.integer]],
) -> harness.Bundle:
    bundle = harness.new_bundle(producer="test")
    harness._set_array(
        bundle,
        harness.INPUT_DIARIZATION_IDS,
        np.array([row[0] for row in rows], dtype=np.int32),
        "inputs.diarization",
    )
    harness._set_array(
        bundle,
        harness.INPUT_DIARIZATION_SPANS,
        np.array([[row[1], row[2]] for row in rows], dtype=np.float32),
        "inputs.diarization",
    )
    harness._set_array(
        bundle,
        harness.DIARIZATION_STATEMENT_LABELS,
        np.array([row[3] for row in rows], dtype=np.int32),
        "statement_labels",
    )
    return bundle


def _cluster_bundle(
    raw_embeddings: np.ndarray, silhouette_k: int, effective_k: int
) -> harness.Bundle:
    bundle = harness.new_bundle(producer="test")
    harness._set_array(
        bundle,
        harness.DIARIZATION_INTERVAL_EMBEDDINGS,
        raw_embeddings.astype(np.float32),
        "interval_embeddings",
    )
    harness._set_scalar(
        bundle,
        harness.DIARIZATION_SILHOUETTE_K,
        silhouette_k,
        "clustering",
    )
    harness._set_scalar(
        bundle,
        harness.DIARIZATION_EFFECTIVE_K,
        effective_k,
        "clustering",
    )
    return bundle


def _refs(
    *,
    width: int,
    owner_index: int,
    entity_indices: list[int] | None = None,
    owner_margin: float | None = encoder_config.OWNER_MARGIN_MIN,
) -> verdict.ReferenceCentroids:
    entity_indices = entity_indices or []
    return verdict.ReferenceCentroids(
        owner_centroid=_axis(owner_index, width),
        owner_threshold=encoder_config.OWNER_THRESHOLD,
        owner_threshold_source="constant_default",
        owner_margin=owner_margin,
        owner_margin_source="constant_default",
        entity_ids=tuple(f"entity_{index}" for index in entity_indices),
        entity_centroids=tuple(_axis(index, width) for index in entity_indices),
        entity_usable=tuple(True for _index in entity_indices),
    )


def _reference_turns(*turns: tuple[float, float, str]) -> verdict.ReferenceTurns:
    return verdict.ReferenceTurns(
        "present",
        tuple(
            verdict.ReferenceTurn(start, end, speaker) for start, end, speaker in turns
        ),
    )


def _write_centroids(
    path: Path,
    *,
    owner: np.ndarray | None,
    entities: list[tuple[str, np.ndarray, bool]],
    owner_meta: dict[str, Any] | None = None,
) -> None:
    manifest_owner = {
        "state": harness.PRESENT if owner is not None else harness.ABSENT_NONE
    }
    if owner is not None:
        manifest_owner["array"] = "owner.centroid"
    if owner_meta:
        manifest_owner.update(owner_meta)
    manifest = {
        "schema": verdict.REFERENCE_CENTROIDS_SCHEMA,
        "schema_version": verdict.SCHEMA_VERSION,
        "owner": manifest_owner,
        "entities": [
            {"id": entity_id, "usable": usable}
            for entity_id, _centroid, usable in entities
        ],
    }
    arrays: dict[str, np.ndarray] = {
        verdict.REFERENCE_CENTROIDS_MANIFEST_KEY: np.array(
            json.dumps(manifest, sort_keys=True)
        ),
    }
    if owner is not None:
        arrays["owner.centroid"] = np.asarray(owner, dtype=np.float32)
    if entities:
        arrays["entities.centroids"] = np.asarray(
            [centroid for _entity_id, centroid, _usable in entities],
            dtype=np.float32,
        )
    with path.open("wb") as fh:
        np.savez(fh, **arrays)


def test_family1_model_free_replay_matches_recorded_scalars(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _case, bundle, _reset = _emit_model_free_bundle(monkeypatch)

    side = verdict._replay_cluster_side(bundle)

    assert side["evaluated"] is True
    assert side["recorded_mismatches"] == []
    assert side["replayed_silhouette_k"] == harness._scalar(
        bundle, harness.DIARIZATION_SILHOUETTE_K
    )
    assert side["replayed_effective_k"] == harness._scalar(
        bundle, harness.DIARIZATION_EFFECTIVE_K
    )


def test_silhouette_improvement_flip_and_no_flip(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    eps = _eps(diarize.SILHOUETTE_IMPROVEMENT)

    def fake_ahc(embs_n: np.ndarray, k: int) -> np.ndarray:
        return np.arange(len(embs_n), dtype=np.int32) % int(k)

    def fake_silhouette(embs_n: np.ndarray, labels: np.ndarray) -> float:
        k = len(np.unique(labels))
        if k == 2:
            return 0.0
        if k == 3 and embs_n[0, 0] > embs_n[0, 1]:
            return diarize.SILHOUETTE_IMPROVEMENT + eps
        if k == 3:
            return diarize.SILHOUETTE_IMPROVEMENT - eps
        raise AssertionError(f"unexpected silhouette k {k}")

    monkeypatch.setattr(diarize, "_ahc", fake_ahc)
    monkeypatch.setattr(diarize, "_silhouette", fake_silhouette)
    left = _cluster_bundle(
        np.array([[0.0, 1.0], [0.0, 0.9], [0.1, 0.9], [0.2, 0.8]]),
        2,
        2,
    )
    right = _cluster_bundle(
        np.array([[1.0, 0.0], [0.9, 0.0], [0.9, 0.1], [0.8, 0.2]]),
        3,
        3,
    )

    flip_report = verdict._family1_report(left, right)
    no_flip_report = verdict._family1_report(left, harness.copy_bundle(left))

    assert flip_report["flip_count"] == 1
    assert flip_report["classification"] == harness.UNEXPECTED_DIFFERS
    assert no_flip_report["flip_count"] == 0
    assert no_flip_report["classification"] == harness.EQUAL


def test_max_k_cap_binds_and_does_not_bind(monkeypatch: pytest.MonkeyPatch) -> None:
    def fake_ahc(embs_n: np.ndarray, k: int) -> np.ndarray:
        return np.arange(len(embs_n), dtype=np.int32) % int(k)

    def fake_silhouette(_embs_n: np.ndarray, labels: np.ndarray) -> float:
        return float(len(np.unique(labels)))

    monkeypatch.setattr(diarize, "_ahc", fake_ahc)
    monkeypatch.setattr(diarize, "_silhouette", fake_silhouette)
    capped = _cluster_bundle(
        np.eye(diarize.MAX_K + 1, dtype=np.float32),
        diarize.MAX_K,
        diarize.MAX_K,
    )
    uncapped_rows = max(3, diarize.MAX_K - 1)
    if uncapped_rows - 1 >= diarize.MAX_K:
        pytest.skip("MAX_K leaves no uncapped multi-cluster case")
    uncapped = _cluster_bundle(
        np.eye(uncapped_rows, dtype=np.float32),
        1,
        1,
    )

    capped_side = verdict._replay_cluster_side(capped)
    uncapped_side = verdict._replay_cluster_side(uncapped)

    assert capped_side["replayed_effective_k"] == diarize.MAX_K
    assert uncapped_side["replayed_effective_k"] < diarize.MAX_K


def test_owner_threshold_flip_and_no_flip() -> None:
    eps = _eps(encoder_config.OWNER_THRESHOLD)
    refs = _refs(width=2, owner_index=0)
    left = _statement_bundle([_unit_vector([encoder_config.OWNER_THRESHOLD - eps])])
    right = _statement_bundle([_unit_vector([encoder_config.OWNER_THRESHOLD + eps])])
    no_flip_left = _statement_bundle(
        [_unit_vector([encoder_config.OWNER_THRESHOLD + eps])]
    )
    no_flip_right = _statement_bundle(
        [_unit_vector([encoder_config.OWNER_THRESHOLD + eps * 2])]
    )

    flip_report, *_ = verdict._family2_report(left, right, refs)
    no_flip_report, *_ = verdict._family2_report(no_flip_left, no_flip_right, refs)

    assert flip_report["flip_count"] == 1
    assert flip_report["classification"] == harness.UNEXPECTED_DIFFERS
    assert no_flip_report["flip_count"] == 0
    assert no_flip_report["classification"] == harness.EQUAL


def test_owner_margin_flip_and_no_flip() -> None:
    eps = _eps(encoder_config.OWNER_MARGIN_MIN)
    owner_score = encoder_config.OWNER_THRESHOLD + encoder_config.OWNER_MARGIN_MIN + eps
    entity_fail = owner_score - encoder_config.OWNER_MARGIN_MIN + eps
    entity_pass = owner_score - encoder_config.OWNER_MARGIN_MIN - eps
    refs = _refs(width=3, owner_index=0, entity_indices=[1])
    left = _statement_bundle([_unit_vector([owner_score, entity_fail])])
    right = _statement_bundle([_unit_vector([owner_score, entity_pass])])
    no_flip_right = _statement_bundle([_unit_vector([owner_score + eps, entity_pass])])

    flip_report, *_ = verdict._family2_report(left, right, refs)
    no_flip_report, *_ = verdict._family2_report(right, no_flip_right, refs)

    assert flip_report["flip_count"] == 1
    assert flip_report["flips"][0]["underlying_values"]["left_owner_margin_declined"]
    assert flip_report["classification"] == harness.UNEXPECTED_DIFFERS
    assert no_flip_report["flip_count"] == 0
    assert no_flip_report["classification"] == harness.EQUAL


def test_asymmetric_owner_evaluability_is_not_a_flip() -> None:
    refs = _refs(width=2, owner_index=0)
    left = _statement_bundle([np.zeros(2, dtype=np.float32)])
    right = _statement_bundle([_unit_vector([encoder_config.OWNER_THRESHOLD])])

    report, *_ = verdict._family2_report(left, right, refs)

    assert report["classification"] == harness.UNEXPECTED_DIFFERS
    assert report["reason"] == "asymmetric_evaluability"
    assert report["flip_count"] == 0
    assert report["asymmetric_evaluability"] == [1]


def test_functionally_equal_rollup_keeps_zero_flip_answer_visible(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _case, left, _reset = _emit_model_free_bundle(monkeypatch)
    right = harness.copy_bundle(left)
    embeddings = harness._array(right, harness.STATEMENT_EMBEDDINGS).copy()
    embeddings[0, 0] += EMBEDDING_MAX_ABS_TOLERANCE / 2
    harness.replace_array(right, harness.STATEMENT_EMBEDDINGS, embeddings)

    report = verdict.compare_verdicts(left, right)

    assert report["classification"] == harness.FUNCTIONALLY_EQUAL
    assert (
        report["components"]["bundle_comparator"]["components"]["statement_embeddings"][
            "classification"
        ]
        == harness.FUNCTIONALLY_EQUAL
    )
    assert report["components"]["decision_flips"]["flip_count"] == 0


def test_acoustic_medium_threshold_flip_and_no_flip() -> None:
    eps = _eps(encoder_config.ACOUSTIC_MEDIUM)
    refs = _refs(width=3, owner_index=1, entity_indices=[0])
    left = _statement_bundle(
        [_unit_vector([encoder_config.ACOUSTIC_MEDIUM - eps, 0.0])]
    )
    right = _statement_bundle(
        [_unit_vector([encoder_config.ACOUSTIC_MEDIUM + eps, 0.0])]
    )
    no_flip_right = _statement_bundle(
        [_unit_vector([encoder_config.ACOUSTIC_MEDIUM + eps * 2, 0.0])]
    )

    left_owner = verdict._family2_report(left, right, refs)
    flip_report = verdict._family3_report(
        left, right, refs, left_owner[0], *left_owner[1:]
    )
    right_owner = verdict._family2_report(right, no_flip_right, refs)
    no_flip_report = verdict._family3_report(
        right, no_flip_right, refs, right_owner[0], *right_owner[1:]
    )

    assert flip_report["flip_count"] == 1
    assert flip_report["classification"] == harness.UNEXPECTED_DIFFERS
    assert no_flip_report["flip_count"] == 0
    assert no_flip_report["classification"] == harness.EQUAL


def test_acoustic_high_threshold_flip_and_no_flip() -> None:
    eps = _eps(encoder_config.ACOUSTIC_HIGH)
    refs = _refs(width=3, owner_index=1, entity_indices=[0])
    left = _statement_bundle([_unit_vector([encoder_config.ACOUSTIC_HIGH - eps, 0.0])])
    right = _statement_bundle([_unit_vector([encoder_config.ACOUSTIC_HIGH + eps, 0.0])])
    no_flip_right = _statement_bundle(
        [_unit_vector([encoder_config.ACOUSTIC_HIGH + eps * 2, 0.0])]
    )

    left_owner = verdict._family2_report(left, right, refs)
    flip_report = verdict._family3_report(
        left, right, refs, left_owner[0], *left_owner[1:]
    )
    right_owner = verdict._family2_report(right, no_flip_right, refs)
    no_flip_report = verdict._family3_report(
        right, no_flip_right, refs, right_owner[0], *right_owner[1:]
    )

    assert flip_report["flip_count"] == 1
    assert flip_report["flips"][0]["left_outcome"]["tier"] == "medium"
    assert flip_report["flips"][0]["right_outcome"]["tier"] == "high"
    assert no_flip_report["flip_count"] == 0
    assert no_flip_report["classification"] == harness.EQUAL


def test_acoustic_margin_flip_and_no_flip() -> None:
    eps = _eps(encoder_config.ACOUSTIC_MARGIN_MIN)
    best = encoder_config.ACOUSTIC_HIGH + eps
    runner_fail = best - encoder_config.ACOUSTIC_MARGIN_MIN + eps
    runner_pass = best - encoder_config.ACOUSTIC_MARGIN_MIN - eps
    refs = _refs(width=4, owner_index=2, entity_indices=[0, 1])
    left = _statement_bundle([_unit_vector([best, runner_fail, 0.0])])
    right = _statement_bundle([_unit_vector([best, runner_pass, 0.0])])
    no_flip_right = _statement_bundle([_unit_vector([best + eps, runner_pass, 0.0])])

    left_owner = verdict._family2_report(left, right, refs)
    flip_report = verdict._family3_report(
        left, right, refs, left_owner[0], *left_owner[1:]
    )
    right_owner = verdict._family2_report(right, no_flip_right, refs)
    no_flip_report = verdict._family3_report(
        right, no_flip_right, refs, right_owner[0], *right_owner[1:]
    )

    assert flip_report["flip_count"] == 1
    assert flip_report["flips"][0]["left_outcome"]["demotion_causes"] == [
        "acoustic_margin_declined"
    ]
    assert no_flip_report["flip_count"] == 0
    assert no_flip_report["classification"] == harness.EQUAL


def test_owner_margin_decline_demotes_acoustic_high() -> None:
    eps = _eps(encoder_config.OWNER_THRESHOLD, encoder_config.OWNER_MARGIN_MIN)
    owner_declined_score = encoder_config.OWNER_THRESHOLD + eps
    best = max(
        encoder_config.ACOUSTIC_HIGH + eps,
        owner_declined_score - encoder_config.OWNER_MARGIN_MIN + eps,
    )
    owner_clear_score = encoder_config.OWNER_THRESHOLD - eps
    refs = _refs(width=3, owner_index=1, entity_indices=[0])
    left = _statement_bundle([_unit_vector([best, owner_declined_score])])
    right = _statement_bundle([_unit_vector([best, owner_clear_score])])

    owner_report = verdict._family2_report(left, right, refs)
    acoustic_report = verdict._family3_report(
        left, right, refs, owner_report[0], *owner_report[1:]
    )

    assert owner_report[0]["left_margin_declined_sids"] == [1]
    assert acoustic_report["flip_count"] == 1
    assert acoustic_report["flips"][0]["left_outcome"]["demotion_causes"] == [
        "owner_margin_declined"
    ]
    assert acoustic_report["flips"][0]["right_outcome"]["tier"] == "high"


@pytest.mark.parametrize(
    ("bundle", "reference_turns", "expected_der", "expected_breakdown"),
    [
        (
            _prediction_bundle([(2, 5.0, 10.0, 2), (1, 0.0, 5.0, 1)]),
            _reference_turns((0.0, 5.0, "a"), (5.0, 10.0, "b")),
            0.0,
            {"missed": 0.0, "false_alarm": 0.0, "confusion": 0.0, "denominator": 10.0},
        ),
        (
            _prediction_bundle([(1, 10.0, 20.0, 1)]),
            _reference_turns((0.0, 10.0, "a")),
            2.0,
            {
                "missed": 10.0,
                "false_alarm": 10.0,
                "confusion": 0.0,
                "denominator": 10.0,
            },
        ),
        (
            _prediction_bundle([(1, 0.0, 4.0, 1)]),
            _reference_turns((0.0, 10.0, "a")),
            0.6,
            {"missed": 6.0, "false_alarm": 0.0, "confusion": 0.0, "denominator": 10.0},
        ),
        (
            _prediction_bundle([(1, 4.0, 10.0, 1), (2, 0.0, 4.0, 2)]),
            _reference_turns((0.0, 4.0, "a"), (4.0, 10.0, "b")),
            0.0,
            {"missed": 0.0, "false_alarm": 0.0, "confusion": 0.0, "denominator": 10.0},
        ),
        (
            _prediction_bundle([(1, 0.0, 15.0, 1)]),
            _reference_turns((0.0, 10.0, "a"), (5.0, 15.0, "b")),
            0.5,
            {"missed": 5.0, "false_alarm": 0.0, "confusion": 5.0, "denominator": 20.0},
        ),
    ],
)
def test_der_hand_computed_cases(
    bundle: harness.Bundle,
    reference_turns: verdict.ReferenceTurns,
    expected_der: float,
    expected_breakdown: dict[str, float],
) -> None:
    score = verdict.score_der(bundle, reference_turns)

    assert score["status"] == harness.PRESENT
    assert score["der"] == pytest.approx(expected_der)
    for key, value in expected_breakdown.items():
        assert score["breakdown"][key] == pytest.approx(value)


def test_der_ignores_nan_and_null_predicted_spans() -> None:
    bundle = _prediction_bundle(
        [
            (1, 0.0, 10.0, harness.LABEL_NULL_SENTINEL),
            (2, float("nan"), float("nan"), 2),
        ]
    )

    score = verdict.score_der(bundle, _reference_turns((0.0, 10.0, "a")))

    assert score["der"] == pytest.approx(1.0)
    assert score["breakdown"]["missed"] == pytest.approx(10.0)


def test_der_counts_distinct_speakers_not_overlapping_turns() -> None:
    bundle = _prediction_bundle([(1, 0.0, 15.0, 1)])

    score = verdict.score_der(
        bundle,
        _reference_turns((0.0, 10.0, "a"), (5.0, 15.0, "a")),
    )

    assert score["der"] == pytest.approx(0.0)
    assert score["breakdown"]["denominator"] == pytest.approx(15.0)


def test_der_reference_absent_empty_and_zero_denominator(tmp_path: Path) -> None:
    bundle = _prediction_bundle([(1, 0.0, 1.0, 1)])
    empty_path = tmp_path / "empty-reference-turns.json"
    empty_path.write_text("[]", encoding="utf-8")

    absent = verdict._der_report(bundle, bundle, verdict.load_reference_turns(None))
    empty = verdict._der_report(
        bundle, bundle, verdict.load_reference_turns(empty_path)
    )
    zero = verdict.score_der(bundle, verdict.ReferenceTurns("present", ()))

    assert absent["reason"] == "reference_turns_absent"
    assert empty["reason"] == "reference_turns_empty"
    assert zero["reason"] == "zero_reference_speech"


def test_der_predicted_labels_field_state_gates_component() -> None:
    bundle = harness.new_bundle(producer="test")
    harness._set_array(
        bundle,
        harness.INPUT_DIARIZATION_IDS,
        np.array([1], dtype=np.int32),
        "inputs.diarization",
    )
    harness._set_array(
        bundle,
        harness.INPUT_DIARIZATION_SPANS,
        np.array([[0.0, 1.0]], dtype=np.float32),
        "inputs.diarization",
    )
    harness._set_state(
        bundle,
        harness.DIARIZATION_STATEMENT_LABELS,
        harness.NOT_EVALUATED,
        "statement_labels",
    )

    score = verdict.score_der(bundle, _reference_turns((0.0, 1.0, "a")))

    assert score["status"] == harness.NOT_EVALUATED
    assert score["reason"] == "predicted_labels_not_present"


def test_reference_turn_reader_rejects_unknown_text_key(tmp_path: Path) -> None:
    path = tmp_path / "reference-turns.json"
    path.write_text(
        json.dumps(
            [{"start_s": 0.0, "end_s": 1.0, "speaker": "a", "text": "redacted"}]
        ),
        encoding="utf-8",
    )

    with pytest.raises(harness.HarnessError, match="text"):
        verdict.load_reference_turns(path)


@pytest.mark.parametrize(
    "payload",
    [
        {"start_s": 0.0, "end_s": 1.0, "speaker": "a"},
        [{"start_s": -1.0, "end_s": 1.0, "speaker": "a"}],
        [{"start_s": 1.0, "end_s": 1.0, "speaker": "a"}],
        [{"start_s": "0", "end_s": 1.0, "speaker": "a"}],
        [{"start_s": 0.0, "end_s": 1.0, "speaker": ""}],
        [
            {"start_s": 2.0, "end_s": 3.0, "speaker": "a"},
            {"start_s": 0.0, "end_s": 1.0, "speaker": "b"},
        ],
    ],
)
def test_reference_turn_reader_rejects_invalid_shapes(
    tmp_path: Path,
    payload: object,
) -> None:
    path = tmp_path / "reference-turns.json"
    path.write_text(json.dumps(payload), encoding="utf-8")

    with pytest.raises(harness.HarnessError):
        verdict.load_reference_turns(path)


def test_reference_centroid_file_manifest_is_single_source_of_truth(
    tmp_path: Path,
) -> None:
    eps = _eps(encoder_config.OWNER_THRESHOLD)
    path = tmp_path / "centroids.npz"
    _write_centroids(
        path,
        owner=_axis(0, 2),
        entities=[("entity_1", _axis(1, 2), True)],
        owner_meta={"threshold": encoder_config.OWNER_THRESHOLD + eps, "margin": None},
    )

    refs = verdict.load_reference_centroids(path)
    with np.load(path, allow_pickle=False) as payload:
        assert set(payload.files) == {
            verdict.REFERENCE_CENTROIDS_MANIFEST_KEY,
            "owner.centroid",
            "entities.centroids",
        }
    threshold_block = verdict._owner_thresholds(refs)

    assert refs is not None
    assert refs.owner_threshold_source == "supplied"
    assert refs.owner_margin is None
    assert refs.owner_margin_source == "supplied"
    assert threshold_block["owner_margin"]["effective_value"] is None


def test_comparator_failure_short_circuits_verdict() -> None:
    left = harness.new_bundle(producer="left")
    right = harness.new_bundle(producer="right")

    report = verdict.compare_verdicts(left, right)

    assert report["classification"] == harness.NOT_EVALUATED
    assert report["failure"] == report["components"]["bundle_comparator"]["failure"]
    assert set(report["components"]) == {"bundle_comparator"}


def test_cli_does_not_use_network_or_write_inside_repository(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    connect_calls: list[object] = []

    def fail_connect(_self: socket.socket, address: object) -> None:
        connect_calls.append(address)
        raise AssertionError("network call attempted")

    monkeypatch.setattr(socket.socket, "connect", fail_connect)
    _case, bundle, _reset = _emit_model_free_bundle(monkeypatch)
    output_dir = tmp_path / "speaker-verdict"
    output_dir.mkdir()
    left_path = output_dir / "left.npz"
    right_path = output_dir / "right.npz"
    centroids_path = output_dir / "centroids.npz"
    reference_path = output_dir / "reference-turns.json"
    report_path = output_dir / "report.json"
    harness.write_bundle(bundle, left_path)
    harness.write_bundle(harness.copy_bundle(bundle), right_path)
    owner = np.zeros(harness._array(bundle, harness.STATEMENT_EMBEDDINGS).shape[1])
    owner[0] = 1.0
    _write_centroids(centroids_path, owner=owner, entities=[])
    spans = harness._array(bundle, harness.INPUT_DIARIZATION_SPANS)
    labels = harness._array(bundle, harness.DIARIZATION_STATEMENT_LABELS)
    reference_turns = [
        {"start_s": float(start), "end_s": float(end), "speaker": str(int(label))}
        for (start, end), label in zip(spans, labels)
        if int(label) != int(harness.LABEL_NULL_SENTINEL)
        and np.isfinite(start)
        and np.isfinite(end)
        and end > start
    ]
    reference_path.write_text(json.dumps(reference_turns), encoding="utf-8")

    before_inventory = repository_inventory(harness.ROOT)
    code = verdict.main(
        [
            str(left_path),
            str(right_path),
            "--reference-centroids",
            str(centroids_path),
            "--reference-turns",
            str(reference_path),
            "--report",
            str(report_path),
        ]
    )
    after_inventory = repository_inventory(harness.ROOT)

    captured = capsys.readouterr()
    assert code == 0
    assert json.loads(report_path.read_text(encoding="utf-8"))["classification"] in {
        harness.EQUAL,
        harness.FUNCTIONALLY_EQUAL,
    }
    assert json.loads(captured.out)["schema"] == verdict.REPORT_SCHEMA
    assert connect_calls == []
    assert_inventory_unchanged(before_inventory, after_inventory)
