# SPDX-License-Identifier: AGPL-3.0-only
# Copyright (c) 2026 sol pbc

"""Derived edge extraction from speaker attribution labels."""

from __future__ import annotations

import json
import logging
import re
from dataclasses import dataclass
from itertools import combinations
from pathlib import Path
from typing import Any

from solstone.think.edge_sources import EdgeContext, segment_ref
from solstone.think.entities.journal import load_all_journal_entities
from solstone.think.utils import get_journal, resolve_journal_path, segment_start_ts_ms

logger = logging.getLogger(__name__)
EntityFingerprint = tuple[int, int, int, int]


@dataclass(frozen=True)
class MentionCandidate:
    """Entity mention candidate selected from journal entities."""

    entity_id: str
    entity_name: str
    variant: str


@dataclass(frozen=True)
class CandidateIndex:
    """Memoized mention-matching structure for one journal root."""

    pattern: re.Pattern[str] | None
    candidates_by_key: dict[str, MentionCandidate]
    entity_fingerprint: EntityFingerprint


_CANDIDATE_CACHE: dict[str, CandidateIndex] = {}


def extract_speaker_edges(payload: dict[str, Any], ctx: EdgeContext) -> list[dict]:
    """Extract spoke-with and mentioned edges from a speaker_labels.json source."""
    composite_id, segment_key = _parse_speaker_label_path(ctx.path)
    journal = get_journal()
    segment_dir = resolve_journal_path(journal, composite_id)
    if not isinstance(payload, dict):
        raise ValueError(f"speaker labels must be a JSON object: {ctx.path}")

    labels = payload.get("labels", [])
    if not isinstance(labels, list):
        return []

    speaker_ids = _distinct_speaker_ids(labels)
    if not speaker_ids:
        return []

    ts = segment_start_ts_ms(ctx.day, segment_key)
    rows = _spoke_with_rows(speaker_ids, ctx, composite_id, ts)

    mention_labels = _valid_label_records(labels)
    if not mention_labels:
        return rows

    transcript_stem = _select_transcript_stem(segment_dir, composite_id)
    if transcript_stem is None:
        return rows

    transcript_texts = _load_transcript_texts(segment_dir, transcript_stem)
    if transcript_texts is None:
        logger.warning("speaker edge transcript missing for %s", composite_id)
        return rows

    if not transcript_texts:
        return rows

    candidates = _candidate_index_for_journal(journal)
    rows.extend(
        _mentioned_rows(
            mention_labels,
            transcript_texts,
            candidates,
            ctx,
            composite_id,
            ts,
        )
    )
    return rows


def _parse_speaker_label_path(path: str) -> tuple[str, str]:
    """Validate a speaker-label path and return composite id and segment key."""
    parts = Path(path).parts
    if len(parts) != 5 or parts[3] != "talents" or parts[4] != "speaker_labels.json":
        raise ValueError(f"invalid speaker labels path: {path}")
    return segment_ref(path)


def _distinct_speaker_ids(labels: list[Any]) -> set[str]:
    """Return non-null speaker ids across all label records."""
    speaker_ids: set[str] = set()
    for label in labels:
        if not isinstance(label, dict):
            continue
        speaker = label.get("speaker")
        if isinstance(speaker, str) and speaker:
            speaker_ids.add(speaker)
    return speaker_ids


def _valid_label_records(labels: list[Any]) -> list[dict[str, Any]]:
    """Return records with positive sentence ids and non-null speakers."""
    records: list[dict[str, Any]] = []
    for label in labels:
        if not isinstance(label, dict):
            continue
        speaker = label.get("speaker")
        sentence_id = label.get("sentence_id")
        if not isinstance(speaker, str) or not speaker:
            continue
        if not isinstance(sentence_id, int) or isinstance(sentence_id, bool):
            continue
        if sentence_id <= 0:
            continue
        records.append({"speaker": speaker, "sentence_id": sentence_id})
    return records


def _is_qualifying_audio_stem(stem: str) -> bool:
    """Return whether a stem participates in speaker attribution."""
    return stem == "audio" or stem.endswith("_audio")


def _select_transcript_stem(segment_dir: Path, composite_id: str) -> str | None:
    """Select transcript stem with npz precedence and safe JSONL fallback."""
    npz_stems = sorted(
        p.stem for p in segment_dir.glob("*.npz") if _is_qualifying_audio_stem(p.stem)
    )
    if npz_stems:
        return npz_stems[0]

    jsonl_stems = sorted(
        p.stem for p in segment_dir.glob("*.jsonl") if _is_qualifying_audio_stem(p.stem)
    )
    if len(jsonl_stems) == 1:
        return jsonl_stems[0]

    logger.warning("speaker edge transcript unresolved for %s", composite_id)
    return None


def _load_transcript_texts(segment_dir: Path, stem: str) -> dict[int, str] | None:
    """Load transcript text keyed by physical sentence line number."""
    transcript_path = segment_dir / f"{stem}.jsonl"
    if not transcript_path.exists():
        return None

    texts: dict[int, str] = {}
    for line_no, line in enumerate(
        transcript_path.read_text(encoding="utf-8").splitlines()
    ):
        if line_no == 0 or not line.strip():
            continue
        try:
            entry = json.loads(line)
        except json.JSONDecodeError:
            continue
        if not isinstance(entry, dict):
            continue
        text = entry.get("text")
        if isinstance(text, str) and text:
            texts[line_no] = text
    return texts


def _candidate_index_for_journal(journal: str) -> CandidateIndex:
    """Return the memoized candidate index for the active journal path."""
    cache_key = str(Path(journal).resolve())
    fingerprint = _entity_file_fingerprint(cache_key)
    cached = _CANDIDATE_CACHE.get(cache_key)
    if cached is None or cached.entity_fingerprint != fingerprint:
        cached = _build_candidate_index(fingerprint)
        _CANDIDATE_CACHE[cache_key] = cached
    return cached


def _entity_file_fingerprint(journal: str) -> EntityFingerprint:
    """Return a cheap mutation fingerprint for journal-level entity files."""
    entities_dir = Path(journal) / "entities"
    if not entities_dir.is_dir():
        return (0, 0, 0, 0)

    count = 0
    max_mtime_ns = 0
    sum_mtime_ns = 0
    sum_size = 0
    for path in sorted(entities_dir.glob("*/entity.json")):
        try:
            stat = path.stat()
        except OSError:
            continue
        count += 1
        max_mtime_ns = max(max_mtime_ns, stat.st_mtime_ns)
        sum_mtime_ns += stat.st_mtime_ns
        sum_size += stat.st_size
    return (count, max_mtime_ns, sum_mtime_ns, sum_size)


def _build_candidate_index(fingerprint: EntityFingerprint) -> CandidateIndex:
    """Build mention candidates, ambiguity drops, and the combined regex."""
    candidates_by_key: dict[str, MentionCandidate] = {}
    ambiguous: set[str] = set()
    for entity_id, entity in load_all_journal_entities().items():
        if entity.get("blocked"):
            continue
        entity_name = entity.get("name")
        if not isinstance(entity_name, str) or not entity_name.strip():
            continue

        for variant in _entity_variants(entity):
            key = variant.casefold()
            if key in ambiguous:
                continue
            candidate = MentionCandidate(entity_id, entity_name, variant)
            existing = candidates_by_key.get(key)
            if existing is None:
                candidates_by_key[key] = candidate
            elif existing.entity_id != entity_id:
                candidates_by_key.pop(key, None)
                ambiguous.add(key)

    if not candidates_by_key:
        return CandidateIndex(
            pattern=None,
            candidates_by_key={},
            entity_fingerprint=fingerprint,
        )

    variants = sorted(
        (candidate.variant for candidate in candidates_by_key.values()),
        key=lambda variant: (-len(variant), variant.casefold(), variant),
    )
    pattern = re.compile(
        r"(?<!\w)(?:"
        + "|".join(re.escape(variant) for variant in variants)
        + r")(?!\w)",
        re.IGNORECASE,
    )
    return CandidateIndex(
        pattern=pattern,
        candidates_by_key=candidates_by_key,
        entity_fingerprint=fingerprint,
    )


def _entity_variants(entity: dict[str, Any]) -> list[str]:
    """Return speakable full-name and parenthetical variants."""
    sources: list[str] = []
    name = entity.get("name")
    if isinstance(name, str):
        sources.append(name)
    aka = entity.get("aka")
    if isinstance(aka, list):
        sources.extend(alias for alias in aka if isinstance(alias, str))

    variants: list[str] = []
    seen: set[str] = set()
    for source in sources:
        for variant in _variants_from_name(source):
            if len(variant) < 3 or not _is_speakable(variant):
                continue
            key = variant.casefold()
            if key not in seen:
                variants.append(variant)
                seen.add(key)
    return variants


def _variants_from_name(name: str) -> list[str]:
    """Return the base name plus comma-separated parenthetical aliases."""
    variants: list[str] = []
    base = re.sub(r"\s*\([^)]+\)", "", name).strip()
    if base:
        variants.append(base)
    for group in re.findall(r"\(([^)]+)\)", name):
        variants.extend(item.strip() for item in group.split(",") if item.strip())
    return variants


def _is_speakable(name: str) -> bool:
    """Return whether a name is suitable for direct transcript matching."""
    return bool(re.fullmatch(r"[a-zA-Z0-9\s.\-']+", name)) and any(
        c.isalpha() for c in name
    )


def _spoke_with_rows(
    speaker_ids: set[str],
    ctx: EdgeContext,
    composite_id: str,
    ts: int,
) -> list[dict[str, Any]]:
    """Build deterministic spoke-with rows from distinct speakers."""
    return [
        {
            "src": src,
            "dst": dst,
            "kind": "spoke-with",
            "src_name": None,
            "dst_name": None,
            "day": ctx.day,
            "facet": ctx.facet,
            "source": "speaker",
            "path": ctx.path,
            "anchor": composite_id,
            "label": "",
            "ts": ts,
            "weight": 1,
        }
        for src, dst in combinations(sorted(speaker_ids), 2)
    ]


def _mentioned_rows(
    labels: list[dict[str, Any]],
    transcript_texts: dict[int, str],
    candidates: CandidateIndex,
    ctx: EdgeContext,
    composite_id: str,
    ts: int,
) -> list[dict[str, Any]]:
    """Build mentioned rows by matching entity variants in transcript sentences."""
    if candidates.pattern is None:
        return []

    pair_sentence_ids: dict[tuple[str, str], set[int]] = {}
    pair_labels: dict[tuple[str, str], str] = {}
    pair_names: dict[tuple[str, str], str] = {}

    for label in sorted(labels, key=lambda item: item["sentence_id"]):
        speaker = label["speaker"]
        sentence_id = label["sentence_id"]
        text = transcript_texts.get(sentence_id)
        if text is None:
            continue

        for match in candidates.pattern.finditer(text):
            # Non-ASCII case-fold matches that cannot round-trip are silently
            # dropped for precision over recall.
            candidate = candidates.candidates_by_key.get(match.group().casefold())
            if candidate is None or candidate.entity_id == speaker:
                continue
            pair = (speaker, candidate.entity_id)
            if pair not in pair_labels:
                pair_labels[pair] = candidate.variant
                pair_names[pair] = candidate.entity_name
            pair_sentence_ids.setdefault(pair, set()).add(sentence_id)

    return [
        {
            "src": speaker,
            "dst": target,
            "kind": "mentioned",
            "src_name": None,
            "dst_name": pair_names[(speaker, target)],
            "day": ctx.day,
            "facet": ctx.facet,
            "source": "mention",
            "path": ctx.path,
            "anchor": composite_id,
            "label": pair_labels[(speaker, target)],
            "ts": ts,
            "weight": len(sentence_ids),
        }
        for (speaker, target), sentence_ids in sorted(pair_sentence_ids.items())
    ]
