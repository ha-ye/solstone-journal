# SPDX-License-Identifier: AGPL-3.0-only
# Copyright (c) 2026 sol pbc

from __future__ import annotations

import hashlib
import json
import multiprocessing
import os
import traceback
from pathlib import Path
from queue import Empty
from typing import Any

import numpy as np
import pytest

from solstone.think.entities import entity_slug
from solstone.think.entities import history as history_mod
from solstone.think.entities import merge as merge_mod
from solstone.think.entities.history import (
    EntityOperationContext,
    iter_entity_history,
)
from solstone.think.entities.journal import load_journal_entity, save_journal_entity
from solstone.think.indexer.edges import rebuild_edges
from solstone.think.indexer.journal import get_journal_index
from tests._sqlite_assertions import edges_content_hash

# This suite exercises the durable prepare/replace/publish protocol; each
# identity write costs several fsyncs, so the 15s unit-test default is not a
# meaningful bound here.
pytestmark = pytest.mark.timeout(120)

STREAM = "test"
HOSTILE_FACET = "../../../../tmp/pwn"


def _read_json(path: Path) -> dict:
    return json.loads(path.read_text(encoding="utf-8"))


def _write_json(path: Path, payload: dict) -> None:
    path.write_text(json.dumps(payload, indent=2) + "\n", encoding="utf-8")


def _read_jsonl(path: Path) -> list[dict]:
    return [
        json.loads(line)
        for line in path.read_text(encoding="utf-8").splitlines()
        if line.strip()
    ]


def _write_jsonl(path: Path, rows: list[dict]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("".join(json.dumps(row) + "\n" for row in rows), encoding="utf-8")


def _write_canary(path: Path, payload: bytes) -> tuple[Path, bytes]:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(payload)
    return path, payload


def _outside_component(base: Path, target: Path) -> str:
    return os.path.relpath(target, base).replace(os.sep, "/")


def _entity_path(env, entity_id: str) -> Path:
    return env.journal / "entities" / entity_id / "entity.json"


def _entity_dir(env, entity_id: str) -> Path:
    return env.journal / "entities" / entity_id


def _audit_log_path(env) -> Path:
    return env.journal / "logs" / "entity-merges.jsonl"


def _private_payload_path(env, target_id: str, merge_id: str) -> Path:
    return _entity_dir(env, target_id) / "history" / "private" / f"{merge_id}.json"


def _events(entity_id: str) -> list[dict]:
    return list(iter_entity_history(entity_id))


def _normalize_entity_identity(entity_id: str) -> None:
    entity = load_journal_entity(entity_id)
    assert entity is not None
    save_journal_entity(
        entity,
        operation=EntityOperationContext(kind="update", caller="test.normalize"),
    )


def _journal_tree_hash(root: Path) -> str:
    digest = hashlib.sha256()
    for path in sorted(root.rglob("*")):
        rel = path.relative_to(root).as_posix()
        if path.name.endswith(".lock") or rel.startswith("indexer/"):
            continue
        stat = path.lstat()
        digest.update(rel.encode("utf-8"))
        if path.is_symlink():
            digest.update(b"symlink")
            digest.update(os.readlink(path).encode("utf-8"))
        elif path.is_file():
            digest.update(b"file")
            digest.update(str(stat.st_mode).encode("ascii"))
            digest.update(path.read_bytes())
        elif path.is_dir():
            digest.update(b"dir")
    return digest.hexdigest()


def _index_hash(env) -> str:
    conn, _ = get_journal_index(str(env.journal))
    try:
        return edges_content_hash(conn)
    finally:
        conn.close()


def _activity_path(env, facet: str, day: str) -> Path:
    return env.journal / "facets" / facet / "activities" / f"{day}.jsonl"


def _labels_path(env, day: str, segment_key: str) -> Path:
    return env.journal / day / STREAM / segment_key / "talents" / "speaker_labels.json"


def _corrections_path(env, day: str, segment_key: str) -> Path:
    return (
        env.journal
        / day
        / STREAM
        / segment_key
        / "talents"
        / "speaker_corrections.json"
    )


def _voiceprint_keys(env, entity_id: str) -> list[tuple]:
    path = _entity_dir(env, entity_id) / "voiceprints.npz"
    if not path.exists():
        return []
    with np.load(path, allow_pickle=False) as data:
        return [
            (
                item.get("day"),
                item.get("segment_key"),
                item.get("source"),
                item.get("sentence_id"),
            )
            for item in (json.loads(str(raw)) for raw in data["metadata"])
        ]


def _seed_round_trip(env) -> dict[str, Path | bytes]:
    day = "20240101"
    segment_key = "101010_300"
    source_id = "undo_source"
    target_id = "undo_target"
    peer_id = "undo_peer"
    env.create_segment(day, segment_key, ["mic_audio"])
    env.create_entity(
        "Undo Source",
        voiceprints=[(day, segment_key, "mic_audio", 1)],
    )
    env.create_entity(
        "Undo Target",
        voiceprints=[(day, segment_key, "mic_audio", 2)],
    )
    env.create_entity("Undo Peer")
    for entity_id in (source_id, target_id, peer_id):
        _normalize_entity_identity(entity_id)

    source = load_journal_entity(source_id)
    target = load_journal_entity(target_id)
    assert source is not None and target is not None
    source["aka"] = ["Undo Alias"]
    source["emails"] = ["undo-source@example.com"]
    source["title"] = "Source-only title"
    save_journal_entity(source)
    save_journal_entity(target)

    env.create_facet_relationship(
        "work",
        source_id,
        description="source relationship",
        observations=["source observation"],
    )
    env.create_facet_relationship("work", target_id, description="target relationship")
    env.create_facet_relationship("work", peer_id)
    target_obs_path = (
        env.journal / "facets" / "work" / "entities" / target_id / "observations.jsonl"
    )
    _write_jsonl(
        target_obs_path,
        [
            {
                "content": "target relation to source",
                "observed_at": 1700000000001,
                "relation": {"kind": "knows", "target_entity_id": source_id},
            }
        ],
    )

    env.create_speaker_labels(
        day,
        segment_key,
        [{"sentence_id": 1, "speaker": source_id, "confidence": "high"}],
    )
    env.create_speaker_corrections(
        day,
        segment_key,
        [
            {
                "sentence_id": 1,
                "original_speaker": source_id,
                "corrected_speaker": source_id,
                "timestamp": 1700000000000,
            }
        ],
    )
    _write_jsonl(
        _activity_path(env, "work", day),
        [
            {
                "id": "undo_activity",
                "active_entities": [source_id],
                "participation": [{"entity_id": source_id}],
                "commitments": [
                    {
                        "owner_entity_id": source_id,
                        "counterparty_entity_id": peer_id,
                    }
                ],
                "closures": [
                    {
                        "owner_entity_id": peer_id,
                        "counterparty_entity_id": source_id,
                    }
                ],
                "decisions": [
                    {
                        "owner_entity_id": source_id,
                        "counterparty_entity_id": peer_id,
                    }
                ],
                "relations": [
                    {
                        "from_entity_id": source_id,
                        "to_entity_id": peer_id,
                        "kind": "knows",
                    }
                ],
            }
        ],
    )
    rebuild_edges(str(env.journal))
    return {
        "source_id": source_id,
        "target_id": target_id,
        "day": day,
        "segment_key": segment_key,
        "source_bytes": _entity_path(env, source_id).read_bytes(),
        "target_bytes": _entity_path(env, target_id).read_bytes(),
        "source_rel_dir": env.journal / "facets" / "work" / "entities" / source_id,
        "target_obs_path": target_obs_path,
    }


def _drain_errors(errors: Any) -> list[str]:
    found = []
    while True:
        try:
            found.append(errors.get_nowait())
        except Empty:
            return found


def _join_processes(processes: list[Any], errors: Any) -> None:
    for process in processes:
        process.start()
    for process in processes:
        process.join(timeout=10)
    for process in processes:
        if process.is_alive():
            process.terminate()
            process.join(timeout=2)

    error_text = "\n".join(_drain_errors(errors))
    assert all(not process.is_alive() for process in processes), error_text
    assert all(process.exitcode == 0 for process in processes), error_text


def _concurrent_identity_update_worker(
    journal_path: str,
    barrier: Any,
    errors: Any,
    target_id: str,
) -> None:
    os.environ["SOLSTONE_JOURNAL"] = journal_path
    try:
        from solstone.think.entities.journal import (
            load_journal_entity,
            save_journal_entity,
        )

        barrier.wait(timeout=5)
        target = load_journal_entity(target_id)
        if target is None:
            raise AssertionError(f"missing target entity {target_id}")
        target["emails"] = ["concurrent@example.com"]
        save_journal_entity(target)
    except BaseException:
        errors.put(traceback.format_exc())
        raise


def _concurrent_speaker_shift_worker(
    journal_path: str,
    barrier: Any,
    errors: Any,
    day: str,
    segment_key: str,
) -> None:
    os.environ["SOLSTONE_JOURNAL"] = journal_path
    try:
        from solstone.apps.speakers.attribution import update_speaker_labels

        seg_dir = Path(journal_path) / "chronicle" / day / STREAM / segment_key

        def transform(current: dict | None) -> dict | None:
            if current is None:
                raise AssertionError("missing speaker labels")
            labels = list(current.get("labels", []))
            labels.insert(
                0,
                {
                    "sentence_id": 99,
                    "speaker": "concurrent_speaker",
                    "confidence": "low",
                    "method": "test",
                },
            )
            return {**current, "labels": labels}

        barrier.wait(timeout=5)
        update_speaker_labels(seg_dir, transform)
    except BaseException:
        errors.put(traceback.format_exc())
        raise


def _concurrent_undo_worker(
    journal_path: str,
    barrier: Any,
    errors: Any,
    merge_id: str,
) -> None:
    os.environ["SOLSTONE_JOURNAL"] = journal_path
    try:
        from solstone.think.entities.merge import undo_entity_merge

        barrier.wait(timeout=5)
        result = undo_entity_merge(merge_id)
        if "error" in result:
            raise AssertionError(result)
    except BaseException:
        errors.put(traceback.format_exc())
        raise


def test_merge_preview_changes_no_journal_or_index_bytes(speakers_env) -> None:
    env = speakers_env()
    env.create_entity("Preview Source")
    env.create_entity("Preview Target")
    _normalize_entity_identity("preview_source")
    _normalize_entity_identity("preview_target")
    rebuild_edges(str(env.journal))
    tree_before = _journal_tree_hash(env.journal)
    index_before = _index_hash(env)

    result = merge_mod.merge_entity("preview_source", "preview_target", commit=False)

    assert result["merged"] is False
    assert "merge_id" not in result
    assert _journal_tree_hash(env.journal) == tree_before
    assert _index_hash(env) == index_before
    private_dir = _entity_dir(env, "preview_target") / "history" / "private"
    assert not list(private_dir.glob("*.json"))


def test_committed_merge_records_merge_id_audit_event_and_payload(speakers_env) -> None:
    env = speakers_env()
    env.create_entity("Record Source")
    env.create_entity("Record Target")
    _normalize_entity_identity("record_source")
    _normalize_entity_identity("record_target")

    result = merge_mod.merge_entity("record_source", "record_target", commit=True)

    merge_id = result["merge_id"]
    assert merge_id.startswith("em_")
    assert load_journal_entity("record_source") is None
    audit_rows = _read_jsonl(_audit_log_path(env))
    assert audit_rows[-1]["merge_id"] == merge_id
    events = _events("record_target")
    assert events[-1]["kind"] == "merge"
    assert events[-1]["operation"]["merge_id"] == merge_id
    payload_path = _private_payload_path(env, "record_target", merge_id)
    assert payload_path.is_file()
    payload = _read_json(payload_path)
    assert payload["merge_id"] == merge_id
    for snapshot in payload["source_state"]["snapshots"]:
        rel = snapshot["rel"]
        assert not rel.startswith("/")
        assert ".." not in Path(rel).parts


def test_merge_then_undo_round_trips_supported_owner_stores(speakers_env) -> None:
    env = speakers_env()
    fixture = _seed_round_trip(env)
    source_id = str(fixture["source_id"])
    target_id = str(fixture["target_id"])
    day = str(fixture["day"])
    segment_key = str(fixture["segment_key"])
    source_rel_dir = fixture["source_rel_dir"]
    target_obs_path = fixture["target_obs_path"]
    assert isinstance(source_rel_dir, Path)
    assert isinstance(target_obs_path, Path)

    result = merge_mod.merge_entity(source_id, target_id, commit=True)
    undo = merge_mod.undo_entity_merge(result["merge_id"])

    assert undo["undone"] is True
    assert _entity_path(env, source_id).read_bytes() == fixture["source_bytes"]
    assert _entity_path(env, target_id).read_bytes() == fixture["target_bytes"]
    assert source_rel_dir.exists()
    assert [item["content"] for item in _read_jsonl(target_obs_path)] == [
        "target relation to source"
    ]
    assert _read_jsonl(target_obs_path)[0]["relation"]["target_entity_id"] == source_id
    activity = _read_jsonl(_activity_path(env, "work", day))[0]
    assert activity["active_entities"] == [source_id]
    assert activity["participation"][0]["entity_id"] == source_id
    assert activity["commitments"][0]["owner_entity_id"] == source_id
    assert activity["closures"][0]["counterparty_entity_id"] == source_id
    assert activity["decisions"][0]["owner_entity_id"] == source_id
    assert activity["relations"][0]["from_entity_id"] == source_id
    assert (
        _read_json(_labels_path(env, day, segment_key))["labels"][0]["speaker"]
        == source_id
    )
    correction = _read_json(_corrections_path(env, day, segment_key))["corrections"][0]
    assert correction["original_speaker"] == source_id
    assert correction["corrected_speaker"] == source_id
    assert _voiceprint_keys(env, source_id) == [(day, segment_key, "mic_audio", 1)]
    assert _voiceprint_keys(env, target_id) == [(day, segment_key, "mic_audio", 2)]
    rebuild_edges(str(env.journal))


def test_non_lifo_overlapping_support_survives_undo_by_id(speakers_env) -> None:
    env = speakers_env()
    day = "20240101"
    segment_key = "111111_300"
    key = (day, segment_key, "mic_audio", 1)
    env.create_segment(day, segment_key, ["mic_audio"])
    env.create_entity("Overlap A", voiceprints=[key])
    env.create_entity("Overlap B", voiceprints=[key])
    env.create_entity("Overlap Target")
    for entity_id in ("overlap_a", "overlap_b", "overlap_target"):
        _normalize_entity_identity(entity_id)
    for entity_id, title in (
        ("overlap_a", "Title A"),
        ("overlap_b", "Title B"),
    ):
        entity = load_journal_entity(entity_id)
        assert entity is not None
        entity["aka"] = ["Shared Alias"]
        entity["emails"] = ["shared@example.com"]
        entity["title"] = title
        save_journal_entity(entity)
        env.create_facet_relationship(
            "work",
            entity_id,
            description=title,
            observations=["shared observation"],
        )
    env.create_facet_relationship("work", "overlap_target")

    merge_a = merge_mod.merge_entity("overlap_a", "overlap_target", commit=True)
    merge_b = merge_mod.merge_entity("overlap_b", "overlap_target", commit=True)
    undo_a = merge_mod.undo_entity_merge(merge_a["merge_id"])

    assert undo_a["undone"] is True
    assert load_journal_entity("overlap_a") is not None
    assert load_journal_entity("overlap_b") is None
    target = load_journal_entity("overlap_target")
    assert target is not None
    assert "Shared Alias" in target["aka"]
    assert "shared@example.com" in target["emails"]
    assert target["title"] == "Title B"
    target_obs = _read_jsonl(
        env.journal
        / "facets"
        / "work"
        / "entities"
        / "overlap_target"
        / "observations.jsonl"
    )
    assert [item["content"] for item in target_obs] == ["shared observation"]
    rel = _read_json(
        env.journal / "facets" / "work" / "entities" / "overlap_target" / "entity.json"
    )
    assert rel["description"] == "Title B"
    assert _voiceprint_keys(env, "overlap_target") == [key]
    assert _private_payload_path(env, "overlap_target", merge_b["merge_id"]).is_file()


def test_corrupt_active_sibling_payload_blocks_non_lifo_undo_without_mutation(
    speakers_env,
) -> None:
    env = speakers_env()
    for entity_id in ("sibling_a", "sibling_b", "sibling_target"):
        env.create_entity(entity_id.replace("_", " ").title())
        _normalize_entity_identity(entity_id)
    for entity_id, title in (("sibling_a", "Title A"), ("sibling_b", "Title B")):
        entity = load_journal_entity(entity_id)
        assert entity is not None
        entity["aka"] = ["Sibling Alias"]
        entity["emails"] = ["sibling@example.com"]
        entity["title"] = title
        save_journal_entity(entity)
        env.create_facet_relationship(
            "work",
            entity_id,
            description=title,
            observations=["sibling observation"],
        )
    env.create_facet_relationship("work", "sibling_target")

    merge_a = merge_mod.merge_entity("sibling_a", "sibling_target", commit=True)
    merge_b = merge_mod.merge_entity("sibling_b", "sibling_target", commit=True)
    payload_b_path = _private_payload_path(env, "sibling_target", merge_b["merge_id"])
    payload_b = _read_json(payload_b_path)
    payload_b["manifest"].pop("identity")
    _write_json(payload_b_path, payload_b)

    tree_before = _journal_tree_hash(env.journal)
    index_before = _index_hash(env)

    undo = merge_mod.undo_entity_merge(merge_a["merge_id"])

    assert "error" in undo
    assert merge_b["merge_id"] in undo["error"]
    assert _journal_tree_hash(env.journal) == tree_before
    assert _index_hash(env) == index_before
    assert load_journal_entity("sibling_a") is None
    target = load_journal_entity("sibling_target")
    assert target is not None
    assert "Sibling Alias" in target["aka"]
    assert "sibling@example.com" in target["emails"]
    assert target["title"] == "Title A"
    target_obs = _read_jsonl(
        env.journal
        / "facets"
        / "work"
        / "entities"
        / "sibling_target"
        / "observations.jsonl"
    )
    assert [item["content"] for item in target_obs] == ["sibling observation"]


def _seed_chain(env) -> tuple[str, str]:
    for name in ("Chain C", "Chain A", "Chain Target"):
        env.create_entity(name)
    for entity_id in ("chain_c", "chain_a", "chain_target"):
        _normalize_entity_identity(entity_id)

    merge_c = merge_mod.merge_entity("chain_c", "chain_a", commit=True)
    merge_a = merge_mod.merge_entity("chain_a", "chain_target", commit=True)
    assert "error" not in merge_c
    assert "error" not in merge_a
    return merge_c["merge_id"], merge_a["merge_id"]


def test_chained_lineage_descendant_merge_stays_reachable(speakers_env) -> None:
    env = speakers_env()
    merge_c, merge_a = _seed_chain(env)

    assert _private_payload_path(env, "chain_target", merge_c).is_file()
    undo_c = merge_mod.undo_entity_merge(merge_c)

    assert undo_c["undone"] is True
    assert load_journal_entity("chain_c") is not None
    assert load_journal_entity("chain_a") is None
    assert load_journal_entity("chain_target") is not None
    assert not _private_payload_path(env, "chain_target", merge_c).exists()
    assert _private_payload_path(env, "chain_target", merge_a).is_file()

    env = speakers_env()
    merge_c, merge_a = _seed_chain(env)

    undo_a = merge_mod.undo_entity_merge(merge_a)

    assert undo_a["undone"] is True
    assert load_journal_entity("chain_a") is not None
    assert load_journal_entity("chain_c") is None
    assert _private_payload_path(env, "chain_a", merge_c).is_file()
    undo_c = merge_mod.undo_entity_merge(merge_c)
    assert undo_c["undone"] is True
    assert load_journal_entity("chain_c") is not None


def test_parenthetical_source_name_merges_and_undoes_without_alias_corruption(
    speakers_env,
) -> None:
    env = speakers_env()
    env.create_entity("Jordan (Work)")
    env.create_entity("Jordan Canon")
    _normalize_entity_identity("jordan_work")
    _normalize_entity_identity("jordan_canon")
    source_before = _read_json(_entity_path(env, "jordan_work"))

    result = merge_mod.merge_entity("jordan_work", "jordan_canon", commit=True)
    undo = merge_mod.undo_entity_merge(result["merge_id"])

    assert undo["undone"] is True
    assert load_journal_entity("jordan_work") == source_before
    target = load_journal_entity("jordan_canon")
    assert target is not None
    assert "Jordan (Work)" not in target.get("aka", [])
    assert "jordan_work" not in target.get("aka", [])


def _seed_preflight_pair(env, *, with_voiceprint: bool = False) -> None:
    if with_voiceprint:
        day = "20240101"
        segment_key = "121212_300"
        env.create_segment(day, segment_key, ["mic_audio"])
        env.create_entity(
            "Preflight Source",
            voiceprints=[(day, segment_key, "mic_audio", 1)],
        )
    else:
        env.create_entity("Preflight Source")
    env.create_entity("Preflight Target")
    _normalize_entity_identity("preflight_source")
    _normalize_entity_identity("preflight_target")


def _corrupt_preflight_owner(env, owner: str) -> None:
    if owner == "facet":
        env.create_facet_relationship("work", "preflight_source")
        path = (
            env.journal
            / "facets"
            / "work"
            / "entities"
            / "preflight_source"
            / "entity.json"
        )
        path.write_text("{", encoding="utf-8")
    elif owner == "observation":
        env.create_facet_relationship(
            "work",
            "preflight_source",
            observations=["malformed soon"],
        )
        path = (
            env.journal
            / "facets"
            / "work"
            / "entities"
            / "preflight_source"
            / "observations.jsonl"
        )
        path.write_text("{\n", encoding="utf-8")
    elif owner == "activity":
        _write_jsonl(
            _activity_path(env, "work", "20240101"),
            [{"id": "bad_activity", "active_entities": ["preflight_source"]}],
        )
        _activity_path(env, "work", "20240101").write_text("{\n", encoding="utf-8")
    elif owner == "speaker":
        day = "20240101"
        segment_key = "131313_300"
        env.create_segment(day, segment_key, ["mic_audio"])
        env.create_speaker_labels(
            day,
            segment_key,
            [{"sentence_id": 1, "speaker": "preflight_source"}],
        )
        _labels_path(env, day, segment_key).write_text("{", encoding="utf-8")
    elif owner == "voiceprint":
        path = _entity_dir(env, "preflight_source") / "voiceprints.npz"
        path.write_bytes(b"not an npz")
    elif owner == "edge":
        day = "20240101"
        segment_key = "141414_300"
        env.create_segment(day, segment_key, ["mic_audio"])
        path = env.journal / day / STREAM / segment_key / "talents" / "documents.json"
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text("{", encoding="utf-8")
    else:
        raise AssertionError(owner)


@pytest.mark.parametrize(
    "owner",
    ["facet", "observation", "activity", "speaker", "voiceprint", "edge"],
)
def test_strict_preflight_rejects_malformed_owner_inputs_unchanged(
    speakers_env,
    owner: str,
) -> None:
    env = speakers_env()
    _seed_preflight_pair(env, with_voiceprint=owner == "voiceprint")
    rebuild_edges(str(env.journal))
    _corrupt_preflight_owner(env, owner)
    tree_before = _journal_tree_hash(env.journal)
    index_before = _index_hash(env)

    result = merge_mod.merge_entity(
        "preflight_source",
        "preflight_target",
        commit=True,
    )

    assert "error" in result
    assert _journal_tree_hash(env.journal) == tree_before
    assert _index_hash(env) == index_before
    assert load_journal_entity("preflight_source") is not None
    assert load_journal_entity("preflight_target") is not None


@pytest.mark.parametrize(
    ("phase", "symbol"),
    [
        ("private_payload", "_write_private_payload"),
        ("voiceprints", "save_voiceprints_batch"),
        ("facets", "_apply_facet_additive_plan"),
        ("segments", "_apply_segment_plan"),
        ("activities", "_apply_activity_remaps"),
        ("lineage", "_rebase_descendant_payloads"),
        ("cleanup", "_apply_destructive_plan"),
        ("observation relation remap", "_apply_observation_relation_remaps"),
        ("history", "save_journal_entity"),
        ("edges", "_fold_edges"),
        ("audit", "_append_audit_log"),
    ],
)
def test_failure_injection_rolls_back_each_apply_phase(
    speakers_env,
    monkeypatch: pytest.MonkeyPatch,
    phase: str,
    symbol: str,
) -> None:
    env = speakers_env()
    fixture = _seed_round_trip(env)
    source_id = str(fixture["source_id"])
    target_id = str(fixture["target_id"])
    tree_before = _journal_tree_hash(env.journal)
    index_before = _index_hash(env)
    events_before = _events(target_id)

    def fail_phase(*_args: Any, **_kwargs: Any) -> None:
        raise RuntimeError(f"{phase} boom")

    monkeypatch.setattr(merge_mod, symbol, fail_phase)

    result = merge_mod.merge_entity(source_id, target_id, commit=True)

    assert result["error"] == f"{phase} boom"
    assert result["failed_phase"] == phase
    assert _journal_tree_hash(env.journal) == tree_before
    assert _index_hash(env) == index_before
    assert load_journal_entity(source_id) is not None
    assert _events(target_id) == events_before
    assert not _audit_log_path(env).exists()


def test_history_crash_reconciliation_windows(
    speakers_env,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    speakers_env()

    prepared_entity = {
        "id": "crash_after_prepare",
        "name": "Crash After Prepare",
        "type": "Person",
    }
    save_journal_entity(prepared_entity)
    prepared_after = {**prepared_entity, "name": "Crash After Prepare Updated"}
    with monkeypatch.context() as patch:
        patch.setattr(
            history_mod,
            "_write_identity_snapshot",
            lambda *_args, **_kwargs: (_ for _ in ()).throw(
                RuntimeError("after prepare")
            ),
        )
        with pytest.raises(RuntimeError, match="after prepare"):
            save_journal_entity(prepared_after)
    save_journal_entity(prepared_after)
    assert load_journal_entity("crash_after_prepare") == prepared_after

    identity_entity = {
        "id": "crash_after_identity",
        "name": "Crash After Identity",
        "type": "Person",
    }
    save_journal_entity(identity_entity)
    identity_after = {**identity_entity, "name": "Crash After Identity Updated"}
    with monkeypatch.context() as patch:
        patch.setattr(
            history_mod,
            "_publish_prepared_event",
            lambda *_args, **_kwargs: (_ for _ in ()).throw(
                RuntimeError("after identity")
            ),
        )
        with pytest.raises(RuntimeError, match="after identity"):
            save_journal_entity(identity_after)
    save_journal_entity(identity_after)
    assert load_journal_entity("crash_after_identity") == identity_after

    marker_entity = {
        "id": "crash_before_marker",
        "name": "Crash Before Marker",
        "type": "Person",
    }
    save_journal_entity(marker_entity)
    marker_after = {**marker_entity, "name": "Crash Before Marker Updated"}
    original_discard = history_mod._discard_prepared_event
    crashed = False

    def fail_after_visible_once(*args: Any, **kwargs: Any) -> None:
        nonlocal crashed
        if not crashed:
            crashed = True
            raise RuntimeError("before marker")
        original_discard(*args, **kwargs)

    with monkeypatch.context() as patch:
        patch.setattr(history_mod, "_discard_prepared_event", fail_after_visible_once)
        with pytest.raises(RuntimeError, match="before marker"):
            save_journal_entity(marker_after)
    save_journal_entity(marker_after)
    assert load_journal_entity("crash_before_marker") == marker_after


def test_concurrent_update_and_shifted_speaker_locator_survive_undo(
    speakers_env,
) -> None:
    env = speakers_env()
    day = "20240101"
    segment_key = "151515_300"
    env.create_segment(day, segment_key, ["mic_audio"])
    env.create_entity("Concurrent Source")
    env.create_entity("Concurrent Target")
    env.create_entity("Concurrent Speaker")
    for entity_id in ("concurrent_source", "concurrent_target", "concurrent_speaker"):
        _normalize_entity_identity(entity_id)
    env.create_speaker_labels(
        day,
        segment_key,
        [
            {
                "sentence_id": 1,
                "speaker": "concurrent_source",
                "confidence": "high",
                "method": "test",
            }
        ],
    )

    result = merge_mod.merge_entity(
        "concurrent_source",
        "concurrent_target",
        commit=True,
    )
    assert "error" not in result

    ctx = multiprocessing.get_context("spawn")
    barrier = ctx.Barrier(3)
    errors = ctx.Queue()
    processes = [
        ctx.Process(
            target=_concurrent_identity_update_worker,
            args=(str(env.journal), barrier, errors, "concurrent_target"),
        ),
        ctx.Process(
            target=_concurrent_speaker_shift_worker,
            args=(str(env.journal), barrier, errors, day, segment_key),
        ),
        ctx.Process(
            target=_concurrent_undo_worker,
            args=(str(env.journal), barrier, errors, result["merge_id"]),
        ),
    ]
    _join_processes(processes, errors)

    source = load_journal_entity("concurrent_source")
    target = load_journal_entity("concurrent_target")
    assert source is not None
    assert target is not None
    assert target["emails"] == ["concurrent@example.com"]
    labels = _read_json(_labels_path(env, day, segment_key))["labels"]
    assert labels[0]["sentence_id"] == 99
    assert any(
        label["sentence_id"] == 1 and label["speaker"] == "concurrent_source"
        for label in labels
    )


def _seed_hostile_merge(env, case: str, facet_kind: str | None) -> tuple[str, str]:
    suffix = f"{case} {os.getpid()}"
    source_name = f"Hostile Source {suffix}"
    target_name = f"Hostile Target {suffix}"
    source_id = entity_slug(source_name)
    target_id = entity_slug(target_name)
    env.create_entity(source_name)
    env.create_entity(target_name)
    _normalize_entity_identity(source_id)
    _normalize_entity_identity(target_id)
    if facet_kind is not None:
        env.create_facet_relationship(
            "work",
            source_id,
            description="source relationship",
        )
        if facet_kind == "merge":
            env.create_facet_relationship(
                "work",
                target_id,
                description="target relationship",
            )
    return source_id, target_id


def _facet_escape_canary(env, target_id: str, entry: dict) -> tuple[Path, bytes]:
    canary_path = (
        env.journal / "facets" / HOSTILE_FACET / "entities" / target_id / "entity.json"
    ).resolve()
    assert not canary_path.is_relative_to(env.journal.resolve())
    if entry["kind"] == "move":
        body = dict(entry["relationship"])
        body["entity_id"] = target_id
    else:
        body = {"entity_id": target_id, "canary": "outside"}
    payload = json.dumps(body, separators=(",", ":")).encode("utf-8") + b"\n"
    return _write_canary(canary_path, payload)


@pytest.mark.parametrize(
    ("case", "facet_kind"),
    [
        pytest.param("snapshot_rel_dotdot", None, id="snapshot-rel-dotdot"),
        pytest.param("snapshot_rel_absolute", None, id="snapshot-rel-absolute"),
        pytest.param("snapshot_rel_symlink", None, id="snapshot-rel-symlink"),
        pytest.param("facet_move", "move", id="facet-move"),
        pytest.param("facet_merge", "merge", id="facet-merge"),
        pytest.param("target_id_escape", None, id="target-id-escape"),
        pytest.param("source_id_escape", None, id="source-id-escape"),
        pytest.param("missing_source_state", None, id="missing-source-state"),
        pytest.param("missing_manifest", None, id="missing-manifest"),
        pytest.param("missing_identity_manifest", None, id="missing-identity-manifest"),
        pytest.param("invalid_json", None, id="invalid-json"),
        pytest.param("missing_payload", None, id="missing-payload"),
    ],
)
def test_hostile_manifest_fails_before_undo_mutation(
    speakers_env,
    tmp_path: Path,
    case: str,
    facet_kind: str | None,
) -> None:
    env = speakers_env()
    source_id, target_id = _seed_hostile_merge(env, case, facet_kind)
    result = merge_mod.merge_entity(source_id, target_id, commit=True)
    payload_path = _private_payload_path(env, target_id, result["merge_id"])
    payload = _read_json(payload_path)
    canaries: list[tuple[Path, bytes]] = []

    if case == "snapshot_rel_dotdot":
        payload["source_state"]["snapshots"][0]["rel"] = "../escape"
        _write_json(payload_path, payload)
    elif case == "snapshot_rel_absolute":
        payload["source_state"]["snapshots"][0]["rel"] = str(
            tmp_path.parent / f"{case}_{os.getpid()}"
        )
        _write_json(payload_path, payload)
    elif case == "snapshot_rel_symlink":
        outside = tmp_path.parent / f"{case}_{os.getpid()}"
        outside.mkdir(parents=True, exist_ok=True)
        link = env.journal / f"{case}_link"
        link.symlink_to(outside, target_is_directory=True)
        payload["source_state"]["snapshots"][0]["rel"] = f"{link.name}/snapshot"
        _write_json(payload_path, payload)
    elif case in {"facet_move", "facet_merge"}:
        entries = payload["manifest"]["facets"]["entries"]
        assert len(entries) == 1
        entry = entries[0]
        assert entry["kind"] == facet_kind
        canaries.append(_facet_escape_canary(env, target_id, entry))
        entry["facet"] = HOSTILE_FACET
        _write_json(payload_path, payload)
    elif case == "target_id_escape":
        outside = tmp_path.parent / f"{case}_{os.getpid()}"
        canaries.append(
            _write_canary(outside / "entity.json", b'{"canary":"target"}\n')
        )
        payload["target_id"] = _outside_component(env.journal / "entities", outside)
        assert ".." in Path(payload["target_id"]).parts
        _write_json(payload_path, payload)
    elif case == "source_id_escape":
        outside = tmp_path.parent / f"{case}_{os.getpid()}"
        canaries.append(
            _write_canary(outside / "entity.json", b'{"canary":"source"}\n')
        )
        payload["source_id"] = _outside_component(env.journal / "entities", outside)
        assert ".." in Path(payload["source_id"]).parts
        _write_json(payload_path, payload)
    elif case == "missing_source_state":
        payload.pop("source_state")
        _write_json(payload_path, payload)
    elif case == "missing_manifest":
        payload.pop("manifest")
        _write_json(payload_path, payload)
    elif case == "missing_identity_manifest":
        payload["manifest"].pop("identity")
        _write_json(payload_path, payload)
    elif case == "invalid_json":
        payload_path.write_text("{", encoding="utf-8")
    elif case == "missing_payload":
        payload_path.unlink()
    else:
        raise AssertionError(case)

    tree_before = _journal_tree_hash(env.journal)
    index_before = _index_hash(env)

    try:
        undo = merge_mod.undo_entity_merge(result["merge_id"])

        assert "error" in undo
        assert _journal_tree_hash(env.journal) == tree_before
        assert _index_hash(env) == index_before
        assert load_journal_entity(source_id) is None
        for canary_path, canary_bytes in canaries:
            assert canary_path.read_bytes() == canary_bytes
    finally:
        for canary_path, canary_bytes in canaries:
            if canary_path.exists() and canary_path.read_bytes() == canary_bytes:
                canary_path.unlink()


def test_double_undo_and_payload_disposition(speakers_env) -> None:
    env = speakers_env()
    env.create_entity("Payload Source")
    env.create_entity("Payload Target")
    _normalize_entity_identity("payload_source")
    _normalize_entity_identity("payload_target")
    result = merge_mod.merge_entity("payload_source", "payload_target", commit=True)
    payload_path = _private_payload_path(env, "payload_target", result["merge_id"])
    assert payload_path.is_file()

    first = merge_mod.undo_entity_merge(result["merge_id"])
    second = merge_mod.undo_entity_merge(result["merge_id"])

    assert first["undone"] is True
    assert second["error"] == f"Merge already undone: {result['merge_id']}"
    assert not payload_path.exists()
    assert (_entity_dir(env, "payload_source") / "history").exists()
    assert not any(
        result["merge_id"] in path.name
        for path in (_entity_dir(env, "payload_target") / "history" / "private").glob(
            "*.json"
        )
    )
