# SPDX-License-Identifier: AGPL-3.0-only
# Copyright (c) 2026 sol pbc

"""Verdict layers for recorded speaker differential bundles.

This test-only instrument adds two pure layers above
``tests.verify_speaker_differential`` bundles: decision-flip replay and DER
measurement. It performs no model inference, journal access, network access, or
audio reads.

Normalizer fidelity is per family. The clustering replay calls production
``diarize._normalize_rows`` because ``diarize._cluster_intervals`` uses that
matrix normalizer internally; it leaves zero rows as zero and guards tiny norms
with its own row-wise floor. Owner and acoustic replay use
``voiceprints.normalize_embedding`` because speaker attribution uses that helper;
it casts a single vector to float32, returns ``None`` for zero vectors, and
normalizes any positive norm.

DER uses the standard no-collar, no-forgiveness formulation: elementary
intervals come from the union of reference and predicted boundaries; each
interval contributes missed speech, false alarm, confusion, and denominator by
the distinct active reference and system speaker labels after a Hungarian
one-to-one mapping maximizes correct speaker-time.

The acoustic cluster path is intentionally not replayed. The bundle records the
diarization plane, but not production's source JSONL integer-speaker file or the
post-owner/post-structural unresolved population. The per-statement acoustic
replay therefore reports a ``structural_layer_not_replayed`` caveat: its
population is a superset of production's Layer 3 population.
"""

from __future__ import annotations

import argparse
import json
import logging
import platform
import sys
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import numpy as np
from scipy.optimize import linear_sum_assignment

from solstone.apps.speakers import encoder_config
from solstone.apps.speakers.attribution import _passes_acoustic_margin
from solstone.observe.transcribe import diarize
from solstone.think.entities.voiceprints import normalize_embedding
from solstone.think.utils import get_rev
from tests import verify_speaker_differential as differential

logger = logging.getLogger(__name__)

REPORT_SCHEMA = "solstone-speaker-verdict-report"
REFERENCE_CENTROIDS_SCHEMA = "solstone-speaker-verdict-centroids"
SCHEMA_VERSION = 1
REFERENCE_CENTROIDS_MANIFEST_KEY = "__speaker_verdict_centroids_manifest_json__"

DIARIZE_MODULE = "solstone.observe.transcribe.diarize"
ENCODER_MODULE = "solstone.apps.speakers.encoder_config"
_MISSING = object()


@dataclass(frozen=True)
class ReferenceCentroids:
    owner_centroid: np.ndarray | None
    owner_threshold: float
    owner_threshold_source: str
    owner_margin: float | None
    owner_margin_source: str
    entity_ids: tuple[str, ...]
    entity_centroids: tuple[np.ndarray, ...]
    entity_usable: tuple[bool, ...]


@dataclass(frozen=True)
class ReferenceTurn:
    start_s: float
    end_s: float
    speaker: str


@dataclass(frozen=True)
class ReferenceTurns:
    status: str
    turns: tuple[ReferenceTurn, ...]
    reason: str | None = None


@dataclass(frozen=True)
class OwnerDecision:
    statement_id: int
    outcome: str
    owner_score: float
    best_non_owner_score: float
    owner_margin_delta: float
    owner_margin_declined: bool


@dataclass(frozen=True)
class AcousticDecision:
    statement_id: int
    outcome: str
    entity_id: str | None
    tier: str
    best_score: float
    runner_up_score: float
    margin_delta: float
    demotion_causes: tuple[str, ...]


@dataclass(frozen=True)
class DerBreakdown:
    missed: float
    false_alarm: float
    confusion: float
    denominator: float

    @property
    def total_error(self) -> float:
        return self.missed + self.false_alarm + self.confusion

    @property
    def der(self) -> float | None:
        if self.denominator == 0:
            return None
        return self.total_error / self.denominator


def _base_report(
    left: differential.Bundle | None = None,
    right: differential.Bundle | None = None,
) -> dict[str, Any]:
    return {
        "schema": REPORT_SCHEMA,
        "schema_version": SCHEMA_VERSION,
        "classification": differential.NOT_EVALUATED,
        "failure": None,
        "provenance": {
            "generated_at": datetime.now(UTC).isoformat().replace("+00:00", "Z"),
            "harness": {
                "name": "tests.verify_speaker_verdict",
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


def _threshold_record(
    *,
    constant: str,
    module: str,
    constant_value: Any,
    effective_value: Any = _MISSING,
    effective_source: str = "constant_default",
) -> dict[str, Any]:
    value = constant_value if effective_value is _MISSING else effective_value
    return {
        "constant": constant,
        "module": module,
        "constant_value": constant_value,
        "effective_value": value,
        "effective_source": effective_source,
    }


def _not_replayed_threshold(
    *,
    constant: str,
    module: str,
    value: Any,
    reason: str,
    detail: str,
) -> dict[str, Any]:
    return {
        "constant": constant,
        "module": module,
        "constant_value": value,
        "effective_value": value,
        "effective_source": "constant_default",
        "replayed": False,
        "reason": reason,
        "detail": detail,
    }


def _family1_thresholds() -> dict[str, Any]:
    return {
        "max_k": _threshold_record(
            constant="MAX_K",
            module=DIARIZE_MODULE,
            constant_value=diarize.MAX_K,
        ),
        "silhouette_improvement": _threshold_record(
            constant="SILHOUETTE_IMPROVEMENT",
            module=DIARIZE_MODULE,
            constant_value=diarize.SILHOUETTE_IMPROVEMENT,
        ),
        "ahc_linkage": _threshold_record(
            constant="AHC_LINKAGE",
            module=DIARIZE_MODULE,
            constant_value=diarize.AHC_LINKAGE,
        ),
        "ahc_metric": _threshold_record(
            constant="AHC_METRIC",
            module=DIARIZE_MODULE,
            constant_value=diarize.AHC_METRIC,
        ),
    }


def _upstream_thresholds_not_replayed() -> dict[str, Any]:
    detail = "Recorded interval embeddings are downstream of interval selection."
    return {
        "min_interval_s": _not_replayed_threshold(
            constant="MIN_INTERVAL_S",
            module=DIARIZE_MODULE,
            value=diarize.MIN_INTERVAL_S,
            reason="recorded_plane_downstream",
            detail=detail,
        ),
        "min_frame_confidence": _not_replayed_threshold(
            constant="MIN_FRAME_CONFIDENCE",
            module=DIARIZE_MODULE,
            value=diarize.MIN_FRAME_CONFIDENCE,
            reason="recorded_plane_downstream",
            detail=detail,
        ),
    }


def _family3_not_replayed_thresholds() -> dict[str, Any]:
    detail = "The acoustic cluster path needs source JSONL and structural state."
    return {
        "cc_coverage_gate": _not_replayed_threshold(
            constant="CC_COVERAGE_GATE",
            module=ENCODER_MODULE,
            value=encoder_config.CC_COVERAGE_GATE,
            reason="acoustic_cluster_not_replayed",
            detail=detail,
        ),
        "cc_confidence_gate": _not_replayed_threshold(
            constant="CC_CONFIDENCE_GATE",
            module=ENCODER_MODULE,
            value=encoder_config.CC_CONFIDENCE_GATE,
            reason="acoustic_cluster_not_replayed",
            detail=detail,
        ),
    }


def _owner_thresholds(refs: ReferenceCentroids) -> dict[str, Any]:
    return {
        "owner_threshold": _threshold_record(
            constant="OWNER_THRESHOLD",
            module=ENCODER_MODULE,
            constant_value=encoder_config.OWNER_THRESHOLD,
            effective_value=refs.owner_threshold,
            effective_source=refs.owner_threshold_source,
        ),
        "owner_margin": _threshold_record(
            constant="OWNER_MARGIN_MIN",
            module=ENCODER_MODULE,
            constant_value=encoder_config.OWNER_MARGIN_MIN,
            effective_value=refs.owner_margin,
            effective_source=refs.owner_margin_source,
        ),
    }


def _acoustic_thresholds() -> dict[str, Any]:
    return {
        "acoustic_high": _threshold_record(
            constant="ACOUSTIC_HIGH",
            module=ENCODER_MODULE,
            constant_value=encoder_config.ACOUSTIC_HIGH,
        ),
        "acoustic_medium": _threshold_record(
            constant="ACOUSTIC_MEDIUM",
            module=ENCODER_MODULE,
            constant_value=encoder_config.ACOUSTIC_MEDIUM,
        ),
        "acoustic_margin": _threshold_record(
            constant="ACOUSTIC_MARGIN_MIN",
            module=ENCODER_MODULE,
            constant_value=encoder_config.ACOUSTIC_MARGIN_MIN,
        ),
    }


def _field_unavailable(
    bundle: differential.Bundle, keys: tuple[str, ...]
) -> str | None:
    for key in keys:
        try:
            if differential._field_state(bundle, key) != differential.PRESENT:
                return key
        except differential.HarnessError:
            return key
    return None


def _unique_ints(values: np.ndarray, field: str) -> list[int]:
    result = [int(value) for value in np.asarray(values).tolist()]
    if len(result) != len(set(result)):
        raise differential.HarnessError(f"duplicate statement ids in {field}: {result}")
    return result


def _statement_embedding_map(
    bundle: differential.Bundle,
) -> tuple[dict[int, np.ndarray], set[int], str | None]:
    missing = _field_unavailable(
        bundle,
        (
            differential.STATEMENT_EMBEDDING_IDS,
            differential.STATEMENT_EMBEDDINGS,
        ),
    )
    if missing is not None:
        return {}, set(), missing
    ids = _unique_ints(
        differential._array(bundle, differential.STATEMENT_EMBEDDING_IDS),
        differential.STATEMENT_EMBEDDING_IDS,
    )
    embeddings = differential._array(bundle, differential.STATEMENT_EMBEDDINGS)
    if len(ids) != len(embeddings):
        raise differential.HarnessError("statement embedding length mismatch")
    normalized: dict[int, np.ndarray] = {}
    unusable: set[int] = set()
    for statement_id, embedding in zip(ids, embeddings):
        row = normalize_embedding(np.asarray(embedding))
        if row is None:
            unusable.add(statement_id)
        else:
            normalized[statement_id] = row
    return normalized, unusable, None


def _entity_centroids(refs: ReferenceCentroids) -> dict[str, np.ndarray]:
    return {
        entity_id: centroid
        for entity_id, centroid, usable in zip(
            refs.entity_ids, refs.entity_centroids, refs.entity_usable
        )
        if usable
    }


def _replay_cluster_side(bundle: differential.Bundle) -> dict[str, Any]:
    missing = _field_unavailable(
        bundle,
        (
            differential.DIARIZATION_INTERVAL_EMBEDDINGS,
            differential.DIARIZATION_SILHOUETTE_K,
            differential.DIARIZATION_EFFECTIVE_K,
        ),
    )
    if missing is not None:
        return {
            "evaluated": False,
            "reason": "clustering_fields_not_present",
            "field": missing,
        }
    raw = differential._array(bundle, differential.DIARIZATION_INTERVAL_EMBEDDINGS)
    if len(raw) == 0:
        return {"evaluated": False, "reason": "interval_embeddings_empty"}
    normalized = diarize._normalize_rows(raw)
    silhouette_k = int(diarize._pick_k_silhouette(normalized, diarize.MAX_K))
    labels = diarize._cluster_intervals(raw, None)
    effective_k = int(len(np.unique(labels))) if len(labels) else None
    recorded_silhouette_k = differential._scalar(
        bundle, differential.DIARIZATION_SILHOUETTE_K
    )
    recorded_effective_k = differential._scalar(
        bundle, differential.DIARIZATION_EFFECTIVE_K
    )
    mismatches = []
    if recorded_silhouette_k != silhouette_k:
        mismatches.append(
            {
                "field": differential.DIARIZATION_SILHOUETTE_K,
                "recorded": recorded_silhouette_k,
                "replayed": silhouette_k,
            }
        )
    if recorded_effective_k != effective_k:
        mismatches.append(
            {
                "field": differential.DIARIZATION_EFFECTIVE_K,
                "recorded": recorded_effective_k,
                "replayed": effective_k,
            }
        )
    return {
        "evaluated": True,
        "recorded_silhouette_k": recorded_silhouette_k,
        "recorded_effective_k": recorded_effective_k,
        "replayed_silhouette_k": silhouette_k,
        "replayed_effective_k": effective_k,
        "replayed_cluster_labels": labels.astype(np.int32).tolist(),
        "recorded_mismatches": mismatches,
    }


def _family1_report(
    left: differential.Bundle,
    right: differential.Bundle,
) -> dict[str, Any]:
    left_side = _replay_cluster_side(left)
    right_side = _replay_cluster_side(right)
    report: dict[str, Any] = {
        "classification": differential.NOT_EVALUATED,
        "thresholds": _family1_thresholds(),
        "upstream_thresholds_not_replayed": _upstream_thresholds_not_replayed(),
        "population_evaluated": 0,
        "flip_count": 0,
        "flip_rate": None,
        "flips": [],
        "left": left_side,
        "right": right_side,
    }
    if not left_side.get("evaluated") or not right_side.get("evaluated"):
        report["reason"] = "clustering_not_evaluated"
        return report
    report["population_evaluated"] = 1
    report["flip_rate"] = 0.0
    mismatches = left_side["recorded_mismatches"] + right_side["recorded_mismatches"]
    if left_side["replayed_effective_k"] != right_side["replayed_effective_k"]:
        report["flip_count"] = 1
        report["flip_rate"] = 1.0
        report["flips"] = [
            {
                "family": "cluster_count",
                "decision_id": "selected_cluster_count",
                "left_outcome": left_side["replayed_effective_k"],
                "right_outcome": right_side["replayed_effective_k"],
                "thresholds": report["thresholds"],
                "underlying_values": {
                    "left_silhouette_k": left_side["replayed_silhouette_k"],
                    "right_silhouette_k": right_side["replayed_silhouette_k"],
                    "left_effective_k": left_side["replayed_effective_k"],
                    "right_effective_k": right_side["replayed_effective_k"],
                },
            }
        ]
    if report["flip_count"] or mismatches:
        report["classification"] = differential.UNEXPECTED_DIFFERS
        if mismatches:
            report["recorded_replay_mismatches"] = mismatches
    else:
        report["classification"] = differential.EQUAL
    return report


def load_reference_centroids(path: Path | None) -> ReferenceCentroids | None:
    if path is None:
        return None
    try:
        with np.load(path, allow_pickle=False) as payload:
            if REFERENCE_CENTROIDS_MANIFEST_KEY not in payload.files:
                raise differential.HarnessError(
                    f"reference centroids {path} missing "
                    f"{REFERENCE_CENTROIDS_MANIFEST_KEY}"
                )
            manifest_array = payload[REFERENCE_CENTROIDS_MANIFEST_KEY]
            if manifest_array.shape != ():
                raise differential.HarnessError(
                    f"reference centroids {path} manifest is not a 0-d array"
                )
            manifest = json.loads(str(manifest_array.item()))
            return _parse_reference_centroids_manifest(path, payload, manifest)
    except differential.HarnessError:
        raise
    except Exception as exc:
        raise differential.HarnessError(
            f"failed to load reference centroids {path}: {exc}"
        ) from exc


def _parse_reference_centroids_manifest(
    path: Path,
    payload: Any,
    manifest: dict[str, Any],
) -> ReferenceCentroids:
    if manifest.get("schema") != REFERENCE_CENTROIDS_SCHEMA:
        raise differential.HarnessError(
            f"reference centroids {path} has unsupported schema "
            f"{manifest.get('schema')!r}"
        )
    if manifest.get("schema_version") != SCHEMA_VERSION:
        raise differential.HarnessError(
            f"reference centroids {path} has unsupported schema_version "
            f"{manifest.get('schema_version')!r}"
        )
    owner_meta = manifest.get("owner", {})
    owner_centroid = None
    if owner_meta.get("state") == differential.PRESENT:
        owner_key = str(owner_meta.get("array", "owner.centroid"))
        if owner_key not in payload.files:
            raise differential.HarnessError("reference owner centroid array missing")
        owner_centroid = normalize_embedding(np.asarray(payload[owner_key]).reshape(-1))
    threshold, threshold_source = _optional_threshold(
        owner_meta,
        "threshold",
        encoder_config.OWNER_THRESHOLD,
    )
    margin, margin_source = _optional_threshold(
        owner_meta,
        "margin",
        encoder_config.OWNER_MARGIN_MIN,
        allow_none=True,
    )

    entity_meta = manifest.get("entities", [])
    if not isinstance(entity_meta, list):
        raise differential.HarnessError("reference entities must be a list")
    entity_array = (
        np.asarray(payload["entities.centroids"])
        if "entities.centroids" in payload.files
        else np.zeros((0, 0), dtype=np.float32)
    )
    if len(entity_meta) != len(entity_array):
        raise differential.HarnessError("reference entity centroid count mismatch")
    entity_ids: list[str] = []
    entity_centroids: list[np.ndarray] = []
    entity_usable: list[bool] = []
    for index, row in enumerate(entity_meta):
        if not isinstance(row, dict):
            raise differential.HarnessError("reference entity metadata must be objects")
        entity_id = row.get("id")
        if not isinstance(entity_id, str) or not entity_id:
            raise differential.HarnessError(
                "reference entity id must be non-empty string"
            )
        if entity_id in entity_ids:
            raise differential.HarnessError(
                f"duplicate reference entity id {entity_id}"
            )
        normalized = normalize_embedding(np.asarray(entity_array[index]).reshape(-1))
        usable = bool(row.get("usable", False)) and normalized is not None
        entity_ids.append(entity_id)
        entity_centroids.append(
            np.zeros_like(np.asarray(entity_array[index]), dtype=np.float32)
            if normalized is None
            else normalized
        )
        entity_usable.append(usable)
    return ReferenceCentroids(
        owner_centroid=owner_centroid,
        owner_threshold=float(threshold),
        owner_threshold_source=threshold_source,
        owner_margin=None if margin is None else float(margin),
        owner_margin_source=margin_source,
        entity_ids=tuple(entity_ids),
        entity_centroids=tuple(entity_centroids),
        entity_usable=tuple(entity_usable),
    )


def _optional_threshold(
    mapping: dict[str, Any],
    key: str,
    default: float,
    *,
    allow_none: bool = False,
) -> tuple[float | None, str]:
    if key not in mapping:
        return float(default), "constant_default"
    value = mapping[key]
    if value is None and allow_none:
        return None, "supplied"
    if isinstance(value, bool) or not isinstance(value, int | float):
        raise differential.HarnessError(f"reference owner {key} must be numeric")
    return float(value), "supplied"


def _family2_not_evaluated(
    reason: str, refs: ReferenceCentroids | None
) -> dict[str, Any]:
    thresholds = (
        _owner_thresholds(refs)
        if refs is not None
        else {
            "owner_threshold": _threshold_record(
                constant="OWNER_THRESHOLD",
                module=ENCODER_MODULE,
                constant_value=encoder_config.OWNER_THRESHOLD,
            ),
            "owner_margin": _threshold_record(
                constant="OWNER_MARGIN_MIN",
                module=ENCODER_MODULE,
                constant_value=encoder_config.OWNER_MARGIN_MIN,
            ),
        }
    )
    return {
        "classification": differential.NOT_EVALUATED,
        "reason": reason,
        "thresholds": thresholds,
        "population_evaluated": 0,
        "flip_count": 0,
        "flip_rate": None,
        "flips": [],
        "asymmetric_evaluability": [],
        "left_margin_declined_sids": [],
        "right_margin_declined_sids": [],
    }


def _owner_decisions(
    bundle: differential.Bundle,
    refs: ReferenceCentroids,
) -> tuple[dict[int, OwnerDecision], set[int], set[int], str | None]:
    embeddings, unusable, missing = _statement_embedding_map(bundle)
    if missing is not None:
        return {}, unusable, set(), missing
    if refs.owner_centroid is None:
        return {}, unusable, set(), "owner_centroid_absent"
    usable_entities = _entity_centroids(refs)
    decisions: dict[int, OwnerDecision] = {}
    margin_declined: set[int] = set()
    for statement_id, embedding in embeddings.items():
        owner_score = float(np.dot(embedding, refs.owner_centroid))
        owner_claimed = owner_score >= refs.owner_threshold
        best_non_owner = float("-inf")
        if owner_claimed and refs.owner_margin is not None:
            for centroid in usable_entities.values():
                best_non_owner = max(best_non_owner, float(np.dot(embedding, centroid)))
            owner_claimed = (owner_score - best_non_owner) >= refs.owner_margin
        margin_delta = owner_score - best_non_owner
        declined = (
            owner_score >= refs.owner_threshold
            and refs.owner_margin is not None
            and not owner_claimed
        )
        if declined:
            margin_declined.add(statement_id)
        decisions[statement_id] = OwnerDecision(
            statement_id=statement_id,
            outcome="owner" if owner_claimed else "non_owner",
            owner_score=owner_score,
            best_non_owner_score=best_non_owner,
            owner_margin_delta=margin_delta,
            owner_margin_declined=declined,
        )
    return decisions, unusable, margin_declined, None


def _family2_report(
    left: differential.Bundle,
    right: differential.Bundle,
    refs: ReferenceCentroids | None,
) -> tuple[
    dict[str, Any],
    set[int],
    set[int],
    dict[int, OwnerDecision],
    dict[int, OwnerDecision],
]:
    if refs is None:
        report = _family2_not_evaluated("reference_centroids_absent", None)
        return report, set(), set(), {}, {}
    if refs.owner_centroid is None:
        report = _family2_not_evaluated("owner_centroid_absent", refs)
        return report, set(), set(), {}, {}
    left_decisions, left_unusable, left_margin_declined, left_missing = (
        _owner_decisions(left, refs)
    )
    right_decisions, right_unusable, right_margin_declined, right_missing = (
        _owner_decisions(right, refs)
    )
    report: dict[str, Any] = {
        "classification": differential.NOT_EVALUATED,
        "thresholds": _owner_thresholds(refs),
        "population_evaluated": 0,
        "flip_count": 0,
        "flip_rate": None,
        "flips": [],
        "asymmetric_evaluability": [],
        "left_margin_declined_sids": sorted(left_margin_declined),
        "right_margin_declined_sids": sorted(right_margin_declined),
    }
    if left_missing is not None or right_missing is not None:
        report["reason"] = "statement_embeddings_not_present"
        report["missing_fields"] = {"left": left_missing, "right": right_missing}
        return (
            report,
            left_margin_declined,
            right_margin_declined,
            left_decisions,
            right_decisions,
        )

    left_eval = set(left_decisions)
    right_eval = set(right_decisions)
    asymmetric = sorted(left_eval.symmetric_difference(right_eval))
    unevaluable_both = sorted(left_unusable & right_unusable)
    common = sorted(left_eval & right_eval)
    report["population_evaluated"] = len(common)
    report["flip_rate"] = 0.0 if common else None
    if unevaluable_both:
        report["unevaluable_both"] = unevaluable_both
    if asymmetric:
        report["classification"] = differential.UNEXPECTED_DIFFERS
        report["reason"] = "asymmetric_evaluability"
        report["asymmetric_evaluability"] = asymmetric
    for statement_id in common:
        left_decision = left_decisions[statement_id]
        right_decision = right_decisions[statement_id]
        if left_decision.outcome != right_decision.outcome:
            report["flips"].append(
                {
                    "family": "owner_claim",
                    "decision_id": statement_id,
                    "left_outcome": left_decision.outcome,
                    "right_outcome": right_decision.outcome,
                    "threshold": report["thresholds"]["owner_threshold"],
                    "margin": report["thresholds"]["owner_margin"],
                    "underlying_values": {
                        "left_owner_score": left_decision.owner_score,
                        "right_owner_score": right_decision.owner_score,
                        "left_best_non_owner_score": left_decision.best_non_owner_score,
                        "right_best_non_owner_score": right_decision.best_non_owner_score,
                        "left_owner_margin_delta": left_decision.owner_margin_delta,
                        "right_owner_margin_delta": right_decision.owner_margin_delta,
                        "left_owner_margin_declined": left_decision.owner_margin_declined,
                        "right_owner_margin_declined": right_decision.owner_margin_declined,
                    },
                }
            )
    report["flip_count"] = len(report["flips"])
    if common:
        report["flip_rate"] = len(report["flips"]) / len(common)
    if report["flip_count"]:
        report["classification"] = differential.UNEXPECTED_DIFFERS
    elif report["classification"] != differential.UNEXPECTED_DIFFERS:
        report["classification"] = (
            differential.EQUAL if common else differential.NOT_EVALUATED
        )
        if not common:
            report["reason"] = "empty_population"
    return (
        report,
        left_margin_declined,
        right_margin_declined,
        left_decisions,
        right_decisions,
    )


def _acoustic_decisions(
    bundle: differential.Bundle,
    refs: ReferenceCentroids,
    owner_decisions: dict[int, OwnerDecision],
    margin_declined_sids: set[int],
) -> tuple[dict[int, AcousticDecision], str | None]:
    embeddings, _unusable, missing = _statement_embedding_map(bundle)
    if missing is not None:
        return {}, missing
    entity_centroids = _entity_centroids(refs)
    if not entity_centroids:
        return {}, "entity_centroids_absent"
    decisions: dict[int, AcousticDecision] = {}
    for statement_id, owner_decision in owner_decisions.items():
        if owner_decision.outcome != "non_owner":
            continue
        embedding = embeddings.get(statement_id)
        if embedding is None:
            continue
        best_score = float("-inf")
        best_entity: str | None = None
        runner_up_scores: list[float] = []
        for entity_id, centroid in entity_centroids.items():
            score = float(np.dot(embedding, centroid))
            if score > best_score:
                if best_entity is not None:
                    runner_up_scores.append(best_score)
                best_score = score
                best_entity = entity_id
            else:
                runner_up_scores.append(score)
        runner_up = max(0.0, max(runner_up_scores, default=0.0))
        margin_delta = best_score - runner_up
        demotion_causes: list[str] = []
        if best_score >= encoder_config.ACOUSTIC_HIGH:
            acoustic_margin_declined = not _passes_acoustic_margin(
                best_score,
                runner_up_scores,
            )
            if acoustic_margin_declined:
                demotion_causes.append("acoustic_margin_declined")
            if statement_id in margin_declined_sids:
                demotion_causes.append("owner_margin_declined")
            tier = "medium" if demotion_causes else "high"
            outcome = f"{best_entity}:{tier}"
        elif best_score >= encoder_config.ACOUSTIC_MEDIUM:
            tier = "medium"
            outcome = f"{best_entity}:medium"
        else:
            tier = "none"
            best_entity = None
            outcome = "none"
        decisions[statement_id] = AcousticDecision(
            statement_id=statement_id,
            outcome=outcome,
            entity_id=best_entity,
            tier=tier,
            best_score=best_score,
            runner_up_score=runner_up,
            margin_delta=margin_delta,
            demotion_causes=tuple(demotion_causes),
        )
    return decisions, None


def _family3_report(
    left: differential.Bundle,
    right: differential.Bundle,
    refs: ReferenceCentroids | None,
    left_margin_declined: set[int],
    right_margin_declined: set[int],
    left_owner_decisions: dict[int, OwnerDecision],
    right_owner_decisions: dict[int, OwnerDecision],
    owner_report: dict[str, Any],
) -> dict[str, Any]:
    report: dict[str, Any] = {
        "classification": differential.NOT_EVALUATED,
        "thresholds": _acoustic_thresholds(),
        "cluster_path_thresholds_not_replayed": _family3_not_replayed_thresholds(),
        "caveats": {
            "structural_layer_not_replayed": (
                "Per-statement replay population is a superset of production Layer 3."
            ),
            "acoustic_cluster_not_replayed": (
                "Cluster acoustic path needs source JSONL and structural state."
            ),
        },
        "population_evaluated": 0,
        "flip_count": 0,
        "flip_rate": None,
        "flips": [],
    }
    if refs is None:
        report["reason"] = "reference_centroids_absent"
        return report
    if refs.owner_centroid is None:
        report["reason"] = "owner_centroid_absent"
        return report
    if owner_report["classification"] == differential.NOT_EVALUATED:
        report["reason"] = "owner_replay_not_evaluated"
        return report
    if not _entity_centroids(refs):
        report["reason"] = "entity_centroids_absent"
        return report
    left_decisions, left_missing = _acoustic_decisions(
        left, refs, left_owner_decisions, left_margin_declined
    )
    right_decisions, right_missing = _acoustic_decisions(
        right, refs, right_owner_decisions, right_margin_declined
    )
    if left_missing is not None or right_missing is not None:
        report["reason"] = "statement_embeddings_not_present"
        report["missing_fields"] = {"left": left_missing, "right": right_missing}
        return report
    common = sorted(set(left_decisions) & set(right_decisions))
    report["population_evaluated"] = len(common)
    if not common:
        report["reason"] = "empty_population_after_owner_replay"
        return report
    for statement_id in common:
        left_decision = left_decisions[statement_id]
        right_decision = right_decisions[statement_id]
        if left_decision.outcome != right_decision.outcome:
            report["flips"].append(
                {
                    "family": "acoustic_tier",
                    "decision_id": statement_id,
                    "left_outcome": {
                        "entity_id": left_decision.entity_id,
                        "tier": left_decision.tier,
                        "demotion_causes": list(left_decision.demotion_causes),
                    },
                    "right_outcome": {
                        "entity_id": right_decision.entity_id,
                        "tier": right_decision.tier,
                        "demotion_causes": list(right_decision.demotion_causes),
                    },
                    "thresholds": report["thresholds"],
                    "underlying_values": {
                        "left_best_score": left_decision.best_score,
                        "right_best_score": right_decision.best_score,
                        "left_runner_up_score": left_decision.runner_up_score,
                        "right_runner_up_score": right_decision.runner_up_score,
                        "left_margin_delta": left_decision.margin_delta,
                        "right_margin_delta": right_decision.margin_delta,
                    },
                }
            )
    report["flip_count"] = len(report["flips"])
    report["flip_rate"] = len(report["flips"]) / len(common)
    report["classification"] = (
        differential.UNEXPECTED_DIFFERS if report["flips"] else differential.EQUAL
    )
    return report


def load_reference_turns(path: Path | None) -> ReferenceTurns:
    if path is None:
        return ReferenceTurns("absent", (), "reference_turns_absent")
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except Exception as exc:
        raise differential.HarnessError(
            f"failed to load reference turns {path}: {exc}"
        ) from exc
    if not isinstance(data, list):
        raise differential.HarnessError("reference turns top level must be a list")
    turns: list[ReferenceTurn] = []
    previous_start: float | None = None
    for index, item in enumerate(data):
        if not isinstance(item, dict):
            raise differential.HarnessError(f"reference turn {index} must be an object")
        keys = set(item)
        expected = {"start_s", "end_s", "speaker"}
        unknown = keys - expected
        if unknown:
            names = sorted(unknown)
            if "text" in unknown:
                raise differential.HarnessError(
                    f"reference turn {index} has unknown key text"
                )
            raise differential.HarnessError(
                f"reference turn {index} has unknown keys {names}"
            )
        missing = expected - keys
        if missing:
            raise differential.HarnessError(
                f"reference turn {index} missing keys {sorted(missing)}"
            )
        start = _reference_bound(item["start_s"], index, "start_s")
        end = _reference_bound(item["end_s"], index, "end_s")
        if start < 0 or end < 0:
            raise differential.HarnessError(
                f"reference turn {index} bounds must be non-negative"
            )
        if end <= start:
            raise differential.HarnessError(
                f"reference turn {index} end_s must be greater than start_s"
            )
        if previous_start is not None and start < previous_start:
            raise differential.HarnessError("reference turns must be sorted by start_s")
        speaker = item["speaker"]
        if not isinstance(speaker, str) or not speaker:
            raise differential.HarnessError(
                f"reference turn {index} speaker must be a non-empty string"
            )
        previous_start = start
        turns.append(ReferenceTurn(start, end, speaker))
    if not turns:
        return ReferenceTurns("empty", (), "reference_turns_empty")
    return ReferenceTurns("present", tuple(turns))


def _reference_bound(value: object, index: int, key: str) -> float:
    if isinstance(value, bool) or not isinstance(value, int | float):
        raise differential.HarnessError(f"reference turn {index} {key} must be numeric")
    return float(value)


def _predicted_turns(
    bundle: differential.Bundle,
) -> tuple[tuple[ReferenceTurn, ...], str | None]:
    missing = _field_unavailable(
        bundle,
        (
            differential.INPUT_DIARIZATION_IDS,
            differential.INPUT_DIARIZATION_SPANS,
            differential.DIARIZATION_STATEMENT_LABELS,
        ),
    )
    if missing is not None:
        return (), "predicted_labels_not_present"
    ids = _unique_ints(
        differential._array(bundle, differential.INPUT_DIARIZATION_IDS),
        differential.INPUT_DIARIZATION_IDS,
    )
    spans = differential._array(bundle, differential.INPUT_DIARIZATION_SPANS)
    labels = differential._array(bundle, differential.DIARIZATION_STATEMENT_LABELS)
    if len(ids) != len(spans) or len(ids) != len(labels):
        raise differential.HarnessError("diarization prediction length mismatch")
    turns: list[ReferenceTurn] = []
    for statement_id in sorted(ids):
        idx = ids.index(statement_id)
        label = int(labels[idx])
        if label == int(differential.LABEL_NULL_SENTINEL):
            continue
        start = float(spans[idx][0])
        end = float(spans[idx][1])
        if not np.isfinite(start) or not np.isfinite(end) or end <= start:
            continue
        turns.append(ReferenceTurn(start, end, str(label)))
    return tuple(turns), None


def score_der(
    bundle: differential.Bundle,
    reference_turns: ReferenceTurns,
) -> dict[str, Any]:
    if reference_turns.status != "present":
        return {
            "status": differential.NOT_EVALUATED,
            "reason": reference_turns.reason,
            "breakdown": None,
            "der": None,
        }
    predicted, reason = _predicted_turns(bundle)
    if reason is not None:
        return {
            "status": differential.NOT_EVALUATED,
            "reason": reason,
            "breakdown": None,
            "der": None,
        }
    breakdown = _der_breakdown(reference_turns.turns, predicted)
    if breakdown.denominator == 0:
        return {
            "status": differential.NOT_EVALUATED,
            "reason": "zero_reference_speech",
            "breakdown": _breakdown_dict(breakdown),
            "der": None,
        }
    return {
        "status": differential.PRESENT,
        "reason": None,
        "breakdown": _breakdown_dict(breakdown),
        "der": breakdown.der,
    }


def _der_breakdown(
    reference_turns: tuple[ReferenceTurn, ...],
    predicted_turns: tuple[ReferenceTurn, ...],
) -> DerBreakdown:
    boundaries = sorted(
        {
            bound
            for turn in reference_turns + predicted_turns
            for bound in (turn.start_s, turn.end_s)
        }
    )
    ref_speakers = sorted({turn.speaker for turn in reference_turns})
    sys_speakers = sorted({turn.speaker for turn in predicted_turns})
    mapping = _speaker_mapping(
        reference_turns, predicted_turns, ref_speakers, sys_speakers
    )
    missed = 0.0
    false_alarm = 0.0
    confusion = 0.0
    denominator = 0.0
    for start, end in zip(boundaries, boundaries[1:]):
        duration = end - start
        if duration <= 0:
            continue
        ref_active = _active_speakers(reference_turns, start, end)
        sys_active = _active_speakers(predicted_turns, start, end)
        n_ref = len(ref_active)
        n_sys = len(sys_active)
        n_correct = sum(
            1
            for sys_label in sys_active
            if sys_label in mapping and mapping[sys_label] in ref_active
        )
        missed += duration * max(0, n_ref - n_sys)
        false_alarm += duration * max(0, n_sys - n_ref)
        confusion += duration * (min(n_ref, n_sys) - n_correct)
        denominator += duration * n_ref
    return DerBreakdown(missed, false_alarm, confusion, denominator)


def _speaker_mapping(
    reference_turns: tuple[ReferenceTurn, ...],
    predicted_turns: tuple[ReferenceTurn, ...],
    ref_speakers: list[str],
    sys_speakers: list[str],
) -> dict[str, str]:
    if not ref_speakers or not sys_speakers:
        return {}
    matrix = np.zeros((len(sys_speakers), len(ref_speakers)), dtype=np.float64)
    ref_index = {speaker: idx for idx, speaker in enumerate(ref_speakers)}
    sys_index = {speaker: idx for idx, speaker in enumerate(sys_speakers)}
    boundaries = sorted(
        {
            bound
            for turn in reference_turns + predicted_turns
            for bound in (turn.start_s, turn.end_s)
        }
    )
    for start, end in zip(boundaries, boundaries[1:]):
        duration = end - start
        if duration <= 0:
            continue
        ref_active = _active_speakers(reference_turns, start, end)
        sys_active = _active_speakers(predicted_turns, start, end)
        for sys_label in sys_active:
            for ref_label in ref_active:
                matrix[sys_index[sys_label], ref_index[ref_label]] += duration
    rows, cols = linear_sum_assignment(-matrix)
    return {sys_speakers[row]: ref_speakers[col] for row, col in zip(rows, cols)}


def _active_speakers(
    turns: tuple[ReferenceTurn, ...],
    start: float,
    end: float,
) -> set[str]:
    return {
        turn.speaker
        for turn in turns
        if max(start, turn.start_s) < min(end, turn.end_s)
    }


def _breakdown_dict(breakdown: DerBreakdown) -> dict[str, float]:
    return {
        "missed": breakdown.missed,
        "false_alarm": breakdown.false_alarm,
        "confusion": breakdown.confusion,
        "denominator": breakdown.denominator,
        "total_error": breakdown.total_error,
    }


def _der_report(
    left: differential.Bundle,
    right: differential.Bundle,
    reference_turns: ReferenceTurns,
) -> dict[str, Any]:
    if reference_turns.status != "present":
        return {
            "classification": differential.NOT_EVALUATED,
            "reason": reference_turns.reason,
            "reference_status": reference_turns.status,
            "left": None,
            "right": None,
            "delta": None,
        }
    left_score = score_der(left, reference_turns)
    right_score = score_der(right, reference_turns)
    report = {
        "classification": differential.NOT_EVALUATED,
        "reason": None,
        "reference_status": reference_turns.status,
        "left": left_score,
        "right": right_score,
        "delta": None,
    }
    if (
        left_score["status"] != differential.PRESENT
        or right_score["status"] != differential.PRESENT
    ):
        report["reason"] = left_score["reason"] or right_score["reason"]
        return report
    delta = float(right_score["der"] - left_score["der"])
    report["delta"] = {
        "der": delta,
        "missed": right_score["breakdown"]["missed"]
        - left_score["breakdown"]["missed"],
        "false_alarm": right_score["breakdown"]["false_alarm"]
        - left_score["breakdown"]["false_alarm"],
        "confusion": right_score["breakdown"]["confusion"]
        - left_score["breakdown"]["confusion"],
        "denominator": right_score["breakdown"]["denominator"]
        - left_score["breakdown"]["denominator"],
    }
    report["classification"] = (
        differential.EQUAL if delta == 0.0 else differential.FUNCTIONALLY_EQUAL
    )
    return report


def _decision_report(
    left: differential.Bundle,
    right: differential.Bundle,
    refs: ReferenceCentroids | None,
    comparator_classification: str,
) -> dict[str, Any]:
    family1 = _family1_report(left, right)
    family2, left_margin, right_margin, left_owner, right_owner = _family2_report(
        left, right, refs
    )
    family3 = _family3_report(
        left,
        right,
        refs,
        left_margin,
        right_margin,
        left_owner,
        right_owner,
        family2,
    )
    families = {
        "cluster_count": family1,
        "owner_claim": family2,
        "acoustic_tier": family3,
    }
    flip_count = sum(int(family.get("flip_count", 0)) for family in families.values())
    population = sum(
        int(family.get("population_evaluated", 0)) for family in families.values()
    )
    if (
        any(
            family["classification"] == differential.UNEXPECTED_DIFFERS
            for family in families.values()
        )
        or comparator_classification == differential.UNEXPECTED_DIFFERS
    ):
        classification = differential.UNEXPECTED_DIFFERS
    elif flip_count:
        classification = differential.UNEXPECTED_DIFFERS
    elif comparator_classification == differential.FUNCTIONALLY_EQUAL:
        classification = differential.FUNCTIONALLY_EQUAL
    elif any(
        family["classification"] == differential.EQUAL for family in families.values()
    ):
        classification = differential.EQUAL
    else:
        classification = differential.NOT_EVALUATED
    return {
        "classification": classification,
        "flip_count": flip_count,
        "population_evaluated": population,
        "families": families,
    }


def compare_verdicts(
    left: differential.Bundle,
    right: differential.Bundle,
    *,
    reference_centroids: ReferenceCentroids | None = None,
    reference_turns: ReferenceTurns | None = None,
) -> dict[str, Any]:
    report = _base_report(left, right)
    reference_turns = reference_turns or ReferenceTurns(
        "absent", (), "reference_turns_absent"
    )
    comparator = differential.compare_bundles(left, right)
    report["components"]["bundle_comparator"] = comparator
    if comparator.get("failure") is not None:
        report["classification"] = differential.NOT_EVALUATED
        report["failure"] = comparator["failure"]
        return report
    decision = _decision_report(
        left,
        right,
        reference_centroids,
        str(comparator["classification"]),
    )
    der = _der_report(left, right, reference_turns)
    report["components"]["decision_flips"] = decision
    report["components"]["der"] = der
    report["classification"] = _rollup(str(comparator["classification"]), decision, der)
    return report


def _rollup(
    comparator_classification: str,
    decision: dict[str, Any],
    der: dict[str, Any],
) -> str:
    if comparator_classification == differential.UNEXPECTED_DIFFERS:
        return differential.UNEXPECTED_DIFFERS
    if decision["classification"] == differential.UNEXPECTED_DIFFERS:
        return differential.UNEXPECTED_DIFFERS
    if (
        comparator_classification == differential.FUNCTIONALLY_EQUAL
        or decision["classification"] == differential.FUNCTIONALLY_EQUAL
        or der["classification"] == differential.FUNCTIONALLY_EQUAL
    ):
        return differential.FUNCTIONALLY_EQUAL
    if (
        comparator_classification == differential.EQUAL
        or decision["classification"] == differential.EQUAL
        or der["classification"] == differential.EQUAL
    ):
        return differential.EQUAL
    return differential.NOT_EVALUATED


def parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("left_bundle", help="Left speaker differential .npz bundle")
    parser.add_argument("right_bundle", help="Right speaker differential .npz bundle")
    parser.add_argument("--reference-centroids", help="Reference centroid .npz file")
    parser.add_argument("--reference-turns", help="Reference speaker turns JSON file")
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
            report_path = differential._refuse_repo_destination(requested_report_path)
        left = differential.load_bundle(Path(args.left_bundle))
        right = differential.load_bundle(Path(args.right_bundle))
        reference_centroids = load_reference_centroids(
            Path(args.reference_centroids) if args.reference_centroids else None
        )
        reference_turns = load_reference_turns(
            Path(args.reference_turns) if args.reference_turns else None
        )
        report = compare_verdicts(
            left,
            right,
            reference_centroids=reference_centroids,
            reference_turns=reference_turns,
        )
    except Exception as exc:
        logger.exception("speaker verdict failed")
        report = _base_report()
        report["failure"] = {
            "class": differential.HARNESS_ERROR,
            "message": str(exc),
        }
        report["classification"] = differential.NOT_EVALUATED

    rendered = differential._render_report(report)
    if report_path is not None:
        report_path.parent.mkdir(parents=True, exist_ok=True)
        report_path.write_text(rendered, encoding="utf-8")
    sys.stdout.write(rendered)
    return (
        0
        if report.get("classification")
        in {differential.EQUAL, differential.FUNCTIONALLY_EQUAL}
        else 1
    )


if __name__ == "__main__":
    raise SystemExit(main())
