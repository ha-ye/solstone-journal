# SPDX-License-Identifier: AGPL-3.0-only
# Copyright (c) 2026 sol pbc

"""Owner voice identification helpers for the speakers app."""

from __future__ import annotations

import json
import logging
import os
from collections.abc import Iterator
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import TYPE_CHECKING, Any

from solstone.apps.speakers._overlap import _read_segment_overlap_fraction
from solstone.apps.speakers.audio import resolve_audio_url
from solstone.apps.speakers.encoder_config import (
    NOISY_FLYWHEEL_OVERLAP_MAX,
    OWNER_BOOTSTRAP_MIN_INTRA_COSINE_P25,
    OWNER_BOOTSTRAP_MIN_MEDIAN_DURATION_S,
    OWNER_BOOTSTRAP_MIN_STMTS,
    OWNER_BOOTSTRAP_PROVISIONAL_GUARD_MIN_TAGS,
    OWNER_THRESHOLD,
)
from solstone.think.awareness import get_current, update_state
from solstone.think.entities.core import entity_slug, get_identity_names
from solstone.think.entities.journal import (
    create_journal_entity,
    ensure_journal_entity_memory,
    get_journal_principal,
    journal_entity_memory_path,
    load_journal_entity,
)
from solstone.think.entities.voiceprints import load_entity_voiceprints_file
from solstone.think.journal_io.errors import LockTimeout
from solstone.think.journal_io.npz import load_npz, load_npz_row_count, save_npz
from solstone.think.utils import day_dirs, get_journal, segment_parse, segment_path

if TYPE_CHECKING:
    import numpy as np

    from solstone.apps.speakers.candidate_tracker import CandidateProfile
    from solstone.think.entities.core import EntityDict

logger = logging.getLogger(__name__)

OWNER_CANDIDATE_SOURCE = "candidate_pool"
OWNER_CANDIDATE_EXPANSION_MAX_EMBEDDINGS = 3000
LOW_QUALITY_REASON_TOO_FEW_STMTS = "too_few_stmts"
LOW_QUALITY_REASON_MEDIAN_DURATION_TOO_SHORT = "median_duration_too_short"
LOW_QUALITY_REASON_CLUSTER_TOO_DIFFUSE = "cluster_too_diffuse"
MANUAL_OWNER_METHODS = frozenset({"user_assigned", "user_corrected", "user_confirmed"})
VOICEPRINT_BUSY_ERROR_KIND = "voiceprint_busy"
VOICEPRINT_BUSY_ERROR = "voiceprint storage is busy; try again"
_PROVISIONAL_GUARD_CACHE: dict[str, tuple[int, int, np.ndarray]] | None = None


@dataclass(frozen=True)
class OwnerCentroid:
    """Confirmed owner centroid plus browser-facing metadata."""

    centroid: np.ndarray
    threshold: float
    cluster_size: int
    last_refreshed_at: str
    intra_cosine_p25: float | None
    streams: list[str]


@dataclass(frozen=True)
class OwnerCandidateExpansion:
    """Expanded candidate-pool rows and journal-touch diagnostics."""

    embeddings: np.ndarray
    durations: list[float]
    provenance: list[dict[str, Any]]
    segments_checked: int
    segments_available: int
    embeddings_available: int
    skipped: dict[str, int]


def _mark_no_cluster(segment_count: int) -> None:
    """Record that detection ran but did not produce a usable cluster."""
    update_state(
        "voiceprint",
        {
            "status": "no_cluster",
            "segments_checked": segment_count,
            "attempted_at": _iso_now(),
        },
    )


def _mark_low_quality(
    reason: str,
    observed: float,
    threshold: float,
    segment_count: int,
    *,
    source: str,
) -> None:
    """Record that detection found a cluster, but it failed quality gates."""
    update_state(
        "voiceprint",
        {
            "status": "low_quality",
            "source": source,
            "low_quality_reason": reason,
            "observed_value": float(observed),
            "threshold_value": float(threshold),
            "segments_checked": int(segment_count),
            "attempted_at": _iso_now(),
        },
    )


def _bail_low_quality(
    reason: str,
    observed: float,
    threshold: float,
    segment_count: int,
    embeddings_count: int,
    *,
    source: str,
) -> dict[str, Any]:
    """Record and return a locked low-quality owner detection result."""
    _mark_low_quality(
        reason,
        observed,
        threshold,
        segment_count,
        source=source,
    )
    return {
        "status": "low_quality",
        "source": source,
        "recommendation": "low_quality",
        "segments_available": int(segment_count),
        "embeddings_available": int(embeddings_count),
        "low_quality_reason": reason,
        "observed_value": float(observed),
        "threshold_value": float(threshold),
    }


def _pairwise_cosines(embeddings: np.ndarray) -> np.ndarray:
    """Return pairwise cosine similarities for a cluster of embeddings."""
    import numpy as np

    n = embeddings.shape[0]
    if n < 2:
        return np.empty(0, dtype=np.float32)
    norms = np.linalg.norm(embeddings, axis=1, keepdims=True)
    norms = np.where(norms < 1e-9, 1.0, norms)
    e_norm = embeddings / norms
    if n > 5000:
        rng = np.random.default_rng(seed=0)
        i = rng.integers(0, n, size=1000)
        j = rng.integers(0, n, size=1000)
        mask = i != j
        i = i[mask]
        j = j[mask]
        return np.einsum("ij,ij->i", e_norm[i], e_norm[j]).astype(
            np.float32, copy=False
        )
    sim = e_norm @ e_norm.T
    iu = np.triu_indices(n, k=1)
    return sim[iu].astype(np.float32, copy=False)


def compute_intra_cosine_p25(embeddings: np.ndarray) -> float | None:
    """Return p25 pairwise cosine for embeddings, or None when unavailable."""
    import numpy as np

    cosines = _pairwise_cosines(np.asarray(embeddings, dtype=np.float32))
    if cosines.size == 0:
        return None
    return float(np.percentile(cosines, 25))


def _routes_helpers():
    """Load speakers route helpers lazily to avoid import cycles."""
    from solstone.apps.speakers.routes import (
        _load_embeddings_file,
        _normalize_embedding,
    )

    return _load_embeddings_file, _normalize_embedding


def _owner_candidate_path(*, create: bool = False) -> Path:
    """Return the temporary owner candidate NPZ path."""
    awareness_dir = Path(get_journal()) / "awareness"
    if create:
        awareness_dir.mkdir(parents=True, exist_ok=True)
    return awareness_dir / "owner_candidate.npz"


def _voiceprint_busy_result(exc: LockTimeout) -> dict[str, Any]:
    logger.warning("voiceprint storage busy for %s", exc.path)
    return {
        "error": VOICEPRINT_BUSY_ERROR,
        "error_kind": VOICEPRINT_BUSY_ERROR_KIND,
    }


def _principal_id_or_none() -> str | None:
    """Return the current journal principal entity id, if one exists."""
    principal = get_journal_principal()
    if principal is None:
        return None
    return str(principal["id"])


def principal_identity_or_none() -> tuple[str, str] | None:
    """Return (entity_id, display_name) for the configured identity, or None."""
    names = get_identity_names()
    if not names:
        return None
    display_name = names[0]
    entity_id = entity_slug(display_name)
    if not entity_id:
        return None
    return entity_id, display_name


def ensure_principal_entity() -> EntityDict | None:
    """Return the principal entity, creating it from identity config if absent."""
    principal = get_journal_principal()
    if principal is not None:
        return principal
    identity = principal_identity_or_none()
    if identity is None:
        return None
    entity_id, display_name = identity
    entity = load_journal_entity(entity_id)
    if entity is not None:
        return entity
    logger.info("Creating principal entity %s from identity config", entity_id)
    return create_journal_entity(
        entity_id=entity_id, name=display_name, entity_type="Person"
    )


def _iso_now() -> str:
    """Return a timestamp string for persisted metadata."""
    return datetime.now(UTC).isoformat().replace("+00:00", "Z")


def _fallback_statement_durations(jsonl_path: Path) -> dict[int, float | None]:
    """Estimate statement durations from adjacent transcript start times."""
    if not jsonl_path.exists():
        return {}

    starts: list[tuple[int, int]] = []
    try:
        with open(jsonl_path, encoding="utf-8") as f:
            lines = f.readlines()
    except OSError:
        return {}

    for sentence_id, line in enumerate(lines[1:], start=1):
        try:
            entry = json.loads(line)
        except json.JSONDecodeError:
            continue
        start = entry.get("start")
        if not isinstance(start, str):
            continue
        try:
            hours, minutes, seconds = (int(part) for part in start.split(":", 2))
        except ValueError:
            continue
        starts.append((sentence_id, hours * 3600 + minutes * 60 + seconds))

    durations: dict[int, float | None] = {}
    for idx, (sentence_id, start_seconds) in enumerate(starts):
        next_start = starts[idx + 1][1] if idx + 1 < len(starts) else None
        # Why: older transcript JSONL files only persist statement starts, so
        # we estimate legacy durations from adjacent sentence boundaries.
        durations[sentence_id] = (
            None if next_start is None else float(next_start - start_seconds)
        )
    return durations


def _load_manual_tag_rows(principal_id: str) -> list[dict[str, Any]]:
    """Return validated owner-attested voiceprint rows for the principal."""
    result = load_entity_voiceprints_file(principal_id)
    if result is None:
        return []

    _embeddings, metadata_rows = result
    latest_rows: dict[tuple[str, str, str, int], tuple[int, int, dict[str, Any]]] = {}
    for index, raw_row in enumerate(metadata_rows):
        day = raw_row.get("day")
        segment_key = raw_row.get("segment_key")
        source = raw_row.get("source")
        sentence_id = raw_row.get("sentence_id")
        if not isinstance(day, str) or not isinstance(segment_key, str):
            continue
        if not isinstance(source, str):
            continue
        try:
            sentence_id_int = int(sentence_id)
        except (TypeError, ValueError):
            continue
        added_at = raw_row.get("added_at")
        try:
            added_at_int = int(added_at)
        except (TypeError, ValueError):
            added_at_int = -1
        dedupe_key = (day, segment_key, source, sentence_id_int)
        current = latest_rows.get(dedupe_key)
        if current is not None and (added_at_int, index) <= (current[0], current[1]):
            continue
        normalized = dict(raw_row)
        normalized["day"] = day
        normalized["segment_key"] = segment_key
        normalized["source"] = source
        normalized["sentence_id"] = sentence_id_int
        latest_rows[dedupe_key] = (added_at_int, index, normalized)

    chronicle_root = Path(get_journal()) / "chronicle"
    rows: list[dict[str, Any]] = []
    labels_cache: dict[Path, dict[str, Any] | None] = {}
    overlap_cache: dict[Path, float] = {}
    for _added_at, _index, row in sorted(
        latest_rows.values(),
        key=lambda item: (
            item[2]["day"],
            str(item[2].get("stream") or ""),
            item[2]["segment_key"],
            item[2]["source"],
            item[2]["sentence_id"],
        ),
    ):
        day = row["day"]
        stream = row.get("stream")
        segment_key = row["segment_key"]
        source = row["source"]
        sentence_id = row["sentence_id"]

        segment_dir: Path | None = None
        if isinstance(stream, str) and stream:
            candidate = chronicle_root / day / stream / segment_key
            if candidate.is_dir():
                segment_dir = candidate
        else:
            matches = [
                candidate
                for candidate in (chronicle_root / day).glob(f"*/{segment_key}")
                if candidate.is_dir()
            ]
            if len(matches) == 1:
                segment_dir = matches[0]
                row["stream"] = matches[0].parent.name
                stream = row["stream"]
            else:
                if len(matches) > 1:
                    logger.info(
                        "owner manual bootstrap skip: ambiguous segment for %s/%s/%s",
                        day,
                        segment_key,
                        source,
                    )
                continue

        if segment_dir is None:
            continue

        labels_path = segment_dir / "talents" / "speaker_labels.json"
        if labels_path not in labels_cache:
            if not labels_path.is_file():
                labels_cache[labels_path] = None
            else:
                try:
                    with open(labels_path, encoding="utf-8") as f:
                        labels_cache[labels_path] = json.load(f)
                except (json.JSONDecodeError, OSError):
                    labels_cache[labels_path] = None
        labels_data = labels_cache[labels_path]
        if not isinstance(labels_data, dict):
            continue

        label_match = None
        for label in labels_data.get("labels", []):
            try:
                label_sentence_id = int(label.get("sentence_id", -1))
            except (TypeError, ValueError):
                continue
            if label_sentence_id != sentence_id:
                continue
            label_match = label
            break
        if label_match is None:
            continue
        if label_match.get("speaker") != principal_id:
            continue
        if label_match.get("method") not in MANUAL_OWNER_METHODS:
            continue

        jsonl_path = segment_dir / f"{source}.jsonl"
        overlap = overlap_cache.setdefault(
            jsonl_path,
            _read_segment_overlap_fraction(jsonl_path),
        )
        if overlap >= NOISY_FLYWHEEL_OVERLAP_MAX:
            logger.info(
                "owner manual bootstrap skip: overlap=%.3f at %s/%s/%s",
                overlap,
                day,
                segment_key,
                source,
            )
            continue

        rows.append(
            {
                "day": day,
                "stream": stream,
                "segment_key": segment_key,
                "source": source,
                "sentence_id": sentence_id,
                "segment_dir": segment_dir,
                "jsonl_path": jsonl_path,
            }
        )

    return rows


def count_manual_tag_embeddings(principal_id: str) -> int:
    """Count validated owner manual-tag rows for the principal."""
    return len(_load_manual_tag_rows(principal_id))


def load_manual_tag_stats(principal_id: str) -> dict[str, int]:
    """Return aggregate counts for validated owner manual-tag rows."""
    rows = _load_manual_tag_rows(principal_id)
    streams = {row["stream"] for row in rows if row.get("stream")}
    return {
        "manual_tags_count": len(rows),
        "streams_represented": len(streams),
    }


def _safe_scandir(path: Path) -> list[os.DirEntry[str]]:
    try:
        return list(os.scandir(path))
    except OSError:
        return []


def _iter_audio_embedding_sources() -> list[tuple[str, str, str, Path, list[str]]]:
    """Return segment/source candidates for inventory without opening speakers.json."""
    rows: list[tuple[str, str, str, Path, list[str]]] = []
    for day, day_path in day_dirs().items():
        for stream_entry in sorted(
            _safe_scandir(day_path), key=lambda entry: entry.name
        ):
            if not stream_entry.is_dir():
                continue
            stream = stream_entry.name
            for segment_entry in sorted(
                _safe_scandir(Path(stream_entry.path)), key=lambda entry: entry.name
            ):
                if not segment_entry.is_dir():
                    continue
                segment_key = segment_entry.name
                if segment_parse(segment_key)[0] is None:
                    continue
                segment_dir = Path(segment_entry.path)
                sources = sorted(
                    npz_path.stem
                    for npz_path in segment_dir.glob("*.npz")
                    if npz_path.stem == "audio" or npz_path.stem.endswith("_audio")
                )
                if sources:
                    rows.append((day, stream, segment_key, segment_dir, sources))
    return rows


def load_owner_embedding_inventory() -> dict[str, int]:
    """Return journal-wide segment and embedding availability for owner bootstrap."""
    segment_count = 0
    embeddings_count = 0
    overlap_cache: dict[Path, float] = {}
    for (
        _day,
        _stream,
        _segment_key,
        segment_dir,
        sources,
    ) in _iter_audio_embedding_sources():
        segment_count += 1
        for source in sources:
            jsonl_path = segment_dir / f"{source}.jsonl"
            overlap = overlap_cache.setdefault(
                jsonl_path,
                _read_segment_overlap_fraction(jsonl_path),
            )
            if overlap > NOISY_FLYWHEEL_OVERLAP_MAX:
                continue
            row_count = load_npz_row_count(segment_dir / f"{source}.npz", "embeddings")
            if row_count is None:
                continue
            embeddings_count += row_count

    return {
        "segments_available": segment_count,
        "embeddings_available": embeddings_count,
    }


def load_owner_bootstrap_diagnostics(
    principal_id: str | None,
) -> dict[str, int | bool]:
    """Return counts that drive owner bootstrap diagnostics surfaces."""
    inventory = load_owner_embedding_inventory()
    manual_stats = (
        load_manual_tag_stats(principal_id)
        if principal_id is not None
        else {"manual_tags_count": 0, "streams_represented": 0}
    )
    manual_tags_count = int(manual_stats["manual_tags_count"])
    return {
        "manual_tags_count": manual_tags_count,
        "segments_available": int(inventory["segments_available"]),
        "embeddings_available": int(inventory["embeddings_available"]),
        "streams_represented": int(manual_stats["streams_represented"]),
        "can_build_from_tags": manual_tags_count >= OWNER_BOOTSTRAP_MIN_STMTS,
    }


def clear_owner_provisional_cache(principal_id: str | None = None) -> None:
    """Clear the cached provisional owner centroid for one principal or all."""
    global _PROVISIONAL_GUARD_CACHE

    if _PROVISIONAL_GUARD_CACHE is None:
        return
    if principal_id is None:
        _PROVISIONAL_GUARD_CACHE = None
        return
    _PROVISIONAL_GUARD_CACHE.pop(principal_id, None)
    if not _PROVISIONAL_GUARD_CACHE:
        _PROVISIONAL_GUARD_CACHE = None


def _write_owner_centroid(
    principal_id: str, centroid: np.ndarray, cluster_size: int
) -> Path:
    """Write owner_centroid.npz with the canonical schema."""
    import numpy as np

    owner_path = ensure_journal_entity_memory(principal_id) / "owner_centroid.npz"
    save_npz(
        owner_path,
        {
            "centroid": np.asarray(centroid, dtype=np.float32).reshape(-1),
            "cluster_size": np.array(cluster_size, dtype=np.int32),
            "threshold": np.array(OWNER_THRESHOLD, dtype=np.float32),
            "last_refreshed_at": np.array(_iso_now()),
        },
        expected_keys=("centroid", "cluster_size", "threshold", "last_refreshed_at"),
    )
    return owner_path


def _apply_owner_quality_gates(
    cluster_embeddings: np.ndarray,
    cluster_durations: list[float],
    segment_count: int,
    embeddings_count: int,
    source: str,
) -> dict[str, Any] | None:
    """Return a low-quality payload when a gate fails, or None when all pass."""
    import numpy as np

    cluster_size = int(cluster_embeddings.shape[0])
    if cluster_size < OWNER_BOOTSTRAP_MIN_STMTS:
        return _bail_low_quality(
            LOW_QUALITY_REASON_TOO_FEW_STMTS,
            observed=cluster_size,
            threshold=OWNER_BOOTSTRAP_MIN_STMTS,
            segment_count=segment_count,
            embeddings_count=embeddings_count,
            source=source,
        )

    median_duration = (
        0.0 if not cluster_durations else float(np.median(cluster_durations))
    )
    if median_duration < OWNER_BOOTSTRAP_MIN_MEDIAN_DURATION_S:
        return _bail_low_quality(
            LOW_QUALITY_REASON_MEDIAN_DURATION_TOO_SHORT,
            observed=median_duration,
            threshold=OWNER_BOOTSTRAP_MIN_MEDIAN_DURATION_S,
            segment_count=segment_count,
            embeddings_count=embeddings_count,
            source=source,
        )

    intra_cosines = _pairwise_cosines(cluster_embeddings)
    intra_p25 = (
        0.0 if intra_cosines.size == 0 else float(np.percentile(intra_cosines, 25))
    )
    if intra_p25 < OWNER_BOOTSTRAP_MIN_INTRA_COSINE_P25:
        return _bail_low_quality(
            LOW_QUALITY_REASON_CLUSTER_TOO_DIFFUSE,
            observed=intra_p25,
            threshold=OWNER_BOOTSTRAP_MIN_INTRA_COSINE_P25,
            segment_count=segment_count,
            embeddings_count=embeddings_count,
            source=source,
        )

    return None


def _collect_manual_tag_embeddings(
    principal_id: str,
) -> tuple[np.ndarray, list[dict[str, Any]]]:
    """Load validated owner-tag embeddings and provenance for the principal."""
    import numpy as np

    load_embeddings_file, _ = _routes_helpers()

    rows = _load_manual_tag_rows(principal_id)
    if not rows:
        return np.empty((0, 256), dtype=np.float32), []

    embeddings_cache: dict[
        Path, tuple[np.ndarray, np.ndarray, np.ndarray | None] | None
    ] = {}
    fallback_cache: dict[Path, dict[int, float | None]] = {}
    embedding_rows: list[np.ndarray] = []
    provenance: list[dict[str, Any]] = []
    for row in rows:
        npz_path = row["segment_dir"] / f"{row['source']}.npz"
        emb_data = embeddings_cache.setdefault(npz_path, load_embeddings_file(npz_path))
        if emb_data is None:
            continue
        embeddings, statement_ids, durations_data = emb_data
        sentence_id = row["sentence_id"]
        matched_index = None
        for idx, statement_id in enumerate(statement_ids):
            if int(statement_id) == sentence_id:
                matched_index = idx
                break
        if matched_index is None:
            continue

        duration_s: float | None
        if durations_data is not None:
            duration_s = float(durations_data[matched_index])
        else:
            fallback_durations = fallback_cache.setdefault(
                row["jsonl_path"],
                _fallback_statement_durations(row["jsonl_path"]),
            )
            duration_s = fallback_durations.get(sentence_id)

        embedding_rows.append(np.asarray(embeddings[matched_index], dtype=np.float32))
        provenance.append(
            {
                "day": row["day"],
                "stream": row["stream"],
                "segment_key": row["segment_key"],
                "source": row["source"],
                "sentence_id": sentence_id,
                "duration_s": duration_s,
            }
        )

    if not embedding_rows:
        return np.empty((0, 256), dtype=np.float32), []
    return np.vstack(embedding_rows).astype(np.float32, copy=False), provenance


def load_owner_provisional_centroid(principal_id: str) -> np.ndarray | None:
    """Load or rebuild a cached provisional owner centroid from manual tags."""
    global _PROVISIONAL_GUARD_CACHE
    import numpy as np

    centroid_path = journal_entity_memory_path(principal_id) / "owner_centroid.npz"
    if centroid_path.exists():
        clear_owner_provisional_cache(principal_id)
        return None

    voiceprints_path = journal_entity_memory_path(principal_id) / "voiceprints.npz"
    if not voiceprints_path.exists():
        clear_owner_provisional_cache(principal_id)
        return None

    try:
        mtime_ns = voiceprints_path.stat().st_mtime_ns
    except OSError:
        clear_owner_provisional_cache(principal_id)
        return None

    rows = _load_manual_tag_rows(principal_id)
    manual_tag_count = len(rows)
    if manual_tag_count < OWNER_BOOTSTRAP_PROVISIONAL_GUARD_MIN_TAGS:
        clear_owner_provisional_cache(principal_id)
        return None

    if _PROVISIONAL_GUARD_CACHE is not None:
        cached = _PROVISIONAL_GUARD_CACHE.get(principal_id)
        if (
            cached is not None
            and cached[0] == mtime_ns
            and cached[1] == manual_tag_count
        ):
            return cached[2]

    embeddings, _provenance = _collect_manual_tag_embeddings(principal_id)
    embeddings_count = int(embeddings.shape[0])
    if embeddings_count < OWNER_BOOTSTRAP_PROVISIONAL_GUARD_MIN_TAGS:
        clear_owner_provisional_cache(principal_id)
        return None

    _, normalize_embedding = _routes_helpers()
    centroid = normalize_embedding(np.mean(embeddings, axis=0))
    if centroid is None:
        clear_owner_provisional_cache(principal_id)
        return None

    if _PROVISIONAL_GUARD_CACHE is None:
        _PROVISIONAL_GUARD_CACHE = {}
    _PROVISIONAL_GUARD_CACHE[principal_id] = (mtime_ns, manual_tag_count, centroid)
    return centroid


def _confirmed_owner_payload(centroid: OwnerCentroid) -> dict[str, Any]:
    return {
        "status": "confirmed",
        "recommendation": "confirmed",
        "cluster_size": centroid.cluster_size,
        "streams_represented": len(centroid.streams),
        "samples": [],
    }


def _candidate_payload_from_awareness(voiceprint: dict[str, Any]) -> dict[str, Any]:
    samples = voiceprint.get("samples")
    return {
        "status": "candidate",
        "cluster_size": int(voiceprint.get("cluster_size") or 0),
        "streams_represented": int(voiceprint.get("streams_represented") or 0),
        "recommendation": str(voiceprint.get("recommendation") or "single_stream"),
        "samples": samples if isinstance(samples, list) else [],
    }


def _candidate_no_cluster(
    reason: str,
    *,
    segments_checked: int,
    segments_available: int,
    embeddings_available: int,
) -> dict[str, Any]:
    _mark_no_cluster(segments_checked)
    return {
        "status": "no_cluster",
        "reason": reason,
        "segments_checked": int(segments_checked),
        "segments_available": int(segments_available),
        "embeddings_available": int(embeddings_available),
        "recommendation": "no_cluster",
    }


def _select_owner_candidate(
    principal_id: str | None,
) -> tuple[CandidateProfile | None, str | None]:
    from solstone.apps.speakers.candidate_tracker import CandidateTracker

    tracker = CandidateTracker()
    pool_exists = tracker.store_path.exists()
    candidates = tracker.load_all_candidates()
    if not pool_exists:
        return None, "pool_missing"
    if not candidates:
        return None, "pool_empty"

    eligible = [
        candidate
        for candidate in candidates
        if candidate.status != "rejected"
        and (
            candidate.confirmed_entity is None
            or candidate.confirmed_entity == principal_id
        )
    ]
    if not eligible:
        return None, "no_eligible_candidate"
    return (
        sorted(
            eligible,
            key=lambda item: (
                -item.n_intervals,
                -item.total_duration_s,
                item.cand_id,
            ),
        )[0],
        None,
    )


def _round_robin_source_segments(
    source_segments: list[dict[str, Any]],
) -> Iterator[dict[str, Any]]:
    grouped: dict[str, list[dict[str, Any]]] = {}
    for source_segment in source_segments:
        stream = str(source_segment.get("stream") or "")
        grouped.setdefault(stream, []).append(source_segment)

    streams = sorted(grouped)
    positions = {stream: 0 for stream in streams}
    while True:
        made_progress = False
        for stream in streams:
            position = positions[stream]
            if position >= len(grouped[stream]):
                continue
            yield grouped[stream][position]
            positions[stream] = position + 1
            made_progress = True
        if not made_progress:
            return


def _expand_owner_candidate(
    candidate: CandidateProfile,
    *,
    max_embeddings: int = OWNER_CANDIDATE_EXPANSION_MAX_EMBEDDINGS,
) -> OwnerCandidateExpansion:
    """Expand one candidate pool profile into usable owner bootstrap rows."""
    import numpy as np

    from solstone.apps.speakers.attribution import _load_integer_speaker_labels

    load_embeddings_file, normalize_embedding = _routes_helpers()

    embeddings_cache: dict[
        Path, tuple[np.ndarray, np.ndarray, np.ndarray | None] | None
    ] = {}
    labels_cache: dict[tuple[Path, str], dict[int, int]] = {}
    fallback_cache: dict[Path, dict[int, float | None]] = {}
    overlap_cache: dict[Path, float] = {}
    skipped: dict[str, int] = {}
    seen_segments: set[tuple[str, str, str]] = set()
    embedding_rows: list[np.ndarray] = []
    durations: list[float] = []
    provenance: list[dict[str, Any]] = []

    def skip(reason: str) -> None:
        skipped[reason] = skipped.get(reason, 0) + 1

    for source_segment in _round_robin_source_segments(candidate.source_segments):
        if len(embedding_rows) >= max_embeddings:
            break

        try:
            day = str(source_segment["day"])
            stream = str(source_segment["stream"])
            segment_key = str(source_segment["segment_key"])
            source = str(source_segment["source"])
            cluster_label = int(source_segment["cluster_label"])
        except (KeyError, TypeError, ValueError):
            skip("invalid_source_segment")
            continue

        segment_triplet = (day, stream, segment_key)
        if segment_triplet not in seen_segments:
            seen_segments.add(segment_triplet)

        seg_dir = segment_path(day, segment_key, stream, create=False)
        if not seg_dir.is_dir():
            skip("missing_segment_dir")
            continue

        jsonl_path = seg_dir / f"{source}.jsonl"
        overlap = overlap_cache.setdefault(
            jsonl_path,
            _read_segment_overlap_fraction(jsonl_path),
        )
        if overlap > NOISY_FLYWHEEL_OVERLAP_MAX:
            skip("noisy_overlap")
            continue

        npz_path = seg_dir / f"{source}.npz"
        emb_data = embeddings_cache.setdefault(npz_path, load_embeddings_file(npz_path))
        if emb_data is None:
            skip("missing_or_invalid_npz")
            continue
        embeddings, statement_ids, durations_data = emb_data

        labels_key = (seg_dir, source)
        integer_labels = labels_cache.setdefault(
            labels_key,
            _load_integer_speaker_labels(seg_dir, source),
        )
        if not integer_labels:
            skip("missing_integer_labels")
            continue

        statement_index = {
            int(statement_id): idx for idx, statement_id in enumerate(statement_ids)
        }
        fallback_durations = (
            {}
            if durations_data is not None
            else fallback_cache.setdefault(
                jsonl_path,
                _fallback_statement_durations(jsonl_path),
            )
        )

        for sentence_id, speaker_label in sorted(integer_labels.items()):
            if len(embedding_rows) >= max_embeddings:
                break
            if int(speaker_label) != cluster_label:
                continue
            matched_index = statement_index.get(int(sentence_id))
            if matched_index is None:
                skip("sentence_id_absent")
                continue
            if matched_index >= len(embeddings):
                skip("embedding_row_out_of_range")
                continue

            normalized = normalize_embedding(embeddings[matched_index])
            if normalized is None:
                skip("embedding_normalization_failed")
                continue

            duration_s: float | None
            if durations_data is not None and matched_index < len(durations_data):
                duration_s = float(durations_data[matched_index])
            else:
                duration_s = fallback_durations.get(int(sentence_id))
            if duration_s is not None:
                durations.append(float(duration_s))

            embedding_rows.append(np.asarray(normalized, dtype=np.float32))
            provenance.append(
                {
                    "day": day,
                    "stream": stream,
                    "segment_key": segment_key,
                    "source": source,
                    "sentence_id": int(sentence_id),
                    "duration_s": duration_s,
                }
            )

    if not embedding_rows:
        embeddings_matrix = np.empty((0, 0), dtype=np.float32)
    else:
        embeddings_matrix = np.vstack(embedding_rows).astype(np.float32, copy=False)

    return OwnerCandidateExpansion(
        embeddings=embeddings_matrix,
        durations=durations,
        provenance=provenance,
        segments_checked=len(seen_segments),
        segments_available=int(candidate.n_segments),
        embeddings_available=int(embeddings_matrix.shape[0]),
        skipped=skipped,
    )


def _owner_candidate_samples(
    embeddings: np.ndarray,
    centroid: np.ndarray,
    provenance: list[dict[str, Any]],
) -> list[dict[str, Any]]:
    import numpy as np

    similarities = np.dot(embeddings, centroid)
    sorted_positions = np.argsort(similarities)[::-1]

    samples: list[dict[str, Any]] = []
    seen_segments: set[tuple[str, str, str]] = set()

    for position in sorted_positions:
        record = provenance[int(position)]
        segment_triplet = (record["day"], record["stream"], record["segment_key"])
        if segment_triplet in seen_segments:
            continue
        seen_segments.add(segment_triplet)
        samples.append(
            {
                **record,
                "audio_url": resolve_audio_url(
                    record["day"],
                    record["stream"],
                    record["segment_key"],
                    record["source"],
                ),
            }
        )
        if len(samples) == 3:
            return samples

    for position in sorted_positions:
        record = provenance[int(position)]
        sample = {
            **record,
            "audio_url": resolve_audio_url(
                record["day"],
                record["stream"],
                record["segment_key"],
                record["source"],
            ),
        }
        if sample in samples:
            continue
        samples.append(sample)
        if len(samples) == 3:
            break
    return samples


def detect_owner_candidate() -> dict[str, Any]:
    """Detect a likely owner voice centroid from the candidate pool.

    The cheap statement-count prefilter opens no segment files, so its
    low-quality payload reports the candidate's recorded n_segments and
    n_intervals as segments_available and embeddings_available.
    """
    import numpy as np

    confirmed = load_owner_centroid()
    if confirmed is not None:
        if get_current().get("voiceprint", {}).get("status") != "confirmed":
            # Repair stale awareness so status polling stops auto-detecting.
            update_state(
                "voiceprint",
                {
                    "status": "confirmed",
                    "cluster_size": confirmed.cluster_size,
                    "confirmed_at": _iso_now(),
                },
            )
        return _confirmed_owner_payload(confirmed)

    candidate_path = _owner_candidate_path()
    voiceprint = get_current().get("voiceprint", {})
    if candidate_path.exists() and voiceprint.get("status") == "candidate":
        return _candidate_payload_from_awareness(voiceprint)

    principal_id = _principal_id_or_none()
    candidate, missing_reason = _select_owner_candidate(principal_id)
    if candidate is None:
        return _candidate_no_cluster(
            missing_reason or "no_eligible_candidate",
            segments_checked=0,
            segments_available=0,
            embeddings_available=0,
        )

    if candidate.n_intervals < OWNER_BOOTSTRAP_MIN_STMTS:
        return _bail_low_quality(
            LOW_QUALITY_REASON_TOO_FEW_STMTS,
            candidate.n_intervals,
            OWNER_BOOTSTRAP_MIN_STMTS,
            candidate.n_segments,
            candidate.n_intervals,
            source=OWNER_CANDIDATE_SOURCE,
        )

    expansion = _expand_owner_candidate(
        candidate,
        max_embeddings=OWNER_CANDIDATE_EXPANSION_MAX_EMBEDDINGS,
    )
    if expansion.embeddings_available == 0:
        return _candidate_no_cluster(
            "candidate_no_usable_embeddings",
            segments_checked=expansion.segments_checked,
            segments_available=expansion.segments_available,
            embeddings_available=0,
        )

    low_quality = _apply_owner_quality_gates(
        expansion.embeddings,
        expansion.durations,
        expansion.segments_checked,
        expansion.embeddings_available,
        source=OWNER_CANDIDATE_SOURCE,
    )
    if low_quality is not None:
        return low_quality

    _, normalize_embedding = _routes_helpers()
    centroid = normalize_embedding(np.mean(expansion.embeddings, axis=0))
    if centroid is None:
        return _candidate_no_cluster(
            "candidate_centroid_unusable",
            segments_checked=expansion.segments_checked,
            segments_available=expansion.segments_available,
            embeddings_available=expansion.embeddings_available,
        )

    cluster_streams = {record["stream"] for record in expansion.provenance}
    streams_represented = len(cluster_streams)
    cluster_size = int(expansion.embeddings.shape[0])
    recommendation = "ready" if streams_represented > 1 else "single_stream"
    samples = _owner_candidate_samples(
        expansion.embeddings, centroid, expansion.provenance
    )

    version = _iso_now()
    try:
        save_npz(
            _owner_candidate_path(create=True),
            {
                "centroid": centroid.astype(np.float32),
                "cluster_size": np.array(cluster_size, dtype=np.int32),
                "threshold": np.array(OWNER_THRESHOLD, dtype=np.float32),
                "version": np.array(version),
            },
            expected_keys=("centroid", "cluster_size", "threshold", "version"),
        )
    except LockTimeout as exc:
        return _voiceprint_busy_result(exc)

    update_state(
        "voiceprint",
        {
            "status": "candidate",
            "cluster_size": cluster_size,
            "streams_represented": streams_represented,
            "recommendation": recommendation,
            "samples": samples,
            "detected_at": version,
        },
    )

    return {
        "status": "candidate",
        "cluster_size": cluster_size,
        "streams_represented": streams_represented,
        "recommendation": recommendation,
        "samples": samples,
    }


def owner_detection_ready() -> dict[str, Any]:
    """Check cheap owner voice candidate state without running detection."""
    if load_owner_centroid() is not None:
        return {"ready": False, "reason": "centroid_exists"}

    voiceprint = get_current().get("voiceprint", {})
    rejected_at = voiceprint.get("rejected_at")
    if rejected_at:
        try:
            rejection_time = datetime.fromisoformat(str(rejected_at))
            now = datetime.now(rejection_time.tzinfo)
            days_since = (now - rejection_time).days
            if days_since < 14:
                return {
                    "ready": False,
                    "reason": "cooldown",
                    "days_remaining": 14 - days_since,
                }
        except (ValueError, TypeError):
            pass

    if _owner_candidate_path().exists() and voiceprint.get("status") == "candidate":
        if voiceprint.get("recommendation") == "ready":
            return {
                "ready": True,
                "reason": "candidate_found",
                "cluster_size": voiceprint.get("cluster_size"),
                "streams_represented": voiceprint.get("streams_represented"),
                "samples": voiceprint.get("samples", []),
            }
        return {
            "ready": False,
            "reason": voiceprint.get("recommendation") or "candidate_not_ready",
        }

    return {"ready": False, "reason": "no_candidate"}


def _load_owner_voiceprint_summary(
    principal_id: str,
) -> tuple[float | None, list[str]]:
    """Compute owner cohesion and stream list from the principal voiceprints."""
    voiceprints = load_entity_voiceprints_file(principal_id)
    if voiceprints is None:
        return None, []

    embeddings, metadata = voiceprints
    streams = sorted(
        {
            str(item["stream"])
            for item in metadata
            if isinstance(item.get("stream"), str) and item.get("stream")
        }
    )
    return compute_intra_cosine_p25(embeddings), streams


def load_owner_centroid() -> OwnerCentroid | None:
    """Load the confirmed owner centroid and metadata for the principal entity."""
    import numpy as np

    principal = get_journal_principal()
    if not principal:
        return None

    principal_id = str(principal["id"])
    centroid_path = journal_entity_memory_path(principal_id) / "owner_centroid.npz"
    if not centroid_path.exists():
        return None

    try:
        data = load_npz(centroid_path)
        if data is None:
            return None
        centroid = data.get("centroid")
        threshold = data.get("threshold")
        cluster_size = data.get("cluster_size")
        last_refreshed_at = data.get("last_refreshed_at")
        if centroid is None or threshold is None or cluster_size is None:
            return None

        normalized = centroid.astype(np.float32).reshape(-1)
        norm = np.linalg.norm(normalized)
        if norm == 0:
            return None
        normalized = normalized / norm
        refreshed = (
            str(np.asarray(last_refreshed_at).item())
            if last_refreshed_at is not None
            else ""
        )
        size = int(np.asarray(cluster_size).item())
        thresh = float(np.asarray(threshold).item())

        intra_p25, streams = _load_owner_voiceprint_summary(principal_id)
        return OwnerCentroid(
            centroid=normalized,
            threshold=thresh,
            cluster_size=size,
            last_refreshed_at=refreshed,
            intra_cosine_p25=intra_p25,
            streams=streams,
        )
    except Exception as exc:
        logger.warning("Failed to load owner centroid %s: %s", centroid_path, exc)
        return None


def classify_sentences(
    day: str, stream: str, segment_key: str, source: str
) -> list[dict[str, Any]]:
    """Classify segment sentences against the confirmed owner centroid."""
    import numpy as np

    load_embeddings_file, normalize_embedding = _routes_helpers()

    centroid_data = load_owner_centroid()
    if centroid_data is None:
        return []

    centroid = centroid_data.centroid
    threshold = centroid_data.threshold
    emb_data = load_embeddings_file(
        segment_path(day, segment_key, stream, create=False) / f"{source}.npz"
    )
    if emb_data is None:
        return []

    embeddings, statement_ids, _ = emb_data
    results = []
    for embedding, statement_id in zip(embeddings, statement_ids):
        normalized = normalize_embedding(embedding)
        if normalized is None:
            continue
        score = float(np.dot(normalized, centroid))
        results.append(
            {
                "sentence_id": int(statement_id),
                "is_owner": score >= threshold,
                "score": round(score, 4),
            }
        )
    return results


def confirm_owner_candidate() -> dict[str, Any]:
    """Confirm the current owner voice candidate and persist the centroid.

    Moves the candidate centroid from awareness/ to the principal entity's
    memory directory as owner_centroid.npz. Updates awareness state to
    "confirmed".

    Returns a dict with status and principal_id on success, or an error key.
    """
    import numpy as np

    candidate_path = _owner_candidate_path()
    if not candidate_path.exists():
        return {"error": "No candidate available"}

    try:
        data = load_npz(candidate_path)
        if data is None:
            return {"error": "No candidate available"}
        centroid = data["centroid"]
        cluster_size = int(np.asarray(data["cluster_size"]).item())
    except Exception as e:
        logger.warning("Failed to load owner candidate %s: %s", candidate_path, e)
        return {"error": "No candidate available"}

    principal = ensure_principal_entity()
    if principal is None:
        return {"error": "No principal entity found"}

    try:
        _write_owner_centroid(principal["id"], np.asarray(centroid), cluster_size)
    except LockTimeout as exc:
        return _voiceprint_busy_result(exc)
    clear_owner_provisional_cache(principal["id"])
    candidate_path.unlink(missing_ok=True)

    update_state(
        "voiceprint",
        {
            "status": "confirmed",
            "cluster_size": cluster_size,
            "confirmed_at": _iso_now(),
        },
    )

    return {
        "status": "confirmed",
        "principal_id": principal["id"],
        "cluster_size": cluster_size,
    }


def bootstrap_owner_from_manual_tags() -> dict[str, Any]:
    """Promote validated principal manual tags into a confirmed owner centroid."""
    import numpy as np

    _, normalize_embedding = _routes_helpers()

    principal_id = _principal_id_or_none()
    if principal_id is None:
        return {"error": "No principal entity found"}

    centroid_path = journal_entity_memory_path(principal_id) / "owner_centroid.npz"
    if centroid_path.exists():
        clear_owner_provisional_cache(principal_id)
        cluster_size = None
        try:
            data = load_npz(centroid_path)
            if data is not None:
                cluster_size = int(np.asarray(data["cluster_size"]).item())
        except Exception as exc:
            logger.warning(
                "Failed to read owner centroid metadata %s: %s",
                centroid_path,
                exc,
            )
        return {
            "status": "confirmed",
            "principal_id": principal_id,
            "cluster_size": cluster_size,
        }

    embeddings, provenance = _collect_manual_tag_embeddings(principal_id)
    segment_count = len(
        {
            (record["day"], record["stream"], record["segment_key"])
            for record in provenance
            if record.get("stream")
        }
    )
    embeddings_count = int(embeddings.shape[0])
    durations = [
        float(record["duration_s"])
        for record in provenance
        if record.get("duration_s") is not None
    ]
    low_quality = _apply_owner_quality_gates(
        embeddings,
        durations,
        segment_count,
        embeddings_count,
        source="manual_tags",
    )
    if low_quality is not None:
        return low_quality

    centroid = normalize_embedding(np.mean(embeddings, axis=0))
    if centroid is None:
        return _bail_low_quality(
            LOW_QUALITY_REASON_CLUSTER_TOO_DIFFUSE,
            observed=0.0,
            threshold=OWNER_BOOTSTRAP_MIN_INTRA_COSINE_P25,
            segment_count=segment_count,
            embeddings_count=embeddings_count,
            source="manual_tags",
        )

    try:
        _write_owner_centroid(principal_id, centroid, embeddings_count)
    except LockTimeout as exc:
        return _voiceprint_busy_result(exc)
    clear_owner_provisional_cache(principal_id)
    update_state(
        "voiceprint",
        {
            "status": "confirmed",
            "cluster_size": embeddings_count,
            "confirmed_at": _iso_now(),
        },
    )
    return {
        "status": "confirmed",
        "principal_id": principal_id,
        "cluster_size": embeddings_count,
    }


def reject_owner_candidate() -> dict[str, Any]:
    """Reject the current owner voice candidate and enter cooldown.

    Deletes the candidate file and records rejection with timestamp in
    awareness state. The timestamp enables 14-day cooldown enforcement.

    Returns a dict with the updated status.
    """
    candidate_path = _owner_candidate_path()
    candidate_path.unlink(missing_ok=True)
    update_state(
        "voiceprint",
        {
            "status": "rejected",
            "rejected_at": _iso_now(),
        },
    )
    return {"status": "rejected"}
