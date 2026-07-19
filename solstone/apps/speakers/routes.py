# SPDX-License-Identifier: AGPL-3.0-only
# Copyright (c) 2026 sol pbc

"""Speaker voiceprint management app - sentence-based embeddings.

Voiceprints are stored at the journal level (not per-facet) since a person's
voice is the same regardless of which facet they appear in.
"""

from __future__ import annotations

import json
import logging
import re
from dataclasses import dataclass
from datetime import date
from pathlib import Path
from typing import TYPE_CHECKING, Any

from flask import (
    Blueprint,
    current_app,
    jsonify,
    request,
    send_file,
)

from solstone.apps.speakers.attribution import (
    accumulate_voiceprints,
    append_speaker_correction,
    apply_label_patches,
    attribute_segment,
    backfill_last_seen,
    backfill_segments,
    propagate_speaker_correction,
    save_speaker_labels,
)
from solstone.apps.speakers.audio import audio_serve_url, resolve_audio_file
from solstone.apps.speakers.bootstrap import (
    bootstrap_voiceprints,
    link_import,
    merge_names,
    resolve_name_variants,
    seed_from_imports,
)
from solstone.apps.speakers.copy import (
    OWNER_DETECT_CANDIDATE_GUIDANCE,
    OWNER_REJECTION_COOLDOWN_GUIDANCE,
    SPK_OVERVIEW_KNOWN_VOICES_SORTS,
    speaker_copy_payload,
)
from solstone.apps.speakers.discovery import (
    discover_unknown_speakers,
    identify_cluster,
    load_resolved_cluster,
)
from solstone.apps.speakers.encoder_config import (
    OWNER_BOOTSTRAP_MIN_STMTS,
    OWNER_THRESHOLD,
)
from solstone.apps.speakers.owner import (
    bootstrap_owner_from_manual_tags,
    classify_sentences,
    confirm_owner_candidate,
    detect_owner_candidate,
    ensure_principal_entity,
    load_owner_bootstrap_diagnostics,
    load_owner_centroid,
    load_owner_manual_bootstrap_guidance,
    load_owner_provisional_centroid,
    owner_detection_ready,
    owner_rejection_cooldown_payload,
    principal_identity_or_none,
    rebuild_owner_centroid,
    reject_owner_candidate,
)
from solstone.apps.speakers.status import get_speakers_status
from solstone.apps.speakers.suggest import format_suggestions, suggest_opportunities
from solstone.apps.speakers.wipe import wipe_speaker_artifacts
from solstone.apps.utils import log_app_action
from solstone.convey.date_nav import build_date_nav_index
from solstone.convey.day_grid import build_day_grid_payload
from solstone.convey.reasons import (
    ENTITY_BLOCKED,
    ENTITY_NOT_FOUND,
    FILE_NOT_FOUND,
    FILE_READ_FAILED,
    INVALID_DAY,
    INVALID_MONTH,
    INVALID_REQUEST_VALUE,
    INVALID_SEGMENT_OR_STREAM,
    MISSING_REQUEST_BODY,
    MISSING_REQUIRED_FIELD,
    SPEAKER_ATTRIBUTION_STATE_INVALID,
    SPEAKER_COMMAND_FAILED,
    SPEAKER_LABELS_BUSY,
    SPEAKER_NOT_FOUND,
    SPEAKER_OWNER_CENTROID_REQUIRED,
    SPEAKER_OWNER_IDENTITY_REQUIRED,
    SPEAKER_OWNER_VOICE_TOO_CLOSE,
    SPEAKER_REVIEW_UNAVAILABLE,
    SPEAKER_SENTENCE_MISSING,
    SPEAKER_VOICEPRINT_BUSY,
)
from solstone.convey.utils import (
    DATE_RE,
    error_response,
    safe_day_path,
    success_response,
)
from solstone.think.awareness import get_current
from solstone.think.entities import find_matching_entity
from solstone.think.entities.journal import (
    ensure_journal_entity_memory,
    get_journal_principal,
    journal_entity_memory_path,
    load_all_journal_entities,
    load_journal_entity,
)
from solstone.think.journal_io.errors import LockTimeout
from solstone.think.journal_io.npz import load_npz, update_npz
from solstone.think.media import MIME_TYPES
from solstone.think.utils import (
    STREAM_RE,
    day_dirs,
    day_path,
    get_journal,
    iter_segments,
    now_ms,
    segment_parse,
    segment_start_ts_ms,
)
from solstone.think.utils import segment_key as validate_segment_key
from solstone.think.utils import segment_path as get_segment_path

if TYPE_CHECKING:
    import numpy as np

    from solstone.think.entities.core import EntityDict

logger = logging.getLogger(__name__)
SEGMENT_KEY_RE = re.compile(r"\d{6}_\d+")
VOICEPRINT_KEYS = ("embeddings", "metadata")
OWNER_STATUS_CANDIDATE = "candidate"
OWNER_STATUS_CONFIRMED = "confirmed"
OWNER_STATUS_ROUTING_TOKENS = {
    "candidate": OWNER_STATUS_CANDIDATE,
    "confirmed": OWNER_STATUS_CONFIRMED,
}
PROPAGATION_CLI_VERB = "speakers propagate-correction"


@dataclass(frozen=True)
class VoiceprintRemovalResult:
    outcome: str
    entity_id: str
    keys_removed: list[str]
    file_deleted: bool
    voiceprints_path: Path | None


speakers_bp = Blueprint(
    "app:speakers",
    __name__,
    url_prefix="/app/speakers",
)


def _normalize_embedding(emb: np.ndarray) -> np.ndarray | None:
    from solstone.think.entities import normalize_embedding

    return normalize_embedding(emb)


def _parse_time_to_seconds(time_str: str) -> int:
    """Parse HH:MM:SS time string to seconds."""
    parts = time_str.split(":")
    return int(parts[0]) * 3600 + int(parts[1]) * 60 + int(parts[2])


def _time_to_seconds(t) -> int:
    """Convert datetime.time to seconds since midnight."""
    return t.hour * 3600 + t.minute * 60 + t.second


def _load_embeddings_file(
    npz_path: Path,
) -> tuple[np.ndarray, np.ndarray, np.ndarray | None] | None:
    """Load embeddings, statement_ids, and optional durations from NPZ file.

    Returns tuple of (embeddings, statement_ids, durations_s) or None if file is invalid.
    """
    if not npz_path.exists():
        return None

    try:
        data = load_npz(npz_path)
        if data is None:
            return None
        embeddings = data.get("embeddings")
        statement_ids = data.get("statement_ids")
        durations_s = data.get("durations_s")

        if embeddings is None or statement_ids is None:
            return None

        return embeddings, statement_ids, durations_s
    except Exception as e:
        logger.warning("Failed to load embeddings %s: %s", npz_path, e)
        return None


def _load_segment_speakers(segment_dir: Path) -> list[str]:
    """Load speaker names from segment's speakers.json.

    Args:
        segment_dir: Path to segment directory

    Returns:
        List of speaker name strings, or empty list if not found/invalid.
    """
    speakers_path = segment_dir / "talents" / "speakers.json"
    if not speakers_path.exists():
        return []

    try:
        with open(speakers_path, "r", encoding="utf-8") as f:
            data = json.load(f)

        # Must be a list of strings
        if not isinstance(data, list):
            return []

        # Filter to only strings
        return [name for name in data if isinstance(name, str) and name.strip()]
    except (json.JSONDecodeError, OSError) as e:
        logger.warning("Failed to load speakers.json from %s: %s", segment_dir, e)
        return []


def _load_entity_voiceprints_file(
    entity_id: str,
) -> tuple[np.ndarray, list[dict]] | None:
    from solstone.think.entities import load_entity_voiceprints_file

    return load_entity_voiceprints_file(entity_id)


def _save_voiceprint(
    entity_id: str,
    embedding: np.ndarray,
    day: str,
    segment_key: str,
    source: str,
    sentence_id: int,
    stream: str | None = None,
) -> Path:
    """Save a voiceprint to the entity's journal-level voiceprints.npz.

    Voiceprints are stored at entities/<id>/voiceprints.npz since a person's
    voice is the same across all facets.

    Args:
        entity_id: Entity ID (slug)
        embedding: Normalized embedding vector (256-dim)
        day: Day string (YYYYMMDD)
        segment_key: Segment directory name
        source: Audio source stem
        sentence_id: Sentence ID within transcript

    Returns:
        Path to the voiceprints.npz file
    """
    folder = ensure_journal_entity_memory(entity_id)
    npz_path = folder / "voiceprints.npz"

    metadata = {
        "day": day,
        "segment_key": segment_key,
        "source": source,
        "sentence_id": sentence_id,
        "added_at": now_ms(),
        "last_seen_ts": segment_start_ts_ms(day, segment_key),
    }
    if stream:
        metadata["stream"] = stream
    metadata_json = json.dumps(metadata)

    def transform(current: dict[str, np.ndarray]) -> dict[str, np.ndarray]:
        import numpy as np

        if current:
            existing_embeddings = current["embeddings"]
            existing_metadata = current["metadata"]
        else:
            existing_embeddings = np.empty((0, 256), dtype=np.float32)
            existing_metadata = np.asarray([], dtype=str)

        return {
            "embeddings": np.vstack(
                [
                    existing_embeddings,
                    embedding.reshape(1, -1).astype(np.float32),
                ]
            ),
            "metadata": np.append(existing_metadata, metadata_json),
        }

    update_npz(npz_path, transform, expected_keys=VOICEPRINT_KEYS)
    return npz_path


def _remove_voiceprint(
    entity_id: str,
    day: str,
    segment_key: str,
    source: str,
    sentence_id: int,
) -> VoiceprintRemovalResult:
    """Remove a specific voiceprint entry from an entity's voiceprints.npz.

    Matches by (day, segment_key, source, sentence_id) metadata key.
    """
    rendered_key = _render_voiceprint_key(day, segment_key, source, sentence_id)
    try:
        folder = journal_entity_memory_path(entity_id)
    except (RuntimeError, ValueError):
        return VoiceprintRemovalResult(
            outcome="not_found",
            entity_id=entity_id,
            keys_removed=[],
            file_deleted=False,
            voiceprints_path=None,
        )

    npz_path = folder / "voiceprints.npz"
    if not npz_path.exists():
        return VoiceprintRemovalResult(
            outcome="not_found",
            entity_id=entity_id,
            keys_removed=[],
            file_deleted=False,
            voiceprints_path=npz_path,
        )

    outcome = "not_found"
    keys_removed: list[str] = []

    def transform(current: dict[str, np.ndarray]) -> dict[str, np.ndarray] | None:
        nonlocal outcome, keys_removed
        embeddings = current.get("embeddings")
        metadata_arr = current.get("metadata")
        if embeddings is None or metadata_arr is None:
            outcome = "not_found"
            return None

        keep = []
        matched = False
        for i, m_str in enumerate(metadata_arr):
            try:
                m = json.loads(str(m_str))
                if (
                    m.get("day") == day
                    and m.get("segment_key") == segment_key
                    and m.get("source") == source
                    and m.get("sentence_id") == sentence_id
                ):
                    matched = True
                    continue
            except (json.JSONDecodeError, TypeError):
                pass
            keep.append(i)

        if not matched:
            outcome = "not_found"
            return None

        keys_removed = [rendered_key]
        if not keep:
            outcome = "unlinked"
            return {}

        outcome = "rewritten"
        return {
            "embeddings": embeddings[keep],
            "metadata": metadata_arr[keep],
        }

    update_npz(npz_path, transform, expected_keys=VOICEPRINT_KEYS)
    return VoiceprintRemovalResult(
        outcome=outcome,
        entity_id=entity_id,
        keys_removed=keys_removed,
        file_deleted=outcome == "unlinked",
        voiceprints_path=npz_path,
    )


def _render_voiceprint_key(
    day: str,
    segment_key: str,
    source: str,
    sentence_id: int,
) -> str:
    return f"{day}/{segment_key}/{source}#{sentence_id}"


def _voiceprint_removal_payload(result: VoiceprintRemovalResult) -> dict[str, Any]:
    rel_path = None
    if result.voiceprints_path is not None:
        journal_root = Path(get_journal())
        try:
            rel_path = str(result.voiceprints_path.relative_to(journal_root))
        except ValueError:
            rel_path = str(result.voiceprints_path)
    return {
        "outcome": result.outcome,
        "entity_id": result.entity_id,
        "keys_removed": result.keys_removed,
        "file_deleted": result.file_deleted,
        "path": rel_path,
    }


def _propagation_reversal_payload(old_speaker: str, new_speaker: str) -> dict[str, str]:
    return {
        "verb": PROPAGATION_CLI_VERB,
        "old_speaker": new_speaker,
        "new_speaker": old_speaker,
        "bounded_to": "segments where these two appear",
    }


def _propagation_response_payload(result: dict[str, Any]) -> dict[str, Any]:
    payload = dict(result)
    payload["reversal"] = _propagation_reversal_payload(
        str(result["old_speaker"]),
        str(result["new_speaker"]),
    )
    return payload


def _propagation_offer(old_speaker: str | None, new_speaker: str) -> dict[str, Any]:
    if not old_speaker:
        return {
            "available": False,
            "reason": "no_old_speaker",
            "statement_count": 0,
            "segment_count": 0,
        }
    try:
        result = propagate_speaker_correction(old_speaker, new_speaker, commit=False)
    except Exception:
        logger.exception("Failed to preview speaker correction propagation")
        return {
            "available": False,
            "reason": "preview_failed",
            "statement_count": 0,
            "segment_count": 0,
        }

    statement_count = int(result.get("statement_count") or 0)
    segment_count = int(result.get("segment_count") or 0)
    if statement_count == 0:
        return {
            "available": False,
            "reason": "no_changes",
            "statement_count": 0,
            "segment_count": 0,
        }

    return {
        "available": True,
        "statement_count": statement_count,
        "segment_count": segment_count,
        "route": "/app/speakers/api/propagate-correction",
        "request": {
            "old_speaker": old_speaker,
            "new_speaker": new_speaker,
            "commit": False,
        },
    }


def _load_speaker_labels(segment_dir: Path) -> dict | None:
    """Load speaker_labels.json from a segment's talents/ directory.

    Returns the parsed JSON dict, or None if not found/invalid.
    """
    labels_path = segment_dir / "talents" / "speaker_labels.json"
    if not labels_path.is_file():
        return None
    try:
        with open(labels_path) as f:
            return json.load(f)
    except (json.JSONDecodeError, OSError):
        return None


def _audio_embedding_sources(segment_path: Path) -> list[str]:
    """Return audio embedding source stems from a segment directory."""
    return sorted(
        path.stem
        for path in segment_path.glob("*.npz")
        if path.stem.endswith("_audio") or path.stem == "audio"
    )


def _speaker_sentence_needs_review(
    label: dict | None, labels_data: dict | None
) -> bool:
    """Return the shared web/CLI review flag for one sentence."""
    if label:
        return label.get("confidence") == "medium" or not label.get("speaker")
    return True if labels_data else False


def _segment_has_speaker_review(labels_data: dict | None) -> bool:
    if not labels_data:
        return False
    return any(
        _speaker_sentence_needs_review(label, labels_data)
        for label in labels_data.get("labels", [])
    )


def _load_speaker_corrections(segment_dir: Path) -> list[dict]:
    """Load speaker_corrections.json from a segment's talents/ directory.

    Returns list of correction entries, or empty list if not found.
    """
    corr_path = segment_dir / "talents" / "speaker_corrections.json"
    if not corr_path.is_file():
        return []
    try:
        with open(corr_path) as f:
            data = json.load(f)
        return data.get("corrections", [])
    except (json.JSONDecodeError, OSError):
        return []


def _check_owner_contamination(embedding: np.ndarray) -> bool:
    """Check if an embedding is too close to the owner centroid.

    Returns True if the embedding is contaminated (should NOT be saved
    to a non-owner entity's voiceprints).
    """
    import numpy as np

    from solstone.apps.speakers.owner import load_owner_centroid

    centroid_data = load_owner_centroid()
    if centroid_data is not None:
        owner_centroid = centroid_data.centroid
        owner_threshold = centroid_data.threshold
    else:
        principal_id = _principal_id_or_none()
        if principal_id is None:
            return False
        owner_centroid = load_owner_provisional_centroid(principal_id)
        if owner_centroid is None:
            return False
        owner_threshold = OWNER_THRESHOLD
    score = float(np.dot(embedding, owner_centroid))
    return score >= owner_threshold


def _principal_id_or_none() -> str | None:
    """Return the current journal principal id if one exists."""
    principal = get_journal_principal()
    if principal is None:
        return None
    return str(principal["id"])


def _ensure_attribution_target(entity_id: str) -> EntityDict | None:
    """Resolve an attribution target, creating the principal on first owner tag."""
    entity = load_journal_entity(entity_id)
    if entity is not None:
        return entity
    if get_journal_principal() is not None:
        return None
    identity = principal_identity_or_none()
    if identity is None or identity[0] != entity_id:
        return None
    return ensure_principal_entity()


def _assign_attribution_impl(
    day: Any,
    stream: Any,
    segment_key: Any,
    source: Any,
    sentence_id: Any,
    speaker: Any,
) -> Any:
    """Assign a speaker to a sentence, inserting a label row when a stub omitted it."""
    if not all([day, stream, segment_key, source, sentence_id is not None, speaker]):
        return error_response(
            MISSING_REQUIRED_FIELD,
            detail="Missing required fields",
        )
    if not isinstance(day, str) or not DATE_RE.fullmatch(day):
        return error_response(
            INVALID_DAY,
            detail="Use a valid day, stream, and segment, then pick a sentence.",
        )
    if not isinstance(segment_key, str) or not SEGMENT_KEY_RE.fullmatch(segment_key):
        return error_response(
            INVALID_SEGMENT_OR_STREAM,
            detail="Use a valid day, stream, and segment, then pick a sentence.",
        )
    if not isinstance(stream, str) or not STREAM_RE.fullmatch(stream):
        return error_response(
            INVALID_SEGMENT_OR_STREAM,
            detail="Use a valid day, stream, and segment, then pick a sentence.",
        )
    try:
        sentence_id_int = int(sentence_id)
    except (TypeError, ValueError):
        return error_response(
            INVALID_REQUEST_VALUE,
            detail="Use a numeric sentence id.",
        )
    speaker_id = str(speaker)

    segment_dir = get_segment_path(day, segment_key, stream)
    labels_data = _load_speaker_labels(segment_dir)
    if not labels_data:
        return error_response(
            SPEAKER_REVIEW_UNAVAILABLE,
            detail="No speaker labels found",
        )

    label = None
    for item in labels_data.get("labels", []):
        if item.get("sentence_id") == sentence_id_int:
            label = item
            break

    existing_speaker = label.get("speaker") if label else None
    if existing_speaker == speaker_id and label.get("method") == "user_assigned":
        return success_response({"status": "already_assigned"})
    if existing_speaker:
        return error_response(
            SPEAKER_ATTRIBUTION_STATE_INVALID,
            detail="Pick a sentence without a speaker.",
        )

    sentences, _ = _load_sentences(day, segment_key, source, stream=stream)
    if not any(sentence.get("id") == sentence_id_int for sentence in sentences):
        return error_response(
            SPEAKER_SENTENCE_MISSING,
            detail="Pick a different sentence with an embedding.",
        )

    emb = _get_sentence_embedding(
        day, segment_key, source, sentence_id_int, stream=stream
    )
    if emb is None:
        return error_response(
            SPEAKER_SENTENCE_MISSING,
            detail="Pick a different sentence with an embedding.",
        )

    target_entity = _ensure_attribution_target(speaker_id)
    if not target_entity:
        return error_response(
            SPEAKER_NOT_FOUND,
            detail=f"Entity '{speaker_id}' not found",
        )
    if target_entity.get("blocked"):
        return error_response(
            ENTITY_BLOCKED,
            detail="Choose an unblocked speaker.",
        )

    principal_id = _principal_id_or_none()
    if speaker_id != principal_id and _check_owner_contamination(emb):
        return error_response(
            SPEAKER_OWNER_VOICE_TOO_CLOSE,
            detail="Embedding too similar to owner voice; cannot save",
        )

    try:
        _save_voiceprint(
            speaker_id, emb, day, segment_key, source, sentence_id_int, stream=stream
        )
    except LockTimeout as exc:
        return _voiceprint_busy_response(exc)

    old_method = label.get("method") if label else None
    try:
        apply_label_patches(
            segment_dir,
            {
                sentence_id_int: {
                    "speaker": speaker_id,
                    "confidence": "high",
                    "method": "user_assigned",
                }
            },
            allow_insert=True,
        )
    except LockTimeout as exc:
        return _labels_busy_response(exc)

    try:
        append_speaker_correction(
            segment_dir,
            {
                "sentence_id": sentence_id_int,
                "original_speaker": None,
                "corrected_speaker": speaker_id,
                "original_method": old_method,
                "timestamp": now_ms(),
            },
        )
    except LockTimeout as exc:
        return _labels_busy_response(exc)

    log_app_action(
        app="speakers",
        facet=None,
        action="attribution_assign",
        params={
            "day": day,
            "stream": stream,
            "segment_key": segment_key,
            "source": source,
            "sentence_id": sentence_id_int,
            "speaker": speaker_id,
        },
    )
    _maybe_bootstrap_owner_from_attestation(principal_id, speaker_id)

    return success_response({"status": "assigned", "speaker": speaker_id})


def _voiceprint_busy_response(exc: LockTimeout) -> Any:
    logger.warning("voiceprint storage busy for %s", exc.path)
    return error_response(
        SPEAKER_VOICEPRINT_BUSY,
        detail="voiceprint storage is busy; try again",
    )


def _labels_busy_response(exc: LockTimeout) -> Any:
    logger.warning("speaker labels busy for %s", exc.path)
    return error_response(
        SPEAKER_LABELS_BUSY,
        detail="speaker labels are busy; try again",
    )


def _owner_bootstrap_status_fields() -> dict[str, Any]:
    """Return shared owner bootstrap diagnostics for status surfaces."""
    diagnostics = load_owner_bootstrap_diagnostics(_principal_id_or_none())
    return {
        **diagnostics,
        "segments_with_embeddings": diagnostics["segments_available"],
    }


def _maybe_bootstrap_owner_from_attestation(
    principal_id: str | None, speaker_id: str | None
) -> None:
    """Refresh manual owner bootstrap state after a principal attestation."""
    if principal_id is None or speaker_id != principal_id:
        return
    try:
        result = bootstrap_owner_from_manual_tags()
        if "error" in result:
            logger.warning(
                "owner manual bootstrap failed after attestation: %s",
                result["error"],
            )
    except Exception:
        logger.exception("owner manual bootstrap failed after attestation")


def _resolve_entity_display(
    entity_id: str,
    entity_cache: dict,
    principal_id: str | None,
) -> dict:
    """Resolve an entity ID to display info."""
    if entity_id not in entity_cache:
        entity_cache[entity_id] = load_journal_entity(entity_id)
    entity = entity_cache[entity_id]
    name = entity["name"] if entity else entity_id
    return {
        "name": name,
        "entity_id": entity_id,
        "is_owner": entity_id == principal_id,
    }


def _scan_segment_embeddings(day: str) -> list[dict]:
    """Scan a day for segments with audio embeddings.

    Only includes segments that have audio embedding NPZ files.
    Segments with a speakers.json file will include speaker names;
    segments without speakers.json will have an empty speakers list.

    Returns list of segment info dicts with keys:
        - key: segment directory name (HHMMSS_LEN)
        - start: formatted start time (HH:MM)
        - end: formatted end time (HH:MM)
        - duration: duration in seconds
        - sources: list of audio sources (e.g., ["mic_audio", "sys_audio"])
        - speakers: list of speaker names from speakers.json
        - speaker_count: number of speakers
    """
    segments = []
    for s_stream, s_key, s_path in iter_segments(day):
        # Validate segment key format
        parsed = segment_parse(s_key)
        if parsed[0] is None:
            continue

        start_time, end_time = parsed

        sources = _audio_embedding_sources(s_path)
        if not sources:
            continue

        # Load speakers.json (may be empty if not yet processed)
        speakers = _load_segment_speakers(s_path)

        # Calculate duration from start and end times
        duration = _time_to_seconds(end_time) - _time_to_seconds(start_time)

        segments.append(
            {
                "key": s_key,
                "stream": s_stream,
                "start": f"{start_time.hour:02d}:{start_time.minute:02d}",
                "end": f"{end_time.hour:02d}:{end_time.minute:02d}",
                "duration": duration,
                "sources": sources,
                "speakers": speakers,
                "speaker_count": len(speakers),
            }
        )

    return segments


def _load_sentences(
    day: str, segment_key: str, source: str, stream: str | None = None
) -> tuple[list[dict], tuple[np.ndarray, np.ndarray, np.ndarray | None] | None]:
    """Load transcript sentences and their embeddings for an audio source.

    Args:
        day: Day string (YYYYMMDD)
        segment_key: Segment directory name (HHMMSS_LEN)
        source: Audio source stem (e.g., "mic_audio")
        stream: Stream name for path resolution

    Returns:
        Tuple of (sentences, emb_data):
        - sentences: List of dicts with id, offset, text, has_embedding
        - emb_data: Tuple of (embeddings, statement_ids, durations_s) or None if no embeddings
    """
    if stream:
        segment_dir = get_segment_path(day, segment_key, stream, create=False)
    else:
        segment_dir = day_path(day) / segment_key

    # Load JSONL transcript
    jsonl_path = segment_dir / f"{source}.jsonl"
    if not jsonl_path.exists():
        return [], None

    sentences = []
    with open(jsonl_path) as f:
        lines = f.readlines()

    if not lines:
        return [], None

    # Get segment start time to compute relative offsets
    # JSONL contains absolute wall-clock times (e.g., "14:30:22")
    # Audio files start at time 0, so we need relative offset
    parsed = segment_parse(segment_key)
    segment_start_seconds = _time_to_seconds(parsed[0]) if parsed[0] else 0

    # First line is metadata, skip it
    # Remaining lines are sentences indexed by line number (1-based segment ID)
    for i, line in enumerate(lines[1:], start=1):
        try:
            entry = json.loads(line)
            abs_seconds = _parse_time_to_seconds(entry.get("start", "00:00:00"))
            # Convert absolute time to relative offset from segment start
            offset = abs_seconds - segment_start_seconds
            sentences.append(
                {
                    "id": i,
                    "offset": offset,
                    "text": entry.get("text", ""),
                }
            )
        except (json.JSONDecodeError, ValueError, IndexError):
            continue

    # Load embeddings
    npz_path = segment_dir / f"{source}.npz"
    emb_data = _load_embeddings_file(npz_path)

    if emb_data is not None:
        embeddings, statement_ids, _ = emb_data
        emb_map = {int(sid): True for sid in statement_ids}

        # Mark which sentences have embeddings
        for sentence in sentences:
            sentence["has_embedding"] = sentence["id"] in emb_map

    return sentences, emb_data


def _get_sentence_embedding(
    day: str, segment_key: str, source: str, sentence_id: int, stream: str | None = None
) -> np.ndarray | None:
    """Get a specific sentence's embedding, normalized."""
    if stream:
        segment_dir = get_segment_path(day, segment_key, stream, create=False)
    else:
        segment_dir = day_path(day) / segment_key
    npz_path = segment_dir / f"{source}.npz"

    emb_data = _load_embeddings_file(npz_path)
    if emb_data is None:
        return None

    embeddings, statement_ids, _ = emb_data

    # Find the embedding for this sentence
    for i, sid in enumerate(statement_ids):
        if int(sid) == sentence_id:
            return _normalize_embedding(embeddings[i])

    return None


@speakers_bp.route("/")
def index() -> Any:
    """Serve the speakers SPA shell."""
    return current_app.send_static_file("shell.html")


@speakers_bp.route("/<day>")
def speakers_day(day: str) -> Any:
    """Serve the speakers SPA shell for a specific day."""
    if not DATE_RE.fullmatch(day):
        return "", 404

    return current_app.send_static_file("shell.html")


@speakers_bp.route("/api/state")
def api_state() -> Any:
    """Return initial speakers workspace state."""
    try:
        speaker_filter = (request.args.get("speaker") or "").strip()
        speaker_filter_name = None
        if speaker_filter:
            entity = load_journal_entity(speaker_filter)
            if entity:
                speaker_filter_name = str(entity.get("name") or speaker_filter)
        return jsonify(
            {
                "today": date.today().strftime("%Y%m%d"),
                "owner_min_statements": OWNER_BOOTSTRAP_MIN_STMTS,
                "owner_status_routing_tokens": OWNER_STATUS_ROUTING_TOKENS,
                "speaker_copy": speaker_copy_payload(),
                "speaker_filter_name": speaker_filter_name,
            }
        )
    except Exception:
        logger.exception("error loading speakers state")
        return error_response(
            FILE_READ_FAILED,
            detail="Failed to load speaker state.",
        )


def _speaker_segment_counts(month: str | None = None) -> dict[str, int]:
    stats: dict[str, int] = {}

    for day_name in day_dirs().keys():
        if month is not None and not day_name.startswith(month):
            continue

        segments = _scan_segment_embeddings(day_name)
        if segments:
            stats[day_name] = len(segments)

    return stats


def _coverage_from_counts(counts: dict[str, int]) -> dict[str, str] | None:
    first_day: str | None = None
    last_day: str | None = None
    for day, count in counts.items():
        if count <= 0:
            continue
        if first_day is None or day < first_day:
            first_day = day
        if last_day is None or day > last_day:
            last_day = day
    if first_day is None or last_day is None:
        return None
    return {"start": first_day, "end": last_day}


def _speaker_grid_counts() -> tuple[dict[str, int], dict[str, int]]:
    days: dict[str, int] = {}
    activity: dict[str, int] = {}

    for day_name in day_dirs().keys():
        activity_count = 0
        needs_review_count = 0

        for _stream, segment_key, segment_dir in iter_segments(day_name):
            parsed = segment_parse(segment_key)
            if parsed[0] is None:
                continue
            if not _audio_embedding_sources(segment_dir):
                continue

            activity_count += 1
            labels_data = _load_speaker_labels(segment_dir)
            if _segment_has_speaker_review(labels_data):
                needs_review_count += 1

        if activity_count > 0:
            activity[day_name] = activity_count
        if needs_review_count > 0:
            days[day_name] = needs_review_count

    return days, activity


@speakers_bp.route("/api/index")
def api_index() -> Any:
    """Return read-only whole-journal date navigation coverage."""
    return jsonify(build_date_nav_index(_speaker_segment_counts()))


@speakers_bp.route("/api/grid")
def api_grid() -> Any:
    """Return day-grid data for speaker review progress."""
    days, activity = _speaker_grid_counts()
    return jsonify(
        build_day_grid_payload(
            days,
            max(days, default=None),
            coverage=_coverage_from_counts(activity),
            activity=activity,
        )
    )


@speakers_bp.route("/api/stats/<month>")
def api_stats(month: str) -> Any:
    """Return segment counts for each day in a month.

    Used by calendar heatmap to show days with embedding segments.
    """
    if not re.fullmatch(r"\d{6}", month):
        return error_response(
            INVALID_MONTH,
            detail="Invalid month format, expected YYYYMM",
        )

    stats = _speaker_segment_counts(month)
    return jsonify(stats)


@speakers_bp.route("/api/segments/<day>")
def api_segments(day: str) -> Any:
    """Return segments with audio embeddings for a day."""
    if not DATE_RE.fullmatch(day):
        return error_response(INVALID_DAY, detail="Invalid day format")

    try:
        limit = max(0, int(request.args.get("limit", 20)))
        offset = max(0, int(request.args.get("offset", 0)))
    except (ValueError, TypeError):
        return error_response(
            INVALID_REQUEST_VALUE,
            detail="Invalid limit/offset parameter",
        )

    speaker_filter = request.args.get("speaker")
    if speaker_filter is not None:
        speaker_filter = speaker_filter.strip()
        if not speaker_filter:
            return error_response(
                INVALID_REQUEST_VALUE,
                detail="Invalid speaker parameter",
            )

    segments = _scan_segment_embeddings(day)
    segments.sort(key=lambda s: s["key"])
    if speaker_filter:
        segments = [
            seg
            for seg in segments
            if _segment_has_speaker(day, seg["stream"], seg["key"], speaker_filter)
        ]
    total = len(segments)
    segments = segments[offset : offset + limit]

    principal = get_journal_principal()
    principal_id = principal["id"] if principal else None
    for seg in segments:
        seg_dir = get_segment_path(day, seg["key"], seg["stream"], create=False)
        labels_data = _load_speaker_labels(seg_dir)
        if labels_data:
            labels = labels_data.get("labels", [])
            seg["attribution_total"] = len(labels)
            seg["attribution_needs_review"] = sum(
                1
                for label in labels
                if _speaker_sentence_needs_review(label, labels_data)
            )
            seg["attribution_null"] = sum(
                1 for label in labels if not label.get("speaker")
            )
            owner_count = sum(
                1
                for label in labels
                if label.get("speaker") and label.get("speaker") == principal_id
            )
            seg["attribution_non_owner_total"] = len(labels) - owner_count
        else:
            seg["attribution_total"] = 0
            seg["attribution_needs_review"] = 0
            seg["attribution_null"] = 0
            seg["attribution_non_owner_total"] = 0

    return jsonify({"segments": segments, "total": total})


@speakers_bp.route("/api/segments-cli/<day>")
def api_cli_segments(day: str) -> Any:
    """Return a bounded day segment list for CLI callers."""
    if not DATE_RE.fullmatch(day):
        return error_response(INVALID_DAY, detail="Invalid day format")

    try:
        limit = int(request.args.get("limit", 20))
    except (ValueError, TypeError):
        return error_response(
            INVALID_REQUEST_VALUE,
            detail="Invalid limit parameter",
        )
    if limit < 1:
        return error_response(
            INVALID_REQUEST_VALUE,
            detail="Limit must be at least 1",
        )

    segments = _scan_segment_embeddings(day)
    segments.sort(key=lambda s: s["key"])
    total = len(segments)
    return success_response(
        {
            "day": day,
            "segments": segments[:limit],
            "returned": min(limit, total),
            "limit": limit,
            "total": total,
        }
    )


def _segment_has_speaker(
    day: str, stream: str, segment_key: str, entity_id: str
) -> bool:
    """Return whether a segment has any label attributed to entity_id."""
    seg_dir = get_segment_path(day, segment_key, stream, create=False)
    labels_data = _load_speaker_labels(seg_dir)
    if not labels_data:
        return False
    return any(
        label.get("speaker") == entity_id for label in labels_data.get("labels", [])
    )


@speakers_bp.route("/api/speakers/known")
def api_speakers_known() -> Any:
    """Return known voice cards for the speakers overview."""
    sort = request.args.get("sort") or SPK_OVERVIEW_KNOWN_VOICES_SORTS[0]
    sort = sort.replace("_", " ")
    if sort not in SPK_OVERVIEW_KNOWN_VOICES_SORTS:
        return error_response(
            INVALID_REQUEST_VALUE,
            detail="Invalid sort parameter",
        )

    speakers = list(get_speakers_status(section="speakers"))
    if sort == SPK_OVERVIEW_KNOWN_VOICES_SORTS[1]:
        speakers.sort(
            key=lambda item: (
                -int(item.get("embedding_count") or 0),
                str(item.get("name") or item.get("entity_id") or "").lower(),
                str(item.get("entity_id") or ""),
            )
        )
    elif sort == SPK_OVERVIEW_KNOWN_VOICES_SORTS[2]:
        speakers.sort(
            key=lambda item: (
                str(item.get("name") or item.get("entity_id") or "").lower(),
                str(item.get("entity_id") or ""),
            )
        )
    else:
        speakers.sort(
            key=lambda item: (
                item.get("last_seen_ts") is None,
                -(int(item.get("last_seen_ts") or 0)),
                str(item.get("name") or item.get("entity_id") or "").lower(),
                str(item.get("entity_id") or ""),
            )
        )

    return jsonify({"speakers": speakers, "total": len(speakers), "sort": sort})


@speakers_bp.route("/api/speakers/<day>/<stream>/<segment_key>")
def api_segment_speakers(day: str, stream: str, segment_key: str) -> Any:
    """Return speaker names with entity matching for a segment.

    Matches detected speaker names against all journal entities.
    """
    if not DATE_RE.fullmatch(day):
        return error_response(INVALID_DAY, detail="Invalid day format")

    if not validate_segment_key(segment_key):
        return error_response(
            INVALID_SEGMENT_OR_STREAM,
            detail="Invalid segment key",
        )

    # Load speakers from speakers.json
    segment_dir = get_segment_path(day, segment_key, stream, create=False)
    speakers = _load_segment_speakers(segment_dir)
    if not speakers:
        return jsonify({"matched": [], "unmatched": []})

    # Load all journal entities for matching
    journal_entities = load_all_journal_entities()
    entities_list = [e for e in journal_entities.values() if not e.get("blocked")]

    # Match each speaker name to an entity
    matched = []
    unmatched = []

    for speaker_name in speakers:
        entity = find_matching_entity(speaker_name, entities_list)
        if entity:
            matched.append(
                {
                    "detected_name": speaker_name,
                    "entity_name": entity.get("name"),
                    "entity_type": entity.get("type"),
                }
            )
        else:
            unmatched.append(speaker_name)

    return jsonify(
        {
            "matched": matched,
            "unmatched": unmatched,
        }
    )


@speakers_bp.route("/api/review/<day>/<stream>/<segment_key>/<source>")
def api_review(day: str, stream: str, segment_key: str, source: str) -> Any:
    """Return sentences with pre-computed speaker labels for review."""
    if not DATE_RE.fullmatch(day):
        return error_response(INVALID_DAY, detail="Invalid day format")
    if not validate_segment_key(segment_key):
        return error_response(
            INVALID_SEGMENT_OR_STREAM,
            detail="Invalid segment key",
        )

    sentences, _ = _load_sentences(day, segment_key, source, stream=stream)
    if not sentences:
        return error_response(
            SPEAKER_REVIEW_UNAVAILABLE,
            detail="No transcript found",
        )

    segment_dir = get_segment_path(day, segment_key, stream, create=False)
    labels_data = _load_speaker_labels(segment_dir)
    label_map: dict[int, dict] = {}
    if labels_data:
        for label in labels_data.get("labels", []):
            sid = label.get("sentence_id")
            if sid is not None:
                label_map[int(sid)] = label

    corrections = _load_speaker_corrections(segment_dir)
    correction_map: dict[int, dict] = {}
    for correction in corrections:
        sid = correction.get("sentence_id")
        if sid is not None:
            correction_map[int(sid)] = correction

    principal = get_journal_principal()
    principal_id = principal["id"] if principal else None
    entity_cache: dict[str, dict | None] = {}

    review_sentences = [s for s in sentences if s.get("has_embedding")]
    needs_review_count = 0
    corrections_count = 0

    for sentence in review_sentences:
        sid = sentence["id"]
        label = label_map.get(sid)
        if label:
            entity_id = label.get("speaker")
            confidence = label.get("confidence")
            method = label.get("method")
            if entity_id:
                info = _resolve_entity_display(entity_id, entity_cache, principal_id)
                sentence["speaker_entity_id"] = entity_id
                sentence["speaker_name"] = info["name"]
                sentence["is_owner"] = info["is_owner"]
            else:
                sentence["speaker_entity_id"] = None
                sentence["speaker_name"] = None
                sentence["is_owner"] = False

            sentence["confidence"] = confidence
            sentence["method"] = method
            sentence["needs_review"] = _speaker_sentence_needs_review(
                label, labels_data
            )
        else:
            sentence["speaker_entity_id"] = None
            sentence["speaker_name"] = None
            sentence["confidence"] = None
            sentence["method"] = None
            sentence["is_owner"] = False
            sentence["needs_review"] = _speaker_sentence_needs_review(None, labels_data)

        correction = correction_map.get(sid)
        sentence["is_correction"] = sentence.get("method") in {
            "user_corrected",
            "user_assigned",
        }
        if correction and sentence["is_correction"]:
            orig_speaker = correction.get("original_speaker")
            if orig_speaker:
                orig_info = _resolve_entity_display(
                    orig_speaker,
                    entity_cache,
                    principal_id,
                )
                sentence["original_speaker_entity_id"] = orig_speaker
                sentence["original_speaker_name"] = orig_info["name"]
            else:
                sentence["original_speaker_entity_id"] = None
                sentence["original_speaker_name"] = None
            corrections_count += 1
        else:
            sentence["original_speaker_entity_id"] = None
            sentence["original_speaker_name"] = None

        if sentence.get("needs_review"):
            needs_review_count += 1

    journal_entities = load_all_journal_entities()
    all_entities = []
    for eid, entity in journal_entities.items():
        if entity.get("blocked"):
            continue
        all_entities.append(
            {
                "entity_id": eid,
                "name": entity.get("name", eid),
                "is_principal": bool(entity.get("is_principal")),
            }
        )
    if not any(e.get("is_principal") for e in journal_entities.values()):
        identity = principal_identity_or_none()
        if identity is not None and identity[0] not in journal_entities:
            all_entities.append(
                {"entity_id": identity[0], "name": identity[1], "is_principal": True}
            )
    all_entities.sort(key=lambda x: (not x["is_principal"], x["name"].lower()))

    audio_file = None
    audio_mimetype = None
    audio_path = resolve_audio_file(segment_dir, source)
    if audio_path is not None:
        audio_file = audio_serve_url(day, stream, segment_key, audio_path.name)
        audio_mimetype = MIME_TYPES[audio_path.suffix]

    parsed = segment_parse(segment_key)
    start_time, end_time = parsed if parsed[0] else (None, None)

    return jsonify(
        {
            "segment": {
                "key": segment_key,
                "start": (
                    f"{start_time.hour:02d}:{start_time.minute:02d}"
                    if start_time
                    else ""
                ),
                "end": (
                    f"{end_time.hour:02d}:{end_time.minute:02d}" if end_time else ""
                ),
            },
            "source": source,
            "sentences": review_sentences,
            "all_entities": all_entities,
            "audio_file": audio_file,
            "audio_mimetype": audio_mimetype,
            "has_labels": labels_data is not None,
            "summary": {
                "total": len(review_sentences),
                "needs_review": needs_review_count,
                "corrections": corrections_count,
            },
        }
    )


@speakers_bp.route("/api/review-cli/<day>/<stream>/<segment_key>/<source>")
def api_cli_review(day: str, stream: str, segment_key: str, source: str) -> Any:
    """Return sentence rows for CLI callers without browser-only review payload."""
    if not DATE_RE.fullmatch(day):
        return error_response(INVALID_DAY, detail="Invalid day format")
    if not validate_segment_key(segment_key):
        return error_response(
            INVALID_SEGMENT_OR_STREAM,
            detail="Invalid segment key",
        )
    if not STREAM_RE.fullmatch(stream):
        return error_response(INVALID_SEGMENT_OR_STREAM, detail="Invalid stream")

    sentences, _ = _load_sentences(day, segment_key, source, stream=stream)
    if not sentences:
        return error_response(
            SPEAKER_REVIEW_UNAVAILABLE,
            detail="No transcript found",
        )

    segment_dir = get_segment_path(day, segment_key, stream, create=False)
    labels_data = _load_speaker_labels(segment_dir)
    label_map: dict[int, dict] = {}
    if labels_data:
        for label in labels_data.get("labels", []):
            sid = label.get("sentence_id")
            if sid is not None:
                label_map[int(sid)] = label

    rows = []
    for sentence in sentences:
        sentence_id = int(sentence["id"])
        label = label_map.get(sentence_id)
        rows.append(
            {
                "sentence_id": sentence_id,
                "text": sentence.get("text", ""),
                "has_embedding": bool(sentence.get("has_embedding")),
                "speaker": label.get("speaker") if label else None,
                "confidence": label.get("confidence") if label else None,
                "method": label.get("method") if label else None,
                "needs_review": _speaker_sentence_needs_review(label, labels_data),
            }
        )

    return success_response(
        {
            "day": day,
            "stream": stream,
            "segment_key": segment_key,
            "source": source,
            "sentences": rows,
        }
    )


@speakers_bp.route("/api/confirm-attribution", methods=["POST"])
def api_confirm_attribution() -> Any:
    """Confirm a medium-confidence speaker attribution."""
    data = request.get_json()
    if not data:
        return error_response(MISSING_REQUEST_BODY, detail="No data provided")

    day = data.get("day")
    stream = data.get("stream")
    segment_key = data.get("segment_key")
    source = data.get("source")
    sentence_id = data.get("sentence_id")

    if not all([day, stream, segment_key, source, sentence_id is not None]):
        return error_response(
            MISSING_REQUIRED_FIELD,
            detail="Missing required fields",
        )
    if not DATE_RE.fullmatch(day):
        return error_response(INVALID_DAY, detail="Invalid day format")
    if not SEGMENT_KEY_RE.fullmatch(segment_key):
        return error_response(
            INVALID_SEGMENT_OR_STREAM,
            detail="Invalid segment key",
        )
    if not STREAM_RE.fullmatch(stream):
        return error_response(INVALID_SEGMENT_OR_STREAM, detail="Invalid stream")

    segment_dir = get_segment_path(day, segment_key, stream)
    labels_data = _load_speaker_labels(segment_dir)
    if not labels_data:
        return error_response(
            SPEAKER_REVIEW_UNAVAILABLE,
            detail="No speaker labels found",
        )

    label = None
    for item in labels_data.get("labels", []):
        if item.get("sentence_id") == sentence_id:
            label = item
            break

    if label is None:
        return error_response(
            SPEAKER_SENTENCE_MISSING,
            detail="Sentence not found in labels",
        )

    speaker = label.get("speaker")
    if not speaker:
        return error_response(
            SPEAKER_ATTRIBUTION_STATE_INVALID,
            detail="sentence has no speaker assignment yet",
        )

    confidence = label.get("confidence")
    if confidence == "high" and label.get("method") == "user_confirmed":
        return success_response({"status": "already_confirmed"})
    if confidence != "medium":
        return error_response(
            SPEAKER_ATTRIBUTION_STATE_INVALID,
            detail="attribution is not medium confidence",
        )

    emb = _get_sentence_embedding(day, segment_key, source, sentence_id, stream=stream)
    if emb is None:
        return error_response(
            SPEAKER_SENTENCE_MISSING,
            detail="Sentence embedding not found",
        )

    principal_id = _principal_id_or_none()
    if speaker != principal_id and _check_owner_contamination(emb):
        return error_response(
            SPEAKER_OWNER_VOICE_TOO_CLOSE,
            detail="Embedding too similar to owner voice — cannot save",
        )

    try:
        _save_voiceprint(
            speaker, emb, day, segment_key, source, sentence_id, stream=stream
        )
    except LockTimeout as exc:
        return _voiceprint_busy_response(exc)

    old_method = label.get("method")
    try:
        apply_label_patches(
            segment_dir,
            {sentence_id: {"confidence": "high", "method": "user_confirmed"}},
            allow_insert=False,
        )
    except LockTimeout as exc:
        return _labels_busy_response(exc)

    try:
        append_speaker_correction(
            segment_dir,
            {
                "sentence_id": sentence_id,
                "original_speaker": speaker,
                "corrected_speaker": speaker,
                "original_method": old_method,
                "timestamp": now_ms(),
            },
        )
    except LockTimeout as exc:
        return _labels_busy_response(exc)

    log_app_action(
        app="speakers",
        facet=None,
        action="attribution_confirm",
        params={
            "day": day,
            "stream": stream,
            "segment_key": segment_key,
            "source": source,
            "sentence_id": sentence_id,
            "speaker": speaker,
        },
    )
    _maybe_bootstrap_owner_from_attestation(principal_id, speaker)

    return success_response({"status": "confirmed", "speaker": speaker})


@speakers_bp.route("/api/correct-attribution", methods=["POST"])
def api_correct_attribution() -> Any:
    """Correct a speaker attribution to a different entity."""
    data = request.get_json()
    if not data:
        return error_response(MISSING_REQUEST_BODY, detail="No data provided")

    day = data.get("day")
    stream = data.get("stream")
    segment_key = data.get("segment_key")
    source = data.get("source")
    sentence_id = data.get("sentence_id")
    new_speaker = data.get("new_speaker")

    if not all(
        [day, stream, segment_key, source, sentence_id is not None, new_speaker]
    ):
        return error_response(
            MISSING_REQUIRED_FIELD,
            detail="Missing required fields",
        )
    if not DATE_RE.fullmatch(day):
        return error_response(INVALID_DAY, detail="Invalid day format")
    if not SEGMENT_KEY_RE.fullmatch(segment_key):
        return error_response(
            INVALID_SEGMENT_OR_STREAM,
            detail="Invalid segment key",
        )
    if not STREAM_RE.fullmatch(stream):
        return error_response(INVALID_SEGMENT_OR_STREAM, detail="Invalid stream")

    target_entity = _ensure_attribution_target(new_speaker)
    if not target_entity:
        return error_response(
            SPEAKER_NOT_FOUND,
            detail=f"Entity '{new_speaker}' not found",
        )
    if target_entity.get("blocked"):
        return error_response(
            ENTITY_BLOCKED,
            detail=f"Entity '{new_speaker}' is blocked",
        )

    segment_dir = get_segment_path(day, segment_key, stream)
    labels_data = _load_speaker_labels(segment_dir)
    if not labels_data:
        return error_response(
            SPEAKER_REVIEW_UNAVAILABLE,
            detail="No speaker labels found",
        )

    label = None
    for item in labels_data.get("labels", []):
        if item.get("sentence_id") == sentence_id:
            label = item
            break

    if label is None:
        return error_response(
            SPEAKER_SENTENCE_MISSING,
            detail="Sentence not found in labels",
        )

    old_speaker = label.get("speaker")
    old_method = label.get("method")
    if old_speaker == new_speaker:
        return success_response({"status": "already_correct"})

    emb = _get_sentence_embedding(day, segment_key, source, sentence_id, stream=stream)
    if emb is None:
        return error_response(
            SPEAKER_SENTENCE_MISSING,
            detail="Sentence embedding not found",
        )

    principal_id = _principal_id_or_none()
    if new_speaker != principal_id and _check_owner_contamination(emb):
        return error_response(
            SPEAKER_OWNER_VOICE_TOO_CLOSE,
            detail="Embedding too similar to owner voice — cannot save",
        )

    voiceprint_removal = VoiceprintRemovalResult(
        outcome="not_found",
        entity_id=str(old_speaker or ""),
        keys_removed=[],
        file_deleted=False,
        voiceprints_path=None,
    )
    try:
        if old_speaker:
            voiceprint_removal = _remove_voiceprint(
                old_speaker, day, segment_key, source, sentence_id
            )

        _save_voiceprint(
            new_speaker,
            emb,
            day,
            segment_key,
            source,
            sentence_id,
            stream=stream,
        )
    except LockTimeout as exc:
        return _voiceprint_busy_response(exc)

    try:
        apply_label_patches(
            segment_dir,
            {
                sentence_id: {
                    "speaker": new_speaker,
                    "confidence": "high",
                    "method": "user_corrected",
                }
            },
            allow_insert=False,
        )
    except LockTimeout as exc:
        return _labels_busy_response(exc)

    try:
        append_speaker_correction(
            segment_dir,
            {
                "sentence_id": sentence_id,
                "original_speaker": old_speaker,
                "corrected_speaker": new_speaker,
                "original_method": old_method,
                "timestamp": now_ms(),
            },
        )
    except LockTimeout as exc:
        return _labels_busy_response(exc)

    log_app_action(
        app="speakers",
        facet=None,
        action="attribution_correct",
        params={
            "day": day,
            "stream": stream,
            "segment_key": segment_key,
            "source": source,
            "sentence_id": sentence_id,
            "old_speaker": old_speaker,
            "new_speaker": new_speaker,
            "voiceprint_removal": _voiceprint_removal_payload(voiceprint_removal),
        },
    )
    _maybe_bootstrap_owner_from_attestation(principal_id, new_speaker)
    propagation_offer = _propagation_offer(old_speaker, new_speaker)

    return success_response(
        {
            "status": "corrected",
            "old_speaker": old_speaker,
            "new_speaker": new_speaker,
            "voiceprint_removal": _voiceprint_removal_payload(voiceprint_removal),
            "propagation_offer": propagation_offer,
        }
    )


@speakers_bp.route("/api/propagate-correction", methods=["POST"])
def api_propagate_correction() -> Any:
    """Preview or apply scoped re-attribution after a correction."""
    data = request.get_json(silent=True) or {}
    old_speaker = data.get("old_speaker")
    new_speaker = data.get("new_speaker")
    commit = bool(data.get("commit", False))

    if not old_speaker or not new_speaker:
        return error_response(
            MISSING_REQUIRED_FIELD,
            detail="Missing required fields",
        )
    old_speaker = str(old_speaker)
    new_speaker = str(new_speaker)
    if old_speaker == new_speaker:
        return error_response(
            INVALID_REQUEST_VALUE,
            detail="Choose two different speakers.",
        )

    old_entity = _ensure_attribution_target(old_speaker)
    if not old_entity:
        return error_response(
            SPEAKER_NOT_FOUND,
            detail=f"Entity '{old_speaker}' not found",
        )
    if old_entity.get("blocked"):
        return error_response(
            ENTITY_BLOCKED,
            detail=f"Entity '{old_speaker}' is blocked",
        )

    new_entity = _ensure_attribution_target(new_speaker)
    if not new_entity:
        return error_response(
            SPEAKER_NOT_FOUND,
            detail=f"Entity '{new_speaker}' not found",
        )
    if new_entity.get("blocked"):
        return error_response(
            ENTITY_BLOCKED,
            detail=f"Entity '{new_speaker}' is blocked",
        )

    try:
        result = propagate_speaker_correction(
            old_speaker,
            new_speaker,
            commit=commit,
        )
    except LockTimeout as exc:
        if exc.path.name in ("speaker_labels.json", "speaker_corrections.json"):
            return _labels_busy_response(exc)
        return _voiceprint_busy_response(exc)

    if commit and result.get("statement_count"):
        log_app_action(
            app="speakers",
            facet=None,
            action="attribution_propagate_correction",
            params={
                "old_speaker": old_speaker,
                "new_speaker": new_speaker,
                "statement_count": result["statement_count"],
                "segment_count": result["segment_count"],
            },
        )

    return jsonify(_propagation_response_payload(result))


@speakers_bp.route("/api/assign-attribution", methods=["POST"])
def api_assign_attribution() -> Any:
    """Assign a speaker to an unattributed sentence."""
    data = request.get_json()
    if not data:
        return error_response(MISSING_REQUEST_BODY, detail="No data provided")

    day = data.get("day")
    stream = data.get("stream")
    segment_key = data.get("segment_key")
    source = data.get("source")
    sentence_id = data.get("sentence_id")
    speaker = data.get("speaker")
    return _assign_attribution_impl(
        day, stream, segment_key, source, sentence_id, speaker
    )


@speakers_bp.route("/api/owner/status")
def api_owner_status() -> Any:
    """Return the current owner voiceprint confirmation state."""
    voiceprint = get_current().get("voiceprint", {})
    status = voiceprint.get("status", "none")
    diagnostics = _owner_bootstrap_status_fields()

    if status == "confirmed":
        centroid = load_owner_centroid()
        metadata = {
            "cluster_size": centroid.cluster_size if centroid is not None else 0,
            "streams": centroid.streams if centroid is not None else [],
            "created_at": centroid.created_at if centroid is not None else None,
            "last_refreshed_at": (
                centroid.last_refreshed_at if centroid is not None else ""
            ),
            "threshold": centroid.threshold if centroid is not None else None,
            "margin": centroid.margin if centroid is not None else None,
            "intra_cosine_p25": (
                centroid.intra_cosine_p25 if centroid is not None else None
            ),
            "evidence_hash": centroid.evidence_hash if centroid is not None else None,
            "evidence_intra_cosine_p25": (
                centroid.evidence_intra_cosine_p25 if centroid is not None else None
            ),
        }
        return jsonify(
            {"status": OWNER_STATUS_CONFIRMED, "centroid_metadata": metadata}
        )

    if status == "candidate":
        return jsonify(
            {
                "status": OWNER_STATUS_CANDIDATE,
                "cluster_size": voiceprint.get("cluster_size"),
                "samples": voiceprint.get("samples", []),
            }
        )

    if status == "low_quality":
        guidance = load_owner_manual_bootstrap_guidance(_principal_id_or_none())
        return jsonify(
            {
                "status": "low_quality",
                "source": voiceprint.get("source", "candidate_pool"),
                "low_quality_reason": voiceprint.get("low_quality_reason", ""),
                "observed_value": voiceprint.get("observed_value", 0.0),
                "threshold_value": voiceprint.get("threshold_value", 0.0),
                **diagnostics,
                "next_step": guidance["next_step"],
                "guidance": guidance["guidance"],
            }
        )

    if status == "no_cluster":
        guidance = load_owner_manual_bootstrap_guidance(_principal_id_or_none())
        return jsonify(
            {
                "status": "no_cluster",
                **diagnostics,
                "next_step": guidance["next_step"],
                "guidance": guidance["guidance"],
            }
        )

    if status in {"none", "rejected"}:
        cooldown = owner_rejection_cooldown_payload(voiceprint)
        if cooldown is not None:
            return jsonify(
                {
                    "status": "none",
                    **diagnostics,
                    **cooldown,
                    "next_step": "wait_for_cooldown",
                    "guidance": OWNER_REJECTION_COOLDOWN_GUIDANCE,
                }
            )
        if diagnostics["segments_available"] > 0:
            return jsonify(
                {
                    "status": "needs_detection",
                    **diagnostics,
                    "next_step": "detect_candidate",
                    "guidance": OWNER_DETECT_CANDIDATE_GUIDANCE,
                }
            )
        guidance = load_owner_manual_bootstrap_guidance(_principal_id_or_none())
        return jsonify(
            {
                "status": "none",
                **diagnostics,
                "next_step": guidance["next_step"],
                "guidance": guidance["guidance"],
            }
        )

    guidance = load_owner_manual_bootstrap_guidance(_principal_id_or_none())
    return jsonify(
        {
            "status": "none",
            **diagnostics,
            "next_step": guidance["next_step"],
            "guidance": guidance["guidance"],
        }
    )


@speakers_bp.route("/api/owner/detect", methods=["POST"])
def api_owner_detect() -> Any:
    """Run owner voice candidate detection."""
    try:
        result = detect_owner_candidate()
    except LockTimeout as exc:
        return _voiceprint_busy_response(exc)
    if result.get("error_kind") == "voiceprint_busy":
        return error_response(SPEAKER_VOICEPRINT_BUSY, detail=result["error"])
    return jsonify(result)


@speakers_bp.route("/api/owner/build-from-tags", methods=["POST"])
def api_owner_build_from_tags() -> Any:
    """Build a confirmed owner centroid directly from validated manual tags."""
    try:
        result = bootstrap_owner_from_manual_tags()
    except LockTimeout as exc:
        return _voiceprint_busy_response(exc)
    if result.get("error_kind") == "voiceprint_busy":
        return error_response(SPEAKER_VOICEPRINT_BUSY, detail=result["error"])
    if "error" in result:
        return error_response(ENTITY_NOT_FOUND, detail=result["error"], status=400)
    if result.get("status") == "confirmed":
        log_app_action(
            app="speakers",
            facet=None,
            action="owner_voiceprint_build_from_tags",
            params={
                "principal_id": result["principal_id"],
                "cluster_size": result.get("cluster_size"),
            },
        )
    return jsonify(result)


@speakers_bp.route("/api/owner/rebuild", methods=["POST"])
def api_owner_rebuild() -> Any:
    """Rebuild the confirmed owner centroid from current manual-tag evidence."""
    body = request.get_json(silent=True) or {}
    override = bool(body.get("override") is True) if isinstance(body, dict) else False
    try:
        result = rebuild_owner_centroid(override=override)
    except LockTimeout as exc:
        return _voiceprint_busy_response(exc)
    if result.get("error_kind") == "voiceprint_busy":
        return error_response(SPEAKER_VOICEPRINT_BUSY, detail=result["error"])
    if result.get("status") == "rebuilt":
        log_app_action(
            app="speakers",
            facet=None,
            action="owner_voiceprint_rebuild",
            params={
                "principal_id": result["principal_id"],
                "cluster_size": result.get("cluster_size"),
                "override": bool(result.get("override_applied")),
            },
        )
    return jsonify(result)


@speakers_bp.route("/api/owner/tag-cli", methods=["POST"])
def api_cli_owner_tag() -> Any:
    """Tag one sentence as the configured owner voice through the shared assign path."""
    data = request.get_json(silent=True)
    if not data:
        return error_response(MISSING_REQUEST_BODY, detail="No data provided")

    principal_id = _principal_id_or_none()
    if principal_id is None:
        identity = principal_identity_or_none()
        if identity is None:
            return error_response(
                SPEAKER_OWNER_IDENTITY_REQUIRED,
                detail="Set your journal identity before tagging your voice.",
            )
        principal_id = identity[0]

    return _assign_attribution_impl(
        data.get("day"),
        data.get("stream"),
        data.get("segment_key"),
        data.get("source"),
        data.get("sentence_id"),
        principal_id,
    )


@speakers_bp.route("/api/owner/confirm", methods=["POST"])
def api_owner_confirm() -> Any:
    """Confirm the current owner voice candidate and persist the centroid."""
    try:
        result = confirm_owner_candidate()
    except LockTimeout as exc:
        return _voiceprint_busy_response(exc)
    if result.get("error_kind") == "voiceprint_busy":
        return error_response(SPEAKER_VOICEPRINT_BUSY, detail=result["error"])
    if "error" in result:
        code = 404 if "No candidate" in result["error"] else 400
        reason = SPEAKER_REVIEW_UNAVAILABLE if code == 404 else ENTITY_NOT_FOUND
        return error_response(reason, detail=result["error"], status=code)

    log_app_action(
        app="speakers",
        facet=None,
        action="owner_voiceprint_confirm",
        params={
            "principal_id": result["principal_id"],
            "cluster_size": result["cluster_size"],
        },
    )

    return jsonify({"status": "confirmed", "principal_id": result["principal_id"]})


@speakers_bp.route("/api/owner/reject", methods=["POST"])
def api_owner_reject() -> Any:
    """Reject the current owner voice candidate."""
    try:
        reject_owner_candidate()
    except LockTimeout as exc:
        return _voiceprint_busy_response(exc)
    return jsonify({"status": "needs_detection"})


@speakers_bp.route("/api/owner/classify", methods=["POST"])
def api_owner_classify() -> Any:
    """Classify segment sentences against the confirmed owner centroid."""
    data = request.get_json()
    if not data:
        return error_response(MISSING_REQUEST_BODY, detail="No data provided")

    day = data.get("day")
    stream = data.get("stream")
    segment_key = data.get("segment_key")
    source = data.get("source")

    if not all([day, stream, segment_key, source]):
        return error_response(
            MISSING_REQUIRED_FIELD,
            detail="Missing required fields",
        )
    if not DATE_RE.fullmatch(day):
        return error_response(INVALID_DAY, detail="Invalid day format")
    if not validate_segment_key(segment_key):
        return error_response(
            INVALID_SEGMENT_OR_STREAM,
            detail="Invalid segment key",
        )

    return jsonify(
        {
            "sentences": classify_sentences(day, stream, segment_key, source),
        }
    )


@speakers_bp.route("/api/discovery/scan", methods=["POST"])
def api_discovery_scan() -> Any:
    """Scan for recurring unknown speaker clusters."""
    result = discover_unknown_speakers()
    return jsonify(result)


@speakers_bp.route("/api/discovery/identify", methods=["POST"])
def api_discovery_identify() -> Any:
    """Identify a discovered unknown speaker cluster by naming it."""
    data = request.get_json(silent=True) or {}
    cluster_id = data.get("cluster_id")
    name = data.get("name", "").strip()

    if cluster_id is None:
        return error_response(
            MISSING_REQUIRED_FIELD,
            detail="cluster_id is required",
        )
    if not name:
        return error_response(MISSING_REQUIRED_FIELD, detail="name is required")

    try:
        cluster_id = int(cluster_id)
    except (TypeError, ValueError):
        return error_response(
            INVALID_REQUEST_VALUE,
            detail="cluster_id must be an integer",
        )

    try:
        result = identify_cluster(cluster_id, name)
    except LockTimeout as exc:
        if exc.path.name in ("speaker_labels.json", "speaker_corrections.json"):
            return _labels_busy_response(exc)
        return _voiceprint_busy_response(exc)

    if "error" in result:
        resolved = load_resolved_cluster(cluster_id)
        if resolved and resolved.get("label", "").strip().lower() == name.lower():
            result = {
                "status": "identified",
                "entity_id": resolved.get("entity_id"),
                "entity_name": resolved.get("label"),
                "entity_created": False,
                "voiceprints_saved": 0,
                "segments_updated": 0,
                "sentences_attributed": 0,
            }
        else:
            reason = (
                SPEAKER_NOT_FOUND
                if "Entity" in result["error"]
                else INVALID_REQUEST_VALUE
            )
            return error_response(reason, detail=result["error"], status=400)

    log_app_action(
        app="speakers",
        facet=None,
        action="speaker_identified",
        params={
            "entity_id": result.get("entity_id"),
            "entity_name": result.get("entity_name"),
            "cluster_id": cluster_id,
            "voiceprints_saved": result.get("voiceprints_saved"),
            "segments_updated": result.get("segments_updated"),
        },
    )

    return jsonify(result)


# CLI-backing routes for sol call speakers HTTP cutover.
@speakers_bp.route("/api/status", methods=["GET"])
def api_cli_status() -> Any:
    """Return the full speakers status payload for CLI-side section selection."""
    return jsonify(get_speakers_status(None))


@speakers_bp.route("/api/bootstrap", methods=["POST"])
def api_cli_bootstrap() -> Any:
    """Bootstrap voiceprints for the CLI."""
    data = request.get_json(silent=True) or {}
    commit = bool(data.get("commit", False))
    stats = bootstrap_voiceprints(dry_run=not commit)
    if "error" in stats:
        return error_response(
            SPEAKER_OWNER_CENTROID_REQUIRED,
            detail=stats["error"],
        )
    return jsonify(stats)


@speakers_bp.route("/api/resolve-names", methods=["POST"])
def api_cli_resolve_names() -> Any:
    """Resolve speaker name variants for the CLI."""
    data = request.get_json(silent=True) or {}
    commit = bool(data.get("commit", False))
    return jsonify(resolve_name_variants(dry_run=not commit))


@speakers_bp.route("/api/seed-from-imports", methods=["POST"])
def api_cli_seed_from_imports() -> Any:
    """Seed voiceprints from imports for the CLI."""
    data = request.get_json(silent=True) or {}
    commit = bool(data.get("commit", False))
    stats = seed_from_imports(dry_run=not commit)
    if "error" in stats:
        return error_response(
            SPEAKER_OWNER_CENTROID_REQUIRED,
            detail=stats["error"],
        )
    return jsonify(stats)


@speakers_bp.route("/api/backfill-last-seen", methods=["POST"])
def api_cli_backfill_last_seen() -> Any:
    """Backfill speaker last-seen metadata for the CLI."""
    data = request.get_json(silent=True) or {}
    commit = bool(data.get("commit", False))
    return jsonify(backfill_last_seen(dry_run=not commit))


@speakers_bp.route("/api/backfill", methods=["POST"])
def api_cli_backfill() -> Any:
    """Backfill speaker labels synchronously for the CLI."""
    data = request.get_json(silent=True) or {}
    commit = bool(data.get("commit", False))
    reattribute = bool(data.get("reattribute", False))
    kwargs: dict[str, Any] = {
        "dry_run": not commit,
        "progress_callback": None,
    }
    if reattribute:
        kwargs["reattribute"] = True
    return jsonify(backfill_segments(**kwargs))


@speakers_bp.route("/api/wipe", methods=["POST"])
def api_cli_wipe() -> Any:
    """Wipe speaker artifacts for the CLI."""
    data = request.get_json(silent=True) or {}
    commit = bool(data.get("commit", False))
    report = wipe_speaker_artifacts(dry_run=not commit)
    return jsonify(report.to_dict())


@speakers_bp.route("/api/attribute-segment", methods=["POST"])
def api_cli_attribute_segment() -> Any:
    """Attribute one segment and optionally persist CLI-requested artifacts."""
    data = request.get_json(silent=True) or {}
    day = data.get("day")
    stream = data.get("stream")
    segment = data.get("segment")
    commit = bool(data.get("commit", False))
    save = bool(data.get("save", True))
    accumulate = bool(data.get("accumulate", True))

    if not all([day, stream, segment]):
        return error_response(MISSING_REQUIRED_FIELD, detail="Missing required fields")
    if not DATE_RE.fullmatch(day):
        return error_response(INVALID_DAY, detail="Invalid day format")
    if not SEGMENT_KEY_RE.fullmatch(segment):
        return error_response(
            INVALID_SEGMENT_OR_STREAM,
            detail="Invalid segment key",
        )
    if not STREAM_RE.fullmatch(stream):
        return error_response(INVALID_SEGMENT_OR_STREAM, detail="Invalid stream")

    result = attribute_segment(day, stream, segment)
    if result.get("error"):
        return error_response(
            SPEAKER_OWNER_CENTROID_REQUIRED,
            detail=result["error"],
        )

    labels = result.get("labels", [])
    metadata = result.get("metadata", {})
    source = result.get("source")
    written_path = None
    accumulated = None

    if commit and save:
        try:
            out_path = save_speaker_labels(
                get_segment_path(day, segment, stream),
                labels,
                metadata,
            )
        except LockTimeout as exc:
            return _labels_busy_response(exc)
        written_path = str(out_path)

    if commit and accumulate and source:
        try:
            accumulated = accumulate_voiceprints(day, stream, segment, labels, source)
        except LockTimeout as exc:
            return _voiceprint_busy_response(exc)

    return jsonify(
        {
            "result": result,
            "written_path": written_path,
            "accumulated": accumulated,
        }
    )


@speakers_bp.route("/api/suggest", methods=["GET"])
def api_cli_suggest() -> Any:
    """Return speaker suggestion items plus server-rendered markdown."""
    try:
        limit = int(request.args.get("limit", 5))
    except (TypeError, ValueError):
        return error_response(
            INVALID_REQUEST_VALUE,
            detail="Invalid limit parameter",
        )
    items = suggest_opportunities(limit=limit)
    return jsonify({"items": items, "markdown": format_suggestions(items)})


@speakers_bp.route("/api/discovery/identify-cli", methods=["POST"])
def api_cli_discovery_identify() -> Any:
    """Identify a discovery cluster with CLI-compatible pass-through behavior."""
    data = request.get_json(silent=True) or {}
    cluster_id = data.get("cluster_id")
    name = data.get("name")
    entity_id = data.get("entity_id")

    if cluster_id is None or not name:
        return error_response(
            MISSING_REQUIRED_FIELD,
            detail="cluster_id and name are required",
        )

    try:
        cluster_id = int(cluster_id)
    except (TypeError, ValueError):
        return error_response(
            INVALID_REQUEST_VALUE,
            detail="cluster_id must be an integer",
        )

    try:
        result = identify_cluster(cluster_id, name, entity_id=entity_id)
    except LockTimeout as exc:
        if exc.path.name in ("speaker_labels.json", "speaker_corrections.json"):
            return _labels_busy_response(exc)
        return _voiceprint_busy_response(exc)

    if "error" in result:
        return error_response(
            SPEAKER_COMMAND_FAILED,
            detail=json.dumps(result, indent=2, default=str),
            status=400,
        )
    return jsonify(result)


@speakers_bp.route("/api/merge-names", methods=["POST"])
def api_cli_merge_names() -> Any:
    """Merge two speaker names for the CLI."""
    data = request.get_json(silent=True) or {}
    alias = data.get("alias")
    canonical = data.get("canonical")
    result = merge_names(alias, canonical)
    if "error" in result:
        return error_response(
            SPEAKER_COMMAND_FAILED,
            detail=json.dumps(result, indent=2, default=str),
            status=400,
        )
    return jsonify(result)


@speakers_bp.route("/api/link-import", methods=["POST"])
def api_cli_link_import() -> Any:
    """Link an imported speaker name to an entity for the CLI."""
    data = request.get_json(silent=True) or {}
    name = data.get("name")
    entity_id = data.get("entity_id")
    result = link_import(name, entity_id)
    if "error" in result:
        return error_response(
            SPEAKER_COMMAND_FAILED,
            detail=json.dumps(result, indent=2, default=str),
            status=400,
        )
    return jsonify(result)


@speakers_bp.route("/api/owner/confirm-cli", methods=["POST"])
def api_cli_owner_confirm() -> Any:
    """Confirm the owner candidate with CLI-compatible full-result behavior."""
    try:
        result = confirm_owner_candidate()
    except LockTimeout as exc:
        return _voiceprint_busy_response(exc)
    if result.get("error_kind") == "voiceprint_busy":
        return error_response(SPEAKER_VOICEPRINT_BUSY, detail=result["error"])
    if "error" in result:
        return error_response(
            SPEAKER_COMMAND_FAILED,
            detail=json.dumps(result, indent=2, default=str),
            status=400,
        )
    return jsonify(result)


@speakers_bp.route("/api/owner/reject-cli", methods=["POST"])
def api_cli_owner_reject() -> Any:
    """Reject the owner candidate with CLI-compatible domain result behavior."""
    try:
        result = reject_owner_candidate()
    except LockTimeout as exc:
        return _voiceprint_busy_response(exc)
    return jsonify(result)


@speakers_bp.route("/api/owner/ready", methods=["POST"])
def api_cli_owner_ready() -> Any:
    """Return cheap owner-detection readiness without running detection."""
    return jsonify(owner_detection_ready())


@speakers_bp.route("/api/serve_audio/<day>/<path:rel_path>")
def serve_audio(day: str, rel_path: str) -> Any:
    """Serve audio files for playback."""
    if not DATE_RE.fullmatch(day):
        return error_response(INVALID_DAY, detail="Day not found", status=404)
    path, error = safe_day_path(day, rel_path)
    if error is not None:
        return error
    if not path.is_file():
        return error_response(FILE_NOT_FOUND, detail="File not found")
    mimetype = MIME_TYPES.get(path.suffix.lower())
    if mimetype is None:
        raise ValueError(f"unregistered media extension for serve_audio: {path.suffix}")
    return send_file(path, conditional=True, mimetype=mimetype)
