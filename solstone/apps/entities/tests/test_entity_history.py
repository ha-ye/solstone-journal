# SPDX-License-Identifier: AGPL-3.0-only
# Copyright (c) 2026 sol pbc

from __future__ import annotations

import copy
import json
from pathlib import Path

import pytest

from solstone.think.entities.history import (
    EntityHistoryError,
    EntityOperationContext,
    iter_entity_history,
    restore_journal_entity_version,
)
from solstone.think.entities.journal import (
    delete_journal_entity,
    load_journal_entity,
    save_journal_entity,
)


def _events(entity_id: str) -> list[dict]:
    return list(iter_entity_history(entity_id))


def _save(entity: dict) -> None:
    save_journal_entity(copy.deepcopy(entity))


def test_history_completeness_and_noop_save_adds_no_event(speakers_env) -> None:
    speakers_env()
    entity_id = "history_complete"
    entity = {"id": entity_id, "name": "History Complete", "type": "Person"}
    _save(entity)

    for index in range(3):
        entity = {
            **entity,
            "name": f"History Complete {index}",
            "aka": [f"HC {index}"],
        }
        _save(entity)

    before_noop = _events(entity_id)
    _save(entity)
    after_noop = _events(entity_id)

    assert len(before_noop) == 4
    assert after_noop == before_noop
    assert [event["seq"] for event in before_noop] == [1, 2, 3, 4]
    assert [event["kind"] for event in before_noop] == [
        "create",
        "update",
        "update",
        "update",
    ]
    assert before_noop[0]["identity_before"] is None
    assert before_noop[-1]["identity_after"] == entity


def test_restore_first_version_appends_restore_event(speakers_env) -> None:
    speakers_env()
    entity_id = "restore_subject"
    first = {"id": entity_id, "name": "Restore Subject", "type": "Person"}
    second = {
        **first,
        "name": "Restore Subject Edited",
        "emails": ["restore@example.com"],
    }
    _save(first)
    first_version = _events(entity_id)[0]["version_id"]
    _save(second)

    restore_event = restore_journal_entity_version(
        entity_id,
        first_version,
        caller="test",
    )

    assert load_journal_entity(entity_id) == first
    events = _events(entity_id)
    assert [event["seq"] for event in events] == [1, 2, 3]
    assert events[0]["identity_after"] == first
    assert events[1]["identity_after"] == second
    assert events[2] == restore_event
    assert events[2]["kind"] == "restore"
    assert events[2]["operation"] == {"restored_version_id": first_version}


def test_delete_journal_entity_removes_history_and_preserves_principal(
    speakers_env,
) -> None:
    env = speakers_env()
    entity_id = "history_delete"
    principal_id = "history_principal"
    _save({"id": entity_id, "name": "History Delete", "type": "Person"})
    _save(
        {
            "id": principal_id,
            "name": "History Principal",
            "type": "Person",
            "is_principal": True,
        }
    )

    history_dir = env.journal / "entities" / entity_id / "history"
    principal_history_dir = env.journal / "entities" / principal_id / "history"
    assert history_dir.is_dir()
    assert principal_history_dir.is_dir()

    delete_journal_entity(entity_id)

    assert not (env.journal / "entities" / entity_id).exists()
    assert not history_dir.exists()

    with pytest.raises(ValueError, match="Cannot delete the principal"):
        delete_journal_entity(principal_id)
    assert (env.journal / "entities" / principal_id).exists()
    assert principal_history_dir.is_dir()


def test_restore_guardrails_fail_unchanged_and_point_to_recorded_merge_undo(
    speakers_env,
) -> None:
    env = speakers_env()

    merge_entity_id = "guard_merge"
    merge_before = {"id": merge_entity_id, "name": "Guard Merge", "type": "Person"}
    merge_after = {
        **merge_before,
        "name": "Guard Merge After",
    }
    _save(merge_before)
    first_version = _events(merge_entity_id)[0]["version_id"]
    save_journal_entity(
        merge_after,
        operation=EntityOperationContext(
            kind="merge",
            caller="test",
            metadata={"merge_id": "merge_guard"},
        ),
    )
    unchanged = load_journal_entity(merge_entity_id)
    unchanged_events = _events(merge_entity_id)

    with pytest.raises(EntityHistoryError, match="recorded-merge undo"):
        restore_journal_entity_version(merge_entity_id, first_version, caller="test")

    assert load_journal_entity(merge_entity_id) == unchanged
    assert _events(merge_entity_id) == unchanged_events

    id_entity_id = "guard_id"
    id_snapshot = {"id": id_entity_id, "name": "Guard Id", "type": "Person"}
    _save(id_snapshot)
    bad_event = copy.deepcopy(_events(id_entity_id)[-1])
    bad_event["version_id"] = "vh_badidentity"
    bad_event["seq"] += 1
    bad_event["identity_after"]["id"] = "different_id"
    _write_visible_event(env.journal, id_entity_id, bad_event)
    unchanged = load_journal_entity(id_entity_id)
    unchanged_events = _events(id_entity_id)

    with pytest.raises(EntityHistoryError, match="recorded-merge undo"):
        restore_journal_entity_version(id_entity_id, "vh_badidentity", caller="test")

    assert load_journal_entity(id_entity_id) == unchanged
    assert _events(id_entity_id) == unchanged_events

    principal_entity_id = "guard_principal_restore"
    other_principal_id = "guard_other_principal"
    principal_snapshot = {
        "id": principal_entity_id,
        "name": "Guard Principal Restore",
        "type": "Person",
        "is_principal": True,
    }
    _save(principal_snapshot)
    principal_version = _events(principal_entity_id)[0]["version_id"]
    _save({**principal_snapshot, "is_principal": False})
    _save(
        {
            "id": other_principal_id,
            "name": "Guard Other Principal",
            "type": "Person",
            "is_principal": True,
        }
    )
    unchanged = load_journal_entity(principal_entity_id)
    unchanged_events = _events(principal_entity_id)

    with pytest.raises(EntityHistoryError, match="recorded-merge undo"):
        restore_journal_entity_version(
            principal_entity_id,
            principal_version,
            caller="test",
        )

    assert load_journal_entity(principal_entity_id) == unchanged
    assert _events(principal_entity_id) == unchanged_events


def _write_visible_event(journal: Path, entity_id: str, event: dict) -> None:
    path = (
        journal
        / "entities"
        / entity_id
        / "history"
        / "events"
        / f"{event['seq']:020d}-{event['version_id']}.json"
    )
    path.write_text(
        json.dumps(event, indent=2, sort_keys=True) + "\n", encoding="utf-8"
    )
