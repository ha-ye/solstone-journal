# SPDX-License-Identifier: AGPL-3.0-only
# Copyright (c) 2026 sol pbc
"""Shared curation queue logic for candidate review surfaces."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any

import solstone.think.facet_review_candidates as facet_store
from solstone.think import speaker_review_candidates as speaker_store
from solstone.think.entities import review_candidates as entity_store
from solstone.think.entities.merge import merge_entity
from solstone.think.facets import create_facet

KIND_FACET_CANDIDATE = "facet_candidate"
KIND_ENTITY_MERGE = "entity_merge"
KIND_SPEAKER_NAME_VARIANT = "speaker_name_variant"


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


def _speaker_direction_matches(
    row: dict[str, Any],
    source_id: str,
    target_id: str,
) -> bool:
    return (
        str(row.get("source_id") or "") == source_id
        and str(row.get("target_id") or "") == target_id
    )


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


def load_open_items() -> list[CurationItem]:
    """Load all currently open curation items without mutating journal state."""
    items: list[CurationItem] = []

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
                evidence=dict(evidence),
                strength=strength,
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
            )
        )

    return sorted(items, key=lambda item: (-item.strength, item.key))


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
            return _entity_error(facet, source_slug, target_slug, str(result["error"]))
        return {
            "status": "preview",
            "kind": KIND_ENTITY_MERGE,
            "key": key,
            "merge": result,
        }

    if status == "accepted":
        return {
            "status": "already_accepted",
            "kind": KIND_ENTITY_MERGE,
            "key": key,
            "candidate": row,
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
        return _entity_error(facet, source_slug, target_slug, str(result["error"]))

    accepted = entity_store.accept_candidate(facet, source_slug, target_slug)
    return {
        "status": "accepted",
        "kind": KIND_ENTITY_MERGE,
        "key": key,
        "merge": result,
        "candidate": accepted,
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
            return _speaker_error(source_id, target_id, str(result["error"]))
        return {
            "status": "preview",
            "kind": KIND_SPEAKER_NAME_VARIANT,
            "key": key,
            "merge": result,
        }

    if status == "accepted":
        return {
            "status": "already_accepted",
            "kind": KIND_SPEAKER_NAME_VARIANT,
            "key": key,
            "candidate": row,
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
        return _speaker_error(source_id, target_id, str(result["error"]))

    accepted = speaker_store.accept_candidate(source_id, target_id)
    return {
        "status": "accepted",
        "kind": KIND_SPEAKER_NAME_VARIANT,
        "key": key,
        "merge": result,
        "candidate": accepted,
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
