# SPDX-License-Identifier: AGPL-3.0-only
# Copyright (c) 2026 sol pbc

from __future__ import annotations

import json
import re
import time
from collections.abc import Callable
from datetime import datetime

import pytest

import solstone.apps.entities.routes as routes
from solstone.think.entities.journal import save_journal_entity


class _FakeDeferredDeletes:
    def __init__(self) -> None:
        self.scheduled: list[tuple[str, Callable[[], None], float]] = []
        self._pending: dict[str, tuple[Callable[[], None], float]] = {}

    def schedule_with_id(
        self,
        pending_id: str,
        commit_fn: Callable[[], None],
        ttl_seconds: float = 10.0,
    ) -> str:
        self.scheduled.append((pending_id, commit_fn, ttl_seconds))
        self._pending[pending_id] = (commit_fn, ttl_seconds)
        return pending_id

    def cancel(self, pending_id: str) -> bool:
        if pending_id not in self._pending:
            return False
        self._pending.pop(pending_id)
        return True

    def fire(self, pending_id: str) -> None:
        commit_fn, _ttl_seconds = self._pending.pop(pending_id)
        commit_fn()


@pytest.fixture
def fake_deferred_deletes(monkeypatch):
    fake = _FakeDeferredDeletes()
    monkeypatch.setattr(routes, "deferred_deletes", fake)
    return fake


def _action_log_rows(journal_root, day):
    log_path = journal_root / "config" / "actions" / f"{day}.jsonl"
    if not log_path.exists():
        return []
    return [
        json.loads(line)
        for line in log_path.read_text(encoding="utf-8").splitlines()
        if line.strip()
    ]


def _create_journal_entity(entity_id, *, is_principal=False):
    save_journal_entity(
        {
            "id": entity_id,
            "name": entity_id.title(),
            "type": "Person",
            "is_principal": is_principal,
        }
    )


def _assert_reason(response, *, error, reason_code, detail):
    data = response.get_json()
    assert data["error"] == error
    assert data["reason_code"] == reason_code
    assert data["detail"] == detail


def test_delete_journal_entity_route_rejects_principal(
    client, journal_copy, monkeypatch
):
    entity_id = "principal-delete-test"
    today = datetime.now().strftime("%Y%m%d")
    _create_journal_entity(entity_id, is_principal=True)

    response = client.delete(f"/app/entities/api/journal/entity/{entity_id}")

    assert response.status_code == 400
    _assert_reason(
        response,
        error="I can't delete the principal entity.",
        reason_code="principal_entity_protected",
        detail="Cannot delete the principal (self) entity",
    )
    assert (journal_copy / "entities" / entity_id).exists()
    rows = _action_log_rows(journal_copy, today)
    assert not any(
        row["action"] == "journal_entity_delete"
        and row["params"].get("entity_id") == entity_id
        for row in rows
    )


def test_delete_journal_entity_route_rejects_missing_entity(client):
    response = client.delete("/app/entities/api/journal/entity/missing-entity")

    assert response.status_code == 400
    _assert_reason(
        response,
        error="I couldn't find that entity.",
        reason_code="entity_not_found",
        detail="Entity 'missing-entity' not found",
    )


def test_delete_journal_entity_route_returns_pending_response_shape(
    client, journal_copy, fake_deferred_deletes
):
    entity_id = "pending-delete-test"
    today = datetime.now().strftime("%Y%m%d")
    _create_journal_entity(entity_id)
    before_ms = int(time.time() * 1000)

    response = client.delete(f"/app/entities/api/journal/entity/{entity_id}")

    assert response.status_code == 200
    data = response.get_json()
    assert data["success"] is True
    assert re.fullmatch(r"[0-9a-f]{32}", data["pending"])
    assert data["ttl_seconds"] == routes.ENTITY_DELETE_TTL
    assert data["commit_at_ms"] >= before_ms
    scheduled_id, commit_fn, ttl_seconds = fake_deferred_deletes.scheduled[0]
    assert scheduled_id == data["pending"]
    assert callable(commit_fn)
    assert ttl_seconds == routes.ENTITY_DELETE_TTL
    assert (journal_copy / "entities" / entity_id).exists()
    rows = _action_log_rows(journal_copy, today)
    assert any(
        row["action"] == "journal_entity_delete"
        and row["params"].get("entity_id") == entity_id
        and row["params"].get("phase") == "pending"
        for row in rows
    )
    cancel_response = client.post(f"/app/entities/api/cancel-delete/{data['pending']}")
    assert cancel_response.status_code == 200
    assert fake_deferred_deletes.cancel(data["pending"]) is False


def test_cancel_delete_journal_entity_within_window_keeps_entity(
    client, journal_copy, fake_deferred_deletes
):
    entity_id = "cancel-delete-test"
    _create_journal_entity(entity_id)
    delete_response = client.delete(f"/app/entities/api/journal/entity/{entity_id}")
    pending_id = delete_response.get_json()["pending"]

    cancel_response = client.post(f"/app/entities/api/cancel-delete/{pending_id}")

    assert cancel_response.status_code == 200
    assert cancel_response.get_json() == {"cancelled": pending_id}
    assert (journal_copy / "entities" / entity_id).exists()
    assert fake_deferred_deletes.cancel(pending_id) is False


def test_cancel_delete_journal_entity_too_late_after_commit(
    client, journal_copy, fake_deferred_deletes
):
    entity_id = "late-delete-test"
    today = datetime.now().strftime("%Y%m%d")
    _create_journal_entity(entity_id)
    save_journal_entity(
        {
            "id": entity_id,
            "name": "Late Delete Updated",
            "type": "Person",
            "aka": ["historical alias"],
        }
    )
    history_dir = journal_copy / "entities" / entity_id / "history"
    assert history_dir.is_dir()
    delete_response = client.delete(f"/app/entities/api/journal/entity/{entity_id}")
    pending_id = delete_response.get_json()["pending"]

    fake_deferred_deletes.fire(pending_id)
    cancel_response = client.post(f"/app/entities/api/cancel-delete/{pending_id}")

    assert cancel_response.status_code == 410
    _assert_reason(
        cancel_response,
        error="I couldn't finish because that action is no longer available.",
        reason_code="operation_no_longer_available",
        detail="already committed or unknown",
    )
    assert not (journal_copy / "entities" / entity_id).exists()
    assert not history_dir.exists()
    history_response = client.get(
        f"/app/entities/api/journal/entity/{entity_id}/history"
    )
    assert history_response.status_code == 404
    assert history_response.get_json()["reason_code"] == "entity_not_found"
    rows = _action_log_rows(journal_copy, today)
    assert any(
        row["action"] == "journal_entity_delete"
        and row["params"].get("entity_id") == entity_id
        and row["params"].get("phase") == "committed"
        for row in rows
    )


def test_cancel_delete_journal_entity_unknown_pending_id_returns_410(client):
    response = client.post(f"/app/entities/api/cancel-delete/{'b' * 32}")

    assert response.status_code == 410
    _assert_reason(
        response,
        error="I couldn't finish because that action is no longer available.",
        reason_code="operation_no_longer_available",
        detail="already committed or unknown",
    )
