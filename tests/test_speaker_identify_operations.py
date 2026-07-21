# SPDX-License-Identifier: AGPL-3.0-only
# Copyright (c) 2026 sol pbc

from __future__ import annotations

import json
from pathlib import Path

import pytest

import solstone.think.speaker_identify_operations as ledger


@pytest.fixture
def op_journal(monkeypatch, tmp_path):
    monkeypatch.setenv("SOLSTONE_JOURNAL", str(tmp_path))
    import solstone.think.utils as think_utils

    think_utils._journal_path_cache = None
    return Path(tmp_path)


def _members() -> list[dict[str, object]]:
    return [
        {
            "day": "20260101",
            "stream": "test",
            "segment_key": "090000_300",
            "source": "mic_audio",
            "sentence_id": 1,
        },
        {
            "day": "20260101",
            "stream": "test",
            "segment_key": "090000_300",
            "source": "mic_audio",
            "sentence_id": 2,
        },
    ]


def _prepared_event(request_id: str = "req-1") -> dict[str, object]:
    operation_id = ledger.operation_id_for_request(request_id)
    members = _members()
    fingerprint = ledger.request_fingerprint(
        cluster_members=members,
        target_entity_id="alice_test",
        will_create=True,
        entity_type="Person",
        reviewed_near_match_entity_ids=["bob_test"],
    )
    plan = {
        "plan_schema_version": 1,
        "operation_id": operation_id,
        "request_id": request_id,
        "planned_at": "2026-07-20T12:00:00Z",
        "request": {
            "cluster_id": 7,
            "name": "Alice Test",
            "entity_id": "alice_test",
            "resolve_only": False,
            "create_new": True,
            "entity_type": "Person",
            "reviewed_near_match_entity_ids": ["bob_test"],
        },
        "cluster": {"cluster_id": 7, "member_count": len(members), "members": members},
        "target": {
            "entity_id": "alice_test",
            "entity_name": "Alice Test",
            "entity_type": "Person",
            "will_create": True,
        },
        "entity_identity": {
            "prior_identity": None,
            "intended_identity": {
                "id": "alice_test",
                "name": "Alice Test",
                "type": "Person",
                "created_at": 1,
            },
            "expected_history_operation": {
                "operation_kind": "speaker_identify",
                "operation_id": operation_id,
            },
        },
        "direct_voiceprints": {"preexisting_keys": [], "entries_to_add": []},
        "segments": [],
        "retro_confirm": {
            "matched": False,
            "match_score": None,
            "candidate_id": None,
            "candidate_before": None,
            "candidate_after": None,
            "preexisting_voiceprint_keys": [],
            "voiceprints_to_add": [],
        },
        "sentinel": {
            "cluster_key": "7",
            "prior_entry": None,
            "intended_entry": {
                "entity_id": "alice_test",
                "label": "Alice Test",
                "ts": "2026-07-20T12:00:00Z",
            },
        },
        "keep_separate_assertions": [],
    }
    return {
        "schema_version": ledger.IDENTIFY_OPERATION_SCHEMA_VERSION,
        "event_id": f"{operation_id}:prepared",
        "operation_id": operation_id,
        "request_id": request_id,
        "event_kind": "prepared",
        "ts": "2026-07-20T12:00:00Z",
        "caller": "test",
        "actor": None,
        "request_fingerprint": fingerprint,
        "prepared_plan": plan,
    }


def _checkpoint_event(
    operation_id: str, request_id: str, phase: str
) -> dict[str, object]:
    return {
        "schema_version": ledger.IDENTIFY_OPERATION_SCHEMA_VERSION,
        "event_id": f"{operation_id}:checkpoint:{phase}",
        "operation_id": operation_id,
        "request_id": request_id,
        "event_kind": "checkpoint",
        "ts": "2026-07-20T12:00:01Z",
        "caller": "test",
        "actor": None,
        "phase": phase,
        "checkpoint": {
            "phase_status": "complete",
            "completed_at": "2026-07-20T12:00:01Z",
            "counts": {"saved_count": 2},
            "skipped_reasons": {},
            "saved_count": 2,
            "skipped_existing_count": 0,
            "saved_keys": [
                {
                    "day": "20260101",
                    "segment_key": "090000_300",
                    "source": "mic_audio",
                    "sentence_id": 1,
                }
            ],
        },
    }


def test_operation_id_for_request_is_deterministic():
    assert ledger.operation_id_for_request("abc") == ledger.operation_id_for_request(
        "abc"
    )
    assert ledger.operation_id_for_request("abc").startswith("idop_")
    assert ledger.operation_id_for_request("abc") != ledger.operation_id_for_request(
        "abcd"
    )


def test_request_fingerprint_changes_for_each_identity_input():
    base = ledger.request_fingerprint(
        cluster_members=_members(),
        target_entity_id="alice_test",
        will_create=True,
        entity_type="Person",
        reviewed_near_match_entity_ids=["bob_test"],
    )
    changed_member = ledger.request_fingerprint(
        cluster_members=[{**_members()[0], "sentence_id": 99}],
        target_entity_id="alice_test",
        will_create=True,
        entity_type="Person",
        reviewed_near_match_entity_ids=["bob_test"],
    )
    changed_target = ledger.request_fingerprint(
        cluster_members=_members(),
        target_entity_id="carol_test",
        will_create=True,
        entity_type="Person",
        reviewed_near_match_entity_ids=["bob_test"],
    )
    changed_create = ledger.request_fingerprint(
        cluster_members=_members(),
        target_entity_id="alice_test",
        will_create=False,
        entity_type="Person",
        reviewed_near_match_entity_ids=["bob_test"],
    )
    changed_type = ledger.request_fingerprint(
        cluster_members=_members(),
        target_entity_id="alice_test",
        will_create=True,
        entity_type="Project",
        reviewed_near_match_entity_ids=["bob_test"],
    )
    changed_reviewed = ledger.request_fingerprint(
        cluster_members=_members(),
        target_entity_id="alice_test",
        will_create=True,
        entity_type="Person",
        reviewed_near_match_entity_ids=["carol_test"],
    )

    assert len(base) == 64
    assert (
        len(
            {
                base,
                changed_member,
                changed_target,
                changed_create,
                changed_type,
                changed_reviewed,
            }
        )
        == 6
    )
    assert base not in {
        changed_member,
        changed_target,
        changed_create,
        changed_type,
        changed_reviewed,
    }


def test_append_and_fold_prepared_resume_state(op_journal):
    prepared = _prepared_event()
    checkpoint = _checkpoint_event(
        str(prepared["operation_id"]),
        str(prepared["request_id"]),
        "direct_voiceprints",
    )

    ledger.append_event(prepared)
    ledger.append_event(checkpoint)

    state = ledger.fold_operation(str(prepared["operation_id"]))
    assert state is not None
    assert state.operation_id == prepared["operation_id"]
    assert state.request_fingerprint == prepared["request_fingerprint"]
    assert state.cluster_member_set == {
        ("20260101", "test", "090000_300", "mic_audio", 1),
        ("20260101", "test", "090000_300", "mic_audio", 2),
    }
    assert state.target_entity_id == "alice_test"
    assert state.target_entity_name == "Alice Test"
    assert state.will_create is True
    assert state.entity_type == "Person"
    assert state.reviewed_near_match_entity_ids == ("bob_test",)
    assert state.completed_phases == ("direct_voiceprints",)
    assert state.pending_phases == (
        "entity",
        "keep_separate",
        "corrections",
        "labels",
        "retro_tracker",
        "sentinel",
    )
    assert state.terminal_status == "in_progress"
    assert state.phase_checkpoints["direct_voiceprints"]["counts"]["saved_count"] == 2


def test_read_paths_do_not_create_speakers_dir(op_journal):
    assert ledger.load_operations() == []
    assert ledger.fold_operation("idop_missing") is None
    assert ledger.fold_all_operations() == []
    assert not (op_journal / "speakers").exists()


def test_committed_fold_returns_stored_result(op_journal):
    prepared = _prepared_event()
    operation_id = str(prepared["operation_id"])
    committed = {
        "schema_version": ledger.IDENTIFY_OPERATION_SCHEMA_VERSION,
        "event_id": f"{operation_id}:committed",
        "operation_id": operation_id,
        "request_id": prepared["request_id"],
        "event_kind": "committed",
        "ts": "2026-07-20T12:00:02Z",
        "caller": "test",
        "actor": None,
        "result": {"status": "identified", "operation_id": operation_id},
    }

    ledger.append_event(prepared)
    ledger.append_event(committed)

    state = ledger.fold_operation(operation_id)
    assert state is not None
    assert state.terminal_status == "committed"
    assert state.result == {"status": "identified", "operation_id": operation_id}
    assert state.pending_phases == ()


def test_undo_started_folds_as_undoing_not_committed(op_journal):
    prepared = _prepared_event()
    operation_id = str(prepared["operation_id"])
    committed = {
        "schema_version": ledger.IDENTIFY_OPERATION_SCHEMA_VERSION,
        "event_id": f"{operation_id}:committed",
        "operation_id": operation_id,
        "request_id": prepared["request_id"],
        "event_kind": "committed",
        "ts": "2026-07-20T12:00:02Z",
        "caller": "test",
        "actor": None,
        "result": {"status": "identified", "operation_id": operation_id},
    }
    undo_prepared = {
        "schema_version": ledger.IDENTIFY_OPERATION_SCHEMA_VERSION,
        "event_id": f"{operation_id}:undo_prepared",
        "operation_id": operation_id,
        "request_id": prepared["request_id"],
        "event_kind": "undo_prepared",
        "ts": "2026-07-20T12:00:03Z",
        "caller": "test",
        "actor": None,
        "undo_started_at": "2026-07-20T12:00:03Z",
    }

    ledger.append_event(prepared)
    ledger.append_event(committed)
    ledger.append_event(undo_prepared)

    state = ledger.fold_operation(operation_id)
    assert state is not None
    assert state.terminal_status == "undoing"
    assert state.pending_phases == ledger.UNDO_PHASE_ORDER


def test_identical_duplicate_event_id_folds_once(op_journal):
    prepared = _prepared_event()
    path = ledger.identify_operations_path(create=True)
    path.write_text(
        json.dumps(prepared) + "\n" + json.dumps(prepared) + "\n",
        encoding="utf-8",
    )

    state = ledger.fold_operation(str(prepared["operation_id"]))
    assert state is not None
    assert state.terminal_status == "in_progress"


def test_non_identical_duplicate_event_id_raises(op_journal):
    prepared = _prepared_event()
    changed = dict(prepared)
    changed["ts"] = "2026-07-20T12:00:09Z"
    path = ledger.identify_operations_path(create=True)
    path.write_text(
        json.dumps(prepared) + "\n" + json.dumps(changed) + "\n",
        encoding="utf-8",
    )

    with pytest.raises(ledger.IdentifyOperationError):
        ledger.fold_operation(str(prepared["operation_id"]))


def test_strict_malformed_row_raises(op_journal):
    ledger.identify_operations_path(create=True).write_text(
        "not-json\n",
        encoding="utf-8",
    )

    with pytest.raises(ledger.IdentifyOperationError):
        ledger.load_operations()
