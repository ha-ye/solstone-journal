# SPDX-License-Identifier: AGPL-3.0-only
# Copyright (c) 2026 sol pbc

from __future__ import annotations

import json
import threading
import time
from pathlib import Path

import pytest

from solstone.convey import create_app
from solstone.think.journal_config import write_journal_config
from solstone.think.services import operations, spp, spp_handoff


def _payload(suffix: str = "one") -> dict[str, str]:
    return {
        "endpoint_url": f"https://spp-{suffix}.example.test/v1",
        "served_model_id": f"confidential-model-{suffix}",
        "credential": f"credential-{suffix}",
        "account_id": f"acct-{suffix}",
        "created_at": "2026-05-24T00:00:00Z",
    }


def _read_config(journal: Path) -> dict:
    return json.loads((journal / "config" / "journal.json").read_text("utf-8"))


def _write_config(payload: dict) -> None:
    payload.setdefault("setup", {"completed_at": 1700000000000})
    write_journal_config(payload)


def _clear_confidential(journal: Path) -> None:
    config = _read_config(journal)
    config.setdefault("services", {}).pop("confidential", None)
    config.setdefault("providers", {}).pop("local", None)
    _write_config(config)


@pytest.fixture
def thinking_client(journal_copy: Path):
    _clear_confidential(journal_copy)
    app = create_app(journal=str(journal_copy.resolve()))
    app.config["TESTING"] = True
    return app.test_client()


@pytest.fixture(autouse=True)
def _stable_handoff_url(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(
        spp_handoff,
        "build_confidential_handoff_url",
        lambda: (
            "http://portal.test/enable/spp?nonce=NONCE",
            "NONCE",
            "http://portal.test",
        ),
    )


def _wait_until(predicate, timeout: float = 2.0) -> None:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if predicate():
            return
        time.sleep(0.01)
    raise AssertionError("condition not met before timeout")


def _providers(client) -> dict:
    response = client.get("/app/thinking/api/providers")
    assert response.status_code == 200
    return response.get_json()


def test_enable_confidential_returns_operation_and_lands_not_verified(
    thinking_client,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    def runner(**_kwargs):
        spp.provision_confidential_handoff(_payload())
        return operations.HandoffResult(
            "enabled",
            "Hardware attestation is not yet verified.",
            False,
        )

    monkeypatch.setattr(spp_handoff, "run_confidential_handoff", runner)

    response = thinking_client.post("/app/thinking/api/confidential/enable")

    assert response.status_code == 202
    data = response.get_json()
    assert data["service"] == "spp"
    assert data["operation"]["phase"] == "starting"
    assert (
        data["operation"]["portal_url"] == "http://portal.test/enable/spp?nonce=NONCE"
    )
    _wait_until(
        lambda: (
            _providers(thinking_client)["active_lane"]["confidential_operation"][
                "phase"
            ]
            == "not_verified"
        )
    )
    payload = _providers(thinking_client)
    assert payload["active_lane"]["lane"] == "confidential"
    assert payload["active_lane"]["confidential_enabled"] is True
    assert payload["active_lane"]["confidential_provenance_configured"] is True
    assert payload["active_lane"]["confidential_operation"]["phase"] == "not_verified"
    assert payload["active_lane"]["confidential_attestation"] == {
        "state": "verifying",
        "provenance": None,
        "reason": "attestation_not_yet_verified",
    }


def test_service_busy_for_second_confidential_operation(
    thinking_client,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    started = threading.Event()
    release = threading.Event()

    def runner(**_kwargs):
        started.set()
        release.wait(2)
        return operations.HandoffResult("enabled", None, False)

    monkeypatch.setattr(spp_handoff, "run_confidential_handoff", runner)

    first = thinking_client.post("/app/thinking/api/confidential/enable")
    _wait_until(started.is_set)
    second = thinking_client.post("/app/thinking/api/confidential/enable")
    release.set()

    assert first.status_code == 202
    assert second.status_code == 503
    assert second.get_json()["reason_code"] == "service_busy"


def test_enable_confidential_rejects_when_provenance_exists(thinking_client) -> None:
    spp.provision_confidential_handoff(_payload())

    response = thinking_client.post("/app/thinking/api/confidential/enable")

    assert response.status_code == 400
    assert response.get_json()["reason_code"] == "invalid_operation_for_state"


def test_disable_confidential_restores_synchronously(
    thinking_client, journal_copy: Path
):
    spp.provision_confidential_handoff(_payload("disable"))

    response = thinking_client.post("/app/thinking/api/confidential/disable")

    assert response.status_code == 200
    data = response.get_json()
    assert data["service"] == "spp"
    assert data["result"] == {"was_enabled": True, "credential_preserved": False}
    config = _read_config(journal_copy)
    assert config["providers"]["generate"]["provider"] == "google"
    assert config["providers"]["cogitate"]["provider"] == "openai"
    assert config["providers"]["local"] == {}
    assert "confidential" not in config["services"]


def test_confidential_routes_and_provider_payload_are_secret_free(
    thinking_client,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    def runner(**_kwargs):
        spp.provision_confidential_handoff(_payload("secret"))
        return operations.HandoffResult("enabled", "not yet verified", False)

    monkeypatch.setattr(spp_handoff, "run_confidential_handoff", runner)

    start = thinking_client.post("/app/thinking/api/confidential/enable")
    assert start.status_code == 202
    _wait_until(
        lambda: _providers(thinking_client)["active_lane"][
            "confidential_provenance_configured"
        ]
    )
    providers_enabled = thinking_client.get("/app/thinking/api/providers")
    disable = thinking_client.post("/app/thinking/api/confidential/disable")
    providers_disabled = thinking_client.get("/app/thinking/api/providers")
    serialized = "\n".join(
        [
            start.get_data(as_text=True),
            providers_enabled.get_data(as_text=True),
            disable.get_data(as_text=True),
            providers_disabled.get_data(as_text=True),
        ]
    )

    assert "credential-secret" not in serialized
    assert "acct-secret" not in serialized
    assert "credential_fingerprint_sha256" not in serialized
