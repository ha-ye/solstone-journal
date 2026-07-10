# SPDX-License-Identifier: AGPL-3.0-only
# Copyright (c) 2026 sol pbc

"""Tests for ``sol call entities merge``."""

from __future__ import annotations

import json
import os
import sqlite3
from pathlib import Path

import numpy as np
import pytest
from typer.testing import CliRunner

from solstone.apps.entities.call import app as entities_app
from solstone.think.convey_client import ConveyClient
from solstone.think.entities import merge as merge_mod
from solstone.think.entities.journal import load_journal_entity
from solstone.think.indexer.edges import insert_edges, rebuild_edges
from solstone.think.indexer.journal import get_journal_index
from tests._baseline_harness import make_test_client
from tests._sqlite_assertions import edges_content_hash

runner = CliRunner()
STREAM = "test"


@pytest.fixture(autouse=True)
def _entities_client(monkeypatch: pytest.MonkeyPatch) -> None:
    def client() -> ConveyClient:
        journal = Path(os.environ["SOLSTONE_JOURNAL"])
        return ConveyClient(
            session=make_test_client(journal),
            base_url="",
        )

    monkeypatch.setattr("solstone.apps.entities.call.get_client", client)


def _read_json(path):
    return json.loads(path.read_text(encoding="utf-8"))


def _write_json(path, payload) -> None:
    path.write_text(json.dumps(payload, indent=2) + "\n", encoding="utf-8")


def _entity_path(env, entity_id: str):
    return env.journal / "entities" / entity_id / "entity.json"


def _update_entity(env, entity_id: str, **fields) -> None:
    path = _entity_path(env, entity_id)
    payload = _read_json(path)
    payload.update(fields)
    _write_json(path, payload)


def _labels_path(env, day: str, segment_key: str):
    return env.journal / day / STREAM / segment_key / "talents" / "speaker_labels.json"


def _corrections_path(env, day: str, segment_key: str):
    return (
        env.journal
        / day
        / STREAM
        / segment_key
        / "talents"
        / "speaker_corrections.json"
    )


def _activity_path(env, facet: str, day: str) -> Path:
    return env.journal / "facets" / facet / "activities" / f"{day}.jsonl"


def _write_activity_records(
    env,
    facet: str,
    day: str,
    records: list[dict],
) -> Path:
    path = _activity_path(env, facet, day)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        "".join(json.dumps(record) + "\n" for record in records),
        encoding="utf-8",
    )
    return path


def _audit_log_path(env):
    return env.journal / "logs" / "entity-merges.jsonl"


def _voiceprint_count(env, entity_id: str) -> int:
    path = env.journal / "entities" / entity_id / "voiceprints.npz"
    with np.load(path, allow_pickle=False) as data:
        return len(data["embeddings"])


def _edge_row(
    src: str,
    dst: str,
    kind: str,
    path: str,
    *,
    weight: int = 1,
) -> dict:
    return {
        "src": src,
        "dst": dst,
        "kind": kind,
        "source": "merge-test",
        "path": path,
        "weight": weight,
    }


def _insert_edge_rows(env, rows: list[dict]) -> None:
    conn, _ = get_journal_index(str(env.journal))
    insert_edges(conn, rows)
    conn.commit()
    conn.close()


def _edge_hash(env) -> str:
    conn, _ = get_journal_index(str(env.journal))
    try:
        return edges_content_hash(conn)
    finally:
        conn.close()


def _edge_rows(env) -> list[dict]:
    conn, _ = get_journal_index(str(env.journal))
    conn.row_factory = sqlite3.Row
    try:
        return [
            dict(row)
            for row in conn.execute(
                "SELECT src, dst, path FROM edges ORDER BY path"
            ).fetchall()
        ]
    finally:
        conn.close()


def _edge_rows_with_kind(env) -> list[dict]:
    conn, _ = get_journal_index(str(env.journal))
    conn.row_factory = sqlite3.Row
    try:
        return [
            dict(row)
            for row in conn.execute(
                "SELECT src, dst, kind, source, path "
                "FROM edges ORDER BY kind, path, src, dst"
            ).fetchall()
        ]
    finally:
        conn.close()


def test_merge_dry_run_plans_without_writing(speakers_env):
    env = speakers_env()
    env.create_segment("20240101", "143022_300", ["mic_audio"])
    env.create_entity(
        "Dry Alias",
        voiceprints=[
            ("20240101", "143022_300", "mic_audio", 1),
            ("20240101", "143022_300", "mic_audio", 2),
        ],
    )
    env.create_entity(
        "Dry Canon",
        voiceprints=[("20240101", "143022_300", "mic_audio", 3)],
    )
    env.create_facet_relationship(
        "work",
        "dry_alias",
        observations=["Likes coffee"],
    )
    env.create_facet_relationship(
        "work",
        "dry_canon",
        observations=["Senior role"],
    )
    env.create_facet_relationship("personal", "dry_alias", description="Runner")
    env.create_speaker_labels(
        "20240101",
        "143022_300",
        [
            {
                "sentence_id": 1,
                "speaker": "dry_alias",
                "confidence": "high",
                "method": "acoustic",
            }
        ],
    )
    env.create_speaker_corrections(
        "20240101",
        "143022_300",
        [
            {
                "sentence_id": 1,
                "original_speaker": "dry_alias",
                "corrected_speaker": "dry_alias",
                "timestamp": 1700000000000,
            }
        ],
    )
    activity_path = _write_activity_records(
        env,
        "work",
        "20240101",
        [
            {
                "id": "meeting_143022_300",
                "activity": "meeting",
                "title": "Dry-run merge meeting",
                "created_at": 1700000000000,
                "participation": [
                    {"role": "attendee", "entity_id": "dry_alias"},
                    {"role": "attendee", "entity_id": "dry_peer"},
                ],
                "commitments": [
                    {
                        "owner_entity_id": "dry_alias",
                        "counterparty_entity_id": "dry_peer",
                        "action": "follow up",
                    }
                ],
            }
        ],
    )
    cache_path = env.journal / "awareness" / "discovery_clusters.json"
    cache_path.parent.mkdir(parents=True, exist_ok=True)
    cache_path.write_text('{"clusters": []}', encoding="utf-8")

    source_before = _entity_path(env, "dry_alias").read_text(encoding="utf-8")
    target_before = _entity_path(env, "dry_canon").read_text(encoding="utf-8")
    labels_before = _labels_path(env, "20240101", "143022_300").read_text(
        encoding="utf-8"
    )
    corrections_before = _corrections_path(env, "20240101", "143022_300").read_text(
        encoding="utf-8"
    )
    activity_before = activity_path.read_text(encoding="utf-8")

    result = runner.invoke(entities_app, ["merge", "dry_alias", "dry_canon"])

    assert result.exit_code == 0, f"{result.output}\n{result.exception!r}"
    data = json.loads(result.output)
    assert data["merged"] is False
    assert data["identity"]["akas_added"] == []
    assert data["voiceprints"]["added"] == 0
    assert data["facets"]["moved"] == []
    assert data["segments"]["files_scanned"] == 0
    assert "Dry Alias" in data["would_identity"]["akas_added"]
    assert data["would_voiceprints"]["added"] == 2
    assert data["would_facets"]["merged"] == ["work"]
    assert data["would_facets"]["moved"] == ["personal"]
    assert data["would_segments"]["labels_rewritten"] == 1
    assert data["would_segments"]["corrections_rewritten"] == 1
    assert data["activities"] == {
        "records_rewritten": 0,
        "fields_rewritten": 0,
        "files_scanned": 0,
        "files_rewritten": 0,
        "errors": [],
    }
    assert data["would_activities"] == {
        "records_rewritten": 1,
        "fields_rewritten": 2,
        "files_scanned": 1,
        "files_rewritten": 1,
        "errors": [],
    }
    assert data["would_fold_edges"] is None
    assert data["audit_log_path"] is None
    assert data["caches_cleared"] == []

    assert _entity_path(env, "dry_alias").read_text(encoding="utf-8") == source_before
    assert _entity_path(env, "dry_canon").read_text(encoding="utf-8") == target_before
    assert (
        _labels_path(env, "20240101", "143022_300").read_text(encoding="utf-8")
        == labels_before
    )
    assert (
        _corrections_path(env, "20240101", "143022_300").read_text(encoding="utf-8")
        == corrections_before
    )
    assert activity_path.read_text(encoding="utf-8") == activity_before
    assert cache_path.exists()
    assert load_journal_entity("dry_alias") is not None
    assert not _audit_log_path(env).exists()


def test_merge_dry_run_reports_would_fold_edges(speakers_env):
    env = speakers_env()
    env.create_entity("Edge Count Source")
    env.create_entity("Edge Count Target")
    _insert_edge_rows(
        env,
        [
            _edge_row("edge_count_source", "edge_count_peer", "co-present", "count/1"),
            _edge_row(
                "edge_other_peer", "edge_count_source", "committed-to", "count/2"
            ),
        ],
    )
    before_hash = _edge_hash(env)

    result = merge_mod.merge_entity(
        "edge_count_source",
        "edge_count_target",
        commit=False,
    )

    assert result["merged"] is False
    assert result["would_fold_edges"] == 2
    assert result["edges"] == {
        "rows_folded": 0,
        "self_edges_dropped": 0,
        "error": None,
    }
    assert _edge_hash(env) == before_hash
    assert load_journal_entity("edge_count_source") is not None


def test_merge_commit_deep_merges_and_logs(speakers_env):
    env = speakers_env()
    env.create_segment("20240101", "143022_300", ["mic_audio"])
    env.create_entity(
        "Alice Alias",
        voiceprints=[
            ("20240101", "143022_300", "mic_audio", 1),
            ("20240101", "143022_300", "mic_audio", 2),
        ],
    )
    env.create_entity(
        "Alice Canonical",
        voiceprints=[("20240101", "143022_300", "mic_audio", 3)],
    )
    env.create_facet_relationship(
        "work",
        "alice_alias",
        description="Works at Acme",
        attached_at=1600000000000,
        observations=["Likes coffee", "Morning person"],
    )
    env.create_facet_relationship(
        "work",
        "alice_canonical",
        description="Senior engineer",
        attached_at=1700000000000,
        observations=["Staff role"],
    )
    env.create_facet_relationship("personal", "alice_alias", description="Hiker")
    env.create_speaker_labels(
        "20240101",
        "143022_300",
        [
            {
                "sentence_id": 1,
                "speaker": "alice_alias",
                "confidence": "high",
                "method": "acoustic",
            },
            {
                "sentence_id": 2,
                "speaker": "alice_canonical",
                "confidence": "high",
                "method": "acoustic",
            },
            {
                "sentence_id": 3,
                "speaker": "alice_alias",
                "confidence": "medium",
                "method": "context",
            },
        ],
    )
    env.create_speaker_corrections(
        "20240101",
        "143022_300",
        [
            {
                "sentence_id": 1,
                "original_speaker": "alice_alias",
                "corrected_speaker": "alice_alias",
                "timestamp": 1700000000000,
            },
        ],
    )
    cache_path = env.journal / "awareness" / "discovery_clusters.json"
    cache_path.parent.mkdir(parents=True, exist_ok=True)
    cache_path.write_text('{"clusters": []}', encoding="utf-8")

    result = runner.invoke(
        entities_app,
        ["merge", "alice_alias", "alice_canonical", "--commit"],
    )

    assert result.exit_code == 0, f"{result.output}\n{result.exception!r}"
    data = json.loads(result.output)
    assert data["merged"] is True
    assert "Alice Alias" in data["identity"]["akas_added"]
    assert data["voiceprints"]["added"] == 2
    assert data["voiceprints"]["target_total"] == 3
    assert "work" in data["facets"]["merged"]
    assert "personal" in data["facets"]["moved"]
    assert data["facets"]["observations_appended"] == 2
    assert data["segments"]["labels_rewritten"] == 1
    assert data["segments"]["corrections_rewritten"] == 1
    assert data["segments"]["errors"] == []
    assert data["activities"] == {
        "records_rewritten": 0,
        "fields_rewritten": 0,
        "files_scanned": 0,
        "files_rewritten": 0,
        "errors": [],
    }
    assert data["audit_log_path"] == str(_audit_log_path(env))
    assert data["caches_cleared"] == ["discovery_clusters"]

    assert load_journal_entity("alice_alias") is None
    canonical = load_journal_entity("alice_canonical")
    assert canonical is not None
    assert "Alice Alias" in canonical["aka"]

    assert _voiceprint_count(env, "alice_canonical") == 3

    labels = _read_json(_labels_path(env, "20240101", "143022_300"))
    speakers = [label["speaker"] for label in labels["labels"]]
    assert "alice_alias" not in speakers
    assert speakers.count("alice_canonical") == 3

    corrections = _read_json(_corrections_path(env, "20240101", "143022_300"))
    for correction in corrections["corrections"]:
        assert correction.get("original_speaker") != "alice_alias"
        assert correction.get("corrected_speaker") != "alice_alias"

    observations_path = (
        env.journal
        / "facets"
        / "work"
        / "entities"
        / "alice_canonical"
        / "observations.jsonl"
    )
    contents = [
        json.loads(line)["content"]
        for line in observations_path.read_text(encoding="utf-8").splitlines()
        if line.strip()
    ]
    assert set(contents) == {"Staff role", "Likes coffee", "Morning person"}
    assert not cache_path.exists()

    audit_entries = [
        json.loads(line)
        for line in _audit_log_path(env).read_text(encoding="utf-8").splitlines()
        if line.strip()
    ]
    assert len(audit_entries) == 1
    assert isinstance(audit_entries[0]["ts"], int)
    assert audit_entries[0]["caller"] == "entities.merge"
    assert audit_entries[0]["source_id"] == "alice_alias"
    assert audit_entries[0]["source_display_name"] == "Alice Alias"
    assert audit_entries[0]["target_id"] == "alice_canonical"
    assert audit_entries[0]["target_display_name"] == "Alice Canonical"
    assert audit_entries[0]["principal_transferred"] is False
    assert set(audit_entries[0]["counts"]) == {
        "identity",
        "voiceprints",
        "facets",
        "segments",
        "activities",
        "edges",
    }


def test_merge_commit_folds_edges_and_records_audit_counts(speakers_env):
    env = speakers_env()
    env.create_entity("Edge Fold Source")
    env.create_entity("Edge Fold Target")
    _insert_edge_rows(
        env,
        [
            _edge_row(
                "edge_fold_source",
                "edge_fold_peer",
                "co-present",
                "fold/survive",
            ),
            _edge_row(
                "edge_fold_source",
                "edge_fold_target",
                "co-present",
                "fold/self",
            ),
        ],
    )

    result = merge_mod.merge_entity("edge_fold_source", "edge_fold_target", commit=True)

    assert result["merged"] is True
    assert result["edges"] == {
        "rows_folded": 2,
        "self_edges_dropped": 1,
        "error": None,
    }
    rows = [row for row in _edge_rows(env) if row["path"].startswith("fold/")]
    assert len(rows) == 1
    assert all("edge_fold_source" not in {row["src"], row["dst"]} for row in rows)
    assert all("edge_fold_target" in {row["src"], row["dst"]} for row in rows)

    audit_entries = [
        json.loads(line)
        for line in _audit_log_path(env).read_text(encoding="utf-8").splitlines()
        if line.strip()
    ]
    assert audit_entries[-1]["counts"]["edges"] == {
        "rows_folded": 2,
        "self_edges_dropped": 1,
        "error": None,
    }


def test_merge_then_rebuild_converges_all_edge_source_kinds(speakers_env):
    env = speakers_env()
    day = "20240101"
    segment_key = "120000_300"
    source_id = "merge_edge_source"
    target_id = "merge_edge_target"
    peer_id = "merge_edge_peer"
    mention_id = "merge_edge_mention"

    env.create_segment(day, segment_key, ["audio"], num_sentences=2)
    transcript = "\n".join(
        [
            json.dumps({"raw": "audio.flac", "model": "test"}),
            json.dumps({"text": "Merge Edge Mention came up in planning."}),
            json.dumps({"text": "The peer answered."}),
        ]
    )
    for segment_dir in (
        env.journal / day / STREAM / segment_key,
        env.journal / "chronicle" / day / STREAM / segment_key,
    ):
        (segment_dir / "audio.jsonl").write_text(
            transcript + "\n",
            encoding="utf-8",
        )

    for name in (
        "Merge Edge Source",
        "Merge Edge Target",
        "Merge Edge Peer",
        "Merge Edge Mention",
    ):
        env.create_entity(name)
    for entity_id in (source_id, target_id, peer_id, mention_id):
        env.create_facet_relationship("work", entity_id)

    source_obs_path = (
        env.journal / "facets" / "work" / "entities" / source_id / "observations.jsonl"
    )
    source_obs_path.write_text(
        json.dumps(
            {
                "content": "source observation carried forward",
                "observed_at": 1700000001000,
            }
        )
        + "\n",
        encoding="utf-8",
    )
    target_obs_path = (
        env.journal / "facets" / "work" / "entities" / target_id / "observations.jsonl"
    )
    target_obs_path.write_text(
        json.dumps(
            {
                "content": "target relation to source",
                "observed_at": 1700000002000,
                "source_day": day,
                "relation": {
                    "kind": "family-of",
                    "target_entity_id": source_id,
                    "target_name": "Merge Edge Source",
                    "note": "Self after merge",
                },
            }
        )
        + "\n",
        encoding="utf-8",
    )
    peer_obs_path = (
        env.journal / "facets" / "work" / "entities" / peer_id / "observations.jsonl"
    )
    peer_obs_path.write_text(
        json.dumps(
            {
                "content": "peer observation relation to source",
                "observed_at": 1700000003000,
                "source_day": day,
                "relation": {
                    "kind": "knows",
                    "target_entity_id": source_id,
                    "target_name": "Merge Edge Source",
                    "note": "Observation-backed relation",
                },
            }
        )
        + "\n",
        encoding="utf-8",
    )

    detected_path = env.journal / "facets" / "work" / "entities" / f"{day}.jsonl"
    detected_path.parent.mkdir(parents=True, exist_ok=True)
    detected_path.write_text(
        "\n".join(
            [
                json.dumps({"name": "Merge Edge Source", "segments": [segment_key]}),
                json.dumps({"name": "Merge Edge Peer", "segments": [segment_key]}),
            ]
        )
        + "\n",
        encoding="utf-8",
    )
    env.create_speaker_labels(
        day,
        segment_key,
        [
            {"sentence_id": 1, "speaker": source_id},
            {"sentence_id": 2, "speaker": peer_id},
        ],
    )
    _write_activity_records(
        env,
        "work",
        day,
        [
            {
                "id": "meeting_120000_300",
                "activity": "meeting",
                "title": "Edge convergence review",
                "created_at": 1700000000000,
                "participation": [
                    {"role": "attendee", "entity_id": source_id},
                    {"role": "attendee", "entity_id": peer_id},
                ],
                "relations": [
                    {
                        "from": "Merge Edge Source",
                        "to": "Merge Edge Peer",
                        "from_entity_id": source_id,
                        "to_entity_id": peer_id,
                        "kind": kind,
                        "note": f"{kind} relation",
                        "quote": None,
                    }
                    for kind in (
                        "works-with",
                        "works-at",
                        "reports-to",
                        "family-of",
                        "knows",
                        "uses",
                        "created",
                        "other",
                    )
                ],
                "decisions": [
                    {
                        "owner": "Merge Edge Source",
                        "counterparty": "Merge Edge Peer",
                        "owner_entity_id": source_id,
                        "counterparty_entity_id": peer_id,
                        "action": "Use the merge convergence fixture",
                    }
                ],
                "commitments": [
                    {
                        "owner_entity_id": source_id,
                        "counterparty_entity_id": peer_id,
                        "action": "rerun rebuild",
                    }
                ],
            }
        ],
    )
    segment_dir = env.journal / "chronicle" / day / STREAM / segment_key
    (segment_dir / "screen.jsonl").write_text(
        "\n".join(
            [
                json.dumps({"raw": "screen.png", "model": "fixture"}),
                json.dumps(
                    {
                        "timestamp": 0,
                        "content": {
                            "messaging": {
                                "view": "conversation",
                                "app": "Signal",
                                "thread": "Merge Edge Thread",
                                "messages": [
                                    {
                                        "sender": "Merge Edge Source",
                                        "timestamp": "2024-01-01T12:00:00Z",
                                        "subject": "",
                                        "text": "Can you review this merge?",
                                    },
                                    {
                                        "sender": "Merge Edge Peer",
                                        "timestamp": "2024-01-01T12:00:30Z",
                                        "subject": "",
                                        "text": "Yes, I can.",
                                    },
                                ],
                            },
                            "calendar": {
                                "view": "day",
                                "app": "Calendar",
                                "events": [
                                    {
                                        "title": "Merge Edge Calendar",
                                        "start": "2024-01-01T12:30:00Z",
                                        "end": "2024-01-01T13:00:00Z",
                                        "calendar": "Work",
                                        "guests": [
                                            "Merge Edge Source",
                                            "Merge Edge Peer",
                                        ],
                                    }
                                ],
                            },
                        },
                    }
                ),
            ]
        )
        + "\n",
        encoding="utf-8",
    )
    talents_dir = segment_dir / "talents"
    talents_dir.mkdir(parents=True, exist_ok=True)
    (talents_dir / "documents.json").write_text(
        json.dumps(
            {
                "overview": "Merge convergence document.",
                "parties": [
                    {"name": "Merge Edge Source", "role": "author"},
                    {"name": "Merge Edge Peer", "role": "reviewer"},
                ],
                "key_provisions": [],
                "assets": [],
                "conditions": [],
                "important_dates": [],
                "summary": "Merge source and peer are parties.",
            },
            indent=2,
        )
        + "\n",
        encoding="utf-8",
    )

    rebuild_edges(str(env.journal))
    pre_rows = _edge_rows_with_kind(env)
    source_kinds = {
        row["kind"] for row in pre_rows if source_id in {row["src"], row["dst"]}
    }
    new_kinds = {
        "works-with",
        "works-at",
        "reports-to",
        "family-of",
        "knows",
        "uses",
        "created",
        "other",
        "decided-with",
        "messaged-with",
        "scheduled-with",
        "party-of",
    }
    assert source_kinds == {
        "attended-with",
        "co-present",
        "committed-to",
        "decided-with",
        "family-of",
        "knows",
        "mentioned",
        "messaged-with",
        "party-of",
        "scheduled-with",
        "spoke-with",
        "works-at",
        "works-with",
        "reports-to",
        "uses",
        "created",
        "other",
    }
    assert new_kinds <= source_kinds
    assert any(
        row["source"] == "observation"
        and row["kind"] == "knows"
        and row["dst"] == source_id
        for row in pre_rows
    )

    result = merge_mod.merge_entity(source_id, target_id, commit=True)

    assert result["merged"] is True
    assert result["facets"]["observations_appended"] == 1
    assert result["facets"]["observation_relations_rewritten"] == 2
    assert result["activities"]["fields_rewritten"] == 11
    assert all(
        source_id not in {row["src"], row["dst"]} for row in _edge_rows_with_kind(env)
    )
    target_observations = [
        json.loads(line)
        for line in target_obs_path.read_text(encoding="utf-8").splitlines()
        if line.strip()
    ]
    assert {item["content"] for item in target_observations} == {
        "target relation to source",
        "source observation carried forward",
    }
    assert target_observations[0]["relation"]["target_entity_id"] == target_id
    peer_observations = [
        json.loads(line)
        for line in peer_obs_path.read_text(encoding="utf-8").splitlines()
        if line.strip()
    ]
    assert peer_observations[0]["relation"]["target_entity_id"] == target_id

    folded_hash = _edge_hash(env)
    rebuild_edges(str(env.journal))
    assert _edge_hash(env) == folded_hash
    rebuilt_rows = _edge_rows_with_kind(env)
    assert all(source_id not in {row["src"], row["dst"]} for row in rebuilt_rows)
    target_kinds = {
        row["kind"] for row in rebuilt_rows if target_id in {row["src"], row["dst"]}
    }
    assert target_kinds == {
        "attended-with",
        "co-present",
        "committed-to",
        "decided-with",
        "family-of",
        "knows",
        "mentioned",
        "messaged-with",
        "party-of",
        "scheduled-with",
        "spoke-with",
        "works-at",
        "works-with",
        "reports-to",
        "uses",
        "created",
        "other",
    }
    assert new_kinds <= target_kinds
    assert any(
        row["source"] == "observation"
        and row["kind"] == "knows"
        and target_id in {row["src"], row["dst"]}
        for row in rebuilt_rows
    )

    first_hash = _edge_hash(env)
    rebuild_edges(str(env.journal))
    assert _edge_hash(env) == first_hash


def test_merge_commit_continues_when_edge_fold_fails(speakers_env, monkeypatch):
    env = speakers_env()
    env.create_entity("Edge Failure Source")
    env.create_entity("Edge Failure Target")

    def fail_fold(*args, **kwargs):
        raise sqlite3.OperationalError("boom")

    monkeypatch.setattr("solstone.think.indexer.edges.fold_entity_edges", fail_fold)

    result = merge_mod.merge_entity(
        "edge_failure_source",
        "edge_failure_target",
        commit=True,
    )

    assert result["merged"] is True
    assert result["edges"] == {
        "rows_folded": 0,
        "self_edges_dropped": 0,
        "error": "boom",
    }
    assert load_journal_entity("edge_failure_source") is None
    audit_entries = [
        json.loads(line)
        for line in _audit_log_path(env).read_text(encoding="utf-8").splitlines()
        if line.strip()
    ]
    assert audit_entries[-1]["counts"]["edges"] == {
        "rows_folded": 0,
        "self_edges_dropped": 0,
        "error": "boom",
    }


def test_merge_facet_move_writes_target_before_source_delete_on_cleanup_failure(
    speakers_env,
    monkeypatch,
):
    env = speakers_env()
    env.create_entity("Cleanup Alias")
    env.create_entity("Cleanup Canon")
    source_rel_dir = env.create_facet_relationship(
        "work",
        "cleanup_alias",
        description="Source relationship",
    )

    def fail_cleanup(*args, **kwargs):
        raise RuntimeError("cleanup boom")

    monkeypatch.setattr(
        merge_mod,
        "_apply_destructive_plan",
        fail_cleanup,
        raising=False,
    )

    result = merge_mod.merge_entity("cleanup_alias", "cleanup_canon", commit=True)

    assert result["failed_phase"] == "cleanup"
    target_rel_path = (
        env.journal / "facets" / "work" / "entities" / "cleanup_canon" / "entity.json"
    )
    assert _read_json(target_rel_path)["entity_id"] == "cleanup_canon"
    assert source_rel_dir.exists()


def test_merge_commit_failure_reports_failed_phase_and_resume_marker(
    speakers_env,
    monkeypatch,
):
    env = speakers_env()
    env.create_entity("Failure Source")
    env.create_entity("Failure Target")
    env.create_segment("20240101", "143022_300", ["mic_audio"])
    env.create_speaker_labels(
        "20240101",
        "143022_300",
        [
            {
                "sentence_id": 1,
                "speaker": "failure_source",
                "confidence": "high",
                "method": "acoustic",
            }
        ],
    )

    def fail_segments(*args, **kwargs):
        raise RuntimeError("segment boom")

    monkeypatch.setattr(merge_mod, "_apply_segment_plan", fail_segments)

    result = merge_mod.merge_entity("failure_source", "failure_target", commit=True)

    assert result["error"] == "segment boom"
    assert result["failed_phase"] == "segments"
    assert "recovery" in result
    assert result["source_id"] == "failure_source"
    assert result["target_id"] == "failure_target"
    source = load_journal_entity("failure_source")
    assert source is not None
    assert source["merged_into"] == "failure_target"


def test_merge_segment_rewrites_use_owner_byte_shapes(speakers_env):
    env = speakers_env()
    env.create_segment("20240101", "143022_300", ["mic_audio"])
    env.create_entity("Bytes Source")
    env.create_entity("Bytes Target")
    labels_before = [
        {
            "sentence_id": 1,
            "speaker": "bytes_source",
            "confidence": "high",
            "method": "acoustic",
        },
        {
            "sentence_id": 2,
            "speaker": "other_person",
            "confidence": "medium",
            "method": "context",
        },
    ]
    corrections_before = [
        {
            "sentence_id": 1,
            "original_speaker": "bytes_source",
            "corrected_speaker": "bytes_source",
            "original_method": "acoustic",
            "timestamp": 1700000000000,
        }
    ]
    env.create_speaker_labels("20240101", "143022_300", labels_before)
    env.create_speaker_corrections("20240101", "143022_300", corrections_before)

    result = merge_mod.merge_entity("bytes_source", "bytes_target", commit=True)

    assert result["merged"] is True
    expected_labels = {
        "labels": [
            {
                "sentence_id": 1,
                "speaker": "bytes_target",
                "confidence": "high",
                "method": "acoustic",
            },
            {
                "sentence_id": 2,
                "speaker": "other_person",
                "confidence": "medium",
                "method": "context",
            },
        ],
        "owner_centroid_last_refreshed_at": None,
        "voiceprint_versions": {},
    }
    labels_path = _labels_path(env, "20240101", "143022_300")
    assert labels_path.read_bytes() == (
        json.dumps(expected_labels, indent=2) + "\n"
    ).encode("utf-8")
    assert _read_json(labels_path) == expected_labels

    expected_corrections = {
        "corrections": [
            {
                "sentence_id": 1,
                "original_speaker": "bytes_target",
                "corrected_speaker": "bytes_target",
                "original_method": "acoustic",
                "timestamp": 1700000000000,
            }
        ]
    }
    corrections_path = _corrections_path(env, "20240101", "143022_300")
    assert corrections_path.read_bytes() == json.dumps(
        expected_corrections,
        indent=2,
    ).encode("utf-8")
    assert _read_json(corrections_path) == expected_corrections


def test_merge_default_keeps_source_as_aka(speakers_env):
    env = speakers_env()
    env.create_entity("Keep Alias")
    env.create_entity("Keep Canon")

    result = runner.invoke(
        entities_app,
        ["merge", "keep_alias", "keep_canon", "--commit"],
    )

    assert result.exit_code == 0, f"{result.output}\n{result.exception!r}"
    canonical = load_journal_entity("keep_canon")
    assert canonical is not None
    assert "Keep Alias" in canonical["aka"]


def test_merge_no_keep_source_as_aka_keeps_only_existing_aliases(speakers_env):
    env = speakers_env()
    env.create_entity("Skip Alias")
    env.create_entity("Skip Canon")
    _update_entity(env, "skip_alias", aka=["SA", "S.A."])

    result = runner.invoke(
        entities_app,
        [
            "merge",
            "skip_alias",
            "skip_canon",
            "--commit",
            "--no-keep-source-as-aka",
        ],
    )

    assert result.exit_code == 0, f"{result.output}\n{result.exception!r}"
    canonical = load_journal_entity("skip_canon")
    assert canonical is not None
    assert "Skip Alias" not in canonical.get("aka", [])
    assert {"SA", "S.A."} <= set(canonical["aka"])


def test_merge_transfers_principal_from_source_to_target(speakers_env):
    env = speakers_env()
    env.create_entity("Principal Source", is_principal=True)
    env.create_entity("Principal Target")

    result = runner.invoke(
        entities_app,
        ["merge", "principal_source", "principal_target", "--commit"],
    )

    assert result.exit_code == 0, f"{result.output}\n{result.exception!r}"
    data = json.loads(result.output)
    assert data["identity"]["principal_transferred"] is True
    assert load_journal_entity("principal_source") is None
    target = load_journal_entity("principal_target")
    assert target is not None
    assert target["is_principal"] is True


def test_merge_errors_when_both_entities_are_principal(speakers_env):
    env = speakers_env()
    env.create_entity("First Principal", is_principal=True)
    env.create_entity("Second Principal", is_principal=True)

    result = runner.invoke(
        entities_app,
        ["merge", "first_principal", "second_principal", "--commit"],
    )

    assert result.exit_code == 1, f"{result.output}\n{result.exception!r}"
    data = json.loads(result.output)
    assert data["error"] == "Cannot merge two principal entities."
    assert load_journal_entity("first_principal") is not None
    assert load_journal_entity("second_principal") is not None


def test_merge_errors_on_aka_cross_reference(speakers_env):
    env = speakers_env()
    env.create_entity("Cross Source")
    env.create_entity("Cross Target")
    env.create_entity("Cross Watcher")
    _update_entity(env, "cross_watcher", aka=["cross_source", "Watcher Alias"])

    result = runner.invoke(
        entities_app,
        ["merge", "cross_source", "cross_target", "--commit"],
    )

    assert result.exit_code == 1, f"{result.output}\n{result.exception!r}"
    data = json.loads(result.output)
    assert (
        data["error"]
        == "Cannot merge 'cross_source': referenced in aka lists of entity ids: cross_watcher"
    )
    assert load_journal_entity("cross_source") is not None
    assert load_journal_entity("cross_target") is not None


def test_merge_validation_errors(speakers_env):
    env = speakers_env()
    env.create_entity("Blocked Source")
    env.create_entity("Validation Target")
    _update_entity(env, "blocked_source", blocked=True)

    cases = [
        (
            ["merge", "validation_target", "validation_target", "--commit"],
            "Source and target must be different entities.",
        ),
        (
            ["merge", "missing_source", "validation_target", "--commit"],
            "Source entity not found: missing_source",
        ),
        (
            ["merge", "validation_target", "missing_target", "--commit"],
            "Target entity not found: missing_target",
        ),
        (
            ["merge", "blocked_source", "validation_target", "--commit"],
            "Cannot merge blocked entity: blocked_source",
        ),
    ]

    for argv, expected_error in cases:
        result = runner.invoke(entities_app, argv)
        assert result.exit_code == 1, f"{result.output}\n{result.exception!r}"
        data = json.loads(result.output)
        assert data["error"] == expected_error
