# SPDX-License-Identifier: AGPL-3.0-only
# Copyright (c) 2026 sol pbc

"""Headless cross-segment speaker candidate pool."""

from __future__ import annotations

import json
from collections import defaultdict
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import numpy as np

from solstone.apps.speakers.attribution import (
    _load_integer_speaker_labels,
    segment_path,
)
from solstone.think.journal_io import (
    MalformedPolicy,
    atomic_replace,
    hold_lock,
    read_json,
)
from solstone.think.utils import get_journal

MERGE_THRESHOLD = 0.72
SPLIT_THRESHOLD = 0.55
STABILITY_THRESHOLD = 0.25
CONFIRM_MIN_SEGMENTS = 2
CONFIRM_MIN_INTERVALS = 5
CONFIRM_MIN_DURATION_S = 25.0


@dataclass
class CandidateProfile:
    cand_id: int
    centroid: np.ndarray
    n_segments: int
    n_intervals: int
    total_duration_s: float
    source_segments: list[dict[str, Any]] = field(default_factory=list)
    confirmed_entity: str | None = None
    status: str = "pending"

    def to_json(self) -> dict[str, Any]:
        return {
            "cand_id": self.cand_id,
            "centroid": self.centroid.astype(float).tolist(),
            "n_segments": self.n_segments,
            "n_intervals": self.n_intervals,
            "total_duration_s": self.total_duration_s,
            "source_segments": self.source_segments,
            "confirmed_entity": self.confirmed_entity,
            "status": self.status,
        }

    @classmethod
    def from_json(cls, data: dict[str, Any]) -> CandidateProfile:
        return cls(
            cand_id=int(data["cand_id"]),
            centroid=np.asarray(data["centroid"], dtype=np.float32),
            n_segments=int(data.get("n_segments", 0)),
            n_intervals=int(data.get("n_intervals", 0)),
            total_duration_s=float(data.get("total_duration_s", 0.0)),
            source_segments=list(data.get("source_segments", [])),
            confirmed_entity=data.get("confirmed_entity"),
            status=str(data.get("status", "pending")),
        )

    def ready_for_confirmation(self) -> bool:
        return (
            self.status == "pending"
            and self.n_segments >= CONFIRM_MIN_SEGMENTS
            and self.n_intervals >= CONFIRM_MIN_INTERVALS
            and self.total_duration_s >= CONFIRM_MIN_DURATION_S
        )


def _routes_helpers():
    from solstone.apps.speakers.routes import (
        _load_embeddings_file,
        _normalize_embedding,
    )

    return _load_embeddings_file, _normalize_embedding


def _source_key(source_segment: dict[str, Any]) -> tuple[str, str, str, str, int]:
    return (
        str(source_segment["day"]),
        str(source_segment["segment_key"]),
        str(source_segment["stream"]),
        str(source_segment["source"]),
        int(source_segment["cluster_label"]),
    )


class CandidateTracker:
    def __init__(self, store_path: Path | None = None) -> None:
        self.store_path = store_path or (
            Path(get_journal()) / "awareness" / "speaker_candidates.json"
        )
        self._candidates: dict[int, CandidateProfile] = {}
        self._next_id = 1
        self.load()

    def load(self) -> None:
        data = read_json(
            self.store_path,
            on_error=MalformedPolicy.WARN_AND_SKIP,
            default={"next_id": 1, "candidates": []},
        )
        self._next_id = int(data.get("next_id", 1))
        self._candidates = {
            candidate.cand_id: candidate
            for candidate in (
                CandidateProfile.from_json(raw)
                for raw in data.get("candidates", [])
                if isinstance(raw, dict)
            )
        }

    def save(self) -> None:
        data = {
            "next_id": self._next_id,
            "candidates": [
                candidate.to_json()
                for candidate in sorted(
                    self._candidates.values(), key=lambda item: item.cand_id
                )
            ],
        }
        with hold_lock(self.store_path):
            atomic_replace(
                self.store_path,
                json.dumps(data, indent=2, sort_keys=True) + "\n",
            )

    def _new_id(self) -> int:
        cand_id = self._next_id
        self._next_id += 1
        return cand_id

    def _existing_source_keys(self) -> set[tuple[str, str, str, str, int]]:
        return {
            _source_key(source_segment)
            for candidate in self._candidates.values()
            for source_segment in candidate.source_segments
        }

    def _best_match(self, centroid: np.ndarray) -> tuple[int | None, float]:
        best_id: int | None = None
        best_score = -1.0
        for cand_id, candidate in self._candidates.items():
            if candidate.status == "rejected":
                continue
            score = float(np.dot(centroid, candidate.centroid))
            if score > best_score:
                best_id = cand_id
                best_score = score
        return best_id, best_score

    def _merge_candidate(
        self,
        candidate: CandidateProfile,
        centroid: np.ndarray,
        n_intervals: int,
        duration_s: float,
        source_segment: dict[str, Any],
        normalize_embedding,
    ) -> None:
        combined = candidate.centroid * float(candidate.n_intervals)
        combined += centroid * float(n_intervals)
        merged = normalize_embedding(combined)
        if merged is not None:
            candidate.centroid = merged

        segment_seen = any(
            existing["day"] == source_segment["day"]
            and existing["segment_key"] == source_segment["segment_key"]
            and existing["stream"] == source_segment["stream"]
            and existing["source"] == source_segment["source"]
            for existing in candidate.source_segments
        )
        if not segment_seen:
            candidate.n_segments += 1
        candidate.n_intervals += n_intervals
        candidate.total_duration_s += duration_s
        candidate.source_segments.append(source_segment)

    def _create_candidate(
        self,
        centroid: np.ndarray,
        n_intervals: int,
        duration_s: float,
        source_segment: dict[str, Any],
    ) -> None:
        cand_id = self._new_id()
        self._candidates[cand_id] = CandidateProfile(
            cand_id=cand_id,
            centroid=centroid,
            n_segments=1,
            n_intervals=n_intervals,
            total_duration_s=duration_s,
            source_segments=[source_segment],
        )

    def process_segment(
        self,
        day: str,
        segment_key: str,
        stream: str,
        source: str,
        seg_dir: Path,
    ) -> None:
        load_embeddings_file, normalize_embedding = _routes_helpers()
        integer_labels = _load_integer_speaker_labels(seg_dir, source)
        if not integer_labels:
            return

        emb_data = load_embeddings_file(seg_dir / f"{source}.npz")
        if emb_data is None:
            return
        embeddings, statement_ids, durations_s = emb_data
        sid_to_idx = {int(sid): idx for idx, sid in enumerate(statement_ids)}
        existing_source_keys = self._existing_source_keys()
        changed = False

        cluster_sids: dict[int, list[int]] = defaultdict(list)
        for sid, label in integer_labels.items():
            cluster_sids[int(label)].append(int(sid))

        for cluster_label, sentence_ids in sorted(cluster_sids.items()):
            source_segment = {
                "day": day,
                "segment_key": segment_key,
                "stream": stream,
                "source": source,
                "cluster_label": int(cluster_label),
            }
            source_key = _source_key(source_segment)
            if source_key in existing_source_keys:
                continue

            cluster_embeddings: list[np.ndarray] = []
            duration_s = 0.0
            for sid in sentence_ids:
                idx = sid_to_idx.get(sid)
                if idx is None:
                    continue
                normalized = normalize_embedding(embeddings[idx])
                if normalized is None:
                    continue
                cluster_embeddings.append(normalized)
                if durations_s is not None and idx < len(durations_s):
                    duration_s += float(durations_s[idx])

            if not cluster_embeddings:
                continue

            stacked = np.stack(cluster_embeddings)
            centroid = normalize_embedding(np.mean(stacked, axis=0))
            if centroid is None:
                continue

            spread = float(np.mean(1.0 - stacked @ centroid))
            if spread >= STABILITY_THRESHOLD:
                continue

            n_intervals = len(cluster_embeddings)
            best_id, best_score = self._best_match(centroid)
            if best_id is not None and best_score >= MERGE_THRESHOLD:
                self._merge_candidate(
                    self._candidates[best_id],
                    centroid,
                    n_intervals,
                    duration_s,
                    source_segment,
                    normalize_embedding,
                )
                existing_source_keys.add(source_key)
                changed = True
            elif best_id is None or best_score < SPLIT_THRESHOLD:
                self._create_candidate(
                    centroid,
                    n_intervals,
                    duration_s,
                    source_segment,
                )
                existing_source_keys.add(source_key)
                changed = True

        if changed:
            self.save()

    def confirmation_queue(self) -> list[CandidateProfile]:
        return [
            candidate
            for candidate in self._candidates.values()
            if candidate.ready_for_confirmation()
        ]

    def confirm(self, cand_id: int, entity_id: str) -> None:
        candidate = self._candidates[int(cand_id)]
        candidate.status = "confirmed"
        candidate.confirmed_entity = entity_id
        self.save()

    def reject(self, cand_id: int) -> None:
        candidate = self._candidates[int(cand_id)]
        candidate.status = "rejected"
        candidate.confirmed_entity = None
        self.save()

    def retroactive_confirm(self, centroid: np.ndarray, entity_id: str) -> int:
        from solstone.apps.speakers.attribution import accumulate_voiceprints

        _, normalize_embedding = _routes_helpers()
        normalized_centroid = normalize_embedding(centroid)
        if normalized_centroid is None:
            return 0

        cand_id, score = self._best_match(normalized_centroid)
        if cand_id is None or score < MERGE_THRESHOLD:
            return 0

        candidate = self._candidates[cand_id]
        saved_total = 0
        for source_segment in candidate.source_segments:
            day = str(source_segment["day"])
            segment_key = str(source_segment["segment_key"])
            stream = str(source_segment["stream"])
            source = str(source_segment["source"])
            cluster_label = int(source_segment["cluster_label"])

            seg_dir = segment_path(day, segment_key, stream, create=False)
            if not seg_dir.exists():
                continue
            integer_labels = _load_integer_speaker_labels(seg_dir, source)
            synthetic_labels = [
                {
                    "sentence_id": sid,
                    "speaker": entity_id,
                    "confidence": "high",
                    "method": "acoustic_cluster",
                }
                for sid, label in sorted(integer_labels.items())
                if int(label) == cluster_label
            ]
            if not synthetic_labels:
                continue
            saved = accumulate_voiceprints(
                day,
                stream,
                segment_key,
                synthetic_labels,
                source,
            )
            saved_total += sum(saved.values())

        candidate.status = "confirmed"
        candidate.confirmed_entity = entity_id
        self.save()
        return saved_total
