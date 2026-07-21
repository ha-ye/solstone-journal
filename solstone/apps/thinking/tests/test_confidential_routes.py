# SPDX-License-Identifier: AGPL-3.0-only
# Copyright (c) 2026 sol pbc

from __future__ import annotations

import json
import threading
import time
from datetime import datetime, timedelta, timezone
from pathlib import Path
from unittest.mock import Mock

import pytest

from solstone.apps.thinking.google_model_pins import (
    GOOGLE_MODEL_RESOLUTION_TARGETS_FIELD,
    GOOGLE_PRO_ALIAS,
)
from solstone.convey import create_app
from solstone.think.journal_io import LockTimeout
from solstone.think.models import LOCAL_MODEL
from solstone.think.providers.brain_state import (
    begin_brain_refresh,
    brain_fingerprint_key_path,
    brain_state_path,
    build_active_brain_fingerprint,
    finish_brain_refresh,
)
from solstone.think.services import operations, spp, spp_handoff, spp_transport
from solstone.think.services.spp_attest.cadence import AttestationSession
from tests.helpers.journal_config import seed_journal_config
from tests.helpers.module_mocks import inline_thread_constructor, module_mock


class _FakeChannel:
    def __init__(self, verdict: object) -> None:
        self.verdict = verdict
        self.closed = False

    def close(self) -> None:
        self.closed = True


class _FakeListener:
    def __init__(self) -> None:
        self.closed = False

    def close(self) -> None:
        self.closed = True


class _AliveThread:
    def is_alive(self) -> bool:
        return True


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
    seed_journal_config(payload)


def _ready_component(now: datetime) -> dict[str, str]:
    return {
        "status": "ok",
        "observed_at": now.isoformat(),
        "expires_at": (now + timedelta(hours=1)).isoformat(),
    }


def _write_ready_brain_record(journal: Path) -> None:
    now = datetime(2026, 1, 2, 3, 4, 5, tzinfo=timezone.utc)
    permit = begin_brain_refresh(now, journal_path=journal)
    assert permit is not None
    component = _ready_component(now)
    finish_brain_refresh(
        permit,
        {
            "configuration": component,
            "lane_prerequisites": component,
            "generate": component,
            "cogitate": component,
        },
        now,
        journal_path=journal,
    )


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
    monkeypatch.setattr(
        operations,
        "threading",
        module_mock(
            operations.threading,
            Thread=inline_thread_constructor(),
        ),
    )

    response = thinking_client.post("/app/thinking/api/confidential/enable")

    assert response.status_code == 202
    data = response.get_json()
    assert data["service"] == "spp"
    assert data["operation"]["phase"] == "starting"
    assert (
        data["operation"]["portal_url"] == "http://portal.test/enable/spp?nonce=NONCE"
    )
    payload = _providers(thinking_client)
    assert payload["active_lane"]["lane"] == "confidential"
    assert payload["active_lane"]["confidential_enabled"] is True
    assert payload["active_lane"]["confidential_provenance_configured"] is True
    assert payload["active_lane"]["confidential_operation"]["phase"] == "not_verified"
    assert payload["active_lane"]["confidential_attestation"] == {
        "state": "verifying",
        "provenance": None,
        "last_verified": None,
        "reason": "attestation_not_yet_verified",
    }


def test_enable_confidential_early_access_stays_off(
    thinking_client,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    def runner(**_kwargs):
        return operations.HandoffResult("early_access", None, False)

    monkeypatch.setattr(spp_handoff, "run_confidential_handoff", runner)
    monkeypatch.setattr(
        operations,
        "threading",
        module_mock(
            operations.threading,
            Thread=inline_thread_constructor(),
        ),
    )

    response = thinking_client.post("/app/thinking/api/confidential/enable")

    assert response.status_code == 202
    payload = _providers(thinking_client)
    assert payload["active_lane"]["confidential_enabled"] is False
    assert payload["active_lane"]["confidential_provenance_configured"] is False
    assert payload["active_lane"]["confidential_attestation"] == {
        "state": "off",
        "provenance": None,
        "last_verified": None,
        "reason": "confidential_not_configured",
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


def test_google_prior_active_advisory_save_restores_without_hijacking_lane(
    thinking_client,
    journal_copy: Path,
) -> None:
    config = _read_config(journal_copy)
    config["providers"]["active"] = {
        "provider": "google",
        "model": GOOGLE_PRO_ALIAS,
    }
    _write_config(config)
    spp.provision_confidential_handoff(_payload("prior-alias"))
    _write_ready_brain_record(journal_copy)

    before_config = _read_config(journal_copy)
    key = brain_fingerprint_key_path(journal_path=journal_copy).read_bytes()
    before_fingerprint = build_active_brain_fingerprint(before_config, hmac_key=key)
    before_brain_bytes = brain_state_path(journal_path=journal_copy).read_bytes()
    before = _providers(thinking_client)
    assert before["active_lane"]["lane"] == "confidential"
    assert before["active"] == {"provider": "local", "model": LOCAL_MODEL}
    assert before["configuration_guidance"] is not None
    assert before["configuration_guidance"][GOOGLE_MODEL_RESOLUTION_TARGETS_FIELD] == [
        "confidential_prior"
    ]

    exact_model = "gemini-3.5-flash"
    response = thinking_client.put(
        "/app/thinking/api/providers",
        json={
            "lane": "byo",
            "provider": "google",
            "model": exact_model,
            GOOGLE_MODEL_RESOLUTION_TARGETS_FIELD: ["confidential_prior"],
        },
    )

    assert response.status_code == 200
    after = response.get_json()
    stored = _read_config(journal_copy)
    assert after["active_lane"]["lane"] == "confidential"
    assert after["active"] == {"provider": "local", "model": LOCAL_MODEL}
    assert after["configuration_guidance"] is None
    assert stored["providers"]["active"] == before_config["providers"]["active"]
    assert stored["providers"]["local"] == before_config["providers"]["local"]
    assert stored["providers"]["byo_models"]["google"] == exact_model
    before_byo_models = before_config["providers"].get("byo_models", {})
    after_byo_models = stored["providers"].get("byo_models", {})
    assert {
        key: value for key, value in after_byo_models.items() if key != "google"
    } == {key: value for key, value in before_byo_models.items() if key != "google"}
    before_confidential = before_config["services"]["confidential"]
    after_confidential = stored["services"]["confidential"]
    assert {
        key: value for key, value in after_confidential.items() if key != "prior_active"
    } == {
        key: value
        for key, value in before_confidential.items()
        if key != "prior_active"
    }
    assert after_confidential["prior_active"]["provider"] == "google"
    assert before_confidential["prior_active"]["model"] == GOOGLE_PRO_ALIAS
    assert after_confidential["prior_active"]["model"] == exact_model
    after_fingerprint = build_active_brain_fingerprint(stored, hmac_key=key)
    assert before_fingerprint["active_lane"] == "spp"
    assert after_fingerprint["active_lane"] == "spp"
    assert (
        before_fingerprint["fingerprint_sha256"]
        == after_fingerprint["fingerprint_sha256"]
    )
    assert (
        brain_state_path(journal_path=journal_copy).read_bytes() == before_brain_bytes
    )


def test_normal_byo_model_save_still_switches_when_confidential_active(
    thinking_client,
    journal_copy: Path,
) -> None:
    config = _read_config(journal_copy)
    config["providers"]["active"] = {
        "provider": "google",
        "model": GOOGLE_PRO_ALIAS,
    }
    _write_config(config)
    spp.provision_confidential_handoff(_payload("normal-byo"))

    response = thinking_client.put(
        "/app/thinking/api/providers",
        json={
            "lane": "byo",
            "provider": "google",
            "model": "gemini-3.5-flash",
        },
    )

    assert response.status_code == 200
    payload = response.get_json()
    stored = _read_config(journal_copy)
    assert payload["active_lane"]["lane"] == "byo"
    assert stored["providers"]["active"] == {
        "provider": "google",
        "model": "gemini-3.5-flash",
    }
    assert (
        stored["services"]["confidential"]["prior_active"]["model"] == GOOGLE_PRO_ALIAS
    )


def test_google_model_resolution_rejects_unknown_target_without_config_write(
    thinking_client,
    journal_copy: Path,
) -> None:
    config = _read_config(journal_copy)
    config["providers"]["active"] = {
        "provider": "google",
        "model": GOOGLE_PRO_ALIAS,
    }
    _write_config(config)
    spp.provision_confidential_handoff(_payload("bad-token"))
    config_path = journal_copy / "config" / "journal.json"
    before = config_path.read_bytes()

    response = thinking_client.put(
        "/app/thinking/api/providers",
        json={
            "lane": "byo",
            "provider": "google",
            "model": "gemini-3.5-flash",
            GOOGLE_MODEL_RESOLUTION_TARGETS_FIELD: ["not_real"],
        },
    )

    assert response.status_code == 400
    payload = response.get_json()
    assert payload["reason_code"] == "invalid_config_value"
    assert payload["detail"] == (
        "Invalid Google model resolution targets: not_real. Must be one of: "
        "active, confidential_prior, remembered"
    )
    assert config_path.read_bytes() == before
    assert _providers(thinking_client)["configuration_guidance"] is not None


def test_google_model_resolution_stale_confidential_target_remembers_without_hijack(
    thinking_client,
    journal_copy: Path,
) -> None:
    config = _read_config(journal_copy)
    config["providers"]["active"] = {
        "provider": "google",
        "model": GOOGLE_PRO_ALIAS,
    }
    _write_config(config)
    spp.provision_confidential_handoff(_payload("stale-confidential-target"))
    config = _read_config(journal_copy)
    config["providers"]["byo_models"] = {}
    config["services"]["confidential"]["prior_active"]["model"] = "gemini-3.5-flash"
    _write_config(config)
    before_config = _read_config(journal_copy)

    response = thinking_client.put(
        "/app/thinking/api/providers",
        json={
            "lane": "byo",
            "provider": "google",
            "model": "gemini-3.5-flash",
            GOOGLE_MODEL_RESOLUTION_TARGETS_FIELD: ["confidential_prior"],
        },
    )

    assert response.status_code == 200
    payload = response.get_json()
    stored = _read_config(journal_copy)
    assert payload["active_lane"]["lane"] == "confidential"
    assert stored["providers"]["active"] == before_config["providers"]["active"]
    assert stored["providers"]["local"] == before_config["providers"]["local"]
    assert stored["providers"]["byo_models"]["google"] == "gemini-3.5-flash"
    assert (
        stored["services"]["confidential"]["prior_active"]
        == before_config["services"]["confidential"]["prior_active"]
    )
    assert payload["configuration_guidance"] is None


def test_google_model_resolution_stale_active_target_saves_normally(
    thinking_client,
    journal_copy: Path,
) -> None:
    config = _read_config(journal_copy)
    config["providers"]["active"] = {
        "provider": "local",
        "model": "local/qwen3.5-4b",
    }
    config["providers"]["byo_models"] = {}
    config.setdefault("services", {}).pop("confidential", None)
    config["providers"].pop("local", None)
    _write_config(config)

    response = thinking_client.put(
        "/app/thinking/api/providers",
        json={
            "lane": "byo",
            "provider": "google",
            "model": "gemini-3.5-flash",
            GOOGLE_MODEL_RESOLUTION_TARGETS_FIELD: ["active"],
        },
    )

    assert response.status_code == 200
    payload = response.get_json()
    stored = _read_config(journal_copy)
    assert payload["active_lane"]["lane"] == "byo"
    assert stored["providers"]["active"] == {
        "provider": "google",
        "model": "gemini-3.5-flash",
    }
    assert stored["providers"]["byo_models"]["google"] == "gemini-3.5-flash"
    assert payload["configuration_guidance"] is None


def test_google_model_resolution_repeated_restore_request_is_noop(
    thinking_client,
    journal_copy: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    actions: list[dict] = []
    monkeypatch.setattr(
        "solstone.apps.thinking.routes.log_app_action",
        lambda **kwargs: actions.append(kwargs),
    )
    config = _read_config(journal_copy)
    config["providers"]["active"] = {
        "provider": "google",
        "model": GOOGLE_PRO_ALIAS,
    }
    _write_config(config)
    spp.provision_confidential_handoff(_payload("repeat-restore"))
    config_path = journal_copy / "config" / "journal.json"
    request_payload = {
        "lane": "byo",
        "provider": "google",
        "model": "gemini-3.5-flash",
        GOOGLE_MODEL_RESOLUTION_TARGETS_FIELD: ["confidential_prior"],
    }

    first = thinking_client.put("/app/thinking/api/providers", json=request_payload)

    assert first.status_code == 200
    assert len(actions) == 1
    before_second = config_path.read_bytes()

    second = thinking_client.put("/app/thinking/api/providers", json=request_payload)

    assert second.status_code == 200
    assert config_path.read_bytes() == before_second
    assert len(actions) == 1
    assert second.get_json()["configuration_guidance"] is None


@pytest.mark.parametrize("reason_code", ["model_not_found", "key_missing"])
def test_google_model_probe_failure_preserves_prior_alias(
    thinking_client,
    journal_copy: Path,
    monkeypatch: pytest.MonkeyPatch,
    reason_code: str,
) -> None:
    config = _read_config(journal_copy)
    config["providers"]["active"] = {
        "provider": "google",
        "model": GOOGLE_PRO_ALIAS,
    }
    if reason_code == "key_missing":
        config.get("env", {}).pop("GOOGLE_API_KEY", None)
    _write_config(config)
    spp.provision_confidential_handoff(_payload(f"probe-{reason_code}"))
    config_path = journal_copy / "config" / "journal.json"
    before = config_path.read_bytes()
    if reason_code != "key_missing":
        monkeypatch.setattr(
            "solstone.apps.thinking.routes.validate_model",
            lambda provider, model, api_key: {
                "valid": False,
                "reason_code": reason_code,
                "error": "model did not answer",
            },
        )

    response = thinking_client.post(
        "/app/thinking/api/validate-model",
        json={"provider": "google", "model": "gemini-3.5-flash"},
    )

    assert response.status_code == 200
    payload = response.get_json()
    assert payload["valid"] is False
    assert payload["reason_code"] == reason_code
    assert config_path.read_bytes() == before
    assert _providers(thinking_client)["configuration_guidance"] is not None


def test_google_model_resolution_lock_timeout_preserves_config_and_guidance(
    thinking_client,
    journal_copy: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    config = _read_config(journal_copy)
    config["providers"]["active"] = {
        "provider": "google",
        "model": GOOGLE_PRO_ALIAS,
    }
    _write_config(config)
    spp.provision_confidential_handoff(_payload("lock-timeout"))
    config_path = journal_copy / "config" / "journal.json"
    before = config_path.read_bytes()
    monkeypatch.setattr(
        "solstone.apps.thinking.routes.mutate_journal_config",
        lambda apply: (_ for _ in ()).throw(
            LockTimeout(path=config_path, timeout=0.01)
        ),
    )

    response = thinking_client.put(
        "/app/thinking/api/providers",
        json={
            "lane": "byo",
            "provider": "google",
            "model": "gemini-3.5-flash",
            GOOGLE_MODEL_RESOLUTION_TARGETS_FIELD: ["confidential_prior"],
        },
    )

    assert response.status_code == 503
    assert response.get_json()["reason_code"] == "config_busy"
    assert config_path.read_bytes() == before
    assert _providers(thinking_client)["configuration_guidance"] is not None


def test_disable_confidential_restores_synchronously(
    thinking_client, journal_copy: Path, monkeypatch: pytest.MonkeyPatch
):
    spp.provision_confidential_handoff(_payload("disable"))
    now = datetime.now(timezone.utc)
    spp.record_attestation_verified(
        AttestationSession(
            verdict=object(),
            started_at=now,
            tpm_heartbeat_at=now,
            gpu_reattest_at=now,
        )
    )
    old_channel = _FakeChannel("old")
    old_listener = _FakeListener()
    spp_transport._POOL[:] = [old_channel]
    spp_transport._LISTENER = old_listener
    spp_transport._LISTENER_THREAD = _AliveThread()
    spp_transport._FORWARDER_BASE_URL = "http://127.0.0.1:1111"
    spp_transport._CONFIDENTIAL_BLOCK = {
        "endpoint_url": "https://spp-disable.example.test/v1"
    }

    response = thinking_client.post("/app/thinking/api/confidential/disable")

    assert response.status_code == 200
    data = response.get_json()
    assert data["service"] == "spp"
    assert data["result"] == {"was_enabled": True, "credential_preserved": False}
    config = _read_config(journal_copy)
    assert config["providers"]["active"] == {
        "provider": "google",
        "model": "gemini-3.5-flash",
    }
    assert config["providers"]["local"] == {}
    assert "confidential" not in config["services"]
    state = spp.get_attestation_state()
    assert state.session is None
    assert state.failure is None
    assert state.last_verified is None
    assert old_channel.closed is True
    assert old_listener.closed is True
    assert spp_transport._POOL == []
    assert spp_transport._LISTENER is None
    assert spp_transport._LISTENER_THREAD is None
    assert spp_transport._FORWARDER_BASE_URL is None
    assert spp_transport._CONFIDENTIAL_BLOCK is None

    fresh_channel = _FakeChannel("fresh")
    establish = Mock(return_value=fresh_channel)

    def fake_start_listener() -> None:
        spp_transport._LISTENER = _FakeListener()
        spp_transport._LISTENER_THREAD = _AliveThread()
        spp_transport._FORWARDER_BASE_URL = "http://127.0.0.1:2222"

    monkeypatch.setattr(spp_transport, "establish_attested_channel", establish)
    monkeypatch.setattr(spp_transport, "_start_listener_locked", fake_start_listener)
    spp.provision_confidential_handoff(_payload("fresh"))

    provenance = spp.confidential_provenance()
    assert provenance is not None
    spp_transport.verify_confidential_attestation(provenance)

    assert establish.call_args.args[0].host == "spp-fresh.example.test"
    assert spp_transport._POOL == [fresh_channel]
    assert old_channel not in spp_transport._POOL
    assert spp_transport._CONFIDENTIAL_BLOCK == provenance


def test_recheck_confidential_rejects_when_off(thinking_client) -> None:
    response = thinking_client.post("/app/thinking/api/confidential/recheck")

    assert response.status_code == 400
    assert response.get_json()["reason_code"] == "invalid_operation_for_state"


def test_recheck_confidential_returns_refreshed_provider_state(
    thinking_client,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    spp.provision_confidential_handoff(_payload("recheck"))

    def recheck() -> None:
        spp.record_attestation_failed("failed", "gpu_nonce_mismatch")

    monkeypatch.setattr(spp_transport, "recheck_confidential_attestation", recheck)

    response = thinking_client.post("/app/thinking/api/confidential/recheck")

    assert response.status_code == 200
    payload = response.get_json()
    assert payload["active_lane"]["confidential_enabled"] is True
    assert payload["active_lane"]["confidential_attestation"] == {
        "state": "failed",
        "provenance": None,
        "last_verified": None,
        "reason": "attestation_failed",
    }


def test_confidential_routes_and_provider_payload_are_secret_free(
    thinking_client,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    def runner(**_kwargs):
        spp.provision_confidential_handoff(_payload("secret"))
        return operations.HandoffResult("enabled", "not yet verified", False)

    monkeypatch.setattr(spp_handoff, "run_confidential_handoff", runner)
    monkeypatch.setattr(
        operations,
        "threading",
        module_mock(
            operations.threading,
            Thread=inline_thread_constructor(),
        ),
    )

    start = thinking_client.post("/app/thinking/api/confidential/enable")
    assert start.status_code == 202
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
