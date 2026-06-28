# SPDX-License-Identifier: AGPL-3.0-only
# Copyright (c) 2026 sol pbc

from __future__ import annotations

import json
import uuid
from pathlib import Path
from typing import Any

from solstone.think.link.ca import load_or_generate_ca
from solstone.think.link.paths import LinkState, ca_dir, state_path


def _commit_journal_identity() -> None:
    load_or_generate_ca(ca_dir())


def _read_config(journal: Path) -> dict[str, Any]:
    return json.loads((journal / "config" / "journal.json").read_text("utf-8"))


def _assert_mark_shape(mark: dict[str, Any]) -> None:
    assert set(mark) == {"icon1", "icon2", "words"}
    assert {"name", "svg", "color", "rot"} <= set(mark["icon1"])
    assert {"name", "svg", "color", "rot"} <= set(mark["icon2"])
    assert len(mark["words"]) == 2


def test_init_mark_returns_unlocked_candidate(convey_env_setup_pending) -> None:
    env = convey_env_setup_pending()

    response = env.client.get("/init/mark")

    assert response.status_code == 200
    payload = response.get_json()
    assert payload["locked"] is False
    _assert_mark_shape(payload["mark"])


def test_init_mark_regenerate_changes_unlocked_candidate(
    convey_env_setup_pending,
) -> None:
    env = convey_env_setup_pending()
    first = env.client.get("/init/mark").get_json()["mark"]

    response = env.client.post("/init/mark/regenerate")

    assert response.status_code == 200
    payload = response.get_json()
    assert payload["locked"] is False
    _assert_mark_shape(payload["mark"])
    assert payload["mark"] != first


def test_init_mark_lock_is_idempotent(convey_env_setup_pending) -> None:
    env = convey_env_setup_pending()
    env.client.get("/init/mark")

    first = env.client.post("/init/mark/lock")
    second = env.client.post("/init/mark/lock")

    assert first.status_code == 200
    assert second.status_code == 200
    assert first.get_json()["locked"] is True
    assert second.get_json()["locked"] is True
    assert second.get_json()["mark"] == first.get_json()["mark"]


def test_legacy_lazy_journal_stays_locked_and_preserves_ca(
    convey_env_setup_pending,
) -> None:
    env = convey_env_setup_pending()
    load_or_generate_ca(ca_dir())
    ca_path = ca_dir()
    cert_before = (ca_path / "cert.pem").read_bytes()
    key_before = (ca_path / "private.pem").read_bytes()
    legacy_id = str(uuid.uuid4())
    LinkState(instance_id=legacy_id, home_label="legacy").save()
    state_before = state_path().read_bytes()

    mark_response = env.client.get("/init/mark")
    regenerate_response = env.client.post("/init/mark/regenerate")

    assert mark_response.status_code == 200
    assert mark_response.get_json()["locked"] is True
    assert regenerate_response.status_code == 400
    assert (
        regenerate_response.get_json()["reason_code"] == "invalid_operation_for_state"
    )
    assert (ca_path / "cert.pem").read_bytes() == cert_before
    assert (ca_path / "private.pem").read_bytes() == key_before
    assert state_path().read_bytes() == state_before


def test_finalize_requires_locked_identity_before_config_mutation(
    convey_env_setup_pending,
) -> None:
    env = convey_env_setup_pending()
    before_config = _read_config(env.journal)
    convey_config = env.journal / "config" / "convey.json"
    assert not convey_config.exists()

    blocked = env.client.post(
        "/init/finalize",
        json={"name": "Blocked", "retention_mode": "processed"},
        content_type="application/json",
    )

    assert blocked.status_code == 400
    assert blocked.get_json()["reason_code"] == "identity_not_locked"
    assert _read_config(env.journal) == before_config
    assert not convey_config.exists()

    _commit_journal_identity()
    allowed = env.client.post(
        "/init/finalize",
        json={"name": "Allowed"},
        content_type="application/json",
    )

    assert allowed.status_code == 200
    assert allowed.get_json()["success"] is True
