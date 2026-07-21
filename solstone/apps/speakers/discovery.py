# SPDX-License-Identifier: AGPL-3.0-only
# Copyright (c) 2026 sol pbc

"""Unknown speaker discovery - cluster unmatched embeddings to find recurring voices."""

from __future__ import annotations

import copy
import json
import logging
import uuid
from collections import defaultdict
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from solstone.apps.speakers.attribution import (
    _load_setting_field,
    compute_segment_candidate_evidence_readonly,
)
from solstone.apps.speakers.audio import resolve_audio_url
from solstone.apps.speakers.eligibility import (
    blocked_person_name_collision,
    current_principal_id,
    eligible_speaker_attach_entities,
    principal_name_collision,
    speaker_attach_rejection_reason,
)
from solstone.think.entities.journal import get_journal_principal, load_journal_entity
from solstone.think.journal_io import atomic_replace
from solstone.think.utils import day_dirs, day_path, get_journal, now_ms, segment_path

logger = logging.getLogger(__name__)

MIN_CLUSTER_SIZE = 5
MIN_SAMPLES = 3
MIN_SEGMENT_DIVERSITY = 3
MAX_UNMATCHED_EMBEDDINGS = 10000


def _routes_helpers():
    """Load speakers route helpers lazily to avoid import cycles."""
    from solstone.apps.speakers.routes import (
        _check_owner_contamination,
        _load_embeddings_file,
        _load_speaker_labels,
        _normalize_embedding,
        _scan_segment_embeddings,
    )

    return (
        _load_embeddings_file,
        _load_speaker_labels,
        _normalize_embedding,
        _scan_segment_embeddings,
        _check_owner_contamination,
    )


def _owner_helpers():
    """Load owner helpers lazily to avoid import cycles."""
    from solstone.apps.speakers.owner import load_owner_centroid

    return load_owner_centroid


def _discovery_cache_path(*, create: bool = False) -> Path:
    """Return the temporary cache path for discovery cluster assignments."""
    awareness_dir = Path(get_journal()) / "awareness"
    if create:
        awareness_dir.mkdir(parents=True, exist_ok=True)
    return awareness_dir / "discovery_clusters.json"


def _discovery_resolved_path(*, create: bool = False) -> Path:
    """Return the idempotency sentinel path for resolved discovery clusters."""
    return _discovery_cache_path(create=create).with_suffix(".resolved.json")


def load_discovery_cache() -> dict[str, Any] | None:
    """Return cached discovery cluster assignments, if present and valid."""
    path = _discovery_cache_path()
    if not path.exists():
        return None
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (json.JSONDecodeError, OSError):
        return None
    if not isinstance(data, dict):
        return None
    clusters = data.get("clusters")
    return data if isinstance(clusters, dict) else None


def _get_sentence_text(segment_dir: Path, source: str, sentence_id: int) -> str | None:
    """Return transcript text for a sentence ID from the source transcript."""
    jsonl_path = segment_dir / f"{source}.jsonl"
    if not jsonl_path.exists():
        return None
    try:
        lines = jsonl_path.read_text(encoding="utf-8").splitlines()
        if sentence_id < 1 or sentence_id >= len(lines):
            return None
        entry = json.loads(lines[sentence_id])
        return entry.get("text")
    except (json.JSONDecodeError, OSError, IndexError):
        return None


def _build_cluster_sample(record: dict) -> dict:
    day = record["day"]
    stream = record["stream"]
    segment_key = record["segment_key"]
    source = record["source"]
    sentence_id = record["sentence_id"]
    seg_dir = segment_path(day, segment_key, stream, create=False)
    return {
        **record,
        "audio_url": resolve_audio_url(day, stream, segment_key, source),
        "text": _get_sentence_text(seg_dir, source, sentence_id) or "",
    }


def _serialize_discovery_cluster(
    cluster_id: int,
    members: list[dict[str, Any]],
) -> dict[str, Any] | None:
    normalized_members = [
        normalized
        for member in members
        if (normalized := _normalized_cache_member(member)) is not None
    ]
    if not normalized_members:
        return None
    segment_keys = {
        (
            member["day"],
            member["stream"],
            member["segment_key"],
        )
        for member in normalized_members
    }
    samples: list[dict[str, Any]] = []
    seen_segments: set[tuple[str, str, str]] = set()
    for member in normalized_members:
        segment = (
            member["day"],
            member["stream"],
            member["segment_key"],
        )
        if segment in seen_segments:
            continue
        seen_segments.add(segment)
        samples.append(_build_cluster_sample(member))
        if len(samples) == 3:
            break
    if len(samples) < 3:
        for member in normalized_members:
            sample = _build_cluster_sample(member)
            if sample in samples:
                continue
            samples.append(sample)
            if len(samples) == 3:
                break
    return {
        "cluster_id": int(cluster_id),
        "size": len(normalized_members),
        "segment_count": len(segment_keys),
        "samples": samples,
    }


def _serialize_discovery_clusters(
    clusters: dict[str, Any],
) -> dict[str, Any]:
    from solstone.think.speaker_cluster_dismissals import (
        cluster_dismissal_suppressed,
    )

    rows: list[dict[str, Any]] = []
    for raw_cluster_id, members in clusters.items():
        try:
            cluster_id = int(raw_cluster_id)
        except (TypeError, ValueError):
            continue
        if not isinstance(members, list):
            continue
        normalized_members = [
            normalized
            for member in members
            if (normalized := _normalized_cache_member(member)) is not None
        ]
        if not normalized_members:
            continue
        if cluster_dismissal_suppressed(normalized_members):
            continue
        row = _serialize_discovery_cluster(cluster_id, normalized_members)
        if row is not None:
            rows.append(row)
    rows.sort(key=lambda cluster: (-int(cluster["size"]), int(cluster["cluster_id"])))
    return {"clusters": rows}


def read_discovery_cache_snapshot() -> dict[str, Any]:
    """Return visible discovery clusters from the current cache without scanning."""
    cache = load_discovery_cache()
    if cache is None:
        return {"status": "cache_unavailable", "clusters": []}
    return {"status": "ok", **_serialize_discovery_clusters(cache.get("clusters", {}))}


def _clear_discovery_cache() -> None:
    """Remove the cached discovery assignment file if present."""
    _discovery_cache_path().unlink(missing_ok=True)
    _discovery_resolved_path().unlink(missing_ok=True)


def discover_unknown_speakers() -> dict[str, Any]:
    """Scan journal for recurring unknown speaker clusters."""
    import numpy as np
    from sklearn.cluster import HDBSCAN

    load_owner_centroid = _owner_helpers()
    (
        load_embeddings_file,
        load_speaker_labels,
        normalize_embedding,
        scan_segment_embeddings,
        _,
    ) = _routes_helpers()

    centroid_data = load_owner_centroid()
    if centroid_data is None:
        _clear_discovery_cache()
        return {"clusters": []}

    owner_centroid = centroid_data.centroid
    owner_threshold = centroid_data.threshold
    embedding_chunks: list[np.ndarray] = []
    provenance: list[dict[str, Any]] = []

    for day in sorted(day_dirs().keys()):
        for segment in scan_segment_embeddings(day):
            stream = segment["stream"]
            seg_key = segment["key"]
            seg_dir = segment_path(day, seg_key, stream, create=False)

            labels_data = load_speaker_labels(seg_dir)
            attributed_sids: set[int] = set()
            if labels_data:
                for label in labels_data.get("labels", []):
                    sentence_id = label.get("sentence_id")
                    if label.get("speaker") is not None and sentence_id is not None:
                        attributed_sids.add(int(sentence_id))

            for source in segment["sources"]:
                emb_data = load_embeddings_file(seg_dir / f"{source}.npz")
                if emb_data is None:
                    continue

                embeddings, statement_ids, _ = emb_data
                if len(embeddings) == 0:
                    continue

                for emb, sid in zip(embeddings, statement_ids):
                    sid_int = int(sid)
                    if sid_int in attributed_sids:
                        continue

                    normalized = normalize_embedding(emb)
                    if normalized is None:
                        continue

                    score = float(np.dot(normalized, owner_centroid))
                    if score >= owner_threshold:
                        continue

                    embedding_chunks.append(normalized.reshape(1, -1))
                    provenance.append(
                        {
                            "day": day,
                            "stream": stream,
                            "segment_key": seg_key,
                            "source": source,
                            "sentence_id": sid_int,
                        }
                    )

    if not embedding_chunks:
        _clear_discovery_cache()
        return {"clusters": []}

    embeddings_matrix = np.vstack(embedding_chunks)
    if len(embeddings_matrix) > MAX_UNMATCHED_EMBEDDINGS:
        rng = np.random.default_rng(42)
        indices = rng.choice(
            len(embeddings_matrix),
            MAX_UNMATCHED_EMBEDDINGS,
            replace=False,
        )
        indices.sort()
        embeddings_matrix = embeddings_matrix[indices]
        provenance = [provenance[int(i)] for i in indices]

    if len(embeddings_matrix) < MIN_CLUSTER_SIZE:
        _clear_discovery_cache()
        return {"clusters": []}

    clusterer = HDBSCAN(
        min_cluster_size=MIN_CLUSTER_SIZE,
        min_samples=MIN_SAMPLES,
        metric="euclidean",
    )
    clusterer.fit(embeddings_matrix)
    labels = clusterer.labels_
    if np.all(labels == -1):
        _clear_discovery_cache()
        return {"clusters": []}

    cache_clusters: dict[str, list[dict[str, Any]]] = {}

    for cid in sorted(set(labels[labels != -1])):
        cluster_indices = np.flatnonzero(labels == int(cid))
        segment_set = {
            (
                provenance[int(idx)]["day"],
                provenance[int(idx)]["stream"],
                provenance[int(idx)]["segment_key"],
            )
            for idx in cluster_indices
        }
        if len(segment_set) < MIN_SEGMENT_DIVERSITY:
            continue

        cluster_embeddings = embeddings_matrix[cluster_indices]
        centroid = normalize_embedding(np.mean(cluster_embeddings, axis=0))
        if centroid is None:
            continue
        similarities = np.dot(cluster_embeddings, centroid)
        sorted_positions = np.argsort(similarities)[::-1]
        cache_clusters[str(int(cid))] = [
            provenance[int(cluster_indices[int(pos)])] for pos in sorted_positions
        ]

    if not cache_clusters:
        _clear_discovery_cache()
        return {"clusters": []}

    cache_path = _discovery_cache_path(create=True)
    tmp_path = cache_path.with_suffix(".tmp")
    with open(tmp_path, "w", encoding="utf-8") as f:
        json.dump(
            {
                "version": datetime.now().isoformat(),
                "clusters": cache_clusters,
            },
            f,
            indent=2,
        )
    tmp_path.rename(cache_path)

    return _serialize_discovery_clusters(cache_clusters)


def _conversation_key(
    day: str,
    stream: str,
    segment_key: str,
    setting: str | None,
) -> tuple:
    if setting:
        return (day, stream, setting)
    return (day, stream, "__segment__", segment_key)


@dataclass(frozen=True)
class _ClusterConversationContext:
    distinct_segments: tuple[tuple[str, str, str], ...]
    first_record_by_segment: dict[tuple[str, str, str], dict[str, Any]]
    segment_settings: dict[tuple[str, str, str], str | None]
    conversation_keys: dict[tuple[str, str, str], tuple]
    conversation_count: int


def _normalized_cache_member(member: Any) -> dict[str, Any] | None:
    if not isinstance(member, dict):
        return None
    normalized: dict[str, Any] = {}
    for field in ("day", "stream", "segment_key", "source"):
        value = member.get(field)
        if not isinstance(value, str) or not value:
            return None
        normalized[field] = value
    try:
        normalized["sentence_id"] = int(member["sentence_id"])
    except (KeyError, TypeError, ValueError):
        return None
    return normalized


def _cluster_conversation_context(
    members: list[dict[str, Any]],
) -> _ClusterConversationContext:
    distinct_segments: list[tuple[str, str, str]] = []
    first_record_by_segment: dict[tuple[str, str, str], dict[str, Any]] = {}
    for member in members:
        normalized = _normalized_cache_member(member)
        if normalized is None:
            continue
        segment = (
            normalized["day"],
            normalized["stream"],
            normalized["segment_key"],
        )
        if segment in first_record_by_segment:
            continue
        first_record_by_segment[segment] = normalized
        distinct_segments.append(segment)

    segment_settings: dict[tuple[str, str, str], str | None] = {}
    conversation_keys: dict[tuple[str, str, str], tuple] = {}
    for day, stream, segment_key in distinct_segments:
        seg_dir = segment_path(day, segment_key, stream, create=False)
        setting = _load_setting_field(seg_dir)
        segment = (day, stream, segment_key)
        segment_settings[segment] = setting
        conversation_keys[segment] = _conversation_key(
            day,
            stream,
            segment_key,
            setting,
        )

    conversations = set(conversation_keys.values())
    return _ClusterConversationContext(
        distinct_segments=tuple(distinct_segments),
        first_record_by_segment=first_record_by_segment,
        segment_settings=segment_settings,
        conversation_keys=conversation_keys,
        conversation_count=len(conversations),
    )


def get_cluster_conversation_count(members: list[dict[str, Any]]) -> int:
    """Return distinct conversation count for valid discovery-cache members."""
    return _cluster_conversation_context(members).conversation_count


def resolve_statement_cluster(
    *,
    day: str,
    stream: str,
    segment_key: str,
    source: str,
    sentence_id: int,
) -> dict[str, Any]:
    """Resolve one statement identity to a discovery cluster in the current cache."""
    cache = load_discovery_cache()
    if cache is None:
        return {"status": "cache_unavailable", "cluster_id": None}

    clusters = cache.get("clusters", {})
    eligible: list[tuple[int, list[dict[str, Any]]]] = []
    for raw_cluster_id, members in clusters.items():
        try:
            cluster_id = int(raw_cluster_id)
        except (TypeError, ValueError):
            continue
        if isinstance(members, list):
            eligible.append((cluster_id, members))

    for cluster_id, members in sorted(eligible, key=lambda item: item[0]):
        for member in members:
            normalized = _normalized_cache_member(member)
            if normalized is None:
                continue
            if (
                normalized["day"] == day
                and normalized["stream"] == stream
                and normalized["segment_key"] == segment_key
                and normalized["source"] == source
                and normalized["sentence_id"] == sentence_id
            ):
                return {"status": "hit", "cluster_id": cluster_id}

    return {"status": "miss", "cluster_id": None}


def _voiceprints_exist(entity_id: str) -> bool:
    return (Path(get_journal()) / "entities" / entity_id / "voiceprints.npz").exists()


def _presence_candidate(
    entity_id: str,
    buckets: dict[str, set],
) -> dict[str, Any] | None:
    entity = load_journal_entity(entity_id)
    if entity is None or entity.get("blocked"):
        return None
    return {
        "entity_id": entity_id,
        "name": entity["name"],
        "has_voice": _voiceprints_exist(entity_id),
        "screen_conversations": len(buckets["screen"]),
        "meeting_days": len(buckets["meeting_day"]),
        "setting_conversations": len(buckets["setting"]),
        "speaker_conversations": len(buckets["speakers"]),
    }


def get_cluster_presence(cluster_id: int) -> dict[str, Any] | None:
    """Return read-only co-presence evidence for a discovered cluster."""
    cache = load_discovery_cache()
    if cache is None:
        return None
    members = cache.get("clusters", {}).get(str(cluster_id))
    if not isinstance(members, list) or not members:
        return None

    _, load_speaker_labels, _, _, _ = _routes_helpers()
    conversation_context = _cluster_conversation_context(members)
    distinct_segments = list(conversation_context.distinct_segments)
    first_record_by_segment = conversation_context.first_record_by_segment
    segment_settings = conversation_context.segment_settings
    conversation_keys = conversation_context.conversation_keys

    samples: list[dict[str, Any]] = []
    for segment in distinct_segments[:3]:
        sample = _build_cluster_sample(first_record_by_segment[segment])
        sample["setting"] = segment_settings[segment]
        samples.append(sample)

    entity_buckets: dict[str, dict[str, set]] = defaultdict(
        lambda: {
            "screen": set(),
            "meeting_day": set(),
            "setting": set(),
            "speakers": set(),
        }
    )
    evidence_gaps: list[dict[str, Any]] = []

    for day, stream, segment_key in distinct_segments:
        seg_dir = segment_path(day, segment_key, stream, create=False)
        labels = load_speaker_labels(seg_dir)
        if isinstance(labels, dict) and "candidate_evidence" in labels:
            evidence = labels.get("candidate_evidence") or []
            seg_gaps = labels.get("candidate_evidence_gaps") or []
        else:
            evidence, seg_gaps = compute_segment_candidate_evidence_readonly(
                day,
                stream,
                segment_key,
            )

        for gap in seg_gaps:
            if isinstance(gap, dict):
                evidence_gaps.append(
                    {"day": day, "stream": stream, "segment_key": segment_key, **gap}
                )

        conversation_key = conversation_keys[(day, stream, segment_key)]
        for item in evidence:
            if not isinstance(item, dict):
                continue
            entity_id = item.get("entity_id")
            sources = item.get("sources") or []
            if not entity_id or not isinstance(sources, list):
                continue
            buckets = entity_buckets[str(entity_id)]
            for source in sources:
                if source == "screen":
                    buckets["screen"].add(conversation_key)
                elif source == "meeting_day":
                    buckets["meeting_day"].add(day)
                elif source == "setting":
                    buckets["setting"].add(conversation_key)
                elif source == "speakers":
                    buckets["speakers"].add(conversation_key)

    principal = get_journal_principal()
    principal_id = principal.get("id") if isinstance(principal, dict) else None
    candidates: list[dict[str, Any]] = []
    for entity_id, buckets in entity_buckets.items():
        if entity_id == principal_id:
            continue
        candidate = _presence_candidate(entity_id, buckets)
        if candidate is not None:
            candidates.append(candidate)

    co_presence = [
        candidate
        for candidate in candidates
        if candidate["screen_conversations"] > 0 or candidate["meeting_days"] > 0
    ]
    co_presence.sort(
        key=lambda candidate: (
            -candidate["screen_conversations"],
            -candidate["meeting_days"],
            candidate["name"],
            candidate["entity_id"],
        )
    )

    mention = [
        candidate
        for candidate in candidates
        if candidate not in co_presence
        and (
            candidate["setting_conversations"] > 0
            or candidate["speaker_conversations"] > 0
        )
    ]
    mention.sort(
        key=lambda candidate: (
            -candidate["setting_conversations"],
            -candidate["speaker_conversations"],
            candidate["name"],
            candidate["entity_id"],
        )
    )

    days = {day for day, _stream, _segment_key in distinct_segments}
    streams = {stream for _day, stream, _segment_key in distinct_segments}

    return {
        "cluster_id": cluster_id,
        "facts": {
            "statement_count": len(members),
            "segment_count": len(distinct_segments),
            "day_count": len(days),
            "streams": sorted(streams),
            "conversation_count": conversation_context.conversation_count,
            "samples": samples,
        },
        "evidence_complete": len(evidence_gaps) == 0,
        "evidence_gaps": evidence_gaps,
        "candidates": {
            "co_presence": co_presence,
            "mention": mention,
        },
    }


def _maybe_inject_identify_fault(stage: str) -> None:
    return None


@dataclass(frozen=True)
class _PlannedIdentify:
    operation_id: str
    request_id: str
    request_fingerprint: str
    prepared_plan: dict[str, Any]


class _IdentifyRepairRequired(RuntimeError):
    def __init__(
        self,
        phase: str,
        repair_code: str,
        repair_categories: dict[str, int],
    ) -> None:
        super().__init__(repair_code)
        self.phase = phase
        self.repair_code = repair_code
        self.repair_categories = repair_categories


def _utc_iso() -> str:
    return (
        datetime.now(timezone.utc).isoformat(timespec="seconds").replace("+00:00", "Z")
    )


def _copy_jsonable(value: Any) -> Any:
    return json.loads(json.dumps(value))


def _member_tuple(member: dict[str, Any]) -> tuple[str, str, str, str, int]:
    return (
        str(member["day"]),
        str(member["stream"]),
        str(member["segment_key"]),
        str(member["source"]),
        int(member["sentence_id"]),
    )


def _key_tuple(key: dict[str, Any]) -> tuple[str, str, str, int]:
    return (
        str(key["day"]),
        str(key["segment_key"]),
        str(key["source"]),
        int(key["sentence_id"]),
    )


def _key_dict(day: str, segment_key: str, source: str, sentence_id: int) -> dict:
    return {
        "day": day,
        "segment_key": segment_key,
        "source": source,
        "sentence_id": int(sentence_id),
    }


def _sentence_key(segment: dict[str, Any], sentence_id: int) -> dict[str, Any]:
    return {
        "day": segment["day"],
        "stream": segment["stream"],
        "segment_key": segment["segment_key"],
        "sentence_id": int(sentence_id),
    }


def _canonical_members(cluster_members: list[dict[str, Any]]) -> list[dict[str, Any]]:
    return [
        {
            "day": day,
            "stream": stream,
            "segment_key": segment_key,
            "source": source,
            "sentence_id": sentence_id,
        }
        for day, stream, segment_key, source, sentence_id in sorted(
            _member_tuple(member) for member in cluster_members
        )
    ]


def _load_segment_corrections(seg_dir: Path) -> list[dict[str, Any]]:
    path = seg_dir / "talents" / "speaker_corrections.json"
    if not path.is_file():
        return []
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (json.JSONDecodeError, OSError):
        return []
    corrections = data.get("corrections", []) if isinstance(data, dict) else []
    return [row for row in corrections if isinstance(row, dict)]


def _load_resolved_clusters() -> dict[str, Any]:
    path = _discovery_resolved_path(create=False)
    if not path.is_file():
        return {}
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (json.JSONDecodeError, OSError):
        return {}
    return data if isinstance(data, dict) else {}


def _replace_resolved_clusters(data: dict[str, Any]) -> None:
    path = _discovery_resolved_path(create=True)
    atomic_replace(path, json.dumps(data, indent=2, sort_keys=True))


def _history_refs_for_entity(entity_id: str, operation_id: str) -> list[dict[str, Any]]:
    from solstone.think.entities import iter_entity_history

    refs: list[dict[str, Any]] = []
    for event in iter_entity_history(entity_id):
        operation = event.get("operation")
        if not isinstance(operation, dict):
            continue
        if operation.get("operation_id") != operation_id:
            continue
        refs.append(
            {
                "version_id": event.get("version_id"),
                "seq": event.get("seq"),
                "path": (
                    f"entities/{entity_id}/history/events/"
                    f"{int(event['seq']):020d}-{event['version_id']}.json"
                ),
            }
        )
    return refs


def _meaningful_identity(entity: dict[str, Any]) -> dict[str, Any]:
    fields = ("id", "name", "type", "aka", "emails", "is_principal", "blocked")
    return {field: entity.get(field) for field in fields if field in entity}


def _identity_hash(entity: dict[str, Any] | None) -> str:
    import hashlib

    encoded = json.dumps(
        _meaningful_identity(entity or {}),
        sort_keys=True,
        separators=(",", ":"),
    )
    return hashlib.sha256(encoded.encode("utf-8")).hexdigest()


def _load_embedding_for_member(member: dict[str, Any]) -> Any | None:
    (
        load_embeddings_file,
        _load_speaker_labels,
        normalize_embedding,
        _scan,
        check_owner_contamination,
    ) = _routes_helpers()
    day = str(member["day"])
    stream = str(member["stream"])
    segment_key = str(member["segment_key"])
    source = str(member["source"])
    sentence_id = int(member["sentence_id"])
    seg_dir = segment_path(day, segment_key, stream, create=False)
    emb_data = load_embeddings_file(seg_dir / f"{source}.npz")
    if emb_data is None:
        return None
    embeddings, statement_ids, _durations = emb_data
    for embedding, sid in zip(embeddings, statement_ids):
        if int(sid) != sentence_id:
            continue
        normalized = normalize_embedding(embedding)
        if normalized is None or check_owner_contamination(normalized):
            return None
        return normalized
    return None


def _planned_voiceprint_items(
    entries: list[dict[str, Any]],
) -> list[tuple[Any, dict[str, Any]]]:
    items: list[tuple[Any, dict[str, Any]]] = []
    for entry in entries:
        embedding = _load_embedding_for_member(entry["source_member"])
        if embedding is None:
            raise _IdentifyRepairRequired(
                "direct_voiceprints",
                "source_embedding_unavailable",
                {"direct_voiceprint": 1},
            )
        items.append((embedding, dict(entry["metadata"])))
    return items


def _entity_voiceprint_metadata(
    entity_id: str,
) -> dict[tuple[str, str, str, int], list[dict]]:
    from solstone.think.entities import load_entity_voiceprints_file

    loaded = load_entity_voiceprints_file(entity_id)
    if loaded is None:
        return {}
    _embeddings, metadata = loaded
    by_key: dict[tuple[str, str, str, int], list[dict]] = defaultdict(list)
    for meta in metadata:
        if not isinstance(meta, dict):
            continue
        try:
            by_key[
                (
                    str(meta["day"]),
                    str(meta["segment_key"]),
                    str(meta["source"]),
                    int(meta["sentence_id"]),
                )
            ].append(meta)
        except (KeyError, TypeError, ValueError):
            continue
    return by_key


def _direct_voiceprint_plan(
    target_id: str,
    cluster_members: list[dict[str, Any]],
    *,
    added_at: int,
) -> tuple[dict[str, Any], list[tuple[Any, dict[str, Any]]]]:
    from solstone.think.entities import load_existing_voiceprint_keys

    existing_keys = load_existing_voiceprint_keys(target_id)
    working_keys = set(existing_keys)
    entries_to_add: list[dict[str, Any]] = []
    items: list[tuple[Any, dict[str, Any]]] = []
    for member in _canonical_members(cluster_members):
        day, stream, segment_key, source, sentence_id = _member_tuple(member)
        key = (day, segment_key, source, sentence_id)
        if key in working_keys:
            continue
        embedding = _load_embedding_for_member(member)
        if embedding is None:
            continue
        metadata = {
            "day": day,
            "segment_key": segment_key,
            "source": source,
            "stream": stream,
            "sentence_id": sentence_id,
            "added_at": added_at,
        }
        entries_to_add.append(
            {
                "key": _key_dict(day, segment_key, source, sentence_id),
                "metadata": metadata,
                "source_member": dict(member),
            }
        )
        items.append((embedding, metadata))
        working_keys.add(key)
    return (
        {
            "preexisting_keys": [
                _key_dict(day, segment_key, source, sentence_id)
                for day, segment_key, source, sentence_id in sorted(existing_keys)
            ],
            "entries_to_add": entries_to_add,
        },
        items,
    )


def _segment_plans(
    target_id: str,
    cluster_members: list[dict[str, Any]],
    *,
    timestamp: int,
    operation_id: str,
) -> list[dict[str, Any]]:
    _load_embeddings_file, load_speaker_labels, _normalize, _scan, _check = (
        _routes_helpers()
    )
    grouped: dict[tuple[str, str, str], set[int]] = defaultdict(set)
    sources_by_segment: dict[tuple[str, str, str], set[str]] = defaultdict(set)
    for member in cluster_members:
        day, stream, segment_key, source, sentence_id = _member_tuple(member)
        grouped[(day, stream, segment_key)].add(sentence_id)
        sources_by_segment[(day, stream, segment_key)].add(source)

    plans: list[dict[str, Any]] = []
    for day, stream, segment_key in sorted(grouped):
        seg_dir = day_path(day, create=False) / stream / segment_key
        if not seg_dir.is_dir():
            continue
        labels_data = load_speaker_labels(seg_dir) or {"labels": []}
        labels_by_sid: dict[int, dict[str, Any]] = {}
        for label in labels_data.get("labels", []):
            if not isinstance(label, dict):
                continue
            sentence_id = label.get("sentence_id")
            if sentence_id is not None:
                labels_by_sid[int(sentence_id)] = dict(label)

        existing_corrections = _load_segment_corrections(seg_dir)
        existing_keys = [
            {
                "sentence_id": int(correction["sentence_id"]),
                "corrected_speaker": correction.get("corrected_speaker"),
            }
            for correction in existing_corrections
            if correction.get("sentence_id") is not None
        ]
        existing_key_set = {
            (key["sentence_id"], key.get("corrected_speaker")) for key in existing_keys
        }
        labels: list[dict[str, Any]] = []
        rows_to_append: list[dict[str, Any]] = []
        for sentence_id in sorted(grouped[(day, stream, segment_key)]):
            prior = labels_by_sid.get(sentence_id)
            intended = {
                "sentence_id": sentence_id,
                "speaker": target_id,
                "confidence": "high",
                "method": "user_identified",
            }
            labels.append(
                {
                    "sentence_id": sentence_id,
                    "prior_state": "present" if prior is not None else "absent",
                    "prior_label": copy.deepcopy(prior) if prior is not None else None,
                    "intended_label": intended,
                }
            )
            if (sentence_id, target_id) not in existing_key_set:
                original = prior or {}
                rows_to_append.append(
                    {
                        "sentence_id": sentence_id,
                        "original_speaker": original.get("speaker"),
                        "corrected_speaker": target_id,
                        "original_method": original.get("method"),
                        "timestamp": timestamp,
                        "operation_id": operation_id,
                        "correction_kind": "identify",
                    }
                )
        plans.append(
            {
                "day": day,
                "stream": stream,
                "segment_key": segment_key,
                "source": sorted(sources_by_segment[(day, stream, segment_key)])[0],
                "sources": sorted(sources_by_segment[(day, stream, segment_key)]),
                "labels": labels,
                "corrections": {
                    "existing_keys": existing_keys,
                    "rows_to_append": rows_to_append,
                },
            }
        )
    return plans


def _serializable_retro_plan(retro_plan: Any | None, entity_id: str) -> dict[str, Any]:
    if retro_plan is None:
        return {
            "matched": False,
            "match_score": None,
            "candidate_id": None,
            "candidate_before": None,
            "candidate_after": None,
            "preexisting_voiceprint_keys": [],
            "voiceprints_to_add": [],
        }
    return {
        "matched": bool(retro_plan.matched),
        "match_score": retro_plan.match_score,
        "candidate_id": retro_plan.candidate_id,
        "candidate_before": _copy_jsonable(retro_plan.candidate_before),
        "candidate_after": _copy_jsonable(retro_plan.candidate_after),
        "preexisting_voiceprint_keys": [
            _key_dict(day, segment_key, source, sentence_id)
            for day, segment_key, source, sentence_id in retro_plan.preexisting_voiceprint_keys
        ],
        "voiceprints_to_add": [
            _copy_jsonable(entry) for entry in retro_plan.voiceprints_to_add
        ],
        "entity_id": entity_id,
    }


def _retro_plan_from_prepared(prepared_plan: dict[str, Any]) -> Any:
    from solstone.apps.speakers.candidate_tracker import RetroactiveConfirmPlan

    retro = prepared_plan["retro_confirm"]
    items: list[tuple[Any, dict[str, Any]]] = []
    for entry in retro.get("voiceprints_to_add", []):
        metadata = dict(entry["metadata"])
        member = {
            "day": metadata["day"],
            "stream": metadata["stream"],
            "segment_key": metadata["segment_key"],
            "source": metadata["source"],
            "sentence_id": metadata["sentence_id"],
        }
        embedding = _load_embedding_for_member(member)
        if embedding is None:
            raise _IdentifyRepairRequired(
                "retro_tracker",
                "source_embedding_unavailable",
                {"retro_voiceprint": 1},
            )
        items.append((embedding, metadata))
    return RetroactiveConfirmPlan(
        matched=bool(retro.get("matched")),
        match_score=retro.get("match_score"),
        candidate_id=retro.get("candidate_id"),
        entity_id=prepared_plan["target"]["entity_id"],
        candidate_before=retro.get("candidate_before"),
        candidate_after=retro.get("candidate_after"),
        preexisting_voiceprint_keys=tuple(
            _key_tuple(key) for key in retro.get("preexisting_voiceprint_keys", [])
        ),
        voiceprints_to_add=tuple(retro.get("voiceprints_to_add", [])),
        voiceprint_items_to_add=tuple(items),
    )


def _current_name_variant_detection_count(entity_id_a: str, entity_id_b: str) -> int:
    from solstone.think.speaker_review_candidates import find_candidate, load_candidates

    row = find_candidate(load_candidates(), entity_id_a, entity_id_b)
    if not row:
        return 1
    evidence = row.get("evidence")
    if not isinstance(evidence, dict):
        return 1
    try:
        return max(1, int(evidence.get("detection_count", 1)))
    except (TypeError, ValueError):
        return 1


def _normalize_reviewed_near_match_ids(
    value: Any,
) -> tuple[list[str], dict[str, Any] | None]:
    if value is None:
        return [], None
    if not isinstance(value, list):
        return [], {
            "status": "invalid_request",
            "error": "reviewed_near_match_entity_ids must be a list",
        }
    seen: set[str] = set()
    result: list[str] = []
    for item in value:
        if not isinstance(item, str) or not item.strip():
            return [], {
                "status": "invalid_request",
                "error": "reviewed_near_match_entity_ids must contain strings",
            }
        entity_id = item.strip()
        if entity_id in seen:
            return [], {
                "status": "invalid_request",
                "error": "reviewed_near_match_entity_ids must be unique",
                "invalid_reviewed_near_match_entity_ids": [
                    {"entity_id": entity_id, "reason": "duplicate"}
                ],
            }
        seen.add(entity_id)
        result.append(entity_id)
    return result, None


def _visible_near_match_candidate_rows(
    candidates: Any,
    *,
    entities: dict[str, dict[str, Any]],
    principal_id: str,
) -> list[dict[str, Any]]:
    rows: list[dict[str, Any]] = []
    for candidate in candidates or ():
        entity_id = str(getattr(candidate, "id", "") or "")
        if (
            speaker_attach_rejection_reason(
                entity_id,
                entities,
                principal_id=principal_id,
            )
            is not None
        ):
            continue
        row = candidate.to_dict()
        row["has_voice"] = _voiceprints_exist(entity_id)
        rows.append(row)
    rows.sort(
        key=lambda row: (
            -float(row.get("score") or 0.0),
            str(row.get("name") or "").casefold(),
            str(row.get("id") or ""),
        )
    )
    return rows


def _candidate_ids(candidate_rows: list[dict[str, Any]]) -> list[str]:
    return [str(row["id"]) for row in candidate_rows]


def _validate_near_matches_for_create(
    reviewed_ids: list[str],
    *,
    target_id: str,
    entities: dict[str, dict[str, Any]],
    visible_candidate_ids: list[str],
    principal_id: str,
) -> tuple[list[dict[str, Any]], dict[str, Any] | None]:
    from solstone.think.speaker_keep_separate import pair_key

    shown = set(visible_candidate_ids)
    invalid: list[dict[str, str]] = []
    for reviewed_id in reviewed_ids:
        reason = speaker_attach_rejection_reason(
            reviewed_id,
            entities,
            target_id=target_id,
            visible_candidate_ids=shown,
            principal_id=principal_id,
        )
        if reason is not None:
            invalid.append({"entity_id": reviewed_id, "reason": reason})
    if invalid:
        return [], {
            "status": "invalid_request",
            "error": "invalid reviewed_near_match_entity_ids",
            "invalid_reviewed_near_match_entity_ids": invalid,
        }

    reviewed_set = set(reviewed_ids)
    if reviewed_set != shown:
        return [], {
            "status": "invalid_request",
            "error": "reviewed_near_match_entity_ids must match shown near matches",
            "invalid_request_code": "reviewed_near_match_set_mismatch",
            "expected_reviewed_near_match_entity_ids": sorted(shown),
            "actual_reviewed_near_match_entity_ids": sorted(reviewed_set),
        }

    assertions: list[dict[str, Any]] = []
    for reviewed_id in reviewed_ids:
        detection_count = _current_name_variant_detection_count(target_id, reviewed_id)
        key = pair_key(target_id, reviewed_id)
        left, right = key.split("|", maxsplit=1)
        assertions.append(
            {
                "pair_key": key,
                "entity_id_a": left,
                "entity_id_b": right,
                "planned_target_entity_id": target_id,
                "reviewed_id": reviewed_id,
                "prior_record": None,
                "intended_record": {
                    "pair_key": key,
                    "entity_id_a": left,
                    "entity_id_b": right,
                    "source_kind": "explicit_create_near_match",
                    "operation_id": None,
                    "detection_count": detection_count,
                },
                "detection_count_used": detection_count,
                "source_kind": "explicit_create_near_match",
            }
        )
    return assertions, None


def _plan_identify(
    cluster_id: int,
    *,
    name: str | None,
    entity_id: str | None,
    resolve_only: bool,
    create_new: bool,
    entity_type: str,
    request_id: str,
    operation_id: str,
    reviewed_near_match_entity_ids: Any,
) -> tuple[_PlannedIdentify | None, dict[str, Any] | None]:
    import numpy as np

    from solstone.apps.speakers.candidate_tracker import CandidateTracker
    from solstone.think.entities import (
        EntityResolutionOutcome,
        ResolutionOrigin,
        ResolutionScope,
        closest_resolution_candidates,
        entity_slug,
        is_valid_entity_type,
        load_all_journal_entities,
        load_journal_entity,
        record_entity_resolution,
    )

    cache_path = _discovery_cache_path()
    if not cache_path.exists():
        return None, {"error": "No discovery scan results. Run scan first."}
    cache_data = load_discovery_cache()
    if cache_data is None:
        return None, {"error": "Invalid discovery cache. Run scan again."}
    raw_members = cache_data.get("clusters", {}).get(str(cluster_id))
    if not raw_members:
        return None, {"error": f"Cluster {cluster_id} not found in scan results."}

    cluster_members = _canonical_members(raw_members)
    reviewed_ids, reviewed_error = _normalize_reviewed_near_match_ids(
        reviewed_near_match_entity_ids
    )
    if reviewed_error:
        return None, reviewed_error

    principal_id = current_principal_id()
    journal_entities: dict[str, dict[str, Any]] | None = None
    visible_candidate_rows: list[dict[str, Any]] = []
    name_value = name.strip() if isinstance(name, str) else ""
    entity_id_value = entity_id.strip() if isinstance(entity_id, str) else ""
    will_create = False
    target_type = entity_type
    if entity_id_value:
        entity = load_journal_entity(entity_id_value)
        if not entity:
            return None, {
                "error": f"Entity '{entity_id_value}' not found.",
                "not_found": True,
            }
        target_id = entity_id_value
        target_name = entity.get("name", target_id)
        target_type = str(entity.get("type") or entity_type)
    else:
        if not name_value:
            return None, {"error": "name is required"}
        journal_entities = load_all_journal_entities()
        if principal_name_collision(
            name_value,
            journal_entities,
            principal_id=principal_id,
        ):
            return None, {
                "status": "principal_match",
                "this_is_me": True,
            }
        if blocked_person_name_collision(name_value, journal_entities):
            return None, {
                "status": "invalid_request",
                "error": "name is unavailable",
            }
        entities_list = list(
            eligible_speaker_attach_entities(
                journal_entities,
                principal_id=principal_id,
            )
        )
        resolution = record_entity_resolution(
            name_value,
            entities_list,
            scope=ResolutionScope.journal(),
            origin=ResolutionOrigin(
                lane="apps.speakers.discovery.identify_cluster",
                record_id=str(cluster_id),
                field="name",
            ),
            read_only=True,
        )
        if resolution.outcome == EntityResolutionOutcome.RESOLVED and resolution.entity:
            target_id = resolution.entity["id"]
            target_name = resolution.entity.get("name", name_value)
            target_type = str(resolution.entity.get("type") or entity_type)
        else:
            candidate_source = resolution.candidates
            if resolution.outcome == EntityResolutionOutcome.NO_MATCH:
                candidate_source = closest_resolution_candidates(
                    name_value,
                    entities_list,
                )
            visible_candidate_rows = _visible_near_match_candidate_rows(
                candidate_source,
                entities=journal_entities,
                principal_id=principal_id,
            )
            if resolve_only or not create_new:
                result = {
                    "status": (
                        "ambiguous"
                        if resolution.outcome == EntityResolutionOutcome.AMBIGUOUS
                        else "no_match"
                    ),
                    "candidates": visible_candidate_rows,
                }
                if resolution.outcome == EntityResolutionOutcome.AMBIGUOUS:
                    result["ambiguity_id"] = resolution.ambiguity_id
                return None, result
            will_create = True
            target_id = entity_slug(name_value)
            target_name = name_value

    if resolve_only:
        return None, {
            "status": "resolved",
            "entity_id": target_id,
            "entity_name": target_name,
            "has_voice": _voiceprints_exist(target_id),
        }
    if reviewed_ids and not will_create:
        return None, {
            "status": "invalid_request",
            "error": "reviewed_near_match_entity_ids is only valid for create",
        }
    if will_create and not is_valid_entity_type(entity_type):
        return None, {
            "error": f"Invalid entity type: {entity_type}",
            "invalid_entity_type": True,
        }

    if journal_entities is None:
        journal_entities = load_all_journal_entities()
    keep_separate_assertions, near_match_error = _validate_near_matches_for_create(
        reviewed_ids,
        target_id=target_id,
        entities=journal_entities,
        visible_candidate_ids=_candidate_ids(visible_candidate_rows),
        principal_id=principal_id,
    )
    if near_match_error:
        return None, near_match_error

    planned_at = _utc_iso()
    added_at = now_ms()
    direct_voiceprints, direct_items = _direct_voiceprint_plan(
        target_id,
        cluster_members,
        added_at=added_at,
    )
    retro_plan = None
    if direct_items:
        _load_embeddings_file, _load_labels, normalize_embedding, _scan, _check = (
            _routes_helpers()
        )
        centroid = normalize_embedding(
            np.mean([embedding for embedding, _meta in direct_items], axis=0)
        )
        if centroid is not None:
            retro_plan = CandidateTracker().plan_retroactive_confirm(
                centroid,
                target_id,
            )

    resolved = _load_resolved_clusters()
    cluster_key = str(cluster_id)
    intended_identity = {
        "id": target_id,
        "name": target_name,
        "type": entity_type if will_create else target_type,
    }
    if not will_create:
        intended_identity = load_journal_entity(target_id) or intended_identity
    plan = {
        "plan_schema_version": 1,
        "operation_id": operation_id,
        "request_id": request_id,
        "planned_at": planned_at,
        "request": {
            "cluster_id": int(cluster_id),
            "name": name_value or None,
            "entity_id": entity_id_value or None,
            "resolve_only": False,
            "create_new": bool(create_new),
            "entity_type": entity_type,
            "reviewed_near_match_entity_ids": sorted(reviewed_ids),
        },
        "cluster": {
            "cluster_id": int(cluster_id),
            "member_count": len(cluster_members),
            "members": cluster_members,
        },
        "target": {
            "entity_id": target_id,
            "entity_name": target_name,
            "entity_type": entity_type if will_create else target_type,
            "will_create": will_create,
        },
        "entity_identity": {
            "prior_identity": None if will_create else load_journal_entity(target_id),
            "intended_identity": intended_identity,
            "expected_history_operation": {
                "operation_kind": "speaker_identify",
                "operation_id": operation_id,
            },
        },
        "direct_voiceprints": direct_voiceprints,
        "segments": _segment_plans(
            target_id,
            cluster_members,
            timestamp=added_at,
            operation_id=operation_id,
        ),
        "retro_confirm": _serializable_retro_plan(retro_plan, target_id),
        "sentinel": {
            "cluster_key": cluster_key,
            "prior_entry": copy.deepcopy(resolved.get(cluster_key)),
            "intended_entry": {
                "entity_id": target_id,
                "label": target_name,
                "ts": planned_at,
            },
        },
        "keep_separate_assertions": keep_separate_assertions,
    }
    for assertion in plan["keep_separate_assertions"]:
        assertion["intended_record"]["operation_id"] = operation_id

    from solstone.think.speaker_identify_operations import request_fingerprint

    fingerprint = request_fingerprint(
        cluster_members=cluster_members,
        target_entity_id=target_id,
        will_create=will_create,
        entity_type=entity_type if will_create else target_type,
        reviewed_near_match_entity_ids=reviewed_ids,
    )
    return (
        _PlannedIdentify(
            operation_id=operation_id,
            request_id=request_id,
            request_fingerprint=fingerprint,
            prepared_plan=plan,
        ),
        None,
    )


def _identify_event(
    *,
    operation_id: str,
    request_id: str,
    event_kind: str,
    ts: str | None = None,
    **fields: Any,
) -> dict[str, Any]:
    from solstone.think.speaker_identify_operations import (
        IDENTIFY_OPERATION_SCHEMA_VERSION,
    )

    return {
        "schema_version": IDENTIFY_OPERATION_SCHEMA_VERSION,
        "event_id": fields.pop("event_id"),
        "operation_id": operation_id,
        "request_id": request_id,
        "event_kind": event_kind,
        "ts": ts or _utc_iso(),
        "caller": "apps.speakers.discovery.identify_cluster",
        "actor": None,
        **fields,
    }


def _append_prepared(planned: _PlannedIdentify) -> None:
    from solstone.think.speaker_identify_operations import append_event

    append_event(
        _identify_event(
            operation_id=planned.operation_id,
            request_id=planned.request_id,
            event_kind="prepared",
            event_id=f"{planned.operation_id}:prepared",
            ts=planned.prepared_plan["planned_at"],
            request_fingerprint=planned.request_fingerprint,
            prepared_plan=planned.prepared_plan,
        )
    )


def _checkpoint_payload(checkpoint: dict[str, Any]) -> dict[str, Any]:
    payload = {
        "phase_status": "complete",
        "completed_at": _utc_iso(),
        "counts": {},
        "skipped_reasons": {},
    }
    payload.update(checkpoint)
    return payload


def _append_forward_checkpoint(
    operation_id: str,
    request_id: str,
    phase: str,
    checkpoint: dict[str, Any],
) -> None:
    from solstone.think.speaker_identify_operations import append_event

    append_event(
        _identify_event(
            operation_id=operation_id,
            request_id=request_id,
            event_kind="checkpoint",
            event_id=f"{operation_id}:checkpoint:{phase}",
            phase=phase,
            checkpoint=_checkpoint_payload(checkpoint),
        )
    )


def _append_repair_required(
    operation_id: str,
    request_id: str,
    phase: str,
    *,
    repair_code: str,
    repair_categories: dict[str, int],
) -> dict[str, Any]:
    from solstone.think.speaker_identify_operations import append_event, fold_operation

    state = fold_operation(operation_id)
    if state is not None and state.terminal_status == "repair_required":
        repair = state.repair_required or {}
        return {
            "status": "repair_required",
            "operation_id": operation_id,
            "operation_state": state.terminal_status,
            "phase": repair.get("phase", phase),
            "repair_code": repair.get("repair_code", repair_code),
            "repair_categories": repair.get("repair_categories", {}),
        }
    completed = list(state.completed_phases) if state else []
    pending = list(state.pending_phases) if state else []
    event = _identify_event(
        operation_id=operation_id,
        request_id=request_id,
        event_kind="repair_required",
        event_id=f"{operation_id}:repair_required:{phase}",
        phase=phase,
        repair_code=repair_code,
        repair_categories=repair_categories,
        partial_report={
            "completed_phases": completed,
            "pending_phases": pending,
            "counts_by_phase": {},
        },
    )
    append_event(event)
    return {
        "status": "repair_required",
        "operation_id": operation_id,
        "operation_state": "repair_required",
        "phase": phase,
        "repair_code": repair_code,
        "repair_categories": repair_categories,
        "completed_phases": completed,
        "pending_phases": pending,
    }


def _recoverable_result(operation_id: str, detail: str) -> dict[str, Any]:
    from solstone.think.speaker_identify_operations import fold_operation

    state = fold_operation(operation_id)
    return {
        "status": "recoverable",
        "operation_id": operation_id,
        "operation_state": state.terminal_status if state else "not_prepared",
        "request_id": state.request_id if state else None,
        "completed_phases": list(state.completed_phases) if state else [],
        "pending_phases": list(state.pending_phases) if state else [],
        "detail": detail,
    }


def _stored_request_matches_raw(
    prepared_plan: dict[str, Any],
    *,
    cluster_id: int,
    name: str | None,
    entity_id: str | None,
    create_new: bool,
    entity_type: str,
    reviewed_near_match_entity_ids: Any,
) -> bool:
    reviewed_ids, reviewed_error = _normalize_reviewed_near_match_ids(
        reviewed_near_match_entity_ids
    )
    if reviewed_error is not None:
        return False
    raw_request = {
        "cluster_id": int(cluster_id),
        "name": name.strip() if isinstance(name, str) and name.strip() else None,
        "entity_id": (
            entity_id.strip()
            if isinstance(entity_id, str) and entity_id.strip()
            else None
        ),
        "resolve_only": False,
        "create_new": bool(create_new),
        "entity_type": entity_type,
        "reviewed_near_match_entity_ids": sorted(reviewed_ids),
    }
    return raw_request == prepared_plan.get("request")


def _state_status_result(state: Any) -> dict[str, Any]:
    if state.terminal_status == "committed" and state.result is not None:
        return copy.deepcopy(state.result)
    if state.terminal_status == "repair_required":
        repair = state.repair_required or {}
        return {
            "status": "repair_required",
            "operation_id": state.operation_id,
            "operation_state": state.terminal_status,
            "phase": repair.get("phase"),
            "repair_code": repair.get("repair_code"),
            "repair_categories": repair.get("repair_categories", {}),
            "completed_phases": list(state.completed_phases),
            "pending_phases": list(state.pending_phases),
        }
    if state.terminal_status == "undone":
        return {
            "status": "operation_already_undone",
            "operation_id": state.operation_id,
            "operation_state": state.terminal_status,
        }
    if state.terminal_status == "undo_repair_required":
        repair = state.undo_repair_required or {}
        return {
            "status": "undo_repair_required",
            "operation_id": state.operation_id,
            "operation_state": state.terminal_status,
            "phase": repair.get("phase"),
            "repair_code": repair.get("repair_code"),
            "repair_categories": repair.get("repair_categories", {}),
            "undo_report": repair.get("undo_report"),
        }
    if state.terminal_status == "undoing":
        return {
            "status": "undoing",
            "operation_id": state.operation_id,
            "operation_state": state.terminal_status,
            "completed_phases": list(state.completed_phases),
            "pending_phases": list(state.pending_phases),
            "undo_report": _aggregate_undo_report(state, status="undoing")[
                "undo_report"
            ],
        }
    return {
        "status": "in_progress",
        "operation_id": state.operation_id,
        "operation_state": state.terminal_status,
        "completed_phases": list(state.completed_phases),
        "pending_phases": list(state.pending_phases),
    }


def _fingerprint_conflict_result(operation_id: str, state: Any) -> dict[str, Any]:
    return {
        "status": "conflict",
        "operation_id": operation_id,
        "operation_state": state.terminal_status,
        "conflict_code": "request_fingerprint_mismatch",
    }


def _phase_entity(prepared_plan: dict[str, Any]) -> dict[str, Any]:
    from solstone.think.entities import (
        EntityOperationContext,
        create_journal_entity,
        load_journal_entity,
    )

    target = prepared_plan["target"]
    identity_plan = prepared_plan["entity_identity"]
    target_id = target["entity_id"]
    will_create = bool(target["will_create"])
    entity_created = False
    if will_create:
        expected_identity = identity_plan["intended_identity"]
        current = load_journal_entity(target_id)
        if current is None:
            create_journal_entity(
                entity_id=target_id,
                name=str(expected_identity["name"]),
                entity_type=str(expected_identity["type"]),
                operation=EntityOperationContext(
                    kind="create",
                    caller="apps.speakers.discovery.identify_cluster",
                    metadata={
                        "operation_kind": "speaker_identify",
                        "operation_id": prepared_plan["operation_id"],
                    },
                ),
                skip_principal=True,
            )
            entity_created = True
            current = load_journal_entity(target_id)
        elif _meaningful_identity(current) != _meaningful_identity(expected_identity):
            raise _IdentifyRepairRequired(
                "entity",
                "concurrent_change",
                {"concurrent_change": 1},
            )
        else:
            entity_created = True
        history_refs = _history_refs_for_entity(
            target_id,
            prepared_plan["operation_id"],
        )
        if len(history_refs) != 1:
            raise _IdentifyRepairRequired(
                "entity",
                "concurrent_change",
                {"concurrent_change": 1},
            )
    else:
        current = load_journal_entity(target_id)
        history_refs = []

    if current is None:
        raise _IdentifyRepairRequired("entity", "entity_missing", {"entity": 1})
    return {
        "entity_id": target_id,
        "entity_created": entity_created,
        "identity_after_hash": _identity_hash(current),
        "identity_after": _copy_jsonable(current),
        "history_event_refs": history_refs,
        "counts": {"entity_created": int(entity_created)},
        "skipped_reasons": {},
    }


def _phase_keep_separate(prepared_plan: dict[str, Any]) -> dict[str, Any]:
    from solstone.think.speaker_keep_separate import (
        find_assertion,
        record_keep_separate_assertion,
    )

    recorded = 0
    already = 0
    pair_keys: list[str] = []
    for assertion in prepared_plan.get("keep_separate_assertions", []):
        pair_keys.append(str(assertion["pair_key"]))
        folded = find_assertion(assertion["entity_id_a"], assertion["entity_id_b"])
        source_present = False
        if folded is not None:
            source_present = any(
                source.get("source_kind") == assertion["source_kind"]
                and source.get("operation_id") == prepared_plan["operation_id"]
                for source in folded.sources
            )
        if source_present:
            already += 1
            continue
        record_keep_separate_assertion(
            assertion["entity_id_a"],
            assertion["entity_id_b"],
            source_kind=assertion["source_kind"],
            operation_id=prepared_plan["operation_id"],
            detection_count=int(assertion["detection_count_used"]),
        )
        recorded += 1
    return {
        "pair_keys": sorted(set(pair_keys)),
        "recorded_count": recorded,
        "already_present_count": already,
        "counts": {"recorded": recorded, "already_present": already},
        "skipped_reasons": {},
    }


def _phase_direct_voiceprints(prepared_plan: dict[str, Any]) -> dict[str, Any]:
    from solstone.think.entities import save_voiceprints_batch

    target_id = prepared_plan["target"]["entity_id"]
    entries = prepared_plan["direct_voiceprints"]["entries_to_add"]
    existing_metadata = _entity_voiceprint_metadata(target_id)
    to_save_entries: list[dict[str, Any]] = []
    saved_keys: list[dict[str, Any]] = []
    skipped_existing = 0
    for entry in entries:
        key = _key_tuple(entry["key"])
        rows = existing_metadata.get(key, [])
        if rows:
            if any(row == entry["metadata"] for row in rows):
                saved_keys.append(dict(entry["key"]))
                continue
            raise _IdentifyRepairRequired(
                "direct_voiceprints",
                "voiceprint_metadata_mismatch",
                {"voiceprint": 1},
            )
        to_save_entries.append(entry)
    if to_save_entries:
        save_voiceprints_batch(
            target_id,
            _planned_voiceprint_items(to_save_entries),
        )
        saved_keys.extend(dict(entry["key"]) for entry in to_save_entries)
    skipped_existing = max(0, len(entries) - len(saved_keys))
    return {
        "saved_keys": sorted(saved_keys, key=lambda key: tuple(key.values())),
        "saved_count": len(saved_keys),
        "skipped_existing_count": skipped_existing,
        "counts": {"saved": len(saved_keys)},
        "skipped_reasons": {"existing": skipped_existing},
    }


def _phase_corrections(prepared_plan: dict[str, Any]) -> dict[str, Any]:
    from solstone.apps.speakers.attribution import append_speaker_correction

    appended_keys: list[dict[str, Any]] = []
    appended_count = 0
    skipped_existing = 0
    segment_count = 0
    for segment in prepared_plan["segments"]:
        seg_dir = segment_path(
            segment["day"],
            segment["segment_key"],
            segment["stream"],
            create=False,
        )
        existing = _load_segment_corrections(seg_dir)
        segment_appended = 0
        for row in segment["corrections"]["rows_to_append"]:
            natural_match = [
                existing_row
                for existing_row in existing
                if existing_row.get("sentence_id") == row["sentence_id"]
                and existing_row.get("corrected_speaker") == row["corrected_speaker"]
            ]
            if natural_match:
                if any(
                    existing_row.get("operation_id") == prepared_plan["operation_id"]
                    and existing_row.get("correction_kind") == "identify"
                    for existing_row in natural_match
                ):
                    appended_keys.append(_sentence_key(segment, row["sentence_id"]))
                    segment_appended += 1
                else:
                    skipped_existing += 1
                continue
            append_speaker_correction(seg_dir, dict(row))
            existing.append(dict(row))
            appended_keys.append(_sentence_key(segment, row["sentence_id"]))
            appended_count += 1
            segment_appended += 1
        if segment_appended:
            segment_count += 1
    return {
        "appended_keys": sorted(appended_keys, key=lambda item: tuple(item.values())),
        "appended_count": len(appended_keys),
        "skipped_existing_count": skipped_existing,
        "segment_count": segment_count,
        "counts": {"appended": len(appended_keys)},
        "skipped_reasons": {"existing": skipped_existing},
    }


def _phase_labels(prepared_plan: dict[str, Any]) -> dict[str, Any]:
    from solstone.apps.speakers.attribution import apply_label_patches

    _load_embeddings_file, load_speaker_labels, _normalize, _scan, _check = (
        _routes_helpers()
    )
    patched_keys: list[dict[str, Any]] = []
    inserted_keys: list[dict[str, Any]] = []
    already_intended = 0
    segment_count = 0
    for segment in prepared_plan["segments"]:
        seg_dir = segment_path(
            segment["day"],
            segment["segment_key"],
            segment["stream"],
            create=False,
        )
        labels_data = load_speaker_labels(seg_dir) or {"labels": []}
        current_by_sid: dict[int, dict[str, Any]] = {}
        for label in labels_data.get("labels", []):
            if isinstance(label, dict) and label.get("sentence_id") is not None:
                current_by_sid[int(label["sentence_id"])] = dict(label)

        patches: dict[int, dict[str, Any]] = {}
        actual_segment_changed = False
        for item in segment["labels"]:
            sid = int(item["sentence_id"])
            current = current_by_sid.get(sid)
            intended = item["intended_label"]
            prior_state = item["prior_state"]
            prior = item.get("prior_label")
            changed_by_plan = prior != intended
            if current == intended:
                if changed_by_plan:
                    if prior_state == "absent":
                        inserted_keys.append(_sentence_key(segment, sid))
                    else:
                        patched_keys.append(_sentence_key(segment, sid))
                    actual_segment_changed = True
                else:
                    already_intended += 1
                continue
            if prior_state == "absent" and current is None:
                patches[sid] = {
                    "speaker": intended["speaker"],
                    "confidence": intended["confidence"],
                    "method": intended["method"],
                }
                inserted_keys.append(_sentence_key(segment, sid))
                actual_segment_changed = True
                continue
            if prior_state == "present" and current == prior:
                patches[sid] = {
                    "speaker": intended["speaker"],
                    "confidence": intended["confidence"],
                    "method": intended["method"],
                }
                patched_keys.append(_sentence_key(segment, sid))
                actual_segment_changed = True
                continue
            raise _IdentifyRepairRequired(
                "labels",
                "concurrent_change",
                {"segment_label": 1, "concurrent_change": 1},
            )
        if patches:
            apply_label_patches(seg_dir, patches, allow_insert=True)
        if actual_segment_changed:
            segment_count += 1
    return {
        "patched_sentence_keys": sorted(
            patched_keys, key=lambda item: tuple(item.values())
        ),
        "inserted_sentence_keys": sorted(
            inserted_keys, key=lambda item: tuple(item.values())
        ),
        "patched_count": len(patched_keys),
        "inserted_count": len(inserted_keys),
        "skipped_already_intended_count": already_intended,
        "segment_count": segment_count,
        "counts": {"patched": len(patched_keys), "inserted": len(inserted_keys)},
        "skipped_reasons": {"already_intended": already_intended},
    }


def _phase_retro_tracker(prepared_plan: dict[str, Any]) -> dict[str, Any]:
    from solstone.apps.speakers.candidate_tracker import CandidateTracker

    retro = prepared_plan["retro_confirm"]
    if not retro.get("matched") or retro.get("candidate_id") is None:
        return {
            "matched": False,
            "candidate_id": None,
            "saved_keys": [],
            "voiceprints_saved_count": 0,
            "voiceprints_skipped_existing_count": 0,
            "tracker_updated": False,
            "counts": {},
            "skipped_reasons": {},
        }
    target_id = prepared_plan["target"]["entity_id"]
    existing_metadata = _entity_voiceprint_metadata(target_id)
    saved_keys: list[dict[str, Any]] = []
    skipped_existing = 0
    for entry in retro.get("voiceprints_to_add", []):
        key = _key_tuple(entry["key"])
        rows = existing_metadata.get(key, [])
        if not rows:
            saved_keys.append(dict(entry["key"]))
            continue
        if any(row == entry["metadata"] for row in rows):
            saved_keys.append(dict(entry["key"]))
            continue
        raise _IdentifyRepairRequired(
            "retro_tracker",
            "voiceprint_metadata_mismatch",
            {"voiceprint": 1},
        )
    tracker = CandidateTracker()
    candidate = {
        item.cand_id: item.to_json() for item in tracker.load_all_candidates()
    }.get(int(retro["candidate_id"]))
    if candidate is None:
        raise _IdentifyRepairRequired(
            "retro_tracker",
            "candidate_missing",
            {"speaker_candidate": 1},
        )
    if candidate not in (retro.get("candidate_before"), retro.get("candidate_after")):
        raise _IdentifyRepairRequired(
            "retro_tracker",
            "concurrent_change",
            {"speaker_candidate": 1, "concurrent_change": 1},
        )
    tracker_updated = retro.get("candidate_before") != retro.get("candidate_after")
    if tracker_updated or retro.get("voiceprints_to_add"):
        tracker.apply_retroactive_confirm_plan(_retro_plan_from_prepared(prepared_plan))
        existing_metadata = _entity_voiceprint_metadata(target_id)
        saved_keys = [
            dict(entry["key"])
            for entry in retro.get("voiceprints_to_add", [])
            if any(
                row == entry["metadata"]
                for row in existing_metadata.get(_key_tuple(entry["key"]), [])
            )
        ]
    skipped_existing = max(
        0, len(retro.get("voiceprints_to_add", [])) - len(saved_keys)
    )
    return {
        "matched": True,
        "candidate_id": int(retro["candidate_id"]),
        "saved_keys": sorted(saved_keys, key=lambda key: tuple(key.values())),
        "voiceprints_saved_count": len(saved_keys),
        "voiceprints_skipped_existing_count": skipped_existing,
        "tracker_updated": tracker_updated,
        "counts": {"saved": len(saved_keys), "tracker_updated": int(tracker_updated)},
        "skipped_reasons": {"existing": skipped_existing},
    }


def _phase_sentinel(prepared_plan: dict[str, Any]) -> dict[str, Any]:
    sentinel = prepared_plan["sentinel"]
    cluster_key = str(sentinel["cluster_key"])
    prior = sentinel.get("prior_entry")
    intended = sentinel["intended_entry"]
    data = _load_resolved_clusters()
    current = data.get(cluster_key)
    if current == intended:
        written = True
    elif current == prior or (prior is None and current is None):
        data[cluster_key] = intended
        _replace_resolved_clusters(data)
        written = True
    else:
        raise _IdentifyRepairRequired(
            "sentinel",
            "concurrent_change",
            {"sentinel": 1, "concurrent_change": 1},
        )
    return {
        "cluster_key": cluster_key,
        "written": written,
        "counts": {"written": int(written)},
        "skipped_reasons": {},
    }


_FORWARD_PHASES = {
    "entity": _phase_entity,
    "keep_separate": _phase_keep_separate,
    "direct_voiceprints": _phase_direct_voiceprints,
    "corrections": _phase_corrections,
    "labels": _phase_labels,
    "retro_tracker": _phase_retro_tracker,
    "sentinel": _phase_sentinel,
}


def _forward_success_result(
    prepared_plan: dict[str, Any], checkpoints: dict[str, dict]
) -> dict[str, Any]:
    labels = checkpoints.get("labels", {})
    direct = checkpoints.get("direct_voiceprints", {})
    retro = checkpoints.get("retro_tracker", {})
    entity = checkpoints.get("entity", {})
    return {
        "status": "identified",
        "operation_id": prepared_plan["operation_id"],
        "operation_state": "committed",
        "entity_id": prepared_plan["target"]["entity_id"],
        "entity_name": prepared_plan["target"]["entity_name"],
        "entity_created": bool(entity.get("entity_created", False)),
        "voiceprints_saved": int(direct.get("saved_count", 0)),
        "retro_voiceprints_saved": int(retro.get("voiceprints_saved_count", 0)),
        "segments_updated": int(labels.get("segment_count", 0)),
        "sentences_attributed": int(labels.get("patched_count", 0))
        + int(labels.get("inserted_count", 0)),
        "corrections_appended": int(
            checkpoints.get("corrections", {}).get("appended_count", 0)
        ),
        "keep_separate_assertions_recorded": int(
            checkpoints.get("keep_separate", {}).get("recorded_count", 0)
        ),
    }


def _execute_forward(prepared_plan: dict[str, Any]) -> dict[str, Any]:
    from solstone.think.speaker_identify_operations import (
        FORWARD_PHASE_ORDER,
        append_event,
        fold_operation,
    )

    operation_id = prepared_plan["operation_id"]
    request_id = prepared_plan["request_id"]
    for phase in FORWARD_PHASE_ORDER:
        state = fold_operation(operation_id)
        if state is not None and phase in state.phase_checkpoints:
            continue
        checkpoint = _FORWARD_PHASES[phase](prepared_plan)
        _append_forward_checkpoint(operation_id, request_id, phase, checkpoint)
        _maybe_inject_identify_fault(f"after_{phase}")
    state = fold_operation(operation_id)
    checkpoints = state.phase_checkpoints if state else {}
    result = _forward_success_result(prepared_plan, checkpoints)
    append_event(
        _identify_event(
            operation_id=operation_id,
            request_id=request_id,
            event_kind="committed",
            event_id=f"{operation_id}:committed",
            result=result,
        )
    )
    _maybe_inject_identify_fault("after_committed")
    return result


def identify_cluster(
    cluster_id: int,
    name: str | None = None,
    entity_id: str | None = None,
    *,
    resolve_only: bool = False,
    create_new: bool = False,
    entity_type: str = "Person",
    request_id: str | None = None,
    reviewed_near_match_entity_ids: list[str] | None = None,
) -> dict[str, Any]:
    """Identify a discovered unknown speaker cluster."""
    from solstone.think.entities.history import trust_operation_lock
    from solstone.think.journal_io.errors import LockTimeout
    from solstone.think.speaker_identify_operations import (
        fold_all_operations,
        fold_operation,
        operation_id_for_request,
    )

    request_id_value = request_id or f"server:{uuid.uuid4().hex}"
    operation_id = operation_id_for_request(request_id_value)
    if resolve_only:
        _planned, early = _plan_identify(
            cluster_id,
            name=name,
            entity_id=entity_id,
            resolve_only=True,
            create_new=create_new,
            entity_type=entity_type,
            request_id=request_id_value,
            operation_id=operation_id,
            reviewed_near_match_entity_ids=reviewed_near_match_entity_ids,
        )
        assert early is not None
        return early

    try:
        with trust_operation_lock():
            state = fold_operation(operation_id)
            if state is not None:
                planned, early = _plan_identify(
                    cluster_id,
                    name=name,
                    entity_id=entity_id,
                    resolve_only=False,
                    create_new=create_new,
                    entity_type=entity_type,
                    request_id=request_id_value,
                    operation_id=operation_id,
                    reviewed_near_match_entity_ids=reviewed_near_match_entity_ids,
                )
                if early is None:
                    assert planned is not None
                    if state.request_fingerprint != planned.request_fingerprint:
                        return _fingerprint_conflict_result(operation_id, state)
                elif not _stored_request_matches_raw(
                    state.prepared_plan,
                    cluster_id=cluster_id,
                    name=name,
                    entity_id=entity_id,
                    create_new=create_new,
                    entity_type=entity_type,
                    reviewed_near_match_entity_ids=reviewed_near_match_entity_ids,
                ):
                    return _fingerprint_conflict_result(operation_id, state)
                if state.terminal_status != "in_progress":
                    return _state_status_result(state)
                prepared_plan = state.prepared_plan
            else:
                planned, early = _plan_identify(
                    cluster_id,
                    name=name,
                    entity_id=entity_id,
                    resolve_only=False,
                    create_new=create_new,
                    entity_type=entity_type,
                    request_id=request_id_value,
                    operation_id=operation_id,
                    reviewed_near_match_entity_ids=reviewed_near_match_entity_ids,
                )
                if early is not None:
                    return early
                assert planned is not None
                for other in fold_all_operations():
                    if (
                        other.operation_id == operation_id
                        or other.terminal_status != "committed"
                    ):
                        continue
                    if other.cluster_member_set != frozenset(
                        _member_tuple(member)
                        for member in planned.prepared_plan["cluster"]["members"]
                    ):
                        continue
                    if (
                        other.target_entity_id
                        == planned.prepared_plan["target"]["entity_id"]
                    ):
                        return (
                            copy.deepcopy(other.result)
                            if other.result
                            else {
                                "status": "identified",
                                "operation_id": other.operation_id,
                                "operation_state": "committed",
                            }
                        )
                    return {
                        "status": "conflict",
                        "operation_id": operation_id,
                        "operation_state": "not_prepared",
                        "conflict_code": "member_set_target_conflict",
                        "conflicting_operation_id": other.operation_id,
                    }
                _append_prepared(planned)
                _maybe_inject_identify_fault("after_prepared")
                prepared_plan = planned.prepared_plan
            return _execute_forward(prepared_plan)
    except LockTimeout:
        raise
    except _IdentifyRepairRequired as exc:
        return _append_repair_required(
            operation_id,
            request_id_value,
            exc.phase,
            repair_code=exc.repair_code,
            repair_categories=exc.repair_categories,
        )
    except Exception as exc:
        if isinstance(exc, LockTimeout):
            raise
        return _recoverable_result(operation_id, str(exc))


def _undo_category(**extras: Any) -> dict[str, Any]:
    result = {
        "restored_count": 0,
        "skipped_count": 0,
        "skipped_reasons": {},
    }
    result.update(extras)
    return result


def _empty_undo_report(operation_id: str, *, status: str) -> dict[str, Any]:
    return {
        "status": status,
        "operation_id": operation_id,
        "undo_report": {
            "labels": _undo_category(
                removed_inserted_count=0,
                patched_existing_count=0,
            ),
            "corrections": _undo_category(
                appended_count=0,
                already_present_count=0,
            ),
            "voiceprints": _undo_category(
                removed_count=0,
                missing_count=0,
                metadata_mismatch_count=0,
            ),
            "tracker": _undo_category(restored_candidate_count=0),
            "sentinel": _undo_category(
                removed_count=0,
                restored_prior_count=0,
            ),
            "entity": _undo_category(
                deleted=False,
                blocked_categories=[],
                keep_separate_sources_removed_count=0,
            ),
        },
    }


def _append_undo_prepared_once(operation_id: str, request_id: str) -> str:
    from solstone.think.speaker_identify_operations import append_event, load_operations

    for event in load_operations():
        if (
            event["operation_id"] == operation_id
            and event["event_kind"] == "undo_prepared"
        ):
            return str(event["undo_started_at"])
    undo_started_at = _utc_iso()
    append_event(
        _identify_event(
            operation_id=operation_id,
            request_id=request_id,
            event_kind="undo_prepared",
            event_id=f"{operation_id}:undo_prepared",
            undo_started_at=undo_started_at,
        )
    )
    return undo_started_at


def _append_undo_checkpoint(
    operation_id: str,
    request_id: str,
    phase: str,
    delta: dict[str, Any],
) -> None:
    from solstone.think.speaker_identify_operations import append_event

    append_event(
        _identify_event(
            operation_id=operation_id,
            request_id=request_id,
            event_kind="undo_checkpoint",
            event_id=f"{operation_id}:undo_checkpoint:{phase}",
            phase=phase,
            undo_report_delta=delta,
        )
    )


def _label_plan_map(
    prepared_plan: dict[str, Any],
) -> dict[tuple[str, str, str, int], tuple[dict, dict]]:
    result: dict[tuple[str, str, str, int], tuple[dict, dict]] = {}
    for segment in prepared_plan["segments"]:
        for label in segment["labels"]:
            result[
                (
                    str(segment["day"]),
                    str(segment["stream"]),
                    str(segment["segment_key"]),
                    int(label["sentence_id"]),
                )
            ] = (segment, label)
    return result


def _planned_correction_rows(
    prepared_plan: dict[str, Any],
) -> dict[tuple[str, str, str, int], tuple[dict, dict]]:
    result: dict[tuple[str, str, str, int], tuple[dict, dict]] = {}
    for segment in prepared_plan["segments"]:
        for row in segment["corrections"]["rows_to_append"]:
            result[
                (
                    str(segment["day"]),
                    str(segment["stream"]),
                    str(segment["segment_key"]),
                    int(row["sentence_id"]),
                )
            ] = (segment, row)
    return result


def _undo_labels(state: Any, _undo_started_at: str) -> dict[str, Any]:
    from solstone.apps.speakers.attribution import restore_label_rows

    checkpoint = state.phase_checkpoints.get("labels")
    report = _empty_undo_report(state.operation_id, status="undone")["undo_report"][
        "labels"
    ]
    if not checkpoint:
        return {"labels": report}
    plan_map = _label_plan_map(state.prepared_plan)
    grouped: dict[tuple[str, str, str], list[dict[str, Any]]] = defaultdict(list)
    for key in list(checkpoint.get("patched_sentence_keys", [])) + list(
        checkpoint.get("inserted_sentence_keys", [])
    ):
        map_key = (
            str(key["day"]),
            str(key["stream"]),
            str(key["segment_key"]),
            int(key["sentence_id"]),
        )
        item = plan_map.get(map_key)
        if item is None:
            report["skipped_count"] += 1
            report["skipped_reasons"]["missing_plan"] = (
                report["skipped_reasons"].get("missing_plan", 0) + 1
            )
            continue
        segment, label = item
        grouped[(segment["day"], segment["stream"], segment["segment_key"])].append(
            {
                "sentence_id": label["sentence_id"],
                "expected_current_label": label["intended_label"],
                "prior_state": label["prior_state"],
                "prior_label": label.get("prior_label"),
            }
        )
    for day, stream, segment_key in sorted(grouped):
        seg_dir = segment_path(day, segment_key, stream, create=False)
        if not seg_dir.is_dir():
            count = len(grouped[(day, stream, segment_key)])
            report["skipped_count"] += count
            report["skipped_reasons"]["missing"] = (
                report["skipped_reasons"].get("missing", 0) + count
            )
            continue
        delta = restore_label_rows(seg_dir, grouped[(day, stream, segment_key)])
        for key, value in delta.items():
            if key == "skipped_reasons":
                for reason, count in value.items():
                    report["skipped_reasons"][reason] = report["skipped_reasons"].get(
                        reason, 0
                    ) + int(count)
            elif isinstance(value, int):
                report[key] = int(report.get(key, 0)) + value
    return {"labels": report}


def _undo_corrections(state: Any, undo_started_at: str) -> dict[str, Any]:
    from solstone.apps.speakers.attribution import append_speaker_correction

    checkpoint = state.phase_checkpoints.get("corrections")
    report = _empty_undo_report(state.operation_id, status="undone")["undo_report"][
        "corrections"
    ]
    if not checkpoint:
        return {"corrections": report}
    correction_map = _planned_correction_rows(state.prepared_plan)
    label_map = _label_plan_map(state.prepared_plan)
    for key in checkpoint.get("appended_keys", []):
        map_key = (
            str(key["day"]),
            str(key["stream"]),
            str(key["segment_key"]),
            int(key["sentence_id"]),
        )
        planned = correction_map.get(map_key)
        label_item = label_map.get(map_key)
        if planned is None or label_item is None:
            report["skipped_count"] += 1
            report["skipped_reasons"]["missing_plan"] = (
                report["skipped_reasons"].get("missing_plan", 0) + 1
            )
            continue
        segment, _row = planned
        _segment, label = label_item
        seg_dir = segment_path(
            segment["day"],
            segment["segment_key"],
            segment["stream"],
            create=False,
        )
        existing = _load_segment_corrections(seg_dir)
        if any(
            row.get("operation_id") == state.operation_id
            and row.get("correction_kind") == "identify_undo"
            and int(row.get("sentence_id", -1)) == int(key["sentence_id"])
            for row in existing
        ):
            report["already_present_count"] += 1
            report["skipped_count"] += 1
            report["skipped_reasons"]["already_present"] = (
                report["skipped_reasons"].get("already_present", 0) + 1
            )
            continue
        prior = label.get("prior_label") or {}
        append_speaker_correction(
            seg_dir,
            {
                "sentence_id": int(key["sentence_id"]),
                "original_speaker": state.target_entity_id,
                "corrected_speaker": prior.get("speaker"),
                "original_method": "user_identified",
                "timestamp": undo_started_at,
                "operation_id": state.operation_id,
                "undo_of_operation_id": state.operation_id,
                "correction_kind": "identify_undo",
            },
        )
        report["appended_count"] += 1
        report["restored_count"] += 1
    return {"corrections": report}


def _voiceprint_removals_for_checkpoint(state: Any) -> list[dict[str, Any]]:
    metadata_by_key: dict[tuple[str, str, str, int], dict[str, Any]] = {}
    for entry in state.prepared_plan["direct_voiceprints"]["entries_to_add"]:
        metadata_by_key[_key_tuple(entry["key"])] = dict(entry["metadata"])
    for entry in state.prepared_plan["retro_confirm"].get("voiceprints_to_add", []):
        metadata_by_key[_key_tuple(entry["key"])] = dict(entry["metadata"])

    keys: list[dict[str, Any]] = []
    direct = state.phase_checkpoints.get("direct_voiceprints") or {}
    retro = state.phase_checkpoints.get("retro_tracker") or {}
    keys.extend(direct.get("saved_keys", []))
    keys.extend(retro.get("saved_keys", []))

    seen: set[tuple[str, str, str, int]] = set()
    removals: list[dict[str, Any]] = []
    for key in keys:
        key_tuple = _key_tuple(key)
        if key_tuple in seen or key_tuple not in metadata_by_key:
            continue
        seen.add(key_tuple)
        removals.append(
            {"key": dict(key), "expected_metadata": metadata_by_key[key_tuple]}
        )
    return removals


def _undo_voiceprints(state: Any, _undo_started_at: str) -> dict[str, Any]:
    from solstone.think.entities import remove_voiceprints_by_key

    removals = _voiceprint_removals_for_checkpoint(state)
    report = _empty_undo_report(state.operation_id, status="undone")["undo_report"][
        "voiceprints"
    ]
    if not removals:
        return {"voiceprints": report}
    delta = remove_voiceprints_by_key(state.target_entity_id, removals)
    report["removed_count"] = int(delta.get("removed_count", 0))
    report["restored_count"] = report["removed_count"]
    reasons = delta.get("skipped_reasons", {})
    missing = int(reasons.get("missing", 0)) if isinstance(reasons, dict) else 0
    mismatch = (
        int(reasons.get("metadata_mismatch", 0)) if isinstance(reasons, dict) else 0
    )
    report["missing_count"] = missing
    report["metadata_mismatch_count"] = mismatch
    report["skipped_count"] = int(delta.get("skipped_count", 0))
    report["skipped_reasons"] = {"missing": missing, "metadata_mismatch": mismatch}
    return {"voiceprints": report}


def _undo_tracker(state: Any, _undo_started_at: str) -> dict[str, Any]:
    from solstone.apps.speakers.candidate_tracker import CandidateTracker

    checkpoint = state.phase_checkpoints.get("retro_tracker")
    report = _empty_undo_report(state.operation_id, status="undone")["undo_report"][
        "tracker"
    ]
    if (
        not checkpoint
        or not checkpoint.get("matched")
        or checkpoint.get("candidate_id") is None
    ):
        return {"tracker": report}
    retro = state.prepared_plan["retro_confirm"]
    delta = CandidateTracker().restore_confirmed_candidate(
        int(checkpoint["candidate_id"]),
        expected_after=retro["candidate_after"],
        candidate_before=retro["candidate_before"],
    )
    report["restored_candidate_count"] = int(delta.get("restored_count", 0))
    report["restored_count"] = report["restored_candidate_count"]
    report["skipped_count"] = int(delta.get("skipped_count", 0))
    reasons = delta.get("skipped_reasons", {})
    report["skipped_reasons"] = reasons if isinstance(reasons, dict) else {}
    return {"tracker": report}


def _undo_sentinel(state: Any, _undo_started_at: str) -> dict[str, Any]:
    checkpoint = state.phase_checkpoints.get("sentinel")
    report = _empty_undo_report(state.operation_id, status="undone")["undo_report"][
        "sentinel"
    ]
    if not checkpoint or not checkpoint.get("written"):
        return {"sentinel": report}
    sentinel = state.prepared_plan["sentinel"]
    cluster_key = str(sentinel["cluster_key"])
    intended = sentinel["intended_entry"]
    prior = sentinel.get("prior_entry")
    data = _load_resolved_clusters()
    current = data.get(cluster_key)
    if current == intended:
        if prior is None:
            data.pop(cluster_key, None)
            report["removed_count"] = 1
        else:
            data[cluster_key] = prior
            report["restored_prior_count"] = 1
        _replace_resolved_clusters(data)
        report["restored_count"] = 1
        return {"sentinel": report}
    if current == prior or (current is None and prior is None):
        report["skipped_count"] = 1
        report["skipped_reasons"] = {"already_restored": 1}
        return {"sentinel": report}
    report["skipped_count"] = 1
    report["skipped_reasons"] = {"concurrent_change": 1}
    return {"sentinel": report}


def _undo_entity(state: Any, _undo_started_at: str) -> dict[str, Any]:
    from solstone.think.entities import delete_created_entity_if_unreferenced
    from solstone.think.speaker_keep_separate import remove_operation_sources

    report = _empty_undo_report(state.operation_id, status="undone")["undo_report"][
        "entity"
    ]
    if not state.will_create:
        return {"entity": report}
    keep_checkpoint = state.phase_checkpoints.get("keep_separate") or {}
    pair_keys = keep_checkpoint.get("pair_keys", [])
    tombstones = remove_operation_sources(state.operation_id, pair_keys)
    report["keep_separate_sources_removed_count"] = len(tombstones)

    entity_checkpoint = state.phase_checkpoints.get("entity") or {}
    if not entity_checkpoint.get("entity_created"):
        return {"entity": report}
    delete_result = delete_created_entity_if_unreferenced(
        state.target_entity_id,
        operation_id=state.operation_id,
        expected_identity=entity_checkpoint.get("identity_after")
        or state.prepared_plan["entity_identity"]["intended_identity"],
        expected_history_refs=entity_checkpoint.get("history_event_refs", []),
    )
    report["deleted"] = bool(delete_result.get("deleted"))
    report["restored_count"] = int(bool(report["deleted"]))
    report["blocked_categories"] = list(delete_result.get("blocked_categories", []))
    if not report["deleted"] and report["blocked_categories"]:
        report["skipped_count"] = 1
        report["skipped_reasons"] = {
            category: int(delete_result.get("blocked_counts", {}).get(category, 1))
            for category in report["blocked_categories"]
        }
    return {"entity": report}


_UNDO_PHASES = {
    "labels": _undo_labels,
    "corrections": _undo_corrections,
    "voiceprints": _undo_voiceprints,
    "tracker": _undo_tracker,
    "sentinel": _undo_sentinel,
    "entity": _undo_entity,
}


def _aggregate_undo_report(state: Any, *, status: str) -> dict[str, Any]:
    report = _empty_undo_report(state.operation_id, status=status)
    for phase in (
        "labels",
        "corrections",
        "voiceprints",
        "tracker",
        "sentinel",
        "entity",
    ):
        delta = state.undo_phase_checkpoints.get(phase)
        if not isinstance(delta, dict):
            continue
        for category, value in delta.items():
            if category in report["undo_report"] and isinstance(value, dict):
                report["undo_report"][category] = value
    return report


def _undo_recoverable_result(operation_id: str, detail: str) -> dict[str, Any]:
    from solstone.think.speaker_identify_operations import fold_operation

    state = fold_operation(operation_id)
    result = {
        "status": "recoverable",
        "operation_id": operation_id,
        "operation_state": state.terminal_status if state else "not_found",
        "request_id": state.request_id if state else None,
        "detail": detail,
    }
    if state is not None:
        result["undo_report"] = _aggregate_undo_report(
            state,
            status="recoverable",
        )["undo_report"]
    return result


def _append_undo_repair_required(
    operation_id: str,
    request_id: str,
    phase: str,
    *,
    repair_code: str,
    repair_categories: dict[str, int],
) -> dict[str, Any]:
    from solstone.think.speaker_identify_operations import append_event, fold_operation

    state = fold_operation(operation_id)
    if state is not None and state.terminal_status == "undo_repair_required":
        repair = state.undo_repair_required or {}
        return {
            "status": "undo_repair_required",
            "operation_id": operation_id,
            "operation_state": state.terminal_status,
            "phase": repair.get("phase", phase),
            "repair_code": repair.get("repair_code", repair_code),
            "repair_categories": repair.get("repair_categories", {}),
            "undo_report": repair.get("undo_report"),
        }
    report = (
        _aggregate_undo_report(state, status="undo_repair_required")
        if state is not None
        else _empty_undo_report(operation_id, status="undo_repair_required")
    )
    event = _identify_event(
        operation_id=operation_id,
        request_id=request_id,
        event_kind="undo_repair_required",
        event_id=f"{operation_id}:undo_repair_required:{phase}",
        phase=phase,
        repair_code=repair_code,
        repair_categories=repair_categories,
        undo_report=report,
    )
    append_event(event)
    _maybe_inject_identify_fault("after_undo_repair_required")
    return {
        "status": "undo_repair_required",
        "operation_id": operation_id,
        "operation_state": "undo_repair_required",
        "phase": phase,
        "repair_code": repair_code,
        "repair_categories": repair_categories,
        "undo_report": report.get("undo_report"),
    }


def undo_identify_operation(operation_id: str) -> dict[str, Any]:
    """Undo a committed speaker identify operation by operation id."""
    from solstone.think.entities.history import trust_operation_lock
    from solstone.think.journal_io.errors import LockTimeout
    from solstone.think.speaker_identify_operations import (
        UNDO_PHASE_ORDER,
        append_event,
        fold_operation,
    )

    state = fold_operation(operation_id)
    if state is None:
        return {
            "status": "not_found",
            "operation_id": operation_id,
            "list_command": "sol call speakers identify-operations",
        }
    if state.terminal_status == "undone" and state.undo_report is not None:
        result = copy.deepcopy(state.undo_report)
        result["status"] = "already_undone"
        return result
    if state.terminal_status not in {"committed", "undoing"}:
        return _state_status_result(state)

    current_phase = "undo_prepared"
    try:
        with trust_operation_lock():
            state = fold_operation(operation_id)
            if state is None:
                return {
                    "status": "not_found",
                    "operation_id": operation_id,
                    "list_command": "sol call speakers identify-operations",
                }
            if state.terminal_status == "undone" and state.undo_report is not None:
                result = copy.deepcopy(state.undo_report)
                result["status"] = "already_undone"
                return result
            if state.terminal_status not in {"committed", "undoing"}:
                return _state_status_result(state)
            undo_started_at = _append_undo_prepared_once(
                operation_id,
                state.request_id,
            )
            _maybe_inject_identify_fault("after_undo_prepared")
            for phase in UNDO_PHASE_ORDER:
                state = fold_operation(operation_id)
                if state is not None and phase in state.undo_phase_checkpoints:
                    continue
                current_phase = phase
                delta = _UNDO_PHASES[phase](state, undo_started_at)
                _append_undo_checkpoint(operation_id, state.request_id, phase, delta)
                _maybe_inject_identify_fault(f"after_undo_{phase}")
            state = fold_operation(operation_id)
            report = _aggregate_undo_report(state, status="undone")
            append_event(
                _identify_event(
                    operation_id=operation_id,
                    request_id=state.request_id,
                    event_kind="undo_committed",
                    event_id=f"{operation_id}:undo_committed",
                    undo_report=report,
                )
            )
            _maybe_inject_identify_fault("after_undo_committed")
            return report
    except LockTimeout:
        raise
    except _IdentifyRepairRequired as exc:
        return _append_undo_repair_required(
            operation_id,
            state.request_id if state else operation_id,
            exc.phase,
            repair_code=exc.repair_code,
            repair_categories=exc.repair_categories,
        )
    except Exception as exc:
        if isinstance(exc, LockTimeout):
            raise
        from solstone.think.entities import VoiceprintRemovalError

        if isinstance(exc, VoiceprintRemovalError):
            return _append_undo_repair_required(
                operation_id,
                state.request_id if state else operation_id,
                current_phase,
                repair_code="voiceprint_removal_ambiguous",
                repair_categories={"voiceprints": 1},
            )
        return _undo_recoverable_result(operation_id, str(exc))
