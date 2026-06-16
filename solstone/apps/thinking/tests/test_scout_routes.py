# SPDX-License-Identifier: AGPL-3.0-only
# Copyright (c) 2026 sol pbc

from __future__ import annotations

import json
import threading
import time
from pathlib import Path

import pytest

from solstone.apps.thinking import copy as thinking_copy
from solstone.apps.thinking import scout_lane
from solstone.convey import create_app
from solstone.think.journal_config import write_journal_config
from solstone.think.services import operations, scout, scout_handoff


def _approved_payload(key: str = "google-scout-key") -> dict[str, str]:
    return {
        "state": "approved",
        "google_api_key": key,
        "dispatch_token": "dispatch-secret",
        "account_id": "acct-secret",
        "created_at": "2026-05-24T00:00:00Z",
    }


def _read_config(journal: Path) -> dict:
    return json.loads((journal / "config" / "journal.json").read_text("utf-8"))


def _write_config(payload: dict) -> None:
    payload.setdefault("setup", {"completed_at": 1700000000000})
    write_journal_config(payload)


def _clear_scout(journal: Path) -> None:
    config = _read_config(journal)
    config.setdefault("env", {}).pop("GOOGLE_API_KEY", None)
    config.setdefault("services", {}).pop("scout", None)
    _write_config(config)


@pytest.fixture
def thinking_client(journal_copy: Path):
    _clear_scout(journal_copy)
    app = create_app(journal=str(journal_copy.resolve()))
    app.config["TESTING"] = True
    client = app.test_client()
    return client


def _wait_until(predicate, timeout: float = 2.0) -> None:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if predicate():
            return
        time.sleep(0.01)
    raise AssertionError("condition not met before timeout")


def _scout_status(client) -> dict:
    response = client.get("/app/thinking/api/scout")
    assert response.status_code == 200
    return response.get_json()


def test_get_scout_status_fresh_journal_is_secret_free(thinking_client) -> None:
    response = thinking_client.get("/app/thinking/api/scout")

    assert response.status_code == 200
    data = response.get_json()
    assert data["state"] == thinking_copy.SCOUT_STATE_OFF
    assert data["actions"] == {
        "enable": True,
        "refresh": False,
        "disable": False,
        "check": True,
    }
    assert data["checked"] is False
    assert data["checked_at"] is None
    assert data["check_error"] == "no_credential"
    assert data["operation"] is None
    assert data["provenance"] == {}
    serialized = json.dumps(data).lower()
    assert "dispatch_token" not in serialized
    assert "server_status" not in serialized
    assert "account_id" not in serialized


def test_get_scout_status_after_provision_is_secret_free(thinking_client) -> None:
    scout.provision_scout_handoff(_approved_payload())

    response = thinking_client.get("/app/thinking/api/scout")

    assert response.status_code == 200
    data = response.get_json()
    assert data["state"] == thinking_copy.SCOUT_STATE_ON
    assert data["checked"] is True
    assert data["checked_at"] is None
    assert data["check_error"] is None
    assert data["provenance"]["key_created_at"] == "2026-05-24T00:00:00Z"
    serialized = json.dumps(data).lower()
    assert "dispatch_token" not in serialized
    assert "server_status" not in serialized
    assert "account_id" not in serialized
    assert "dispatch-secret" not in serialized
    assert "acct-secret" not in serialized


def test_enable_success_remaps_terminal_phase_and_reads_enabled_state(
    thinking_client,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    def runner(**_kwargs):
        scout.provision_scout_handoff(_approved_payload())
        return operations.HandoffResult("enabled", None, False, True, None)

    monkeypatch.setattr(scout_handoff, "run_scout_handoff", runner)

    response = thinking_client.post("/app/thinking/api/scout/enable")

    assert response.status_code == 202
    assert response.get_json()["operation"]["phase"] == "starting"
    _wait_until(
        lambda: (
            _scout_status(thinking_client)["operation"]["phase"]
            == thinking_copy.SCOUT_STATE_INVITED
        )
    )
    status = _scout_status(thinking_client)
    assert status["operation"]["phase"] == thinking_copy.SCOUT_STATE_INVITED
    assert status["state"] == thinking_copy.SCOUT_STATE_ON


def test_enable_blocked_when_already_on(thinking_client) -> None:
    scout.provision_scout_handoff(_approved_payload())

    response = thinking_client.post("/app/thinking/api/scout/enable")

    assert response.status_code == 400
    assert response.get_json()["reason_code"] == "invalid_operation_for_state"


def test_enable_blocked_when_manual_key_present(
    journal_copy: Path,
    thinking_client,
) -> None:
    config = _read_config(journal_copy)
    config.setdefault("env", {})["GOOGLE_API_KEY"] = "manual-key"
    _write_config(config)

    response = thinking_client.post("/app/thinking/api/scout/enable")

    assert response.status_code == 400
    data = response.get_json()
    assert data["reason_code"] == "invalid_operation_for_state"
    assert data["detail"] == thinking_copy.SCOUT_MANUAL_KEY_BLOCK_COPY


def test_refresh_allowed_only_when_requested_or_on(
    thinking_client,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(
        scout_handoff,
        "run_scout_handoff",
        lambda **_kwargs: operations.HandoffResult("pending", None, False, True, None),
    )

    off_response = thinking_client.post("/app/thinking/api/scout/refresh")
    assert off_response.status_code == 400

    scout.record_scout_pending("acct-pending", 1770000000000)
    requested_response = thinking_client.post("/app/thinking/api/scout/refresh")
    assert requested_response.status_code == 202


def test_service_busy_for_second_scout_operation(
    thinking_client,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    started = threading.Event()
    release = threading.Event()

    def runner(**_kwargs):
        started.set()
        release.wait(2)
        return operations.HandoffResult("enabled", None, False, True, None)

    monkeypatch.setattr(scout_handoff, "run_scout_handoff", runner)

    first = thinking_client.post("/app/thinking/api/scout/enable")
    _wait_until(started.is_set)
    second = thinking_client.post("/app/thinking/api/scout/enable")
    release.set()

    assert first.status_code == 202
    assert second.status_code == 503
    assert second.get_json()["reason_code"] == "service_busy"


def test_disable_when_on_returns_result_and_off_status(thinking_client) -> None:
    scout.provision_scout_handoff(_approved_payload())

    response = thinking_client.post("/app/thinking/api/scout/disable")

    assert response.status_code == 200
    data = response.get_json()
    assert data["result"] == {"was_enabled": True, "env_key_preserved": False}
    assert data["status"]["state"] == thinking_copy.SCOUT_STATE_OFF


def test_disable_preserves_different_manual_key(
    journal_copy: Path,
    thinking_client,
) -> None:
    scout.provision_scout_handoff(_approved_payload("hosted-key"))
    config = _read_config(journal_copy)
    config.setdefault("env", {})["GOOGLE_API_KEY"] = "manual-key"
    _write_config(config)

    response = thinking_client.post("/app/thinking/api/scout/disable")

    assert response.status_code == 200
    data = response.get_json()
    assert data["result"] == {"was_enabled": True, "env_key_preserved": True}
    assert _read_config(journal_copy)["env"]["GOOGLE_API_KEY"] == "manual-key"


def test_check_route_forces_status_payload_without_operation_registry(
    thinking_client,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    calls: list[bool] = []

    def fake_status_payload(*, force: bool = False) -> dict:
        calls.append(force)
        return {
            "service": "scout",
            "state": thinking_copy.SCOUT_STATE_OFF,
            "guidance": "Scout is off.",
            "provenance": {},
            "actions": {
                "enable": True,
                "refresh": False,
                "disable": False,
                "check": True,
            },
            "operation": None,
            "checked": False,
            "checked_at": None,
            "check_error": "no_credential",
        }

    monkeypatch.setattr(scout_lane, "status_payload", fake_status_payload)
    monkeypatch.setattr(
        operations,
        "start_operation",
        lambda *_args, **_kwargs: (_ for _ in ()).throw(
            AssertionError("operation registry should not run")
        ),
    )

    response = thinking_client.post("/app/thinking/api/scout/check")

    assert response.status_code == 200
    data = response.get_json()
    assert calls == [True]
    assert data["success"] is True
    assert data["checked"] is False
    assert data["check_error"] == "no_credential"
    serialized = json.dumps(data).lower()
    assert "dispatch_token" not in serialized
    assert "server_status" not in serialized


@pytest.mark.parametrize(
    ("raw_phase", "expected_phase"),
    [
        ("pending", thinking_copy.SCOUT_STATE_REQUESTED),
        ("revoked", thinking_copy.SCOUT_STATE_ENDED),
        ("error", thinking_copy.SCOUT_STATE_REPAIR_NEEDED),
    ],
)
def test_terminal_phase_remap(
    thinking_client,
    monkeypatch: pytest.MonkeyPatch,
    raw_phase: str,
    expected_phase: str,
) -> None:
    monkeypatch.setattr(
        scout_handoff,
        "run_scout_handoff",
        lambda **_kwargs: operations.HandoffResult(
            raw_phase,
            "next step",
            raw_phase == "error",
            True,
            None,
        ),
    )

    response = thinking_client.post("/app/thinking/api/scout/enable")

    assert response.status_code == 202
    _wait_until(
        lambda: _scout_status(thinking_client)["operation"]["phase"] == expected_phase
    )
    assert _scout_status(thinking_client)["operation"]["phase"] == expected_phase


def test_status_payload_key_set(thinking_client) -> None:
    data = _scout_status(thinking_client)

    assert set(data) == {
        "success",
        "service",
        "state",
        "guidance",
        "provenance",
        "actions",
        "operation",
        "checked",
        "checked_at",
        "check_error",
    }
    assert set(scout_lane.status_payload()) == {
        "service",
        "state",
        "guidance",
        "provenance",
        "actions",
        "operation",
        "checked",
        "checked_at",
        "check_error",
    }
