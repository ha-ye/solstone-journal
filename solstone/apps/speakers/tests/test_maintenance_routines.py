# SPDX-License-Identifier: AGPL-3.0-only
# Copyright (c) 2026 sol pbc

"""Tests for speakers app-owned maintenance routine descriptors."""

from __future__ import annotations

import json
from pathlib import Path

import numpy as np

from solstone.apps.speakers.maintenance import run_name_variants
from solstone.apps.speakers.suggest import suggest_opportunities
from solstone.think.entities.journal import load_journal_entity, scan_journal_entities
from solstone.think.maintenance import (
    discover_routines,
    expected_schedule_entry,
    maintenance_schedule_name,
)
from solstone.think.speaker_review_candidates import load_candidates


def _write_voiceprints(entity_dir: Path, embedding: np.ndarray, *, offset: int = 0):
    embeddings = np.tile(embedding.reshape(1, -1), (5, 1))
    metadata = np.array(
        [
            json.dumps(
                {
                    "day": "20240101",
                    "segment_key": "143022_300",
                    "source": "mic_audio",
                    "sentence_id": i + offset,
                    "added_at": 1700000000000,
                }
            )
            for i in range(5)
        ],
        dtype=str,
    )
    np.savez_compressed(
        entity_dir / "voiceprints.npz",
        embeddings=embeddings,
        metadata=metadata,
    )


def _create_meetings_md(env, day: str, content: str) -> Path:
    chronicle_day = env.journal / "chronicle" / day
    chronicle_day.mkdir(parents=True, exist_ok=True)
    flat_day = env.journal / day
    if not flat_day.exists():
        flat_day.symlink_to(chronicle_day, target_is_directory=True)
    meetings_path = chronicle_day / "talents" / "meetings.md"
    meetings_path.parent.mkdir(parents=True, exist_ok=True)
    meetings_path.write_text(content, encoding="utf-8")
    return meetings_path


def test_speakers_name_variant_routine_is_discovered():
    routines = discover_routines()

    assert "speakers:name-variants" in routines
    routine = routines["speakers:name-variants"]
    assert routine.every == "daily"
    assert routine.max_runtime == "10m"
    assert expected_schedule_entry("speakers:name-variants", routine) == {
        "cmd": ["journal", "maintenance", "run", "speakers:name-variants"],
        "every": "daily",
        "enabled": True,
        "max_runtime": "10m",
    }
    assert maintenance_schedule_name("speakers:name-variants") == (
        "maintenance:speakers:name-variants"
    )


def test_run_name_variants_records_idempotently_without_merging(speakers_env):
    env = speakers_env()
    embedding = env.create_embedding([1.0, 0.0, 0.0])
    alias_dir = env.create_entity("Alice")
    canonical_dir = env.create_entity("Alice Johnson")
    _write_voiceprints(alias_dir, embedding)
    _write_voiceprints(canonical_dir, embedding, offset=10)
    labels_path = env.create_speaker_labels(
        "20240101",
        "143022_300",
        [
            {
                "sentence_id": 1,
                "speaker": "alice",
                "confidence": "high",
                "method": "voiceprint",
            }
        ],
    )
    labels_before = labels_path.read_text(encoding="utf-8")

    assert run_name_variants([]) == 0
    assert run_name_variants([]) == 0

    rows = load_candidates()
    assert len(rows) == 1
    assert rows[0]["status"] == "open"
    assert rows[0]["source_id"] == "alice"
    assert rows[0]["target_id"] == "alice_johnson"
    assert rows[0]["evidence"]["detection_count"] == 2
    assert load_journal_entity("alice") is not None
    assert load_journal_entity("alice_johnson") is not None
    assert "alice" in scan_journal_entities()
    assert labels_path.read_text(encoding="utf-8") == labels_before


def test_run_name_variants_bypasses_suggest_limit_starvation(speakers_env):
    env = speakers_env()
    env.create_entity("Romeo Montague")
    _create_meetings_md(
        env,
        "20240101",
        "# Meetings\n\n- 10:00 Strategy Call with Romeo and Juliet\n",
    )

    embedding = env.create_embedding([1.0, 0.0, 0.0])
    alias_dir = env.create_entity("Alice")
    canonical_dir = env.create_entity("Alice Johnson")
    _write_voiceprints(alias_dir, embedding)
    _write_voiceprints(canonical_dir, embedding, offset=10)

    limited = suggest_opportunities(limit=1)
    assert [item["type"] for item in limited] == ["import_linkable"]

    assert run_name_variants([]) == 0

    rows = load_candidates()
    assert len(rows) == 1
    assert rows[0]["source_id"] == "alice"
    assert rows[0]["target_id"] == "alice_johnson"
