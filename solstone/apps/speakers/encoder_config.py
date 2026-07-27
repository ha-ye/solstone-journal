# SPDX-License-Identifier: AGPL-3.0-only
# Copyright (c) 2026 sol pbc

"""Encoder-specific calibration constants governed by operator spec signoff."""

import math

ENCODER_ID: str = "wespeaker-resnet34-256"
WESPEAKER_EMBEDDING_WIDTH: int = 256

OWNER_THRESHOLD: float = 0.43
# Benchmark sweep: 0.05 cuts L1 owner false-claims on non-owner statements from 14.1% to 2.6% at a cost of 69% -> 65% per-statement L1 owner recall.
OWNER_MARGIN_MIN: float = 0.05
ACOUSTIC_HIGH: float = 0.36
ACOUSTIC_MEDIUM: float = 0.22
# Benchmark sweep: 0.05 raises HIGH-tier named precision from 78.6% to 90.0%
# while retaining 76.7% of high-volume matches. The cluster path applies this
# per-statement margin to cluster centroids by assumption, not measurement; a
# cluster-specific constant can follow if data justifies it.
ACOUSTIC_MARGIN_MIN: float = 0.05
# Solo-cluster trim currently shares the owner threshold value because it asks
# the same encoder question against a provisional cluster centroid. Keep it
# independently tunable from owner identity decisions.
SOLO_CLUSTER_MIN_COSINE: float = 0.43

# Hybrid-cluster and voiceprint-refinement acoustic constants.
VP_DECAY_LAMBDA: float = math.log(2) / 120
VP_OUTLIER_MIN_SIMILARITY: float = 0.18
VP_OUTLIER_MIN_SAMPLES: int = 5
CC_COVERAGE_GATE: float = 0.45
CC_CONFIDENCE_GATE: float = 0.28

OWNER_BOOTSTRAP_MIN_STMTS: int = 30
OWNER_BOOTSTRAP_MIN_MEDIAN_DURATION_S: float = 1.5
OWNER_BOOTSTRAP_MIN_INTRA_COSINE_P25: float = 0.30
# Operator-derived and locked for owner-bootstrap evidence tiering.
OWNER_BOOTSTRAP_STRONG_EVIDENCE_MIN_STMTS: int = 100
OWNER_BOOTSTRAP_MIN_INTRA_COSINE_P25_STRONG: float = 0.15
OWNER_BOOTSTRAP_EVIDENCE_TIER_STANDARD: str = "standard"
OWNER_BOOTSTRAP_EVIDENCE_TIER_STRONG: str = "strong"
# Smallest manual-tag set that meaningfully constrains the contamination centroid; below this the no-op default holds.
OWNER_BOOTSTRAP_PROVISIONAL_GUARD_MIN_TAGS: int = 5
# Owner centroid rebuild guards governed by the owner-rebuild spec.
OWNER_REBUILD_MIN_CENTROID_AGREEMENT: float = 0.80
OWNER_REBUILD_MIN_CLUSTER_SIZE_RATIO: float = 0.80
OWNER_REBUILD_MAX_COHESION_DROP: float = 0.05
OWNER_REBUILD_SUPERSEDED_SCAN_DAYS: int = 30

NOISY_FLYWHEEL_OVERLAP_MAX: float = 0.10
SLOT_ACTIVE_MIN_SHARE: float = 0.10
SPEAKER_EVIDENCE_MULTI_MIN: float = 0.05
SPEAKER_EVIDENCE_SINGLE_MAX: float = 0.05
DIARIZE_MIN_OVERLAP: float = 0.05
SPEAKER_EVIDENCE_VERSION: str = "windowed-slots-v1"
OVERLAP_DETECTOR_ID: str = "pyannote-segmentation-3.0-onnx"
OVERLAP_DETECTOR_SHA256: str = (
    "057ee564753071c0b09b5b611648b50ac188d50846bff5f01e9f7bbf1591ea25"
)

MERGE_THRESHOLD = 0.72
SPLIT_THRESHOLD = 0.55
STABILITY_THRESHOLD = 0.25
CONSOLIDATE_MIN_INTERVALS = 30
CONSOLIDATE_MERGE_THRESHOLD = 0.65
CONSOLIDATE_SUGGEST_MIN = 0.45
CONFIRM_MIN_SEGMENTS = 2
CONFIRM_MIN_INTERVALS = 5
CONFIRM_MIN_DURATION_S = 25.0
