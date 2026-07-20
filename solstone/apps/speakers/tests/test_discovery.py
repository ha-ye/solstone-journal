# SPDX-License-Identifier: AGPL-3.0-only
# Copyright (c) 2026 sol pbc

"""Tests for unknown speaker discovery."""

from __future__ import annotations

import json
from pathlib import Path

import numpy as np

from solstone.apps.speakers.discovery import (
    _discovery_cache_path,
    _discovery_resolved_path,
    discover_unknown_speakers,
    get_cluster_presence,
    identify_cluster,
    load_discovery_cache,
)
from solstone.apps.speakers.tests.conftest import journal_tree_hash
from solstone.apps.speakers.owner import OWNER_THRESHOLD


def _make_speaker_embeddings(
    base_vector: list[float],
    count: int,
    noise_scale: float = 0.0,
) -> np.ndarray:
    """Create a cluster of similar embeddings around a base direction."""
    base = np.array(base_vector + [0.0] * (256 - len(base_vector)), dtype=np.float32)
    base = base / np.linalg.norm(base)
    rng = np.random.default_rng(42)
    noise = rng.normal(0, noise_scale, (count, 256)).astype(np.float32)
    embeddings = base + noise
    norms = np.linalg.norm(embeddings, axis=1, keepdims=True)
    return embeddings / norms


def _setup_owner_centroid(
    journal: Path,
    vector: list[float],
    entity_id: str = "owner_test",
) -> np.ndarray:
    """Create owner entity with centroid for testing."""
    base = np.array(vector + [0.0] * (256 - len(vector)), dtype=np.float32)
    centroid = base / np.linalg.norm(base)
    entity_dir = journal / "entities" / entity_id
    entity_dir.mkdir(parents=True, exist_ok=True)
    (entity_dir / "entity.json").write_text(
        json.dumps(
            {
                "id": entity_id,
                "name": "Owner Test",
                "type": "Person",
                "is_principal": True,
            }
        ),
        encoding="utf-8",
    )
    np.savez_compressed(
        entity_dir / "owner_centroid.npz",
        centroid=centroid,
        cluster_size=np.array(100, dtype=np.int32),
        threshold=np.array(OWNER_THRESHOLD, dtype=np.float32),
        last_refreshed_at=np.array("2026-01-01T00:00:00Z"),
    )
    return centroid


def _create_cluster_segments(
    env,
    embeddings: np.ndarray,
    *,
    audio_extension: str = ".flac",
) -> list[tuple[str, str, int]]:
    """Create four segments with one qualifying cluster and one filtered cluster."""
    segments = [
        ("20240101", "090000_300"),
        ("20240102", "090000_300"),
        ("20240103", "090000_300"),
        ("20240104", "090000_300"),
    ]
    alt_embeddings = _make_speaker_embeddings([0.0, 0.0, 1.0], embeddings.shape[0])
    results = []
    for idx, (day, segment_key) in enumerate(segments):
        segment_embeddings = embeddings
        if idx < 2:
            segment_embeddings = np.vstack([embeddings, alt_embeddings])
        env.create_segment(
            day,
            segment_key,
            ["audio"],
            embeddings=segment_embeddings,
            audio_extension=audio_extension,
        )
        results.append((day, segment_key, segment_embeddings.shape[0]))
    return results


def _all_sentence_labels(entity_id: str, count: int) -> list[dict]:
    """Build fully attributed labels for a segment."""
    return [
        {
            "sentence_id": idx,
            "speaker": entity_id,
            "confidence": "high",
            "method": "user_identified",
        }
        for idx in range(1, count + 1)
    ]


def _load_voiceprint_count(journal: Path, entity_id: str) -> int:
    """Return number of saved voiceprints for an entity."""
    path = journal / "entities" / entity_id / "voiceprints.npz"
    if not path.exists():
        return 0
    data = np.load(path, allow_pickle=False)
    return int(len(data["embeddings"]))


def _load_corrections_count(journal: Path, day: str, segment_key: str) -> int:
    """Return number of correction entries for a segment."""
    path = journal / day / "test" / segment_key / "talents" / "speaker_corrections.json"
    if not path.exists():
        return 0
    return len(json.loads(path.read_text(encoding="utf-8")).get("corrections", []))


def _write_discovery_cache(env, cluster_id: int, records: list[dict]) -> None:
    awareness_dir = env.journal / "awareness"
    awareness_dir.mkdir(parents=True, exist_ok=True)
    cache_path = awareness_dir / "discovery_clusters.json"
    cache = {"version": "test", "clusters": {}}
    if cache_path.exists():
        cache = json.loads(cache_path.read_text(encoding="utf-8"))
    cache.setdefault("clusters", {})[str(cluster_id)] = records
    (awareness_dir / "discovery_clusters.json").write_text(
        json.dumps(cache, indent=2),
        encoding="utf-8",
    )


def _cluster_record(
    day: str,
    segment_key: str,
    *,
    stream: str = "test",
    source: str = "audio",
    sentence_id: int = 1,
) -> dict:
    return {
        "day": day,
        "stream": stream,
        "segment_key": segment_key,
        "source": source,
        "sentence_id": sentence_id,
    }


def _candidate_evidence(*items: tuple[str, list[str]]) -> dict:
    return {
        "owner_centroid_last_refreshed_at": None,
        "voiceprint_versions": {},
        "candidate_evidence": [
            {"entity_id": entity_id, "sources": sources}
            for entity_id, sources in items
        ],
    }


def test_load_discovery_cache_missing_is_read_only(tmp_path, monkeypatch):
    monkeypatch.setenv("SOLSTONE_JOURNAL", str(tmp_path))
    import solstone.think.utils as think_utils

    think_utils._journal_path_cache = None

    assert load_discovery_cache() is None
    assert not (tmp_path / "awareness").exists()


def test_discover_no_owner_centroid(speakers_env):
    speakers_env()

    result = discover_unknown_speakers()

    assert result == {"clusters": []}


def test_discover_no_unmatched(speakers_env):
    env = speakers_env()
    _setup_owner_centroid(env.journal, [0.0, 1.0])
    env.create_entity("Alice Test")
    embeddings = _make_speaker_embeddings([1.0, 0.0], 5)
    segments = _create_cluster_segments(env, embeddings)

    for day, segment_key, sentence_count in segments:
        env.create_speaker_labels(
            day,
            segment_key,
            _all_sentence_labels("alice_test", sentence_count),
        )

    result = discover_unknown_speakers()

    assert result == {"clusters": []}


def test_discover_clusters_found(speakers_env):
    env = speakers_env()
    _setup_owner_centroid(env.journal, [0.0, 1.0])
    embeddings = _make_speaker_embeddings([1.0, 0.0], 5)
    _create_cluster_segments(env, embeddings)

    result = discover_unknown_speakers()

    assert len(result["clusters"]) == 1
    cluster = result["clusters"][0]
    assert cluster["size"] == 20
    assert cluster["segment_count"] >= 3
    assert len(cluster["samples"]) == 3


def test_discover_samples_use_registered_audio_extension(speakers_env):
    env = speakers_env()
    _setup_owner_centroid(env.journal, [0.0, 1.0])
    embeddings = _make_speaker_embeddings([1.0, 0.0], 5)
    _create_cluster_segments(env, embeddings, audio_extension=".m4a")

    result = discover_unknown_speakers()

    samples = result["clusters"][0]["samples"]
    assert len(samples) == 3
    for sample in samples:
        assert sample["audio_url"] == (
            f"/app/speakers/api/serve_audio/{sample['day']}/"
            f"{sample['stream']}/{sample['segment_key']}/{sample['source']}.m4a"
        )


def test_discover_samples_allow_missing_audio(speakers_env):
    env = speakers_env()
    _setup_owner_centroid(env.journal, [0.0, 1.0])
    embeddings = _make_speaker_embeddings([1.0, 0.0], 5)
    segments = _create_cluster_segments(env, embeddings)
    for day, segment_key, _sentence_count in segments:
        (env.journal / "chronicle" / day / "test" / segment_key / "audio.flac").unlink()

    result = discover_unknown_speakers()

    samples = result["clusters"][0]["samples"]
    assert len(samples) == 3
    assert all(sample["audio_url"] is None for sample in samples)


def test_discover_filters_attributed(speakers_env):
    env = speakers_env()
    _setup_owner_centroid(env.journal, [0.0, 1.0])
    env.create_entity("Alice Test")
    embeddings = _make_speaker_embeddings([1.0, 0.0], 5)
    segments = _create_cluster_segments(env, embeddings)

    for day, segment_key, sentence_count in segments[:3]:
        env.create_speaker_labels(
            day,
            segment_key,
            _all_sentence_labels("alice_test", sentence_count),
        )

    result = discover_unknown_speakers()

    assert result == {"clusters": []}


def test_discover_sample_shape_stays_scan_stable(speakers_env):
    env = speakers_env()
    _setup_owner_centroid(env.journal, [0.0, 1.0])
    embeddings = _make_speaker_embeddings([1.0, 0.0], 5)
    _create_cluster_segments(env, embeddings)

    result = discover_unknown_speakers()

    sample = result["clusters"][0]["samples"][0]
    assert set(sample) == {
        "day",
        "stream",
        "segment_key",
        "source",
        "sentence_id",
        "audio_url",
        "text",
    }


def test_cluster_presence_aggregates_persisted_evidence_and_ranks(speakers_env):
    env = speakers_env()
    env.create_entity(
        "Alice Co",
        voiceprints=[("20240101", "080000_300", "audio", 1)],
    )
    env.create_entity("Bob Co")
    env.create_entity("Carol Mention")
    env.create_entity("Dave Speaker")
    segments = [
        ("20240101", "091000_300", "Room A"),
        ("20240101", "091500_300", "Room A"),
        ("20240101", "092000_300", "Room B"),
    ]
    for day, segment_key, setting in segments:
        env.create_import_segment(
            day,
            segment_key,
            [("", "Unknown voice.")],
            stream="test",
            setting=setting,
        )
    env.create_speaker_labels(
        "20240101",
        "091000_300",
        [],
        metadata=_candidate_evidence(
            ("alice_co", ["screen"]),
            ("bob_co", ["meeting_day"]),
            ("carol_mention", ["setting"]),
            ("dave_speaker", ["speakers"]),
        ),
    )
    env.create_speaker_labels(
        "20240101",
        "091500_300",
        [],
        metadata=_candidate_evidence(
            ("alice_co", ["screen"]),
            ("bob_co", ["screen", "meeting_day"]),
            ("carol_mention", ["setting"]),
            ("dave_speaker", ["speakers"]),
        ),
    )
    env.create_speaker_labels(
        "20240101",
        "092000_300",
        [],
        metadata=_candidate_evidence(
            ("alice_co", ["meeting_day"]),
            ("bob_co", ["screen", "meeting_day"]),
            ("carol_mention", ["speakers"]),
        ),
    )
    _write_discovery_cache(
        env,
        7,
        [
            _cluster_record(day, segment_key, source="imported_audio")
            for day, segment_key, _setting in segments
        ],
    )

    presence = get_cluster_presence(7)

    assert presence is not None
    facts_without_samples = {
        key: value for key, value in presence["facts"].items() if key != "samples"
    }
    assert facts_without_samples == {
        "statement_count": 3,
        "segment_count": 3,
        "day_count": 1,
        "streams": ["test"],
        "conversation_count": 2,
    }
    assert [sample["setting"] for sample in presence["facts"]["samples"]] == [
        "Room A",
        "Room A",
        "Room B",
    ]
    assert presence["evidence_complete"] is True
    assert presence["evidence_gaps"] == []
    assert presence["candidates"]["co_presence"] == [
        {
            "entity_id": "bob_co",
            "name": "Bob Co",
            "has_voice": False,
            "screen_conversations": 2,
            "meeting_days": 1,
            "setting_conversations": 0,
            "speaker_conversations": 0,
        },
        {
            "entity_id": "alice_co",
            "name": "Alice Co",
            "has_voice": True,
            "screen_conversations": 1,
            "meeting_days": 1,
            "setting_conversations": 0,
            "speaker_conversations": 0,
        },
    ]
    assert presence["candidates"]["mention"] == [
        {
            "entity_id": "carol_mention",
            "name": "Carol Mention",
            "has_voice": False,
            "screen_conversations": 0,
            "meeting_days": 0,
            "setting_conversations": 1,
            "speaker_conversations": 1,
        },
        {
            "entity_id": "dave_speaker",
            "name": "Dave Speaker",
            "has_voice": False,
            "screen_conversations": 0,
            "meeting_days": 0,
            "setting_conversations": 0,
            "speaker_conversations": 1,
        },
    ]


def test_cluster_presence_conversation_grouping_setting_vs_no_setting(speakers_env):
    env = speakers_env()
    for segment_key in ("093000_300", "093500_300"):
        env.create_import_segment(
            "20240101",
            segment_key,
            [("", "Unknown voice.")],
            stream="test",
            setting="Shared Room",
        )
        env.create_speaker_labels(
            "20240101",
            segment_key,
            [],
            metadata=_candidate_evidence(),
        )
    _write_discovery_cache(
        env,
        8,
        [
            _cluster_record("20240101", "093000_300", source="imported_audio"),
            _cluster_record("20240101", "093500_300", source="imported_audio"),
        ],
    )

    for segment_key in ("094000_300", "094500_300"):
        env.create_segment("20240101", segment_key, ["audio"])
        env.create_speaker_labels(
            "20240101",
            segment_key,
            [],
            metadata=_candidate_evidence(),
        )
    _write_discovery_cache(
        env,
        9,
        [
            _cluster_record("20240101", "094000_300"),
            _cluster_record("20240101", "094500_300"),
        ],
    )

    shared_setting = get_cluster_presence(8)
    no_setting = get_cluster_presence(9)

    assert shared_setting is not None
    assert no_setting is not None
    assert shared_setting["facts"]["conversation_count"] == 1
    assert no_setting["facts"]["conversation_count"] == 2


def test_cluster_presence_readonly_fallback_uses_legacy_sources_without_writes(
    speakers_env,
):
    env = speakers_env()
    env.create_entity("Alice Test")
    env.create_entity("Bob Test")
    env.create_entity("Carol Test")
    env.create_import_segment(
        "20240101",
        "100000_300",
        [("", "Unknown voice.")],
        stream="test",
        setting="Meeting with Alice Test",
    )
    embedding_path = (
        env.journal
        / "chronicle"
        / "20240101"
        / "test"
        / "100000_300"
        / "imported_audio.npz"
    )
    embedding_path.unlink()
    env.create_screen_json("20240101", "100000_300", ["Bob Test"], stream="test")
    env.create_speakers_json("20240101", "100000_300", ["Carol Test"])
    env.create_speaker_labels(
        "20240101",
        "100000_300",
        [],
        metadata={"owner_centroid_last_refreshed_at": None, "voiceprint_versions": {}},
    )
    _write_discovery_cache(
        env,
        10,
        [_cluster_record("20240101", "100000_300", source="imported_audio")],
    )
    before = journal_tree_hash(env.journal)

    presence = get_cluster_presence(10)

    assert journal_tree_hash(env.journal) == before
    assert presence is not None
    assert presence["evidence_complete"] is True
    assert {cand["entity_id"] for cand in presence["candidates"]["co_presence"]} == {
        "bob_test"
    }
    assert {cand["entity_id"] for cand in presence["candidates"]["mention"]} == {
        "alice_test",
        "carol_test",
    }


def test_cluster_presence_stale_resolution_gap_keeps_siblings(speakers_env):
    from solstone.think.entities import (
        ResolutionOrigin,
        ResolutionScope,
        load_all_journal_entities,
        record_ambiguity_choice,
        record_entity_resolution,
    )

    env = speakers_env()
    env.create_entity("Alice Test")
    env.create_entity("Sarah Connor")
    env.create_entity("Sarah Lee")
    entities = list(load_all_journal_entities().values())
    scope = ResolutionScope.journal()
    origin = ResolutionOrigin(lane="test", field="candidate_name")
    record_entity_resolution("Sarah", entities, scope=scope, origin=origin)
    record_ambiguity_choice("Sarah", "sarah_connor", entities, scope=scope)
    sarah_path = env.journal / "entities" / "sarah_connor" / "entity.json"
    sarah = json.loads(sarah_path.read_text(encoding="utf-8"))
    sarah["blocked"] = True
    sarah_path.write_text(json.dumps(sarah), encoding="utf-8")

    env.create_import_segment(
        "20240101",
        "101000_300",
        [("", "Unknown voice.")],
        stream="test",
        setting="Meeting with Alice Test",
    )
    env.create_screen_json("20240101", "101000_300", ["Sarah"], stream="test")
    _write_discovery_cache(
        env,
        11,
        [_cluster_record("20240101", "101000_300", source="imported_audio")],
    )

    presence = get_cluster_presence(11)

    assert presence is not None
    assert presence["evidence_complete"] is False
    assert presence["evidence_gaps"] == [
        {
            "day": "20240101",
            "stream": "test",
            "segment_key": "101000_300",
            "source": "resolution",
            "reason": "stale_resolution",
        }
    ]
    assert presence["candidates"]["mention"] == [
        {
            "entity_id": "alice_test",
            "name": "Alice Test",
            "has_voice": False,
            "screen_conversations": 0,
            "meeting_days": 0,
            "setting_conversations": 1,
            "speaker_conversations": 0,
        }
    ]


def test_cluster_presence_excludes_principal_blocked_and_missing_entities(
    speakers_env,
):
    env = speakers_env()
    env.create_entity("Owner Test", is_principal=True)
    env.create_entity("Alice Test")
    blocked_dir = env.create_entity("Blocked Test")
    blocked_path = blocked_dir / "entity.json"
    blocked = json.loads(blocked_path.read_text(encoding="utf-8"))
    blocked["blocked"] = True
    blocked_path.write_text(json.dumps(blocked), encoding="utf-8")
    env.create_segment("20240101", "102000_300", ["audio"])
    env.create_speaker_labels(
        "20240101",
        "102000_300",
        [],
        metadata=_candidate_evidence(
            ("owner_test", ["screen"]),
            ("alice_test", ["screen"]),
            ("blocked_test", ["screen"]),
            ("missing_test", ["screen"]),
        ),
    )
    _write_discovery_cache(env, 12, [_cluster_record("20240101", "102000_300")])

    presence = get_cluster_presence(12)

    assert presence is not None
    assert presence["candidates"]["co_presence"] == [
        {
            "entity_id": "alice_test",
            "name": "Alice Test",
            "has_voice": False,
            "screen_conversations": 1,
            "meeting_days": 0,
            "setting_conversations": 0,
            "speaker_conversations": 0,
        }
    ]
    assert presence["candidates"]["mention"] == []


def test_cluster_presence_empty_evidence_and_unknown_cluster(speakers_env):
    env = speakers_env()
    env.create_segment("20240101", "103000_300", ["audio"])
    env.create_speaker_labels(
        "20240101",
        "103000_300",
        [],
        metadata=_candidate_evidence(),
    )
    _write_discovery_cache(env, 13, [_cluster_record("20240101", "103000_300")])

    presence = get_cluster_presence(13)

    assert presence is not None
    assert presence["facts"]["statement_count"] == 1
    assert presence["candidates"] == {"co_presence": [], "mention": []}
    assert get_cluster_presence(999) is None


def test_identify_creates_entity(speakers_env):
    env = speakers_env()
    _setup_owner_centroid(env.journal, [0.0, 1.0])
    embeddings = _make_speaker_embeddings([1.0, 0.0], 5)
    segments = _create_cluster_segments(env, embeddings)

    scan_result = discover_unknown_speakers()
    cluster_id = scan_result["clusters"][0]["cluster_id"]

    result = identify_cluster(cluster_id, "Bob Smith")

    entity_dir = env.journal / "entities" / "bob_smith"
    assert result["status"] == "identified"
    assert result["entity_id"] == "bob_smith"
    assert entity_dir.joinpath("entity.json").exists()
    assert entity_dir.joinpath("voiceprints.npz").exists()
    assert result["voiceprints_saved"] == 20
    assert result["segments_updated"] == 4
    assert result["sentences_attributed"] == 20

    for day, segment_key, _sentence_count in segments:
        labels_path = (
            env.journal / day / "test" / segment_key / "talents" / "speaker_labels.json"
        )
        corrections_path = (
            env.journal
            / day
            / "test"
            / segment_key
            / "talents"
            / "speaker_corrections.json"
        )
        labels_data = json.loads(labels_path.read_text(encoding="utf-8"))
        assert all(label["speaker"] == "bob_smith" for label in labels_data["labels"])
        assert corrections_path.exists()


def test_identify_matches_existing(speakers_env):
    env = speakers_env()
    _setup_owner_centroid(env.journal, [0.0, 1.0])
    env.create_entity("Bob Smith")
    embeddings = _make_speaker_embeddings([1.0, 0.0], 5)
    _create_cluster_segments(env, embeddings)

    scan_result = discover_unknown_speakers()
    cluster_id = scan_result["clusters"][0]["cluster_id"]
    result = identify_cluster(cluster_id, "Bob Smith")

    assert result["entity_id"] == "bob_smith"
    assert result["voiceprints_saved"] == 20
    assert (env.journal / "entities" / "bob_smith" / "voiceprints.npz").exists()


def test_identify_ambiguous_name_returns_before_writes(speakers_env):
    from solstone.think.entities import (
        ResolutionScope,
        load_all_journal_entities,
        load_ambiguities,
        record_ambiguity_choice,
    )

    env = speakers_env()
    _setup_owner_centroid(env.journal, [0.0, 1.0])
    env.create_entity("Sarah Connor")
    env.create_entity("Sarah Lee")
    embeddings = _make_speaker_embeddings([1.0, 0.0], 5)
    segments = _create_cluster_segments(env, embeddings)

    scan_result = discover_unknown_speakers()
    cluster_id = scan_result["clusters"][0]["cluster_id"]
    candidate_path = env.journal / "awareness" / "speaker_candidates.json"
    candidate_path.parent.mkdir(parents=True, exist_ok=True)
    candidate_path.write_text(
        json.dumps(
            {
                "next_id": 2,
                "candidates": [
                    {
                        "cand_id": 1,
                        "centroid": embeddings[0].astype(float).tolist(),
                        "n_segments": 2,
                        "n_intervals": 10,
                        "total_duration_s": 60.0,
                        "source_segments": [],
                        "confirmed_entity": None,
                        "status": "pending",
                    }
                ],
            },
            indent=2,
            sort_keys=True,
        )
        + "\n",
        encoding="utf-8",
    )
    candidate_before = candidate_path.read_bytes()

    result = identify_cluster(cluster_id, "Sarah")

    assert result["status"] == "ambiguous"
    assert result["ambiguity_id"]
    assert {candidate["id"] for candidate in result["candidates"]} == {
        "sarah_connor",
        "sarah_lee",
    }
    assert not (env.journal / "entities" / "sarah" / "entity.json").exists()
    assert not (env.journal / "entities" / "sarah_connor" / "voiceprints.npz").exists()
    for day, segment_key, _sentence_count in segments:
        labels_path = (
            env.journal / day / "test" / segment_key / "talents" / "speaker_labels.json"
        )
        corrections_path = (
            env.journal
            / day
            / "test"
            / segment_key
            / "talents"
            / "speaker_corrections.json"
        )
        assert not labels_path.exists()
        assert not corrections_path.exists()
    assert candidate_path.read_bytes() == candidate_before
    assert load_ambiguities()[0]["normalized_query"] == "sarah"

    record_ambiguity_choice(
        "Sarah",
        "sarah_connor",
        list(load_all_journal_entities().values()),
        scope=ResolutionScope.journal(),
    )

    resolved = identify_cluster(cluster_id, "Sarah")

    assert resolved["entity_id"] == "sarah_connor"
    assert resolved["voiceprints_saved"] == 20
    assert (env.journal / "entities" / "sarah_connor" / "voiceprints.npz").exists()
    row = load_ambiguities()[0]
    assert row["status"] == "resolved"


def test_identify_idempotent(speakers_env):
    env = speakers_env()
    _setup_owner_centroid(env.journal, [0.0, 1.0])
    embeddings = _make_speaker_embeddings([1.0, 0.0], 5)
    segments = _create_cluster_segments(env, embeddings)

    scan_result = discover_unknown_speakers()
    cluster_id = scan_result["clusters"][0]["cluster_id"]

    first = identify_cluster(cluster_id, "Bob Smith")
    first_voiceprints = _load_voiceprint_count(env.journal, "bob_smith")
    first_corrections = {
        (day, segment_key): _load_corrections_count(env.journal, day, segment_key)
        for day, segment_key, _sentence_count in segments
    }

    second = identify_cluster(cluster_id, "Bob Smith")

    assert first["voiceprints_saved"] == 20
    assert second["voiceprints_saved"] == 0
    assert _discovery_cache_path().exists()
    assert _discovery_resolved_path().exists()
    assert _load_voiceprint_count(env.journal, "bob_smith") == first_voiceprints
    for day, segment_key, _sentence_count in segments:
        assert (
            _load_corrections_count(env.journal, day, segment_key)
            == first_corrections[(day, segment_key)]
        )


def test_identify_contamination_guard(speakers_env):
    env = speakers_env()
    _setup_owner_centroid(env.journal, [0.0, 1.0])
    embeddings = _make_speaker_embeddings([1.0, 0.0], 5)
    _create_cluster_segments(env, embeddings)

    scan_result = discover_unknown_speakers()
    cluster_id = scan_result["clusters"][0]["cluster_id"]
    _setup_owner_centroid(env.journal, [1.0, 0.0])

    result = identify_cluster(cluster_id, "Bob Smith")

    assert result["voiceprints_saved"] == 0
    assert not (env.journal / "entities" / "bob_smith" / "voiceprints.npz").exists()
