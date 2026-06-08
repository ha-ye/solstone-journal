# SPDX-License-Identifier: AGPL-3.0-only
# Copyright (c) 2026 sol pbc

"""Tests for import-linking and import-based voiceprint seeding."""

from __future__ import annotations

import json

import numpy as np

from solstone.apps.speakers.bootstrap import link_import, seed_from_imports

# --- link-import tests ---


def test_link_import_success(speakers_env):
    """link-import adds name as aka on entity."""
    env = speakers_env()
    env.create_entity("Sarah Chen")

    result = link_import("Sarah C", "sarah_chen")
    assert result["linked"] is True
    assert result["entity_id"] == "sarah_chen"
    assert result["name_added"] == "Sarah C"
    assert result["already_present"] is False

    # Verify entity was actually updated
    entity_path = env.journal / "entities" / "sarah_chen" / "entity.json"
    entity = json.loads(entity_path.read_text())
    assert "Sarah C" in entity["aka"]
    assert "updated_at" in entity


def test_link_import_already_present(speakers_env):
    """link-import reports already_present when name is already an aka."""
    env = speakers_env()
    entity_dir = env.create_entity("Sarah Chen")
    # Manually add aka
    entity_path = entity_dir / "entity.json"
    entity = json.loads(entity_path.read_text())
    entity["aka"] = ["Sarah C"]
    entity_path.write_text(json.dumps(entity))

    result = link_import("Sarah C", "sarah_chen")
    assert result["already_present"] is True


def test_link_import_entity_not_found(speakers_env):
    """link-import exits 1 with error JSON for missing entity."""
    speakers_env()
    result = link_import("Nobody", "nonexistent")
    assert "error" in result


def test_link_import_collision(speakers_env):
    """link-import exits 1 when aka collides with another entity."""
    env = speakers_env()
    env.create_entity("Alice Johnson")
    env.create_entity("Bob Smith")

    result = link_import("Bob Smith", "alice_johnson")
    assert "conflicts" in result["error"]


# --- seed-from-imports tests ---


def _create_owner_centroid(env, *, threshold: float = 0.85):
    """Helper: create owner centroid file for seed tests."""
    env.create_entity("Owner", is_principal=True)

    owner_dir = env.journal / "entities" / "owner"
    owner_dir.mkdir(parents=True, exist_ok=True)

    # Create a distinct owner embedding (pointing in a specific direction)
    owner_emb = np.zeros(256, dtype=np.float32)
    owner_emb[0] = 1.0  # Owner points along axis 0

    centroid_path = owner_dir / "owner_centroid.npz"
    np.savez_compressed(
        centroid_path,
        centroid=owner_emb,
        cluster_size=np.array(1, dtype=np.int32),
        threshold=np.float32(threshold),
        last_refreshed_at=np.array("2026-03-15T12:00:00Z"),
    )
    return owner_emb


def test_seed_from_imports_happy_path(speakers_env):
    """seed-from-imports seeds voiceprints for matched speakers."""
    env = speakers_env()
    _create_owner_centroid(env)
    env.create_entity("Alice Johnson")

    # Create embeddings that are NOT owner-like (orthogonal to owner)
    embs = np.zeros((3, 256), dtype=np.float32)
    embs[0, 1] = 1.0  # Points along axis 1 (orthogonal to owner axis 0)
    embs[1, 2] = 1.0
    embs[2, 3] = 1.0

    env.create_import_segment(
        "20240101",
        "100000_300",
        [
            ("Alice Johnson", "Hello everyone"),
            ("Alice Johnson", "Let's discuss the project"),
            ("Alice Johnson", "Thanks for joining"),
        ],
        embeddings=embs,
    )

    result = seed_from_imports(dry_run=False)
    assert result["segments_scanned"] >= 1
    assert result["segments_with_speakers"] >= 1
    assert result["embeddings_saved"] == 3
    assert "Alice Johnson" in result["speakers_found"]


def test_seed_from_imports_skips_generic_speakers(speakers_env):
    """seed-from-imports skips Human/Assistant speakers."""
    env = speakers_env()
    _create_owner_centroid(env)

    embs = np.zeros((2, 256), dtype=np.float32)
    embs[0, 1] = 1.0
    embs[1, 2] = 1.0

    env.create_import_segment(
        "20240101",
        "100000_300",
        [
            ("Human", "How do I fix this?"),
            ("Assistant", "Try restarting the service."),
        ],
        stream="import.chatgpt",
        embeddings=embs,
    )

    result = seed_from_imports(dry_run=True)
    assert result["embeddings_saved"] == 0
    assert result["segments_with_speakers"] == 0


def test_seed_from_imports_skips_unmatched_speakers(speakers_env):
    """seed-from-imports skips speakers with no matching entity."""
    env = speakers_env()
    _create_owner_centroid(env)
    # Don't create an entity for "Unknown Person"

    embs = np.zeros((1, 256), dtype=np.float32)
    embs[0, 1] = 1.0

    env.create_import_segment(
        "20240101",
        "100000_300",
        [("Unknown Person", "Hello")],
        embeddings=embs,
    )

    result = seed_from_imports(dry_run=True)
    assert result["embeddings_saved"] == 0


def test_seed_from_imports_owner_contamination(speakers_env):
    """seed-from-imports skips embeddings too similar to owner."""
    env = speakers_env()
    _create_owner_centroid(env, threshold=0.85)
    env.create_entity("Alice Johnson")

    # Create an embedding that IS owner-like (same direction as owner centroid)
    embs = np.zeros((1, 256), dtype=np.float32)
    embs[0, 0] = 1.0  # Same direction as owner (axis 0)

    env.create_import_segment(
        "20240101",
        "100000_300",
        [("Alice Johnson", "Hello")],
        embeddings=embs,
    )

    result = seed_from_imports(dry_run=True)
    assert result["embeddings_skipped_owner"] == 1
    assert result["embeddings_saved"] == 0


def test_seed_from_imports_dedup(speakers_env):
    """seed-from-imports is idempotent — second run skips duplicates."""
    env = speakers_env()
    _create_owner_centroid(env)
    env.create_entity("Alice Johnson")

    embs = np.zeros((1, 256), dtype=np.float32)
    embs[0, 1] = 1.0

    env.create_import_segment(
        "20240101",
        "100000_300",
        [("Alice Johnson", "Hello")],
        embeddings=embs,
    )

    # First run
    result1 = seed_from_imports(dry_run=False)
    assert result1["embeddings_saved"] == 1

    # Second run — should be all duplicates
    result2 = seed_from_imports(dry_run=False)
    assert result2["embeddings_saved"] == 0
    assert result2["embeddings_skipped_duplicate"] == 1


def test_seed_from_imports_default_is_preview(speakers_env):
    """seed-from-imports defaults to preview mode and doesn't write."""
    env = speakers_env()
    _create_owner_centroid(env)
    env.create_entity("Alice Johnson")

    embs = np.zeros((1, 256), dtype=np.float32)
    embs[0, 1] = 1.0

    env.create_import_segment(
        "20240101",
        "100000_300",
        [("Alice Johnson", "Hello")],
        embeddings=embs,
    )

    result = seed_from_imports(dry_run=True)
    assert result["embeddings_saved"] == 1  # Would-be saved count

    # Verify nothing was actually written
    vp_path = env.journal / "entities" / "alice_johnson" / "voiceprints.npz"
    assert not vp_path.exists()


def test_seed_from_imports_no_owner_centroid(speakers_env):
    """seed-from-imports errors when no owner centroid exists."""
    speakers_env()

    result = seed_from_imports(dry_run=True)
    assert "error" in result


def test_seed_from_imports_no_import_segments(speakers_env):
    """seed-from-imports returns zeroed stats when no import segments exist."""
    env = speakers_env()
    _create_owner_centroid(env)

    result = seed_from_imports()
    assert result["segments_scanned"] == 0
    assert result["embeddings_saved"] == 0
