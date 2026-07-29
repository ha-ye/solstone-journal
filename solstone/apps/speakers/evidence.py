# SPDX-License-Identifier: AGPL-3.0-only
# Copyright (c) 2026 sol pbc

"""Shared speaker-evidence wire types."""

from __future__ import annotations

from typing import NamedTuple

VALID_SPEAKER_EVIDENCE_DECISIONS = frozenset({"none", "single", "multi"})


class SpeakerEvidenceDecision(NamedTuple):
    speaker_evidence: str
    multi_window_fraction: float
    mean_window_overlap_share: float


__all__ = [
    "SpeakerEvidenceDecision",
    "VALID_SPEAKER_EVIDENCE_DECISIONS",
]
