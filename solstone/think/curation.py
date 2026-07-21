# SPDX-License-Identifier: AGPL-3.0-only
# Copyright (c) 2026 sol pbc
"""Shared curation queue logic for candidate review surfaces."""

from __future__ import annotations

import logging
import sqlite3
from dataclasses import dataclass
from typing import Any, Callable

import solstone.think.facet_review_candidates as facet_store
from solstone.think import (
    speaker_candidate_pair_review_candidates as speaker_pair_store,
)
from solstone.think import speaker_keep_separate as keep_separate_store
from solstone.think import speaker_review_candidates as speaker_store
from solstone.think.entities import review_candidates as entity_store
from solstone.think.entities.ambiguities import load_ambiguities
from solstone.think.entities.merge import merge_entity
from solstone.think.facets import create_facet
from solstone.think.indexer.edges import load_shared_neighborhood_jaccard
from solstone.think.journal_io import LockTimeout

KIND_FACET_CANDIDATE = "facet_candidate"
KIND_ENTITY_MERGE = "entity_merge"
KIND_ENTITY_AMBIGUITY = "entity_ambiguity"
KIND_SPEAKER_NAME_VARIANT = "speaker_name_variant"
KIND_SPEAKER_CANDIDATE_PAIR = "speaker_candidate_pair"
NEIGHBORHOOD_WEIGHT = 0.25
# Entity detection strength is an integer; a sub-1.0 neighborhood contribution
# can break ties but cannot outrank a genuinely stronger detection count.
_BATCH_BUSY_ERROR = "entity merge candidates are busy; try again"
_BATCH_MALFORMED_ERROR = "candidate is missing facet, source_slug, or target_slug"

logger = logging.getLogger(__name__)


@dataclass(frozen=True)
class CurationItem:
    """Structured curation item for owner-facing renderers."""

    kind: str
    key: str
    name: str | None
    facet: str | None
    source: str | None
    source_slug: str | None
    target: str | None
    target_slug: str | None
    evidence: dict[str, Any]
    strength: int
    composite: float

    def to_dict(self) -> dict[str, Any]:
        """Return a JSON/template-friendly representation."""
        return {
            "kind": self.kind,
            "key": self.key,
            "name": self.name,
            "facet": self.facet,
            "source": self.source,
            "source_slug": self.source_slug,
            "target": self.target,
            "target_slug": self.target_slug,
            "evidence": self.evidence,
            "strength": self.strength,
            "composite": self.composite,
        }


def _int_value(value: Any) -> int:
    if isinstance(value, bool):
        return int(value)
    if isinstance(value, int):
        return value
    try:
        return int(value)
    except (TypeError, ValueError):
        return 0


def _status(row: dict[str, Any]) -> str:
    return str(row.get("status") or "")


def _entity_key(facet: str, source_slug: str, target_slug: str) -> str:
    return entity_store.candidate_key(facet, source_slug, target_slug)


def _speaker_key(source_id: str, target_id: str) -> str:
    return speaker_store.candidate_key(source_id, target_id)


def _speaker_candidate_pair_key(anchor_a: str, anchor_b: str) -> str:
    return speaker_pair_store.candidate_key(anchor_a, anchor_b)


def _find_facet_candidate(name_key: str) -> dict[str, Any] | None:
    return facet_store.find_candidate(facet_store.load_candidates(), name_key)


def _find_entity_candidate(
    facet: str,
    source_slug: str,
    target_slug: str,
) -> dict[str, Any] | None:
    return entity_store.find_candidate(
        entity_store.load_candidates(),
        facet,
        source_slug,
        target_slug,
    )


def _find_speaker_candidate(
    source_id: str,
    target_id: str,
) -> dict[str, Any] | None:
    return speaker_store.find_candidate(
        speaker_store.load_candidates(),
        source_id,
        target_id,
    )


def _find_speaker_candidate_pair(
    anchor_a: str,
    anchor_b: str,
) -> dict[str, Any] | None:
    return speaker_pair_store.find_candidate(
        speaker_pair_store.load_candidates(),
        anchor_a,
        anchor_b,
    )


def _speaker_direction_matches(
    row: dict[str, Any],
    source_id: str,
    target_id: str,
) -> bool:
    return (
        str(row.get("source_id") or "") == source_id
        and str(row.get("target_id") or "") == target_id
    )


def _load_neighborhoods(
    pairs: list[tuple[str, str]],
) -> dict[tuple[str, str], dict[str, Any]]:
    if not pairs:
        return {}
    try:
        return load_shared_neighborhood_jaccard(pairs)
    except (FileNotFoundError, sqlite3.Error, OSError) as exc:
        logger.info("curation neighborhood ranking unavailable: %s", exc)
        return {}


def _facet_error(name_key: str, message: str) -> dict[str, Any]:
    return {
        "status": "error",
        "kind": KIND_FACET_CANDIDATE,
        "key": name_key,
        "error": message,
    }


def _entity_error(
    facet: str,
    source_slug: str,
    target_slug: str,
    message: str,
) -> dict[str, Any]:
    return {
        "status": "error",
        "kind": KIND_ENTITY_MERGE,
        "key": _entity_key(facet, source_slug, target_slug),
        "error": message,
    }


def _speaker_error(
    source_id: str,
    target_id: str,
    message: str,
) -> dict[str, Any]:
    return {
        "status": "error",
        "kind": KIND_SPEAKER_NAME_VARIANT,
        "key": _speaker_key(source_id, target_id),
        "error": message,
    }


def _speaker_candidate_pair_error(
    anchor_a: str,
    anchor_b: str,
    message: str,
) -> dict[str, Any]:
    return {
        "status": "error",
        "kind": KIND_SPEAKER_CANDIDATE_PAIR,
        "key": _speaker_candidate_pair_key(anchor_a, anchor_b),
        "error": message,
    }


def _merge_error_context(result: dict[str, Any]) -> dict[str, Any]:
    error = result.get("error")
    nested_repair = isinstance(error, dict) and error.get("code") == "repair_required"
    if result.get("operation_state") != "repair_required" and not nested_repair:
        return {}
    return {
        key: result.get(key)
        for key in (
            "operation_state",
            "mutation_applied",
            "source_state",
            "target_state",
            "safe_remediation",
        )
        if key in result
    }


def _merge_error_message(result: dict[str, Any]) -> str:
    error = result.get("error")
    if isinstance(error, dict):
        return str(error.get("message") or "entity merge requires repair")
    return str(error)


def _undo_descriptor(
    merge_id: Any,
    *,
    kind: str | None = None,
) -> dict[str, Any]:
    if kind == KIND_SPEAKER_CANDIDATE_PAIR:
        return {
            "available": False,
            "merge_id": None,
            "reason": "Speaker candidate-pair merges cannot be undone.",
        }
    value = str(merge_id or "")
    return {
        "available": bool(value),
        "merge_id": value or None,
        "reason": None if value else "No recorded merge id is available.",
    }


def load_open_items() -> list[CurationItem]:
    """Load all currently open curation items without mutating journal state."""
    items: list[CurationItem] = []
    entity_rows: list[tuple[dict[str, Any], int, str, str, str]] = []
    entity_pairs: list[tuple[str, str]] = []

    for row in facet_store.load_candidates():
        if row.get("status") != "open":
            continue
        evidence = row.get("evidence", {})
        if not isinstance(evidence, dict):
            evidence = {}
        strength = _int_value(row.get("count"))
        facet_evidence = dict(evidence)
        facet_evidence["count"] = strength
        facet_evidence["window_days"] = row.get("window_days")
        name_key = str(row.get("name_key") or "")
        items.append(
            CurationItem(
                kind=KIND_FACET_CANDIDATE,
                key=name_key,
                name=str(row.get("name") or name_key),
                facet=None,
                source=None,
                source_slug=None,
                target=None,
                target_slug=None,
                evidence=facet_evidence,
                strength=strength,
                composite=float(strength),
            )
        )

    for row in entity_store.load_candidates():
        if row.get("status") != "open":
            continue
        evidence = row.get("evidence", {})
        if not isinstance(evidence, dict):
            evidence = {}
        strength = _int_value(evidence.get("detection_count"))
        facet = str(row.get("facet") or "")
        source_slug = str(row.get("source_slug") or "")
        target_slug = str(row.get("target_slug") or "")
        entity_rows.append((row, strength, facet, source_slug, target_slug))
        entity_pairs.append((source_slug, target_slug))

    neighborhoods = _load_neighborhoods(entity_pairs)
    for row, strength, facet, source_slug, target_slug in entity_rows:
        evidence = row.get("evidence", {})
        if not isinstance(evidence, dict):
            evidence = {}
        entity_evidence = dict(evidence)
        composite = float(strength)
        neighborhood = neighborhoods.get((source_slug, target_slug))
        if neighborhood is not None:
            shared_neighbors = list(neighborhood["intersection"])
            similarity = float(neighborhood["jaccard"])
            composite += NEIGHBORHOOD_WEIGHT * similarity
            entity_evidence["shared_neighbors"] = shared_neighbors
            entity_evidence["neighborhood_similarity"] = similarity
        entity_evidence["composite"] = composite
        items.append(
            CurationItem(
                kind=KIND_ENTITY_MERGE,
                key=_entity_key(facet, source_slug, target_slug),
                name=None,
                facet=facet,
                source=str(row.get("source") or source_slug),
                source_slug=source_slug,
                target=str(row.get("target") or target_slug),
                target_slug=target_slug,
                evidence=entity_evidence,
                strength=strength,
                composite=composite,
            )
        )

    for row in load_ambiguities(strict=True):
        if row.get("status") != "open":
            continue
        ambiguity_id = str(row.get("ambiguity_id") or "")
        query = str(row.get("original_query") or row.get("latest_query") or "")
        evidence = {
            "observed_tier": row.get("observed_tier"),
            "ranked_candidates": row.get("ranked_candidates", []),
            "origins": row.get("origins", []),
            "occurrence_count": row.get("occurrence_count", 0),
        }
        strength = _int_value(row.get("occurrence_count"))
        items.append(
            CurationItem(
                kind=KIND_ENTITY_AMBIGUITY,
                key=ambiguity_id,
                name=query,
                facet=None,
                source=query,
                source_slug=None,
                target=None,
                target_slug=None,
                evidence=evidence,
                strength=strength,
                composite=float(strength),
            )
        )

    for row in speaker_store.load_candidates():
        if row.get("status") != "open":
            continue
        evidence = row.get("evidence", {})
        if not isinstance(evidence, dict):
            evidence = {}
        source_id = str(row.get("source_id") or "")
        target_id = str(row.get("target_id") or "")
        detection_count = _int_value(evidence.get("detection_count")) or 1
        if keep_separate_store.name_variant_pair_suppressed(
            source_id,
            target_id,
            detection_count,
        ):
            continue
        similarity = float(row["similarity"])
        speaker_evidence = dict(evidence)
        speaker_evidence["similarity"] = similarity
        speaker_evidence["readiness"] = row.get("readiness")
        items.append(
            CurationItem(
                kind=KIND_SPEAKER_NAME_VARIANT,
                key=_speaker_key(source_id, target_id),
                name=None,
                facet=None,
                source=str(row.get("source_label") or source_id),
                source_slug=source_id,
                target=str(row.get("target_label") or target_id),
                target_slug=target_id,
                evidence=speaker_evidence,
                strength=int(round(similarity * 100)),
                composite=float(int(round(similarity * 100))),
            )
        )

    for row in speaker_pair_store.load_candidates():
        if row.get("status") != "open":
            continue
        evidence = row.get("evidence", {})
        if not isinstance(evidence, dict):
            evidence = {}
        anchor_a = str(row.get("anchor_a") or "")
        anchor_b = str(row.get("anchor_b") or "")
        similarity = float(row.get("similarity") or evidence.get("similarity") or 0.0)
        pair_evidence = dict(evidence)
        pair_evidence["similarity"] = similarity
        items.append(
            CurationItem(
                kind=KIND_SPEAKER_CANDIDATE_PAIR,
                key=_speaker_candidate_pair_key(anchor_a, anchor_b),
                name=None,
                facet=None,
                source="candidate A",
                source_slug=anchor_a,
                target="candidate B",
                target_slug=anchor_b,
                evidence=pair_evidence,
                strength=int(round(similarity * 100)),
                composite=float(int(round(similarity * 100))),
            )
        )

    return sorted(items, key=lambda item: (-item.composite, item.key))


def accept_facet_candidate(name_key: str) -> dict[str, Any]:
    """Accept an open facet candidate by creating the facet, then marking accepted."""
    row = _find_facet_candidate(name_key)
    if row is None:
        return _facet_error(name_key, "candidate not found")

    status = _status(row)
    if status == "accepted":
        return {
            "status": "already_accepted",
            "kind": KIND_FACET_CANDIDATE,
            "key": name_key,
            "candidate": row,
        }
    if status != "open":
        return _facet_error(name_key, f"cannot accept candidate with status {status}")

    try:
        slug = create_facet(title=str(row.get("name") or ""), consent=True)
    except ValueError as exc:
        return _facet_error(name_key, str(exc))

    accepted = facet_store.accept_candidate(name_key)
    return {
        "status": "accepted",
        "kind": KIND_FACET_CANDIDATE,
        "key": name_key,
        "facet_slug": slug,
        "candidate": accepted,
    }


def dismiss_facet_candidate(name_key: str) -> dict[str, Any]:
    """Dismiss an open facet candidate."""
    row = _find_facet_candidate(name_key)
    if row is None:
        return _facet_error(name_key, "candidate not found")

    status = _status(row)
    if status == "dismissed":
        return {
            "status": "already_dismissed",
            "kind": KIND_FACET_CANDIDATE,
            "key": name_key,
            "candidate": row,
        }
    if status != "open":
        return _facet_error(name_key, f"cannot dismiss candidate with status {status}")

    dismissed = facet_store.dismiss_candidate(name_key)
    return {
        "status": "dismissed",
        "kind": KIND_FACET_CANDIDATE,
        "key": name_key,
        "candidate": dismissed,
    }


def accept_entity_candidate(
    facet: str,
    source_slug: str,
    target_slug: str,
    *,
    commit: bool,
) -> dict[str, Any]:
    """Preview or accept one open entity merge candidate."""
    key = _entity_key(facet, source_slug, target_slug)
    row = _find_entity_candidate(facet, source_slug, target_slug)
    if row is None:
        return _entity_error(facet, source_slug, target_slug, "candidate not found")

    status = _status(row)
    if not commit:
        if status != "open":
            return _entity_error(
                facet,
                source_slug,
                target_slug,
                f"cannot preview candidate with status {status}",
            )
        result = merge_entity(
            source_slug,
            target_slug,
            commit=False,
            caller="curation.preview",
        )
        if "error" in result:
            response = _entity_error(
                facet,
                source_slug,
                target_slug,
                _merge_error_message(result),
            )
            response.update(_merge_error_context(result))
            return response
        return {
            "status": "preview",
            "kind": KIND_ENTITY_MERGE,
            "key": key,
            "merge": result,
        }

    if status == "accepted":
        merge_id = row.get("merge_id")
        return {
            "status": "already_accepted",
            "kind": KIND_ENTITY_MERGE,
            "key": key,
            "candidate": row,
            "merge_id": merge_id,
            "undo": _undo_descriptor(merge_id),
        }
    if status != "open":
        return _entity_error(
            facet,
            source_slug,
            target_slug,
            f"cannot accept candidate with status {status}",
        )

    result = merge_entity(
        source_slug,
        target_slug,
        commit=True,
        caller="curation.accept",
    )
    if "error" in result:
        response = _entity_error(
            facet,
            source_slug,
            target_slug,
            _merge_error_message(result),
        )
        response.update(_merge_error_context(result))
        return response

    merge_id = result.get("merge_id")
    accepted = entity_store.accept_candidate(
        facet,
        source_slug,
        target_slug,
        merge_id=str(merge_id or "") or None,
    )
    return {
        "status": "accepted",
        "kind": KIND_ENTITY_MERGE,
        "key": key,
        "merge": result,
        "candidate": accepted,
        "merge_id": merge_id,
        "undo": _undo_descriptor(merge_id),
    }


def dismiss_entity_candidate(
    facet: str,
    source_slug: str,
    target_slug: str,
) -> dict[str, Any]:
    """Dismiss an open entity merge candidate."""
    key = _entity_key(facet, source_slug, target_slug)
    row = _find_entity_candidate(facet, source_slug, target_slug)
    if row is None:
        return _entity_error(facet, source_slug, target_slug, "candidate not found")

    status = _status(row)
    if status == "dismissed":
        return {
            "status": "already_dismissed",
            "kind": KIND_ENTITY_MERGE,
            "key": key,
            "candidate": row,
        }
    if status != "open":
        return _entity_error(
            facet,
            source_slug,
            target_slug,
            f"cannot dismiss candidate with status {status}",
        )

    dismissed = entity_store.dismiss_candidate(facet, source_slug, target_slug)
    return {
        "status": "dismissed",
        "kind": KIND_ENTITY_MERGE,
        "key": key,
        "candidate": dismissed,
    }


def _run_entity_batch(
    items: list[dict[str, Any]],
    apply: Callable[[str, str, str], dict[str, Any]],
    success_statuses: frozenset[str],
) -> tuple[list[dict[str, Any]], int, int]:
    """Apply one entity-candidate action per item, sequentially, never aborting."""
    results: list[dict[str, Any]] = []
    ok = 0
    failed = 0

    for item in items:
        result: dict[str, Any] | None = None
        if isinstance(item, dict):
            facet = str(item.get("facet") or "")
            source_slug = str(item.get("source_slug") or "")
            target_slug = str(item.get("target_slug") or "")
        else:
            facet = ""
            source_slug = ""
            target_slug = ""

        if not facet or not source_slug or not target_slug:
            status = "error"
            error = _BATCH_MALFORMED_ERROR
        else:
            try:
                result = apply(facet, source_slug, target_slug)
            except LockTimeout:
                status = "error"
                error = _BATCH_BUSY_ERROR
            else:
                status = str(result.get("status") or "")
                error = result.get("error") or None

        if status in success_statuses:
            ok += 1
        else:
            failed += 1

        item_result = {
            "facet": facet,
            "source_slug": source_slug,
            "target_slug": target_slug,
            "status": status,
            "error": error,
        }
        if result is not None and status in success_statuses and "undo" in result:
            item_result["merge_id"] = result.get("merge_id")
            item_result["undo"] = result["undo"]
        if result is not None and result.get("operation_state") == "repair_required":
            item_result.update(_merge_error_context(result))
        results.append(item_result)

    return results, ok, failed


def accept_entity_candidate_batch(items: list[dict[str, Any]]) -> dict[str, Any]:
    """Accept many open entity merge candidates, one at a time."""
    results, ok, failed = _run_entity_batch(
        items,
        lambda f, s, t: accept_entity_candidate(f, s, t, commit=True),
        frozenset({"accepted", "already_accepted"}),
    )
    return {"results": results, "accepted": ok, "failed": failed}


def dismiss_entity_candidate_batch(items: list[dict[str, Any]]) -> dict[str, Any]:
    """Dismiss many open entity merge candidates, one at a time."""
    results, ok, failed = _run_entity_batch(
        items,
        dismiss_entity_candidate,
        frozenset({"dismissed", "already_dismissed"}),
    )
    return {"results": results, "dismissed": ok, "failed": failed}


def accept_speaker_candidate(
    source_id: str,
    target_id: str,
    *,
    commit: bool,
) -> dict[str, Any]:
    """Preview or accept one open speaker name-variant merge candidate."""
    key = _speaker_key(source_id, target_id)
    row = _find_speaker_candidate(source_id, target_id)
    if row is None:
        return _speaker_error(source_id, target_id, "candidate not found")
    if not _speaker_direction_matches(row, source_id, target_id):
        return _speaker_error(source_id, target_id, "candidate direction mismatch")

    status = _status(row)
    if not commit:
        if status != "open":
            return _speaker_error(
                source_id,
                target_id,
                f"cannot preview candidate with status {status}",
            )
        result = merge_entity(
            source_id,
            target_id,
            keep_source_as_aka=True,
            commit=False,
            caller="curation.speaker.preview",
        )
        if "error" in result:
            response = _speaker_error(
                source_id,
                target_id,
                _merge_error_message(result),
            )
            response.update(_merge_error_context(result))
            return response
        return {
            "status": "preview",
            "kind": KIND_SPEAKER_NAME_VARIANT,
            "key": key,
            "merge": result,
        }

    if status == "accepted":
        merge_id = row.get("merge_id")
        return {
            "status": "already_accepted",
            "kind": KIND_SPEAKER_NAME_VARIANT,
            "key": key,
            "candidate": row,
            "merge_id": merge_id,
            "undo": _undo_descriptor(merge_id),
        }
    if status != "open":
        return _speaker_error(
            source_id,
            target_id,
            f"cannot accept candidate with status {status}",
        )

    result = merge_entity(
        source_id,
        target_id,
        keep_source_as_aka=True,
        commit=True,
        caller="curation.speaker.accept",
    )
    if "error" in result:
        response = _speaker_error(
            source_id,
            target_id,
            _merge_error_message(result),
        )
        response.update(_merge_error_context(result))
        return response

    merge_id = result.get("merge_id")
    accepted = speaker_store.accept_candidate(
        source_id,
        target_id,
        merge_id=str(merge_id or "") or None,
    )
    return {
        "status": "accepted",
        "kind": KIND_SPEAKER_NAME_VARIANT,
        "key": key,
        "merge": result,
        "candidate": accepted,
        "merge_id": merge_id,
        "undo": _undo_descriptor(merge_id),
    }


def dismiss_speaker_candidate(
    source_id: str,
    target_id: str,
) -> dict[str, Any]:
    """Dismiss an open speaker name-variant merge candidate."""
    key = _speaker_key(source_id, target_id)
    row = _find_speaker_candidate(source_id, target_id)
    if row is None:
        return _speaker_error(source_id, target_id, "candidate not found")
    if not _speaker_direction_matches(row, source_id, target_id):
        return _speaker_error(source_id, target_id, "candidate direction mismatch")

    status = _status(row)
    if status == "dismissed":
        return {
            "status": "already_dismissed",
            "kind": KIND_SPEAKER_NAME_VARIANT,
            "key": key,
            "candidate": row,
        }
    if status != "open":
        return _speaker_error(
            source_id,
            target_id,
            f"cannot dismiss candidate with status {status}",
        )

    dismissed = speaker_store.dismiss_candidate(source_id, target_id)
    return {
        "status": "dismissed",
        "kind": KIND_SPEAKER_NAME_VARIANT,
        "key": key,
        "candidate": dismissed,
    }


def accept_speaker_candidate_pair(anchor_a: str, anchor_b: str) -> dict[str, Any]:
    """Accept one open speaker candidate-pair merge candidate."""
    key = _speaker_candidate_pair_key(anchor_a, anchor_b)
    row = _find_speaker_candidate_pair(anchor_a, anchor_b)
    if row is None:
        return _speaker_candidate_pair_error(anchor_a, anchor_b, "candidate not found")

    status = _status(row)
    if status == "accepted":
        return {
            "status": "already_accepted",
            "kind": KIND_SPEAKER_CANDIDATE_PAIR,
            "key": key,
            "candidate": row,
            "undo": _undo_descriptor(None, kind=KIND_SPEAKER_CANDIDATE_PAIR),
        }
    if status != "open":
        return _speaker_candidate_pair_error(
            anchor_a,
            anchor_b,
            f"cannot accept candidate with status {status}",
        )

    from solstone.apps.speakers.candidate_tracker import CandidateTracker

    merge = CandidateTracker().merge_candidate_pair(anchor_a, anchor_b)
    if merge.get("status") != "merged":
        return _speaker_candidate_pair_error(
            anchor_a,
            anchor_b,
            str(merge.get("error") or "candidate pair is already merged"),
        )

    accepted = speaker_pair_store.accept_candidate(anchor_a, anchor_b)
    return {
        "status": "accepted",
        "kind": KIND_SPEAKER_CANDIDATE_PAIR,
        "key": key,
        "merge": merge,
        "candidate": accepted,
        "undo": _undo_descriptor(None, kind=KIND_SPEAKER_CANDIDATE_PAIR),
    }


def dismiss_speaker_candidate_pair(anchor_a: str, anchor_b: str) -> dict[str, Any]:
    """Dismiss one open speaker candidate-pair merge candidate."""
    key = _speaker_candidate_pair_key(anchor_a, anchor_b)
    row = _find_speaker_candidate_pair(anchor_a, anchor_b)
    if row is None:
        return _speaker_candidate_pair_error(anchor_a, anchor_b, "candidate not found")

    status = _status(row)
    if status == "dismissed":
        return {
            "status": "already_dismissed",
            "kind": KIND_SPEAKER_CANDIDATE_PAIR,
            "key": key,
            "candidate": row,
        }
    if status != "open":
        return _speaker_candidate_pair_error(
            anchor_a,
            anchor_b,
            f"cannot dismiss candidate with status {status}",
        )

    dismissed = speaker_pair_store.dismiss_candidate(anchor_a, anchor_b)
    return {
        "status": "dismissed",
        "kind": KIND_SPEAKER_CANDIDATE_PAIR,
        "key": key,
        "candidate": dismissed,
    }


def merge_preview_fields(merge_result: dict[str, Any]) -> dict[str, Any]:
    """Return compact preview fields used by curation renderers."""
    identity = merge_result.get("would_identity") or {}
    facets = merge_result.get("would_facets") or {}
    segments = merge_result.get("would_segments") or {}
    voiceprints = merge_result.get("would_voiceprints") or {}
    errors = segments.get("errors") if isinstance(segments, dict) else []
    if not isinstance(errors, list):
        errors = []
    return {
        "akas_added": identity.get("akas_added", []),
        "emails_added_count": _int_value(identity.get("emails_added_count")),
        "facet_moved_count": _int_value(facets.get("moved_count")),
        "facet_merged_count": _int_value(facets.get("merged_count")),
        "observations_appended": _int_value(facets.get("observations_appended")),
        "labels_rewritten": _int_value(segments.get("labels_rewritten")),
        "corrections_rewritten": _int_value(segments.get("corrections_rewritten")),
        "segment_errors": errors,
        "voiceprints_added": _int_value(voiceprints.get("added")),
        "voiceprints_target_total": _int_value(voiceprints.get("target_total")),
    }
