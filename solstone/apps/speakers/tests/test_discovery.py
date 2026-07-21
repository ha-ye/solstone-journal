# SPDX-License-Identifier: AGPL-3.0-only
# Copyright (c) 2026 sol pbc

"""Tests for unknown speaker discovery."""

from __future__ import annotations

import json
from pathlib import Path

import numpy as np
import pytest

from solstone.apps.speakers.discovery import (
    _discovery_cache_path,
    _discovery_resolved_path,
    discover_unknown_speakers,
    get_cluster_conversation_count,
    get_cluster_presence,
    identify_cluster,
    load_discovery_cache,
    resolve_statement_cluster,
    undo_identify_operation,
)
from solstone.apps.speakers.owner import OWNER_THRESHOLD
from solstone.apps.speakers.tests.conftest import journal_tree_hash


def _domain_tree_hash(journal: Path) -> dict[str, str]:
    return {
        path: digest
        for path, digest in journal_tree_hash(journal).items()
        if not path.endswith(".lock")
    }


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
            {"entity_id": entity_id, "sources": sources} for entity_id, sources in items
        ],
    }


def _create_identify_cluster(
    env,
    cluster_id: int,
    segment_key: str,
    *,
    day: str = "20240101",
    sentence_count: int = 1,
) -> None:
    embeddings = _make_speaker_embeddings([1.0, 0.0], sentence_count)
    env.create_segment(day, segment_key, ["audio"], embeddings=embeddings)
    _write_discovery_cache(
        env,
        cluster_id,
        [
            _cluster_record(day, segment_key, sentence_id=sentence_id)
            for sentence_id in range(1, sentence_count + 1)
        ],
    )


def _update_entity(env, entity_id: str, **updates) -> None:
    entity_path = env.journal / "entities" / entity_id / "entity.json"
    entity = json.loads(entity_path.read_text(encoding="utf-8"))
    entity.update(updates)
    entity_path.write_text(json.dumps(entity), encoding="utf-8")


def _setup_mixed_person_entities(env) -> None:
    _setup_owner_centroid(env.journal, [0.0, 1.0], entity_id="owner_test")
    env.create_entity("Sarah Connor")
    env.create_entity("Sarah Lee")
    env.create_entity("Sarah Org")
    env.create_entity("Sarah Blocked")
    env.create_entity("Other Person")
    _update_entity(env, "sarah_org", type="Organization")
    _update_entity(env, "sarah_blocked", blocked=True)


def _setup_no_match_near_entities(env) -> None:
    _setup_owner_centroid(env.journal, [0.0, 1.0], entity_id="owner_test")
    _update_entity(env, "owner_test", name="Jnthn Smth Owner")
    env.create_entity("Jonathan Smith")
    env.create_entity("Jnthn Smth Org")
    env.create_entity("Jnthn Smth Blocked")
    _update_entity(env, "jnthn_smth_org", type="Organization")
    _update_entity(env, "jnthn_smth_blocked", blocked=True)


def _assert_no_identify_write_boundary(
    env,
    *,
    target_id: str,
    segment_key: str,
    before: dict[str, str],
    target_must_be_absent: bool = True,
) -> None:
    assert _domain_tree_hash(env.journal) == before
    target_dir = env.journal / "entities" / target_id
    if target_must_be_absent:
        assert not target_dir.exists()
    else:
        assert target_dir.exists()
        assert not (target_dir / "voiceprints.npz").exists()
    assert _speaker_labels_for_segment(env.journal, "20240101", segment_key) == []
    assert _load_corrections_count(env.journal, "20240101", segment_key) == 0
    assert not (env.journal / "speakers" / "identify-operations.jsonl").exists()
    assert not (env.journal / "speakers" / "keep-separate.jsonl").exists()
    assert not _discovery_resolved_path().exists()
    assert not (env.journal / "awareness" / "speaker_candidates.json").exists()


def _speaker_labels_for_segment(
    journal: Path, day: str, segment_key: str
) -> list[dict]:
    path = journal / day / "test" / segment_key / "talents" / "speaker_labels.json"
    if not path.exists():
        return []
    return json.loads(path.read_text(encoding="utf-8")).get("labels", [])


def _create_integer_labeled_segment(
    env,
    day: str,
    segment_key: str,
    embeddings: np.ndarray,
    *,
    cluster_label: int = 7,
) -> Path:
    seg_dir = env.create_segment(
        day,
        segment_key,
        ["audio"],
        embeddings=embeddings,
    )
    jsonl_path = seg_dir / "audio.jsonl"
    rows = jsonl_path.read_text(encoding="utf-8").splitlines()
    updated = [rows[0]]
    for row in rows[1:]:
        payload = json.loads(row)
        payload["speaker"] = cluster_label
        updated.append(json.dumps(payload))
    jsonl_path.write_text("\n".join(updated) + "\n", encoding="utf-8")
    return seg_dir


def test_load_discovery_cache_missing_is_read_only(tmp_path, monkeypatch):
    monkeypatch.setenv("SOLSTONE_JOURNAL", str(tmp_path))
    import solstone.think.utils as think_utils

    think_utils._journal_path_cache = None

    assert load_discovery_cache() is None
    assert not (tmp_path / "awareness").exists()


def test_load_discovery_cache_non_dict_top_level_is_unavailable(tmp_path, monkeypatch):
    monkeypatch.setenv("SOLSTONE_JOURNAL", str(tmp_path))
    import solstone.think.utils as think_utils

    think_utils._journal_path_cache = None
    awareness_dir = tmp_path / "awareness"
    awareness_dir.mkdir()
    (awareness_dir / "discovery_clusters.json").write_text("[]\n", encoding="utf-8")

    assert load_discovery_cache() is None


def test_resolve_statement_cluster_distinguishes_hit_miss_and_unavailable(
    speakers_env,
):
    env = speakers_env()

    assert resolve_statement_cluster(
        day="20240101",
        stream="test",
        segment_key="090000_300",
        source="audio",
        sentence_id=1,
    ) == {"status": "cache_unavailable", "cluster_id": None}

    _write_discovery_cache(
        env,
        8,
        [_cluster_record("20240101", "090000_300", source="audio", sentence_id=12)],
    )
    _write_discovery_cache(
        env,
        9,
        [_cluster_record("20240101", "090000_300", source="screen", sentence_id=12)],
    )
    _write_discovery_cache(
        env,
        3,
        [_cluster_record("20240101", "091000_300", source="audio", sentence_id=1)],
    )

    assert resolve_statement_cluster(
        day="20240101",
        stream="test",
        segment_key="090000_300",
        source="screen",
        sentence_id=12,
    ) == {"status": "hit", "cluster_id": 9}
    assert resolve_statement_cluster(
        day="20240101",
        stream="test",
        segment_key="090000_300",
        source="audio",
        sentence_id=12,
    ) == {"status": "hit", "cluster_id": 8}
    assert resolve_statement_cluster(
        day="20240101",
        stream="test",
        segment_key="090000_300",
        source="imported_audio",
        sentence_id=12,
    ) == {"status": "miss", "cluster_id": None}

    (_discovery_cache_path()).write_text("[]\n", encoding="utf-8")
    assert resolve_statement_cluster(
        day="20240101",
        stream="test",
        segment_key="090000_300",
        source="audio",
        sentence_id=12,
    ) == {"status": "cache_unavailable", "cluster_id": None}


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
    shared_setting_records = [
        _cluster_record("20240101", "093000_300", source="imported_audio"),
        _cluster_record("20240101", "093500_300", source="imported_audio"),
    ]
    _write_discovery_cache(
        env,
        8,
        shared_setting_records,
    )

    for segment_key in ("094000_300", "094500_300"):
        env.create_segment("20240101", segment_key, ["audio"])
        env.create_speaker_labels(
            "20240101",
            segment_key,
            [],
            metadata=_candidate_evidence(),
        )
    no_setting_records = [
        _cluster_record("20240101", "094000_300"),
        _cluster_record("20240101", "094500_300"),
    ]
    _write_discovery_cache(
        env,
        9,
        no_setting_records,
    )

    shared_setting = get_cluster_presence(8)
    no_setting = get_cluster_presence(9)

    assert shared_setting is not None
    assert no_setting is not None
    assert get_cluster_conversation_count(shared_setting_records) == 1
    assert get_cluster_conversation_count(no_setting_records) == 2
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
    before = _domain_tree_hash(env.journal)

    presence = get_cluster_presence(10)

    assert _domain_tree_hash(env.journal) == before
    assert presence is not None
    assert presence["evidence_complete"] is True
    assert {cand["entity_id"] for cand in presence["candidates"]["co_presence"]} == {
        "bob_test"
    }
    assert {cand["entity_id"] for cand in presence["candidates"]["mention"]} == {
        "alice_test",
        "carol_test",
    }


def test_cluster_presence_legacy_fallback_reports_speakers_gap_without_writes(
    speakers_env,
):
    env = speakers_env()
    env.create_entity("Alice Test")
    env.create_import_segment(
        "20240101",
        "100500_300",
        [("", "Unknown voice.")],
        stream="test",
        setting="Meeting with Alice Test",
    )
    env.create_speakers_json(
        "20240101",
        "100500_300",
        [],
        raw=json.dumps([5]),
    )
    env.create_speaker_labels(
        "20240101",
        "100500_300",
        [],
        metadata={"owner_centroid_last_refreshed_at": None, "voiceprint_versions": {}},
    )
    _write_discovery_cache(
        env,
        14,
        [_cluster_record("20240101", "100500_300", source="imported_audio")],
    )
    before = _domain_tree_hash(env.journal)

    presence = get_cluster_presence(14)

    assert _domain_tree_hash(env.journal) == before
    assert presence is not None
    assert presence["evidence_complete"] is False
    assert presence["evidence_gaps"] == [
        {
            "day": "20240101",
            "stream": "test",
            "segment_key": "100500_300",
            "source": "speakers",
            "reason": "wrong_shape",
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


def test_identify_resolve_only_matrix_is_byte_unchanged(speakers_env):
    env = speakers_env()
    env.create_entity(
        "Bob Smith",
        voiceprints=[("20240101", "080000_300", "audio", 1)],
    )
    env.create_entity("Sarah Connor")
    env.create_entity("Sarah Lee")
    _create_identify_cluster(env, 20, "110000_300")

    for call, assert_result in (
        (
            lambda: identify_cluster(20, entity_id="bob_smith", resolve_only=True),
            lambda result: (
                result["status"] == "resolved"
                and result["entity_id"] == "bob_smith"
                and result["has_voice"] is True
            ),
        ),
        (
            lambda: identify_cluster(20, entity_id="missing", resolve_only=True),
            lambda result: result["not_found"] is True,
        ),
        (
            lambda: identify_cluster(20, name="Bob Smith", resolve_only=True),
            lambda result: (
                result["status"] == "resolved" and result["entity_id"] == "bob_smith"
            ),
        ),
        (
            lambda: identify_cluster(20, name="Sarah", resolve_only=True),
            lambda result: (
                result["status"] == "ambiguous"
                and {candidate["id"] for candidate in result["candidates"]}
                == {"sarah_connor", "sarah_lee"}
            ),
        ),
        (
            lambda: identify_cluster(
                20,
                name="Zelda Unknown",
                resolve_only=True,
                create_new=True,
                entity_type="Invalid Type",
            ),
            lambda result: result["status"] == "no_match" and "candidates" in result,
        ),
    ):
        before = journal_tree_hash(env.journal)
        result = call()
        assert assert_result(result)
        assert journal_tree_hash(env.journal) == before

    assert not (env.journal / "entities" / "zelda_unknown").exists()


def test_identify_name_create_matrix_and_entity_type_validation(speakers_env):
    env = speakers_env()
    env.create_entity("Bob Smith")
    env.create_entity("Sarah Connor")
    env.create_entity("Sarah Lee")
    _create_identify_cluster(env, 21, "111000_300")
    _create_identify_cluster(env, 22, "111500_300")
    _create_identify_cluster(env, 23, "112000_300")
    _create_identify_cluster(env, 24, "112500_300")
    _create_identify_cluster(env, 25, "113000_300")
    _create_identify_cluster(env, 27, "113500_300")

    existing = identify_cluster(21, name="Bob Smith")
    existing_create = identify_cluster(27, name="Bob Smith", create_new=True)
    no_match = identify_cluster(22, name="Zelda Unknown")
    created = identify_cluster(
        23,
        name="Yara New",
        create_new=True,
        reviewed_near_match_entity_ids=[
            "bob_smith",
            "sarah_connor",
            "sarah_lee",
        ],
    )
    ambiguous_create_missing_review = identify_cluster(
        24, name="Sarah", create_new=True
    )
    invalid_type = identify_cluster(
        25,
        name="Qzxqv Wvuty",
        create_new=True,
        entity_type="Nope!",
    )

    assert existing["status"] == "identified"
    assert existing["entity_id"] == "bob_smith"
    assert existing["entity_created"] is False
    assert existing_create["status"] == "identified"
    assert existing_create["entity_id"] == "bob_smith"
    assert existing_create["entity_created"] is False
    assert no_match["status"] == "no_match"
    assert not (env.journal / "entities" / "zelda_unknown").exists()
    assert created["status"] == "identified"
    assert created["entity_id"] == "yara_new"
    assert created["entity_created"] is True
    assert (env.journal / "entities" / "yara_new" / "entity.json").exists()
    assert ambiguous_create_missing_review["status"] == "invalid_request"
    assert (
        ambiguous_create_missing_review["invalid_request_code"]
        == "reviewed_near_match_set_mismatch"
    )
    assert not (env.journal / "entities" / "sarah" / "entity.json").exists()
    assert invalid_type["invalid_entity_type"] is True
    assert not (env.journal / "entities" / "qzxqv_wvuty").exists()


def test_identify_entity_id_wins_over_name(speakers_env):
    env = speakers_env()
    env.create_entity("Alice Test")
    _create_identify_cluster(env, 26, "114000_300")

    result = identify_cluster(
        26,
        name="Something Else",
        entity_id="alice_test",
        create_new=True,
    )

    assert result["status"] == "identified"
    assert result["entity_id"] == "alice_test"
    assert result["entity_name"] == "Alice Test"
    assert not (env.journal / "entities" / "something_else").exists()


def test_identify_skips_stale_cache_member_without_creating_segment(speakers_env):
    env = speakers_env()
    env.create_entity("Bob Smith")
    real_segment = "114500_300"
    missing_segment = "115000_300"
    env.create_segment(
        "20240101",
        real_segment,
        ["audio"],
        embeddings=_make_speaker_embeddings([1.0, 0.0], 1),
    )
    missing_dir = env.journal / "chronicle" / "20240101" / "test" / missing_segment
    assert not missing_dir.exists()
    _write_discovery_cache(
        env,
        28,
        [
            _cluster_record("20240101", real_segment),
            _cluster_record("20240101", missing_segment),
        ],
    )

    result = identify_cluster(28, name="Bob Smith")

    assert result["status"] == "identified"
    assert result["entity_id"] == "bob_smith"
    assert result["voiceprints_saved"] == 1
    assert result["segments_updated"] == 1
    assert not missing_dir.exists()
    assert not (missing_dir / "talents" / "speaker_labels.json").exists()
    assert not (missing_dir / "talents" / "speaker_corrections.json").exists()


def test_identify_creates_entity(speakers_env):
    env = speakers_env()
    _setup_owner_centroid(env.journal, [0.0, 1.0])
    embeddings = _make_speaker_embeddings([1.0, 0.0], 5)
    segments = _create_cluster_segments(env, embeddings)

    scan_result = discover_unknown_speakers()
    cluster_id = scan_result["clusters"][0]["cluster_id"]

    result = identify_cluster(cluster_id, "Bob Smith", create_new=True)

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


def test_identify_resolve_only_uses_person_only_universe_and_special_collisions(
    speakers_env,
):
    env = speakers_env()
    _setup_mixed_person_entities(env)
    _create_identify_cluster(env, 60, "130000_300")

    before = _domain_tree_hash(env.journal)
    resolved = identify_cluster(
        60,
        name="Sarah Connor",
        create_new=True,
        resolve_only=True,
    )
    assert resolved["status"] == "resolved"
    assert resolved["entity_id"] == "sarah_connor"
    assert resolved["has_voice"] is False
    assert _domain_tree_hash(env.journal) == before

    ambiguous = identify_cluster(
        60,
        name="Sarah",
        create_new=True,
        resolve_only=True,
    )
    assert ambiguous["status"] == "ambiguous"
    assert [
        (candidate["id"], candidate["name"], candidate["tier"], candidate["has_voice"])
        for candidate in ambiguous["candidates"]
    ] == [
        ("sarah_lee", "Sarah Lee", 5, False),
        ("sarah_connor", "Sarah Connor", 5, False),
    ]
    assert _domain_tree_hash(env.journal) == before

    principal = identify_cluster(
        60,
        name="Owner Test",
        create_new=True,
        resolve_only=True,
    )
    assert principal == {"status": "principal_match", "this_is_me": True}
    assert _domain_tree_hash(env.journal) == before

    blocked = identify_cluster(
        60,
        name="Sarah Blocked",
        create_new=True,
        resolve_only=True,
    )
    assert blocked == {"status": "invalid_request", "error": "name is unavailable"}
    assert "entity_id" not in blocked
    assert "candidates" not in blocked
    _assert_no_identify_write_boundary(
        env,
        target_id="sarah_blocked",
        segment_key="130000_300",
        before=before,
        target_must_be_absent=False,
    )


def test_identify_no_match_returns_person_only_visible_near_candidates(speakers_env):
    env = speakers_env()
    _setup_no_match_near_entities(env)
    _create_identify_cluster(env, 64, "130500_300")
    before = _domain_tree_hash(env.journal)

    no_match = identify_cluster(
        64,
        name="Jnthn Smth",
        create_new=True,
        resolve_only=True,
    )

    assert no_match["status"] == "no_match"
    assert len(no_match["candidates"]) == 1
    candidate = no_match["candidates"][0]
    assert candidate["id"] == "jonathan_smith"
    assert candidate["name"] == "Jonathan Smith"
    assert candidate["tier"] == 8
    assert candidate["score"] < 90
    assert candidate["has_voice"] is False
    assert {
        "owner_test",
        "jnthn_smth_org",
        "jnthn_smth_blocked",
    }.isdisjoint({row["id"] for row in no_match["candidates"]})
    assert _domain_tree_hash(env.journal) == before


def test_identify_no_match_visible_candidates_stay_bounded_in_large_person_universe(
    speakers_env,
):
    from solstone.think.speaker_keep_separate import find_assertion

    env = speakers_env()
    for index in range(25):
        env.create_entity(f"Archive Person {index:02d}")
    _create_identify_cluster(env, 67, "130600_300")
    before = _domain_tree_hash(env.journal)

    preview = identify_cluster(
        67,
        name="Completely New Speaker",
        create_new=True,
        resolve_only=True,
    )

    assert preview["status"] == "no_match"
    visible_ids = [candidate["id"] for candidate in preview["candidates"]]
    assert len(visible_ids) == 3
    assert len(set(visible_ids)) == 3
    assert _domain_tree_hash(env.journal) == before

    result = identify_cluster(
        67,
        name="Completely New Speaker",
        create_new=True,
        request_id="no-match-bounded-create",
        reviewed_near_match_entity_ids=visible_ids,
    )

    assert result["status"] == "identified"
    assert result["entity_id"] == "completely_new_speaker"
    assert result["entity_created"] is True
    assert result["keep_separate_assertions_recorded"] == 3
    assert result["operation_id"].startswith("idop_")
    assert _load_voiceprint_count(env.journal, "completely_new_speaker") == 1
    assert _speaker_labels_for_segment(env.journal, "20240101", "130600_300") == [
        {
            "sentence_id": 1,
            "speaker": "completely_new_speaker",
            "confidence": "high",
            "method": "user_identified",
        }
    ]
    assert _load_corrections_count(env.journal, "20240101", "130600_300") == 1
    for entity_id in visible_ids:
        assert find_assertion("completely_new_speaker", entity_id) is not None
    assert _domain_tree_hash(env.journal) != before


def test_identify_ambiguous_name_returns_before_writes(speakers_env):
    from solstone.think.entities import (
        ResolutionOrigin,
        ResolutionScope,
        load_all_journal_entities,
        load_ambiguities,
        record_ambiguity_choice,
        record_entity_resolution,
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
    assert result["ambiguity_id"] == ""
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
    assert load_ambiguities() == []

    record_entity_resolution(
        "Sarah",
        list(load_all_journal_entities().values()),
        scope=ResolutionScope.journal(),
        origin=ResolutionOrigin(lane="test", field="name"),
    )
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


def test_identify_ineligible_resolved_ambiguity_choice_is_out_of_scope(
    speakers_env,
):
    from solstone.think.entities import (
        EntityResolutionError,
        ResolutionOrigin,
        ResolutionScope,
        load_all_journal_entities,
        record_ambiguity_choice,
        record_entity_resolution,
    )

    env = speakers_env()
    _setup_owner_centroid(env.journal, [0.0, 1.0])
    env.create_entity("Sarah Connor")
    org_path = env.create_entity("Sarah Org")
    org = json.loads((org_path / "entity.json").read_text(encoding="utf-8"))
    org["type"] = "Organization"
    (org_path / "entity.json").write_text(json.dumps(org), encoding="utf-8")
    _create_identify_cluster(env, 63, "130100_300")
    entities = list(load_all_journal_entities().values())
    scope = ResolutionScope.journal()
    record_entity_resolution(
        "Sarah",
        entities,
        scope=scope,
        origin=ResolutionOrigin(lane="test", field="name"),
    )
    record_ambiguity_choice("Sarah", "sarah_org", entities, scope=scope)
    before = _domain_tree_hash(env.journal)

    with pytest.raises(EntityResolutionError, match="outside scope"):
        identify_cluster(
            63,
            name="Sarah",
            create_new=True,
            resolve_only=True,
        )

    assert _domain_tree_hash(env.journal) == before


def test_identify_idempotent(speakers_env):
    env = speakers_env()
    _setup_owner_centroid(env.journal, [0.0, 1.0])
    embeddings = _make_speaker_embeddings([1.0, 0.0], 5)
    segments = _create_cluster_segments(env, embeddings)

    scan_result = discover_unknown_speakers()
    cluster_id = scan_result["clusters"][0]["cluster_id"]

    first = identify_cluster(cluster_id, "Bob Smith", create_new=True)
    first_voiceprints = _load_voiceprint_count(env.journal, "bob_smith")
    first_corrections = {
        (day, segment_key): _load_corrections_count(env.journal, day, segment_key)
        for day, segment_key, _sentence_count in segments
    }

    second = identify_cluster(cluster_id, "Bob Smith")

    assert first["voiceprints_saved"] == 20
    assert second["operation_id"] == first["operation_id"]
    assert second["voiceprints_saved"] == 20
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

    result = identify_cluster(cluster_id, "Bob Smith", create_new=True)

    assert result["voiceprints_saved"] == 0
    assert not (env.journal / "entities" / "bob_smith" / "voiceprints.npz").exists()


@pytest.mark.parametrize(
    "stage",
    [
        "after_prepared",
        "after_entity",
        "after_keep_separate",
        "after_direct_voiceprints",
        "after_corrections",
        "after_labels",
        "after_retro_tracker",
        "after_sentinel",
        "after_committed",
    ],
)
def test_identify_fault_resume_forward_stages(speakers_env, monkeypatch, stage):
    from solstone.apps.speakers import discovery

    env = speakers_env()
    env.create_entity("Bob Smith")
    _create_identify_cluster(env, 40, "120000_300")
    calls = {"failed": False}

    def fail_once(seam: str) -> None:
        if seam == stage and not calls["failed"]:
            calls["failed"] = True
            raise RuntimeError(f"forced {stage}")

    monkeypatch.setattr(discovery, "_maybe_inject_identify_fault", fail_once)
    first = identify_cluster(40, name="Bob Smith", request_id=f"req-{stage}")
    assert first["status"] == "recoverable"

    retry = identify_cluster(40, name="Bob Smith", request_id=f"req-{stage}")

    assert retry["status"] == "identified"
    assert _load_voiceprint_count(env.journal, "bob_smith") == 1
    assert _load_corrections_count(env.journal, "20240101", "120000_300") == 1
    assert _speaker_labels_for_segment(env.journal, "20240101", "120000_300") == [
        {
            "sentence_id": 1,
            "speaker": "bob_smith",
            "confidence": "high",
            "method": "user_identified",
        }
    ]
    resolved = json.loads(_discovery_resolved_path().read_text(encoding="utf-8"))
    assert resolved["40"]["entity_id"] == "bob_smith"


def test_identify_replay_fingerprint_conflict_and_member_target_dedup(speakers_env):
    env = speakers_env()
    env.create_entity("Bob Smith")
    env.create_entity("Alice Smith")
    _create_identify_cluster(env, 41, "121000_300")
    _write_discovery_cache(
        env,
        42,
        [_cluster_record("20240101", "121000_300")],
    )
    _create_identify_cluster(env, 43, "121500_300")

    first = identify_cluster(41, name="Bob Smith", request_id="stable-req")
    replay = identify_cluster(41, name="Bob Smith", request_id="stable-req")
    mismatch = identify_cluster(41, name="Alice Smith", request_id="stable-req")
    dedup = identify_cluster(42, name="Bob Smith", request_id="dedup-req")
    conflict = identify_cluster(42, name="Alice Smith", request_id="target-conflict")
    different_members = identify_cluster(43, name="Alice Smith", request_id="fresh")

    assert replay == first
    assert mismatch["status"] == "conflict"
    assert mismatch["conflict_code"] == "request_fingerprint_mismatch"
    assert dedup == first
    assert conflict["status"] == "conflict"
    assert conflict["conflict_code"] == "member_set_target_conflict"
    assert different_members["status"] == "identified"
    assert different_members["entity_id"] == "alice_smith"


def test_identify_undo_restores_checkpoint_actuals_and_deletes_created_entity(
    speakers_env,
):
    env = speakers_env()
    _create_identify_cluster(env, 44, "122000_300")

    result = identify_cluster(
        44,
        name="Yara Undo",
        create_new=True,
        request_id="undo-create",
    )
    operation_id = result["operation_id"]
    from solstone.think.indexer.edges import rebuild_edges

    rebuild_edges(str(env.journal))

    assert _load_voiceprint_count(env.journal, "yara_undo") == 1
    assert _speaker_labels_for_segment(env.journal, "20240101", "122000_300") == [
        {
            "sentence_id": 1,
            "speaker": "yara_undo",
            "confidence": "high",
            "method": "user_identified",
        }
    ]
    assert _load_corrections_count(env.journal, "20240101", "122000_300") == 1
    assert (
        json.loads(_discovery_resolved_path().read_text(encoding="utf-8"))["44"][
            "entity_id"
        ]
        == "yara_undo"
    )

    undo = undo_identify_operation(operation_id)

    assert undo["status"] == "undone"
    assert not (env.journal / "entities" / "yara_undo").exists()
    assert _speaker_labels_for_segment(env.journal, "20240101", "122000_300") == []
    corrections_path = (
        env.journal
        / "20240101"
        / "test"
        / "122000_300"
        / "talents"
        / "speaker_corrections.json"
    )
    corrections = json.loads(corrections_path.read_text(encoding="utf-8"))[
        "corrections"
    ]
    assert len(corrections) == 2
    assert corrections[-1]["correction_kind"] == "identify_undo"
    assert corrections[-1]["corrected_speaker"] is None
    assert "44" not in json.loads(
        _discovery_resolved_path().read_text(encoding="utf-8")
    )

    before = _domain_tree_hash(env.journal)
    second = undo_identify_operation(operation_id)
    assert second["status"] == "already_undone"
    assert _domain_tree_hash(env.journal) == before


def test_identify_retro_manifest_removed_on_undo(speakers_env):
    from solstone.apps.speakers.candidate_tracker import CandidateTracker
    from solstone.think.speaker_identify_operations import fold_operation

    env = speakers_env()
    _setup_owner_centroid(env.journal, [0.0, 1.0])
    env.create_entity("Bob Smith")
    base = _make_speaker_embeddings([1.0, 0.0], 3)
    retro_seg = _create_integer_labeled_segment(
        env,
        "20240102",
        "123000_300",
        base,
    )
    CandidateTracker().process_segment(
        "20240102",
        "123000_300",
        "test",
        "audio",
        retro_seg,
    )
    env.create_segment(
        "20240103",
        "123500_300",
        ["audio"],
        embeddings=base[:1],
    )
    _write_discovery_cache(
        env,
        45,
        [_cluster_record("20240103", "123500_300")],
    )

    result = identify_cluster(45, name="Bob Smith", request_id="retro-undo")
    state = fold_operation(result["operation_id"])

    assert result["voiceprints_saved"] == 1
    assert result["retro_voiceprints_saved"] == 3
    assert _load_voiceprint_count(env.journal, "bob_smith") == 4
    assert len(state.phase_checkpoints["direct_voiceprints"]["saved_keys"]) == 1
    assert len(state.phase_checkpoints["retro_tracker"]["saved_keys"]) == 3

    undo = undo_identify_operation(result["operation_id"])

    assert undo["status"] == "undone"
    assert _load_voiceprint_count(env.journal, "bob_smith") == 0
    candidate = CandidateTracker().load_all_candidates()[0]
    assert candidate.status == "pending"
    assert candidate.confirmed_entity is None


def test_identify_ambiguous_create_exact_reviewed_set_records_keep_separate_and_replays(
    speakers_env,
):
    from solstone.think.speaker_keep_separate import find_assertion

    env = speakers_env()
    _setup_mixed_person_entities(env)
    _create_identify_cluster(env, 46, "124000_300")
    before = _domain_tree_hash(env.journal)

    result = identify_cluster(
        46,
        name="Sarah",
        create_new=True,
        request_id="near-match-ok",
        reviewed_near_match_entity_ids=["sarah_connor", "sarah_lee"],
    )

    assert result["status"] == "identified"
    assert result["entity_id"] == "sarah"
    assert result["entity_created"] is True
    assert result["keep_separate_assertions_recorded"] == 2
    assert result["operation_id"].startswith("idop_")
    assert _load_voiceprint_count(env.journal, "sarah") == 1
    assert _speaker_labels_for_segment(env.journal, "20240101", "124000_300") == [
        {
            "sentence_id": 1,
            "speaker": "sarah",
            "confidence": "high",
            "method": "user_identified",
        }
    ]
    assert _load_corrections_count(env.journal, "20240101", "124000_300") == 1
    assert _discovery_resolved_path().exists()
    resolved = json.loads(_discovery_resolved_path().read_text(encoding="utf-8"))
    assert resolved["46"]["entity_id"] == "sarah"
    assert (env.journal / "speakers" / "identify-operations.jsonl").exists()
    assert not (env.journal / "awareness" / "speaker_candidates.json").exists()
    assert find_assertion("sarah", "sarah_connor") is not None
    assert find_assertion("sarah", "sarah_lee") is not None
    assert _domain_tree_hash(env.journal) != before

    replay = identify_cluster(
        46,
        name="Sarah",
        create_new=True,
        request_id="near-match-ok",
        reviewed_near_match_entity_ids=["sarah_lee", "sarah_connor"],
    )

    assert replay == result
    assert _load_voiceprint_count(env.journal, "sarah") == 1
    keep_path = env.journal / "speakers" / "keep-separate.jsonl"
    assert len(keep_path.read_text(encoding="utf-8").splitlines()) == 2


def test_identify_no_match_create_accepts_explicit_empty_reviewed_set(speakers_env):
    env = speakers_env()
    _create_identify_cluster(env, 62, "124100_300")
    before = _domain_tree_hash(env.journal)

    result = identify_cluster(
        62,
        name="Voice QA Person",
        create_new=True,
        request_id="no-match-empty-reviewed",
        reviewed_near_match_entity_ids=[],
    )

    assert result["status"] == "identified"
    assert result["entity_id"] == "voice_qa_person"
    assert result["entity_created"] is True
    assert result["keep_separate_assertions_recorded"] == 0
    assert (env.journal / "speakers" / "identify-operations.jsonl").exists()
    assert _load_voiceprint_count(env.journal, "voice_qa_person") == 1
    assert _speaker_labels_for_segment(env.journal, "20240101", "124100_300") == [
        {
            "sentence_id": 1,
            "speaker": "voice_qa_person",
            "confidence": "high",
            "method": "user_identified",
        }
    ]
    assert _load_corrections_count(env.journal, "20240101", "124100_300") == 1
    resolved = json.loads(_discovery_resolved_path().read_text(encoding="utf-8"))
    assert resolved["62"]["entity_id"] == "voice_qa_person"
    assert not (env.journal / "awareness" / "speaker_candidates.json").exists()
    assert not (env.journal / "speakers" / "keep-separate.jsonl").exists()
    assert _domain_tree_hash(env.journal) != before


def test_identify_no_match_create_rejects_empty_reviewed_set_when_candidate_visible(
    speakers_env,
):
    env = speakers_env()
    _setup_no_match_near_entities(env)
    _create_identify_cluster(env, 65, "124200_300")
    before = _domain_tree_hash(env.journal)

    result = identify_cluster(
        65,
        name="Jnthn Smth",
        create_new=True,
        request_id="no-match-empty-reviewed-visible",
        reviewed_near_match_entity_ids=[],
    )

    assert result["status"] == "invalid_request"
    assert result["invalid_request_code"] == "reviewed_near_match_set_mismatch"
    assert result["expected_reviewed_near_match_entity_ids"] == ["jonathan_smith"]
    assert result["actual_reviewed_near_match_entity_ids"] == []
    _assert_no_identify_write_boundary(
        env,
        target_id="jnthn_smth",
        segment_key="124200_300",
        before=before,
    )


@pytest.mark.parametrize(
    ("reviewed_ids", "reason"),
    [
        (["missing_person"], "nonexistent"),
        (["bad_type"], "non_person"),
        (["blocked_person"], "blocked"),
        (["owner_test"], "principal"),
        (["qzxqv_wvuty"], "self"),
    ],
)
def test_identify_near_match_validation_rejects_before_writes(
    speakers_env,
    reviewed_ids,
    reason,
):
    env = speakers_env()
    _setup_owner_centroid(env.journal, [0.0, 1.0], entity_id="owner_test")
    env.create_entity("Alice Smith")
    env.create_entity("Alice Jones")
    env.create_entity("Alice Johnson")
    bad_type_path = env.create_entity("Bad Type")
    bad_type = json.loads((bad_type_path / "entity.json").read_text(encoding="utf-8"))
    bad_type["type"] = "Organization"
    (bad_type_path / "entity.json").write_text(json.dumps(bad_type), encoding="utf-8")
    env.create_entity("Blocked Person")
    _update_entity(env, "blocked_person", blocked=True)
    env.create_entity("Bob Far")
    _create_identify_cluster(env, 47, "124500_300")
    before = _domain_tree_hash(env.journal)

    result = identify_cluster(
        47,
        name="Qzxqv Wvuty",
        create_new=True,
        request_id=f"near-match-{reason}",
        reviewed_near_match_entity_ids=reviewed_ids,
    )

    assert result["status"] == "invalid_request"
    assert result["invalid_reviewed_near_match_entity_ids"][0]["reason"] == reason
    _assert_no_identify_write_boundary(
        env,
        target_id="qzxqv_wvuty",
        segment_key="124500_300",
        before=before,
    )


def test_identify_near_match_validation_rejects_unshown_before_writes(speakers_env):
    env = speakers_env()
    _setup_mixed_person_entities(env)
    _create_identify_cluster(env, 48, "125000_300")
    before = _domain_tree_hash(env.journal)

    result = identify_cluster(
        48,
        name="Sarah",
        create_new=True,
        request_id="near-match-unshown",
        reviewed_near_match_entity_ids=["other_person"],
    )

    assert result["status"] == "invalid_request"
    assert result["invalid_reviewed_near_match_entity_ids"][0]["reason"] == "unshown"
    _assert_no_identify_write_boundary(
        env,
        target_id="sarah",
        segment_key="125000_300",
        before=before,
    )

    env = speakers_env()
    _setup_no_match_near_entities(env)
    duplicate_dir = env.journal / "entities" / "jonathan_clone"
    duplicate_dir.mkdir(parents=True, exist_ok=True)
    (duplicate_dir / "entity.json").write_text(
        json.dumps(
            {
                "id": "jonathan_clone",
                "name": "Jonathan Smith",
                "type": "Person",
                "created_at": 1700000000000,
            }
        ),
        encoding="utf-8",
    )
    _create_identify_cluster(env, 66, "125050_300")
    before = _domain_tree_hash(env.journal)

    result = identify_cluster(
        66,
        name="Jnthn Smth",
        create_new=True,
        request_id="near-match-unshown-no-match",
        reviewed_near_match_entity_ids=["jonathan_clone"],
    )

    assert result["status"] == "invalid_request"
    assert result["invalid_reviewed_near_match_entity_ids"] == [
        {"entity_id": "jonathan_clone", "reason": "unshown"}
    ]
    _assert_no_identify_write_boundary(
        env,
        target_id="jnthn_smth",
        segment_key="125050_300",
        before=before,
    )


@pytest.mark.parametrize(
    ("reviewed_ids", "expected_code", "expected_duplicate", "expected_invalid"),
    [
        (None, "reviewed_near_match_set_mismatch", None, None),
        ([], "reviewed_near_match_set_mismatch", None, None),
        (
            ["sarah_connor"],
            "reviewed_near_match_set_mismatch",
            None,
            None,
        ),
        (
            ["sarah_connor", "sarah_connor"],
            None,
            "sarah_connor",
            None,
        ),
        (
            ["other_person", "sarah_connor"],
            None,
            None,
            [{"entity_id": "other_person", "reason": "unshown"}],
        ),
    ],
)
def test_identify_ambiguous_create_requires_exact_reviewed_set_before_writes(
    speakers_env,
    reviewed_ids,
    expected_code,
    expected_duplicate,
    expected_invalid,
):
    env = speakers_env()
    _setup_mixed_person_entities(env)
    _create_identify_cluster(env, 61, "125100_300")
    before = _domain_tree_hash(env.journal)
    kwargs = {}
    if reviewed_ids is not None:
        kwargs["reviewed_near_match_entity_ids"] = reviewed_ids

    result = identify_cluster(
        61,
        name="Sarah",
        create_new=True,
        request_id=f"reviewed-set-{str(reviewed_ids)}",
        **kwargs,
    )

    assert result["status"] == "invalid_request"
    if expected_code is not None:
        assert result["invalid_request_code"] == expected_code
        assert result["expected_reviewed_near_match_entity_ids"] == [
            "sarah_connor",
            "sarah_lee",
        ]
    elif expected_duplicate is not None:
        assert result["invalid_reviewed_near_match_entity_ids"] == [
            {"entity_id": expected_duplicate, "reason": "duplicate"}
        ]
    else:
        assert result["invalid_reviewed_near_match_entity_ids"] == expected_invalid
    _assert_no_identify_write_boundary(
        env,
        target_id="sarah",
        segment_key="125100_300",
        before=before,
    )


def test_identify_replay_uses_stored_state_when_planning_now_fails(
    speakers_env,
):
    env = speakers_env()
    env.create_entity("Bob Smith")
    _create_identify_cluster(env, 49, "125500_300")

    first = identify_cluster(49, name="Bob Smith", request_id="cacheless-replay")
    _discovery_cache_path().unlink()
    before = _domain_tree_hash(env.journal)

    replay = identify_cluster(49, name="Bob Smith", request_id="cacheless-replay")
    conflict = identify_cluster(49, name="Alice Smith", request_id="cacheless-replay")

    assert replay == first
    assert conflict["status"] == "conflict"
    assert conflict["conflict_code"] == "request_fingerprint_mismatch"
    assert _domain_tree_hash(env.journal) == before


def test_identify_does_not_return_success_for_operation_mid_undo(
    speakers_env,
    monkeypatch,
):
    from solstone.apps.speakers import discovery
    from solstone.think.speaker_identify_operations import fold_operation

    env = speakers_env()
    env.create_entity("Bob Smith")
    _create_identify_cluster(env, 50, "130000_300")
    result = identify_cluster(50, name="Bob Smith", request_id="mid-undo")
    operation_id = result["operation_id"]

    def fail_after_undo_labels(stage: str) -> None:
        if stage == "after_undo_labels":
            raise RuntimeError("forced undo interruption")

    monkeypatch.setattr(
        discovery,
        "_maybe_inject_identify_fault",
        fail_after_undo_labels,
    )
    undo = undo_identify_operation(operation_id)

    assert undo["status"] == "recoverable"
    assert undo["operation_state"] == "undoing"
    state = fold_operation(operation_id)
    assert state.terminal_status == "undoing"

    monkeypatch.setattr(discovery, "_maybe_inject_identify_fault", lambda stage: None)
    replay = identify_cluster(50, name="Bob Smith", request_id="mid-undo")

    assert replay["status"] == "undoing"
    assert replay["operation_state"] == "undoing"
    assert replay["operation_id"] == operation_id


def test_retro_voiceprint_failure_does_not_checkpoint_or_confirm_tracker(
    speakers_env,
    monkeypatch,
):
    from solstone.apps.speakers.candidate_tracker import CandidateTracker
    from solstone.think import entities as entity_api
    from solstone.think.speaker_identify_operations import fold_operation

    env = speakers_env()
    _setup_owner_centroid(env.journal, [0.0, 1.0])
    env.create_entity("Bob Smith")
    base = _make_speaker_embeddings([1.0, 0.0], 3)
    retro_seg = _create_integer_labeled_segment(
        env,
        "20240102",
        "130500_300",
        base,
    )
    CandidateTracker().process_segment(
        "20240102",
        "130500_300",
        "test",
        "audio",
        retro_seg,
    )
    env.create_segment(
        "20240103",
        "131000_300",
        ["audio"],
        embeddings=base[:1],
    )
    _write_discovery_cache(
        env,
        51,
        [_cluster_record("20240103", "131000_300")],
    )
    real_save = entity_api.save_voiceprints_batch
    calls = {"count": 0}

    def fail_retro_save(entity_id, items):
        calls["count"] += 1
        if calls["count"] == 2:
            raise RuntimeError("forced retro save failure")
        return real_save(entity_id, items)

    monkeypatch.setattr(entity_api, "save_voiceprints_batch", fail_retro_save)
    first = identify_cluster(51, name="Bob Smith", request_id="retro-save-fails")

    assert first["status"] == "recoverable"
    state = fold_operation(first["operation_id"])
    assert "retro_tracker" not in state.phase_checkpoints
    assert _load_voiceprint_count(env.journal, "bob_smith") == 1
    candidate = CandidateTracker().load_all_candidates()[0]
    assert candidate.status == "pending"
    assert candidate.confirmed_entity is None

    monkeypatch.setattr(entity_api, "save_voiceprints_batch", real_save)
    retry = identify_cluster(51, name="Bob Smith", request_id="retro-save-fails")

    assert retry["status"] == "identified"
    assert retry["retro_voiceprints_saved"] == 3
    assert _load_voiceprint_count(env.journal, "bob_smith") == 4


def test_undo_voiceprint_ambiguous_removal_emits_durable_repair(
    speakers_env,
):
    from solstone.think.speaker_identify_operations import fold_operation

    env = speakers_env()
    env.create_entity("Bob Smith")
    _create_identify_cluster(env, 52, "131500_300")
    result = identify_cluster(52, name="Bob Smith", request_id="undo-ambiguous-vp")
    operation_id = result["operation_id"]
    path = env.journal / "entities" / "bob_smith" / "voiceprints.npz"
    with np.load(path, allow_pickle=False) as data:
        embeddings = data["embeddings"]
        metadata = data["metadata"]
    np.savez_compressed(
        path,
        embeddings=np.vstack([embeddings, embeddings[0].reshape(1, -1)]),
        metadata=np.asarray([*metadata, metadata[0]], dtype=str),
    )

    undo = undo_identify_operation(operation_id)
    state = fold_operation(operation_id)

    assert undo["status"] == "undo_repair_required"
    assert undo["operation_state"] == "undo_repair_required"
    assert undo["phase"] == "voiceprints"
    assert undo["repair_code"] == "voiceprint_removal_ambiguous"
    assert state.terminal_status == "undo_repair_required"
    assert state.undo_repair_required["repair_categories"] == {"voiceprints": 1}


def test_created_entity_delete_blocks_when_edge_index_missing(speakers_env):
    env = speakers_env()
    _create_identify_cluster(env, 53, "132000_300")
    result = identify_cluster(
        53,
        name="Edge Blocked",
        create_new=True,
        request_id="edge-index-missing",
    )

    undo = undo_identify_operation(result["operation_id"])

    assert undo["status"] == "undone"
    assert (env.journal / "entities" / "edge_blocked").exists()
    entity_report = undo["undo_report"]["entity"]
    assert entity_report["deleted"] is False
    assert "unreadable" in entity_report["blocked_categories"]


def test_identify_near_match_validation_rejects_duplicate_ids_before_writes(
    speakers_env,
):
    env = speakers_env()
    env.create_entity("Alice Smith")
    _create_identify_cluster(env, 54, "132500_300")
    before = _domain_tree_hash(env.journal)

    result = identify_cluster(
        54,
        name="Qzxqv Wvuty",
        create_new=True,
        request_id="near-match-duplicate",
        reviewed_near_match_entity_ids=["alice_smith", "alice_smith"],
    )

    assert result["status"] == "invalid_request"
    assert result["invalid_reviewed_near_match_entity_ids"] == [
        {"entity_id": "alice_smith", "reason": "duplicate"}
    ]
    assert _domain_tree_hash(env.journal) == before
