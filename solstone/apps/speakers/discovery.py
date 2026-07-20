# SPDX-License-Identifier: AGPL-3.0-only
# Copyright (c) 2026 sol pbc

"""Unknown speaker discovery - cluster unmatched embeddings to find recurring voices."""

from __future__ import annotations

import json
import logging
from collections import defaultdict
from datetime import datetime
from pathlib import Path
from typing import Any

from solstone.apps.speakers.attribution import (
    _load_setting_field,
    compute_segment_candidate_evidence_readonly,
)
from solstone.apps.speakers.audio import resolve_audio_url
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
    clusters = data.get("clusters")
    return data if isinstance(clusters, dict) else None


def _write_resolved_cluster(cluster_id: int, entity_id: str, label: str) -> None:
    path = _discovery_resolved_path(create=True)
    try:
        data = json.loads(path.read_text(encoding="utf-8")) if path.exists() else {}
    except (json.JSONDecodeError, OSError):
        data = {}
    data[str(cluster_id)] = {
        "entity_id": entity_id,
        "label": label,
        "ts": datetime.utcnow().isoformat(timespec="seconds") + "Z",
    }
    atomic_replace(path, json.dumps(data, indent=2, sort_keys=True))


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

    result_clusters: list[dict[str, Any]] = []
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

        samples: list[dict[str, Any]] = []
        seen_segments: set[tuple[str, str, str]] = set()

        for pos in sorted_positions:
            record = provenance[int(cluster_indices[int(pos)])]
            seg_triplet = (record["day"], record["stream"], record["segment_key"])
            if seg_triplet in seen_segments:
                continue
            seen_segments.add(seg_triplet)
            samples.append(_build_cluster_sample(record))
            if len(samples) == 3:
                break

        if len(samples) < 3:
            for pos in sorted_positions:
                record = provenance[int(cluster_indices[int(pos)])]
                sample = _build_cluster_sample(record)
                if sample in samples:
                    continue
                samples.append(sample)
                if len(samples) == 3:
                    break

        result_clusters.append(
            {
                "cluster_id": int(cid),
                "size": int(len(cluster_indices)),
                "segment_count": len(segment_set),
                "samples": samples,
            }
        )
        cache_clusters[str(int(cid))] = [provenance[int(i)] for i in cluster_indices]

    if not result_clusters:
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

    result_clusters.sort(key=lambda cluster: cluster["size"], reverse=True)
    return {"clusters": result_clusters}


def _conversation_key(
    day: str,
    stream: str,
    segment_key: str,
    setting: str | None,
) -> tuple:
    if setting:
        return (day, stream, setting)
    return (day, stream, "__segment__", segment_key)


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
    if not members:
        return None

    _, load_speaker_labels, _, _, _ = _routes_helpers()

    distinct_segments: list[tuple[str, str, str]] = []
    first_record_by_segment: dict[tuple[str, str, str], dict] = {}
    for member in members:
        segment = (member["day"], member["stream"], member["segment_key"])
        if segment in first_record_by_segment:
            continue
        first_record_by_segment[segment] = member
        distinct_segments.append(segment)

    segment_settings: dict[tuple[str, str, str], str | None] = {}
    conversation_keys: dict[tuple[str, str, str], tuple] = {}
    for day, stream, segment_key in distinct_segments:
        seg_dir = segment_path(day, segment_key, stream, create=False)
        setting = _load_setting_field(seg_dir)
        segment_settings[(day, stream, segment_key)] = setting
        conversation_keys[(day, stream, segment_key)] = _conversation_key(
            day,
            stream,
            segment_key,
            setting,
        )

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
    conversations = set(conversation_keys.values())

    return {
        "cluster_id": cluster_id,
        "facts": {
            "statement_count": len(members),
            "segment_count": len(distinct_segments),
            "day_count": len(days),
            "streams": sorted(streams),
            "conversation_count": len(conversations),
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


def identify_cluster(
    cluster_id: int,
    name: str | None = None,
    entity_id: str | None = None,
    *,
    resolve_only: bool = False,
    create_new: bool = False,
    entity_type: str = "Person",
) -> dict[str, Any]:
    """Identify a discovered unknown speaker cluster."""
    import numpy as np

    from solstone.apps.speakers.attribution import (
        append_speaker_correction,
        apply_label_patches,
    )
    from solstone.think.entities import (
        EntityResolutionOutcome,
        ResolutionOrigin,
        ResolutionScope,
        closest_resolution_candidates,
        entity_slug,
        is_valid_entity_type,
        load_existing_voiceprint_keys,
        record_entity_resolution,
        save_voiceprints_batch,
    )
    from solstone.think.entities.journal import (
        create_journal_entity,
        load_all_journal_entities,
        load_journal_entity,
    )

    (
        load_embeddings_file,
        load_speaker_labels,
        normalize_embedding,
        _scan,
        check_owner_contamination,
    ) = _routes_helpers()

    cache_path = _discovery_cache_path()
    if not cache_path.exists():
        return {"error": "No discovery scan results. Run scan first."}

    cache_data = load_discovery_cache()
    if cache_data is None:
        return {"error": "Invalid discovery cache. Run scan again."}

    cluster_members = cache_data.get("clusters", {}).get(str(cluster_id))
    if not cluster_members:
        return {"error": f"Cluster {cluster_id} not found in scan results."}

    from solstone.apps.speakers.routes import _load_speaker_corrections

    entity_created = False
    will_create = False
    name_value = name.strip() if isinstance(name, str) else ""
    entity_id_value = entity_id.strip() if isinstance(entity_id, str) else ""

    if entity_id_value:
        entity = load_journal_entity(entity_id_value)
        if not entity:
            return {
                "error": f"Entity '{entity_id_value}' not found.",
                "not_found": True,
            }
        target_id = entity_id_value
        target_name = entity.get("name", target_id)
    else:
        if not name_value:
            return {"error": "name is required"}
        journal_entities = load_all_journal_entities()
        entities_list = [
            entity for entity in journal_entities.values() if not entity.get("blocked")
        ]

        resolution = record_entity_resolution(
            name_value,
            entities_list,
            scope=ResolutionScope.journal(),
            origin=ResolutionOrigin(
                lane="apps.speakers.discovery.identify_cluster",
                record_id=str(cluster_id),
                field="name",
            ),
            read_only=resolve_only,
        )
        if resolution.outcome == EntityResolutionOutcome.AMBIGUOUS:
            return {
                "status": "ambiguous",
                "ambiguity_id": resolution.ambiguity_id,
                "candidates": [
                    candidate.to_dict() for candidate in resolution.candidates
                ],
            }
        if resolution.outcome == EntityResolutionOutcome.RESOLVED and resolution.entity:
            target_id = resolution.entity["id"]
            target_name = resolution.entity.get("name", name_value)
        else:
            if resolve_only or not create_new:
                return {
                    "status": "no_match",
                    "candidates": [
                        candidate.to_dict()
                        for candidate in closest_resolution_candidates(
                            name_value,
                            entities_list,
                        )
                    ],
                }
            will_create = True
            target_id = entity_slug(name_value)
            target_name = name_value

    if resolve_only:
        return {
            "status": "resolved",
            "entity_id": target_id,
            "entity_name": target_name,
            "has_voice": _voiceprints_exist(target_id),
        }

    if will_create:
        if not is_valid_entity_type(entity_type):
            return {
                "error": f"Invalid entity type: {entity_type}",
                "invalid_entity_type": True,
            }
        existing = load_journal_entity(target_id)
        entity_created = existing is None
        entity = existing or create_journal_entity(
            entity_id=target_id,
            name=name_value,
            entity_type=entity_type,
        )
        target_name = entity.get("name", name_value)

    completed: list[str] = []
    try:
        existing_keys = load_existing_voiceprint_keys(target_id)
        vp_batch: list[tuple[np.ndarray, dict[str, Any]]] = []

        for member in cluster_members:
            day = member["day"]
            stream = member["stream"]
            seg_key = member["segment_key"]
            source = member["source"]
            sentence_id = int(member["sentence_id"])

            vp_key = (day, seg_key, source, sentence_id)
            if vp_key in existing_keys:
                continue

            seg_dir = segment_path(day, seg_key, stream, create=False)
            emb_data = load_embeddings_file(seg_dir / f"{source}.npz")
            if emb_data is None:
                continue

            embeddings, statement_ids, _ = emb_data
            emb_vec = None
            for emb, sid in zip(embeddings, statement_ids):
                if int(sid) == sentence_id:
                    emb_vec = normalize_embedding(emb)
                    break

            if emb_vec is None or check_owner_contamination(emb_vec):
                continue

            vp_batch.append(
                (
                    emb_vec,
                    {
                        "day": day,
                        "segment_key": seg_key,
                        "source": source,
                        "stream": stream,
                        "sentence_id": sentence_id,
                        "added_at": now_ms(),
                    },
                )
            )
            existing_keys.add(vp_key)

        voiceprints_saved = (
            save_voiceprints_batch(target_id, vp_batch) if vp_batch else 0
        )
        completed.append("voiceprints")
        _maybe_inject_identify_fault("after_voiceprints")

        segments_map: dict[tuple[str, str, str], list[int]] = defaultdict(list)
        for member in cluster_members:
            key = (member["day"], member["stream"], member["segment_key"])
            segments_map[key].append(int(member["sentence_id"]))

        segments_updated = 0
        sentences_attributed = 0
        timestamp = now_ms()

        for (day, stream, seg_key), sentence_ids in segments_map.items():
            seg_dir_check = day_path(day, create=False) / stream / seg_key
            if not seg_dir_check.is_dir():
                continue
            seg_dir = seg_dir_check
            labels_data = load_speaker_labels(seg_dir)
            if labels_data is None:
                labels_data = {
                    "labels": [],
                    "owner_centroid_last_refreshed_at": None,
                    "voiceprint_versions": {},
                }

            labels_by_sid: dict[int, dict[str, Any]] = {}
            for label in labels_data.get("labels", []):
                sentence_id = label.get("sentence_id")
                if sentence_id is not None:
                    labels_by_sid[int(sentence_id)] = label

            existing_correction_keys = {
                (
                    correction.get("sentence_id"),
                    correction.get("corrected_speaker"),
                )
                for correction in _load_speaker_corrections(seg_dir)
            }

            updated = False
            patches: dict[int, dict[str, Any]] = {}
            for sentence_id in sorted(set(sentence_ids)):
                original = labels_by_sid.get(sentence_id, {})
                new_label = {
                    "sentence_id": sentence_id,
                    "speaker": target_id,
                    "confidence": "high",
                    "method": "user_identified",
                }
                if original != new_label:
                    updated = True
                    sentences_attributed += 1
                    patches[sentence_id] = {
                        "speaker": target_id,
                        "confidence": "high",
                        "method": "user_identified",
                    }

                correction_key = (sentence_id, target_id)
                if correction_key in existing_correction_keys:
                    continue

                append_speaker_correction(
                    seg_dir,
                    {
                        "sentence_id": sentence_id,
                        "original_speaker": original.get("speaker"),
                        "corrected_speaker": target_id,
                        "original_method": original.get("method"),
                        "timestamp": timestamp,
                    },
                )
                existing_correction_keys.add(correction_key)

            if updated:
                apply_label_patches(seg_dir, patches, allow_insert=True)
                segments_updated += 1

        completed.append("segments")
        _maybe_inject_identify_fault("after_segments")

        if vp_batch:
            try:
                cluster_centroid = normalize_embedding(
                    np.mean([embedding for embedding, _ in vp_batch], axis=0)
                )
                if cluster_centroid is not None:
                    from solstone.apps.speakers.candidate_tracker import (
                        CandidateTracker,
                    )

                    CandidateTracker().retroactive_confirm(cluster_centroid, target_id)
            except Exception as exc:
                logger.warning(
                    "Failed to retroactively confirm speaker candidate for %s: %s",
                    target_id,
                    exc,
                )

        _write_resolved_cluster(cluster_id, target_id, target_name)
        completed.append("sentinel")
    except Exception as exc:
        from solstone.think.journal_io.errors import LockTimeout

        if isinstance(exc, LockTimeout):
            raise
        if not completed:
            raise
        failed = [
            category
            for category in ("voiceprints", "segments", "sentinel")
            if category not in completed
        ]
        return {
            "status": "partial",
            "completed": completed,
            "failed": failed,
            "detail": str(exc),
            "entity_id": target_id,
            "entity_name": target_name,
        }

    return {
        "status": "identified",
        "entity_id": target_id,
        "entity_name": target_name,
        "entity_created": entity_created,
        "voiceprints_saved": voiceprints_saved,
        "segments_updated": segments_updated,
        "sentences_attributed": sentences_attributed,
    }
