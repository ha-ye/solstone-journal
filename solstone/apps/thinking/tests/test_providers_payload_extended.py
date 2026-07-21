# SPDX-License-Identifier: AGPL-3.0-only
# Copyright (c) 2026 sol pbc

from __future__ import annotations

import json
from pathlib import Path
from typing import get_args

import pytest

from solstone.apps.thinking import routes
from solstone.apps.thinking.google_model_pins import (
    GOOGLE_MODEL_RESOLUTION_TARGETS_FIELD,
    THINKING_BYO_MODEL_HREF,
)
from solstone.apps.thinking.local_bootstrap import LOCAL_MODEL_SPECS
from solstone.apps.thinking.model_tiers import MODEL_TIERS
from solstone.convey import provider_readiness
from solstone.think.brain_health import HEADLINES
from solstone.think.models import LOCAL_MODEL, NO_BRAIN_PROVIDER, resolve_provider
from solstone.think.providers.artifact_proof import ReadinessOutcome
from solstone.think.providers.brain_state import BRAIN_REASON_CODES
from solstone.think.providers.install_state import InstallState

INSTALL_STATUS_FIELDS = {
    "name",
    "install_state",
    "last_transition_at",
    "last_progress_at",
    "progress_bytes_received",
    "progress_bytes_total",
    "install_error",
}
CANONICAL_INSTALL_STATES = set(get_args(InstallState))
REMOVED_PROVIDER = "mlx"


@pytest.fixture
def settings_client(settings_env, thinking_app):
    client, _journal_path = _settings_client_with_journal(settings_env, thinking_app)
    return client


@pytest.fixture
def settings_client_with_journal(settings_env, thinking_app):
    return _settings_client_with_journal(settings_env, thinking_app)


def _settings_client_with_journal(settings_env, thinking_app):
    from solstone.convey import state

    journal_path, config = settings_env()
    config["setup"] = {"completed_at": 1700000000000}
    (journal_path / "config" / "journal.json").write_text(
        json.dumps(config, indent=2) + "\n",
        encoding="utf-8",
    )
    state.journal_root = str(journal_path)
    return thinking_app.test_client(), journal_path


def _write_config(journal_path, config: dict) -> None:
    (journal_path / "config" / "journal.json").write_text(
        json.dumps(config, indent=2) + "\n",
        encoding="utf-8",
    )


def _assert_install_status(payload: dict) -> None:
    assert INSTALL_STATUS_FIELDS <= set(payload)
    assert payload["install_state"] in CANONICAL_INSTALL_STATES


def _local_readiness_gpu_probe_failed() -> ReadinessOutcome:
    return ReadinessOutcome(
        provider="local",
        status="ready",
        reason_code="ready",
        target={"model_id": LOCAL_MODEL},
        install={
            "install_state": "installed",
            "install_error": None,
            "error_code": None,
            "attempt_id": None,
            "progress_bytes_received": None,
            "progress_bytes_total": None,
            "last_transition_at": None,
            "last_progress_at": None,
        },
        host={
            "ram_sufficient": True,
            "gpu_available": True,
            "gpu_probe_ok": False,
            "backend": "vulkan",
            "backend_reason": "test vulkan",
        },
        artifacts={
            "binary_installed": True,
            "model_installed": True,
            "binary_path": "/tmp/llama-server",
            "model_path": "/tmp/model.gguf",
            "model_id": LOCAL_MODEL,
        },
        proof={
            "binary": {"status": "ready", "reason_code": "ready", "cache_hit": False},
            "model": {"status": "ready", "reason_code": "ready", "cache_hit": False},
        },
    )


def _patch_selected_providers(monkeypatch, *, provider: str = "google") -> None:
    monkeypatch.setattr(
        "solstone.think.models.resolve_provider",
        lambda _interface: (provider, f"{provider}-model"),
    )


def _brain_payload(
    *,
    state: str = "ready",
    headline: str = HEADLINES["ready"],
    reason_code: str | None = None,
    reason_text: str | None = None,
    action: dict[str, object] | None = None,
) -> dict[str, object]:
    return {
        "state": state,
        "headline": headline,
        "reason_code": reason_code,
        "reason_text": reason_text,
        "failing_component": "generate" if reason_code else None,
        "action": action,
        "identity": {"lane": "cloud", "provider": "google", "model": "gemini"},
        "evidence": {
            "observed_at": "2026-04-10T12:00:00Z",
            "age_seconds": 60,
            "age_text": "1m",
        },
        "components": {
            "generate": {
                "status": state,
                "reason_code": reason_code,
                "reason_text": reason_text,
                "observed_at": "2026-04-10T12:00:00Z",
            },
            "cogitate": {
                "status": "ready",
                "reason_code": None,
                "reason_text": None,
                "observed_at": "2026-04-10T12:00:00Z",
            },
        },
        "progressing": False,
    }


def _presentation(
    snapshot: dict[str, object] | None = None,
    *,
    spp_active: bool = False,
    spp_readiness: dict[str, object] | None = None,
    confidential_attestation: dict[str, object] | None = None,
) -> dict[str, object]:
    return {
        "brain": snapshot or _brain_payload(),
        "spp_active": spp_active,
        "spp_readiness": spp_readiness
        or {
            "generate_ready": False,
            "cogitate_ready": False,
            "issues": ["brain_record_missing"],
        },
        "confidential_attestation": confidential_attestation
        or {
            "state": "off",
            "reason": "confidential_not_configured",
            "observed_at": None,
        },
    }


def _patch_brain(monkeypatch, snapshot: dict[str, object] | None = None) -> None:
    monkeypatch.setattr(
        routes,
        "build_brain_presentation",
        lambda *_args, **_kwargs: _presentation(snapshot),
    )


def _assert_brain_shape(payload: dict) -> None:
    brain = payload["brain"]
    assert set(brain) == {
        "state",
        "headline",
        "reason_code",
        "reason_text",
        "failing_component",
        "action",
        "identity",
        "evidence",
        "components",
        "progressing",
    }
    assert set(brain["identity"]) == {"lane", "provider", "model"}
    assert set(brain["evidence"]) == {"observed_at", "age_seconds", "age_text"}
    assert set(brain["components"]) == {"generate", "cogitate"}


def test_get_providers_includes_local_install_state(settings_client, monkeypatch):
    _patch_brain(monkeypatch)

    response = settings_client.get("/app/thinking/api/providers")

    assert response.status_code == 200
    payload = response.get_json()
    assert "bundled" not in payload
    assert "brain" in payload
    assert isinstance(payload["local"], dict)
    assert payload["local_override"] == {
        "enabled": False,
        "endpoint_url": "",
        "served_model_id": "",
        "credential_configured": False,
    }
    assert REMOVED_PROVIDER not in payload
    _assert_install_status(payload["local"])


def test_get_providers_reports_active_personal_cloud_lane(
    settings_client,
):
    response = settings_client.get("/app/thinking/api/providers")

    assert response.status_code == 200
    payload = response.get_json()
    assert payload["active_lane"]["lane"] == "byo"
    assert payload["active"] == {
        "provider": "google",
        "model": "gemini-3.5-flash",
    }


def test_get_providers_reports_none_lane_when_no_engine_selected(
    settings_env,
    monkeypatch,
    thinking_app,
):
    journal_path, config = settings_env(
        {
            "setup": {"completed_at": 1700000000000},
            "env": {},
            "providers": {"contexts": {}, "models": {}},
        }
    )
    _patch_brain(monkeypatch)
    client, _journal_path = _settings_client_with_journal(
        lambda: (journal_path, config), thinking_app
    )

    response = client.get("/app/thinking/api/providers")

    assert response.status_code == 200
    payload = response.get_json()
    assert payload["active_lane"]["lane"] == NO_BRAIN_PROVIDER
    assert payload["active"]["provider"] == NO_BRAIN_PROVIDER


def test_get_providers_reports_one_profile_for_both_interfaces(
    settings_client_with_journal,
):
    client, journal_path = settings_client_with_journal
    config_path = journal_path / "config" / "journal.json"
    config = json.loads(config_path.read_text())
    config["providers"]["active"] = {
        "provider": "openai",
        "model": "gpt-5.4-mini",
    }
    _write_config(journal_path, config)

    response = client.get("/app/thinking/api/providers")

    assert response.status_code == 200
    payload = response.get_json()
    assert payload["active_lane"]["lane"] == "byo"
    assert payload["active"] == config["providers"]["active"]


def test_provider_update_rejects_context_payload(settings_client_with_journal):
    client, _journal_path = settings_client_with_journal

    response = client.put(
        "/app/thinking/api/providers",
        json={"contexts": {"talent.x": {"provider": "local", "model": ""}}},
    )

    assert response.status_code != 200
    payload = response.get_json()
    assert payload["reason_code"] == "invalid_config_value"
    assert payload["detail"] == "Unknown provider fields: contexts"


def test_scout_enabled_google_provider_derives_byo_with_provenance(
    settings_client_with_journal,
):
    client, journal_path = settings_client_with_journal
    config_path = journal_path / "config" / "journal.json"
    config = json.loads(config_path.read_text())
    config.setdefault("env", {})["GOOGLE_API_KEY"] = "scout-key"
    config.setdefault("services", {})["scout"] = {
        "enabled_at": "2026-05-23T00:00:00Z",
        "key_fingerprint_sha256": "fingerprint",
    }
    config["providers"]["active"] = {
        "provider": "google",
        "model": "gemini-3.5-flash",
    }
    _write_config(journal_path, config)

    response = client.get("/app/thinking/api/providers")

    assert response.status_code == 200
    payload = response.get_json()
    assert payload["active_lane"]["lane"] == "byo"
    assert payload["active_lane"]["scout_enabled"] is True
    assert payload["active_lane"]["scout_provenance_configured"] is True


def test_byo_gemini_key_write_succeeds_when_scout_enabled(
    settings_client_with_journal,
    monkeypatch,
):
    client, journal_path = settings_client_with_journal
    config_path = journal_path / "config" / "journal.json"
    config = json.loads(config_path.read_text())
    config.setdefault("env", {})["GOOGLE_API_KEY"] = "scout-key"
    config.setdefault("services", {})["scout"] = {
        "enabled_at": "2026-05-23T00:00:00Z",
        "key_fingerprint_sha256": "fingerprint",
    }
    _write_config(journal_path, config)
    monkeypatch.setattr(
        "solstone.apps.thinking.routes.validate_key",
        lambda _provider, _api_key: {"valid": True},
    )

    response = client.put(
        "/app/thinking/api/keys",
        json={"env_var": "GOOGLE_API_KEY", "value": "manual-key"},
    )

    assert response.status_code == 200
    config = json.loads(config_path.read_text())
    assert config["env"]["GOOGLE_API_KEY"] == "manual-key"


def test_key_clear_pops_remembered_byo_model(
    settings_client_with_journal,
):
    client, journal_path = settings_client_with_journal
    config_path = journal_path / "config" / "journal.json"
    config = json.loads(config_path.read_text())
    config["providers"]["byo_models"] = {
        "google": "gemini-3.5-flash",
        "openai": "gpt-5.5",
    }
    _write_config(journal_path, config)

    response = client.put(
        "/app/thinking/api/keys",
        json={"env_var": "GOOGLE_API_KEY", "value": ""},
    )

    assert response.status_code == 200
    stored = json.loads(config_path.read_text())
    assert stored["providers"]["byo_models"] == {"openai": "gpt-5.5"}


def test_key_save_preserves_remembered_byo_model(
    settings_client_with_journal,
    monkeypatch,
):
    client, journal_path = settings_client_with_journal
    config_path = journal_path / "config" / "journal.json"
    config = json.loads(config_path.read_text())
    config["providers"]["byo_models"] = {"openai": "gpt-5.5"}
    _write_config(journal_path, config)
    monkeypatch.setattr(
        "solstone.apps.thinking.routes.validate_key",
        lambda _provider, _api_key: {"valid": True},
    )

    response = client.put(
        "/app/thinking/api/keys",
        json={"env_var": "OPENAI_API_KEY", "value": "manual-openai-key"},
    )

    assert response.status_code == 200
    stored = json.loads(config_path.read_text())
    assert stored["providers"]["byo_models"] == {"openai": "gpt-5.5"}


def test_validate_all_does_not_cache_result_for_a_concurrently_replaced_key(
    settings_client_with_journal,
    monkeypatch,
):
    client, journal_path = settings_client_with_journal
    config_path = journal_path / "config" / "journal.json"
    current = json.loads(config_path.read_text())
    current["env"]["GOOGLE_API_KEY"] = "new-key"
    current["providers"]["key_validation"]["google"] = {
        "valid": True,
        "timestamp": "new-key-result",
    }
    _write_config(journal_path, current)
    stale = json.loads(json.dumps(current))
    stale["env"]["GOOGLE_API_KEY"] = "old-key"
    monkeypatch.setattr(
        "solstone.apps.thinking.routes.get_journal_config",
        lambda: stale,
    )
    monkeypatch.setattr(
        "solstone.apps.thinking.routes.validate_key",
        lambda _provider, _api_key: {"valid": False, "error": "old key"},
    )

    response = client.post("/app/thinking/api/validate-keys")

    assert response.status_code == 200
    stored = json.loads(config_path.read_text())
    assert stored["providers"]["key_validation"]["google"] == {
        "valid": True,
        "timestamp": "new-key-result",
    }
    assert response.get_json()["key_validation"]["google"]["valid"] is True


def test_thinking_status_payloads_are_secret_free_with_scout_provenance(
    settings_client_with_journal, monkeypatch
):
    client, journal_path = settings_client_with_journal
    config_path = journal_path / "config" / "journal.json"
    config = json.loads(config_path.read_text())
    raw_values = {
        "google-secret-key",
        "openai-secret-key",
        "anthropic-secret-key",
        "scout-account-secret",
        "dispatch-token-secret",
        "fingerprint-secret",
        "confidential-account-secret",
        "confidential-credential-secret",
        "confidential-fingerprint-secret",
        "2026-05-24T00:00:00Z",
        "2026-05-23T00:00:00Z",
    }
    config.setdefault("env", {}).update(
        {
            "GOOGLE_API_KEY": "google-secret-key",
            "OPENAI_API_KEY": "openai-secret-key",
            "ANTHROPIC_API_KEY": "anthropic-secret-key",
        }
    )
    config.setdefault("services", {})["scout"] = {
        "enabled_at": "2026-05-23T00:00:00Z",
        "account_id": "scout-account-secret",
        "dispatch_token": "dispatch-token-secret",
        "key_fingerprint_sha256": "fingerprint-secret",
        "key_created_at": "2026-05-23T00:00:00Z",
    }
    config.setdefault("services", {})["confidential"] = {
        "enabled_at": "2026-05-24T00:00:00Z",
        "account_id": "confidential-account-secret",
        "endpoint_url": "https://spp.example.test",
        "served_model_id": "confidential-model",
        "credential_created_at": "2026-05-24T00:00:00Z",
        "credential_fingerprint_sha256": "confidential-fingerprint-secret",
        "prior_active": {"provider": "google", "model": "gemini-3.5-flash"},
        "prior_local_endpoint": None,
    }
    config.setdefault("providers", {}).setdefault("local", {})["credential"] = (
        "confidential-credential-secret"
    )
    config["providers"]["active"] = {
        "provider": "google",
        "model": "gemini-3.5-flash",
    }
    _write_config(journal_path, config)

    monkeypatch.setattr(
        "solstone.apps.thinking.routes.validate_key",
        lambda provider, _api_key: {
            "valid": False,
            "error": f"{provider} invalid",
            "reason_code": "provider_key_invalid",
        },
    )

    responses = [
        client.get("/app/thinking/api/providers"),
        client.get("/app/thinking/api/keys"),
        client.get("/app/thinking/api/validate-keys"),
        client.get("/app/thinking/api/providers/local/status"),
    ]

    for response in responses:
        assert response.status_code == 200
        body = response.get_data(as_text=True)
        for forbidden in raw_values | {
            "account_id",
            "dispatch_token",
            "key_fingerprint_sha256",
            "key_created_at",
            "credential_fingerprint_sha256",
            "credential_created_at",
        }:
            assert forbidden not in body

    providers_payload = responses[0].get_json()
    assert providers_payload["active_lane"] == {
        "lane": "byo",
        "scout_enabled": True,
        "scout_provenance_configured": True,
        "confidential_enabled": True,
        "confidential_audio": True,
        "confidential_provenance_configured": True,
        "confidential_operation": None,
        "confidential_attestation": {
            "state": "inactive",
            "reason": "confidential_not_active",
            "observed_at": None,
        },
    }


def test_providers_payload_omits_bundled_block(settings_client):
    response = settings_client.get("/app/thinking/api/providers")

    assert response.status_code == 200
    payload = response.get_json()
    assert "bundled" not in payload
    provider_status = payload["provider_status"]
    for name in ("google", "openai", "anthropic"):
        assert set(provider_status[name]) == {
            "provider",
            "configured",
            "generate_ready",
            "cogitate_ready",
            "issues",
        }
    assert set(provider_status["local"]) == {
        "configured",
        "selected",
        "generate_ready",
        "cogitate_ready",
        "issues",
    }
    assert REMOVED_PROVIDER not in payload
    _assert_install_status(payload["local"])


def test_providers_payload_omits_auth(settings_client):
    response = settings_client.get("/app/thinking/api/providers")

    assert response.status_code == 200
    payload = response.get_json()
    assert "auth" not in payload


def test_providers_payload_includes_model_tiers_and_byo_models_as_is(
    settings_client_with_journal,
):
    client, journal_path = settings_client_with_journal
    config_path = journal_path / "config" / "journal.json"
    config = json.loads(config_path.read_text())
    config["providers"]["byo_models"] = {
        "google": "gemini-3.5-flash",
        "unexpected": "kept-as-is",
    }
    _write_config(journal_path, config)

    response = client.get("/app/thinking/api/providers")

    assert response.status_code == 200
    payload = response.get_json()
    assert payload["model_tiers"] == MODEL_TIERS
    assert payload["byo_models"] == {
        "google": "gemini-3.5-flash",
        "unexpected": "kept-as-is",
    }
    assert payload["configuration_guidance"] is None


@pytest.mark.parametrize("slot", ["active", "byo", "prior"])
def test_providers_payload_configuration_guidance_for_google_pro_alias(
    settings_client_with_journal,
    slot: str,
):
    client, journal_path = settings_client_with_journal
    config_path = journal_path / "config" / "journal.json"
    config = json.loads(config_path.read_text())
    if slot == "active":
        config["providers"]["active"] = {
            "provider": "google",
            "model": "gemini-pro-latest",
        }
    elif slot == "byo":
        config["providers"]["byo_models"] = {"google": "gemini-pro-latest"}
    else:
        config.setdefault("services", {})["confidential"] = {
            "prior_active": {"provider": "google", "model": "gemini-pro-latest"}
        }
    _write_config(journal_path, config)

    response = client.get("/app/thinking/api/providers")

    assert response.status_code == 200
    guidance = response.get_json()["configuration_guidance"]
    expected_targets = {
        "active": ["active"],
        "byo": ["remembered"],
        "prior": ["confidential_prior"],
    }[slot]
    assert guidance == {
        "id": "choose_exact_gemini_model",
        "heading": "choose an exact Gemini model",
        GOOGLE_MODEL_RESOLUTION_TARGETS_FIELD: expected_targets,
        "action": {"label": "choose model", "href": THINKING_BYO_MODEL_HREF},
    }
    assert guidance["id"] not in BRAIN_REASON_CODES
    assert guidance["id"] not in provider_readiness.mapped_reason_codes()


def test_think_layer_does_not_import_thinking_model_tiers():
    for path in Path("solstone/think").rglob("*.py"):
        assert "model_tiers" not in path.read_text(encoding="utf-8"), path


def test_providers_payload_includes_secret_free_local_override(
    settings_client_with_journal,
    monkeypatch,
):
    client, journal_path = settings_client_with_journal
    config = json.loads((journal_path / "config" / "journal.json").read_text())
    config["providers"]["local"] = {
        "endpoint_url": "http://host.test:8080/openai/v1",
        "served_model_id": "served-model",
        "credential": "test-token-PLACEHOLDER",
    }
    _write_config(journal_path, config)
    monkeypatch.setattr(
        "solstone.think.providers.local_endpoint.probe_local_endpoint",
        lambda _endpoint, timeout_s=1.0: (True, None),
    )

    response = client.get("/app/thinking/api/providers")

    assert response.status_code == 200
    payload = response.get_json()
    assert payload["local_override"] == {
        "enabled": True,
        "endpoint_url": "http://host.test:8080/openai",
        "served_model_id": "served-model",
        "credential_configured": True,
    }
    assert "test-token-PLACEHOLDER" not in json.dumps(payload)


def test_local_endpoint_override_derives_byo_and_bundled_derives_local(
    settings_client_with_journal,
    monkeypatch,
):
    client, journal_path = settings_client_with_journal
    config_path = journal_path / "config" / "journal.json"
    config = json.loads(config_path.read_text())
    config["providers"]["active"] = {"provider": "local", "model": LOCAL_MODEL}
    _write_config(journal_path, config)

    response = client.get("/app/thinking/api/providers")

    assert response.status_code == 200
    assert response.get_json()["active_lane"]["lane"] == "local"

    config["providers"]["local"] = {
        "endpoint_url": "http://host.test:8080/v1",
        "served_model_id": "served-model",
    }
    _write_config(journal_path, config)
    monkeypatch.setattr(
        "solstone.think.providers.local_endpoint.probe_local_endpoint",
        lambda _endpoint, timeout_s=1.0: (True, None),
    )

    response = client.get("/app/thinking/api/providers")

    assert response.status_code == 200
    payload = response.get_json()
    assert payload["active_lane"]["lane"] == "byo"


def test_local_endpoint_override_derives_confidential_only_with_provenance(
    settings_client_with_journal,
    monkeypatch,
):
    client, journal_path = settings_client_with_journal
    config_path = journal_path / "config" / "journal.json"
    config = json.loads(config_path.read_text())
    config["providers"]["active"] = {"provider": "local", "model": LOCAL_MODEL}
    config["providers"]["local"] = {
        "endpoint_url": "http://host.test:8080/v1",
        "served_model_id": "served-model",
        "credential": "endpoint-credential",
    }
    _write_config(journal_path, config)
    monkeypatch.setattr(
        "solstone.think.providers.local_endpoint.probe_local_endpoint",
        lambda _endpoint, timeout_s=1.0: (True, None),
    )

    response = client.get("/app/thinking/api/providers")

    assert response.status_code == 200
    payload = response.get_json()
    assert payload["active_lane"]["lane"] == "byo"
    assert payload["active_lane"]["confidential_provenance_configured"] is False

    config.setdefault("services", {})["confidential"] = {
        "enabled_at": "2026-05-24T00:00:00Z",
        "account_id": "acct-confidential",
        "endpoint_url": "http://host.test:8080",
        "served_model_id": "served-model",
        "credential_created_at": "2026-05-24T00:00:00Z",
        "credential_fingerprint_sha256": "fingerprint",
        "prior_active": {"provider": "google", "model": "gemini-3.5-flash"},
        "prior_local_endpoint": None,
    }
    _write_config(journal_path, config)

    response = client.get("/app/thinking/api/providers")

    assert response.status_code == 200
    payload = response.get_json()
    assert payload["active_lane"]["lane"] == "confidential"
    assert payload["active_lane"]["confidential_enabled"] is True
    assert payload["active_lane"]["confidential_provenance_configured"] is True


def test_lane_switch_to_local_with_override_rejects_without_config_write(
    settings_client_with_journal,
):
    client, journal_path = settings_client_with_journal
    config_path = journal_path / "config" / "journal.json"
    config = json.loads(config_path.read_text())
    config["providers"]["local"] = {
        "endpoint_url": "http://host.test:8080/v1",
        "served_model_id": "served-model",
        "credential": "secret-token",
    }
    _write_config(journal_path, config)
    before = config_path.read_bytes()

    response = client.put("/app/thinking/api/providers", json={"lane": "local"})

    assert response.status_code == 400
    payload = response.get_json()
    assert payload["reason_code"] == "invalid_operation_for_state"
    assert (
        payload["detail"]
        == "clear your endpoint URL first to run the bundled local model."
    )
    assert config_path.read_bytes() == before


def test_byo_local_without_override_rejects_without_config_write(
    settings_client_with_journal,
):
    client, journal_path = settings_client_with_journal
    config_path = journal_path / "config" / "journal.json"
    before = config_path.read_bytes()

    response = client.put(
        "/app/thinking/api/providers",
        json={"lane": "byo", "provider": "local"},
    )

    assert response.status_code == 400
    payload = response.get_json()
    assert payload["reason_code"] == "invalid_operation_for_state"
    assert payload["detail"] == "save your endpoint URL first to use your own endpoint."
    assert config_path.read_bytes() == before


def test_byo_local_with_override_succeeds_and_rederives_byo(
    settings_client_with_journal,
    monkeypatch,
):
    client, journal_path = settings_client_with_journal
    config_path = journal_path / "config" / "journal.json"
    config = json.loads(config_path.read_text())
    config["providers"]["local"] = {
        "endpoint_url": "http://host.test:8080/v1",
        "served_model_id": "served-model",
    }
    _write_config(journal_path, config)
    monkeypatch.setattr(
        "solstone.think.providers.local_endpoint.probe_local_endpoint",
        lambda _endpoint, timeout_s=1.0: (True, None),
    )

    response = client.put(
        "/app/thinking/api/providers",
        json={"lane": "byo", "provider": "local"},
    )

    assert response.status_code == 200
    payload = response.get_json()
    assert payload["active_lane"]["lane"] == "byo"
    assert payload["active"]["provider"] == "local"


def test_byo_endpoint_switch_never_remembers_local_model(
    settings_client_with_journal,
    monkeypatch,
):
    client, journal_path = settings_client_with_journal
    config_path = journal_path / "config" / "journal.json"
    config = json.loads(config_path.read_text())
    config["providers"]["local"] = {
        "endpoint_url": "http://host.test:8080/v1",
        "served_model_id": "served-model",
    }
    _write_config(journal_path, config)
    monkeypatch.setattr(
        "solstone.think.providers.local_endpoint.probe_local_endpoint",
        lambda _endpoint, timeout_s=1.0: (True, None),
    )

    response = client.put(
        "/app/thinking/api/providers",
        json={"lane": "byo", "provider": "local"},
    )

    assert response.status_code == 200
    stored = json.loads(config_path.read_text())
    assert "local" not in stored["providers"].get("byo_models", {})


def test_byo_lane_with_top_level_model_writes_active_profile_and_memory(
    settings_client_with_journal,
):
    client, journal_path = settings_client_with_journal
    config_path = journal_path / "config" / "journal.json"

    response = client.put(
        "/app/thinking/api/providers",
        json={
            "lane": "byo",
            "provider": "anthropic",
            "model": "claude-opus-4-8",
        },
    )

    assert response.status_code == 200
    payload = response.get_json()
    assert payload["active"] == {
        "provider": "anthropic",
        "model": "claude-opus-4-8",
    }
    assert payload["byo_models"]["anthropic"] == "claude-opus-4-8"

    stored = json.loads(config_path.read_text())
    assert stored["providers"]["active"] == {
        "provider": "anthropic",
        "model": "claude-opus-4-8",
    }
    assert stored["providers"]["byo_models"]["anthropic"] == "claude-opus-4-8"


@pytest.mark.parametrize(
    ("payload", "detail"),
    [
        (
            {"lane": "local", "model": "gemini-3.5-flash"},
            "model is only valid with cloud BYO providers: anthropic, google, openai.",
        ),
        (
            {"lane": "byo", "provider": "google", "model": ""},
            "model must be a non-empty string.",
        ),
    ],
)
def test_top_level_model_rejects_invalid_provider_updates(
    settings_client_with_journal,
    payload,
    detail,
):
    client, journal_path = settings_client_with_journal
    config_path = journal_path / "config" / "journal.json"
    before = config_path.read_bytes()

    response = client.put("/app/thinking/api/providers", json=payload)

    assert response.status_code == 400
    body = response.get_json()
    assert body["reason_code"] == "invalid_config_value"
    assert body["detail"] == detail
    assert config_path.read_bytes() == before


def test_top_level_model_rejects_byo_local_endpoint_provider(
    settings_client_with_journal,
    monkeypatch,
):
    client, journal_path = settings_client_with_journal
    config_path = journal_path / "config" / "journal.json"
    config = json.loads(config_path.read_text())
    config["providers"]["local"] = {
        "endpoint_url": "http://host.test:8080/v1",
        "served_model_id": "served-model",
    }
    _write_config(journal_path, config)
    before = config_path.read_bytes()
    monkeypatch.setattr(
        "solstone.think.providers.local_endpoint.probe_local_endpoint",
        lambda _endpoint, timeout_s=1.0: (True, None),
    )

    response = client.put(
        "/app/thinking/api/providers",
        json={
            "lane": "byo",
            "provider": "local",
            "model": "local/qwen3.5-4b",
        },
    )

    assert response.status_code == 400
    body = response.get_json()
    assert body["reason_code"] == "invalid_config_value"
    assert body["detail"] == (
        "model is only valid with cloud BYO providers: anthropic, google, openai."
    )
    assert config_path.read_bytes() == before


def test_byo_lane_fills_remembered_model_after_hygiene_pop(
    settings_client_with_journal,
):
    client, journal_path = settings_client_with_journal
    config_path = journal_path / "config" / "journal.json"
    config = json.loads(config_path.read_text())
    config["providers"]["active"] = {
        "provider": "google",
        "model": "gemini-3.5-flash",
    }
    config["providers"]["byo_models"] = {"anthropic": "claude-opus-4-8"}
    _write_config(journal_path, config)

    response = client.put(
        "/app/thinking/api/providers",
        json={"lane": "byo", "provider": "anthropic"},
    )

    assert response.status_code == 200
    stored = json.loads(config_path.read_text())
    assert stored["providers"]["active"] == {
        "provider": "anthropic",
        "model": "claude-opus-4-8",
    }


def test_byo_lane_remembered_model_does_not_overwrite_present_model(
    settings_client_with_journal,
):
    client, journal_path = settings_client_with_journal
    config_path = journal_path / "config" / "journal.json"
    config = json.loads(config_path.read_text())
    config["providers"]["active"] = {
        "provider": "anthropic",
        "model": "claude-sonnet-4-6",
    }
    config["providers"]["byo_models"] = {"anthropic": "claude-opus-4-8"}
    _write_config(journal_path, config)

    response = client.put(
        "/app/thinking/api/providers",
        json={"lane": "byo", "provider": "anthropic"},
    )

    assert response.status_code == 200
    stored = json.loads(config_path.read_text())
    assert stored["providers"]["active"]["model"] == "claude-sonnet-4-6"


def test_lane_switch_to_confidential_rejects_without_config_write(
    settings_client_with_journal,
):
    client, journal_path = settings_client_with_journal
    config_path = journal_path / "config" / "journal.json"
    config = json.loads(config_path.read_text())
    before = config_path.read_bytes()

    response = client.put(
        "/app/thinking/api/providers",
        json={"lane": "confidential"},
    )

    assert response.status_code == 400
    payload = response.get_json()
    assert payload["reason_code"] == "invalid_operation_for_state"
    assert (
        payload["detail"]
        == "confidential lane activation must use the confidential enable flow."
    )
    assert config_path.read_bytes() == before

    config["providers"]["local"] = {
        "endpoint_url": "http://host.test:8080/v1",
        "served_model_id": "served-model",
    }
    config.setdefault("services", {})["confidential"] = {
        "enabled_at": "2026-05-24T00:00:00Z",
        "account_id": "acct-confidential",
        "endpoint_url": "http://host.test:8080",
        "served_model_id": "served-model",
        "credential_created_at": "2026-05-24T00:00:00Z",
        "credential_fingerprint_sha256": "fingerprint",
        "prior_active": {"provider": "google", "model": "gemini-3.5-flash"},
        "prior_local_endpoint": None,
    }
    _write_config(journal_path, config)

    response = client.put(
        "/app/thinking/api/providers",
        json={"lane": "confidential"},
    )

    assert response.status_code == 200
    payload = response.get_json()
    assert payload["active_lane"]["lane"] == "confidential"
    assert payload["active"]["provider"] == "local"


def test_switch_from_byo_model_to_local_resolves_local_default(
    settings_client_with_journal,
):
    client, _journal_path = settings_client_with_journal

    response = client.put(
        "/app/thinking/api/providers",
        json={
            "lane": "byo",
            "provider": "google",
            "model": "gemini-3.5-flash",
        },
    )
    assert response.status_code == 200

    response = client.put("/app/thinking/api/providers", json={"lane": "local"})

    assert response.status_code == 200
    assert resolve_provider("generate") == ("local", LOCAL_MODEL)


def test_switch_to_cloud_without_memory_resolves_provider_default(
    settings_client_with_journal,
):
    client, journal_path = settings_client_with_journal
    config_path = journal_path / "config" / "journal.json"
    config = json.loads(config_path.read_text())
    config["providers"]["active"] = {
        "provider": "google",
        "model": "gemini-3.5-flash",
    }
    config["providers"].pop("byo_models", None)
    _write_config(journal_path, config)

    response = client.put(
        "/app/thinking/api/providers",
        json={"lane": "byo", "provider": "anthropic"},
    )

    assert response.status_code == 200
    assert resolve_provider("generate") == ("anthropic", "claude-sonnet-4-6")


def test_active_explicit_model_resolves_without_ui_interaction(
    settings_client_with_journal,
):
    _client, journal_path = settings_client_with_journal
    config_path = journal_path / "config" / "journal.json"
    config = json.loads(config_path.read_text())
    config["providers"]["active"] = {
        "provider": "openai",
        "model": "gpt-5.5",
    }
    _write_config(journal_path, config)

    assert resolve_provider("generate") == ("openai", "gpt-5.5")


def test_confidential_lane_preserves_byo_models(
    settings_client_with_journal,
    monkeypatch,
):
    client, journal_path = settings_client_with_journal
    config_path = journal_path / "config" / "journal.json"
    config = json.loads(config_path.read_text())
    config["providers"]["byo_models"] = {"openai": "gpt-5.5"}
    _write_config(journal_path, config)
    monkeypatch.setattr(
        "solstone.apps.thinking.routes.spp.confidential_provenance",
        lambda: {"configured": True},
    )

    response = client.put(
        "/app/thinking/api/providers",
        json={"lane": "confidential"},
    )

    assert response.status_code == 200
    stored = json.loads(config_path.read_text())
    assert stored["providers"]["byo_models"] == {"openai": "gpt-5.5"}


def test_lane_for_provider_derives_confidential_from_local_endpoint_provenance():
    assert (
        routes._lane_for_provider(
            NO_BRAIN_PROVIDER,
            local_endpoint_configured=True,
            confidential_provenance_present=True,
        )
        == "none"
    )
    assert (
        routes._lane_for_provider(
            "local",
            local_endpoint_configured=False,
            confidential_provenance_present=True,
        )
        == "local"
    )
    assert (
        routes._lane_for_provider(
            "local",
            local_endpoint_configured=True,
            confidential_provenance_present=False,
        )
        == "byo"
    )
    assert (
        routes._lane_for_provider(
            "local",
            local_endpoint_configured=True,
            confidential_provenance_present=True,
        )
        == "confidential"
    )
    assert (
        routes._lane_for_provider(
            "google",
            local_endpoint_configured=True,
            confidential_provenance_present=True,
        )
        == "byo"
    )


def test_get_providers_scout_google_grandfather_is_zero_touch(
    settings_client_with_journal,
):
    client, journal_path = settings_client_with_journal
    config_path = journal_path / "config" / "journal.json"
    config = json.loads(config_path.read_text())
    config.setdefault("env", {})["GOOGLE_API_KEY"] = "scout-key"
    config.setdefault("services", {})["scout"] = {
        "enabled_at": "2026-05-23T00:00:00Z",
        "key_fingerprint_sha256": "fingerprint",
    }
    config["providers"]["active"] = {
        "provider": "google",
        "model": "gemini-3.5-flash",
    }
    _write_config(journal_path, config)
    before = config_path.read_bytes()

    response = client.get("/app/thinking/api/providers")

    assert response.status_code == 200
    assert response.get_json()["active_lane"]["lane"] == "byo"
    assert config_path.read_bytes() == before


def test_providers_payload_local_status_uses_endpoint_readiness_under_byo(
    settings_client_with_journal,
    monkeypatch,
):
    client, journal_path = settings_client_with_journal
    config = json.loads((journal_path / "config" / "journal.json").read_text())
    config["providers"]["local"] = {
        "endpoint_url": "http://host.test:8080/v1",
        "served_model_id": "served-model",
    }
    _write_config(journal_path, config)
    monkeypatch.setattr(
        "solstone.think.providers.local_endpoint.probe_local_endpoint",
        lambda _endpoint, timeout_s=1.0: (True, None),
    )

    response = client.get("/app/thinking/api/providers")

    assert response.status_code == 200
    local_status = response.get_json()["provider_status"]["local"]
    assert local_status == {
        "configured": True,
        "selected": False,
        "generate_ready": True,
        "cogitate_ready": True,
        "issues": [],
    }


def test_get_providers_uses_requested_local_model(settings_client, monkeypatch):
    model_id = next(iter(LOCAL_MODEL_SPECS))
    requested_models: list[str] = []

    def fake_get_state(model: str) -> dict:
        requested_models.append(model)
        return {
            "name": model,
            "install_state": "idle",
            "last_transition_at": None,
            "last_progress_at": None,
            "progress_bytes_received": None,
            "progress_bytes_total": None,
            "install_error": None,
        }

    monkeypatch.setattr(routes.local_bootstrap, "get_state", fake_get_state)

    response = settings_client.get(
        "/app/thinking/api/providers",
        query_string={"local_model": model_id},
    )

    assert response.status_code == 200
    payload = response.get_json()
    _assert_install_status(payload["local"])
    assert requested_models == [model_id]
    assert payload["local"]["name"] == model_id


def test_get_providers_uses_state_local_status(settings_client, monkeypatch):
    sentinel = {
        "configured": True,
        "selected": True,
        "generate_ready": True,
        "cogitate_ready": True,
        "issues": ["sentinel"],
    }
    monkeypatch.setattr(
        "solstone.think.providers.state.local_status_dict",
        lambda: sentinel,
    )

    response = settings_client.get("/app/thinking/api/providers")

    assert response.status_code == 200
    payload = response.get_json()
    assert payload["provider_status"]["local"] == sentinel


def test_get_providers_uses_canonical_spp_local_status_without_probe(
    settings_client,
    monkeypatch,
):
    spp_readiness = {
        "generate_ready": True,
        "cogitate_ready": False,
        "issues": ["cogitate_terminal_error"],
    }
    monkeypatch.setattr(
        routes,
        "build_brain_presentation",
        lambda *_args, **_kwargs: _presentation(
            spp_active=True,
            spp_readiness=spp_readiness,
            confidential_attestation={
                "state": "verified",
                "reason": None,
                "observed_at": "2026-04-10T12:00:00Z",
            },
        ),
    )

    def fail(*_args, **_kwargs):
        raise AssertionError("process-local readiness path called")

    monkeypatch.setattr("solstone.think.providers.state.local_status_dict", fail)
    monkeypatch.setattr(
        "solstone.think.providers.local_endpoint.probe_local_endpoint",
        fail,
    )
    monkeypatch.setattr("solstone.think.services.spp.get_attestation_state", fail)
    monkeypatch.setattr(
        "solstone.think.services.spp_transport.recheck_confidential_attestation",
        fail,
    )

    providers_response = settings_client.get("/app/thinking/api/providers")
    local_response = settings_client.get("/app/thinking/api/providers/local/status")

    assert providers_response.status_code == 200
    assert local_response.status_code == 200
    expected = {
        "configured": True,
        "selected": True,
        "generate_ready": True,
        "cogitate_ready": False,
        "issues": ["cogitate_terminal_error"],
    }
    assert providers_response.get_json()["provider_status"]["local"] == expected
    assert local_response.get_json() == expected


def test_get_providers_brain_shape(settings_client, monkeypatch):
    _patch_brain(monkeypatch)

    response = settings_client.get("/app/thinking/api/providers")

    assert response.status_code == 200
    payload = response.get_json()
    _assert_brain_shape(payload)
    assert payload["local_backend"] == "local"
    assert payload["brain"]["state"] == "ready"


def test_get_providers_brain_surfaces_snapshot_from_builder(
    settings_client, monkeypatch
):
    _patch_brain(
        monkeypatch,
        _brain_payload(
            state="blocked",
            headline=HEADLINES["blocked"],
            reason_code="gpu_unavailable",
            reason_text="gpu unavailable",
            action={"label": "open local setup", "href": "/app/thinking/#local-setup"},
        ),
    )

    response = settings_client.get("/app/thinking/api/providers")

    assert response.status_code == 200
    brain = response.get_json()["brain"]
    assert brain["state"] == "blocked"
    assert brain["reason_code"] == "gpu_unavailable"
    assert brain["action"] == {
        "label": "open local setup",
        "href": "/app/thinking/#local-setup",
    }


def test_get_providers_brain_missing_key_blocks(settings_client, monkeypatch):
    _patch_brain(
        monkeypatch,
        _brain_payload(
            state="blocked",
            headline=HEADLINES["blocked"],
            reason_code="provider_key_invalid",
            reason_text="provider key invalid",
            action={"label": "open thinking", "href": "/app/thinking/#main"},
        ),
    )

    response = settings_client.get("/app/thinking/api/providers")

    assert response.status_code == 200
    payload = response.get_json()
    _assert_brain_shape(payload)
    brain = payload["brain"]
    assert brain["headline"] == HEADLINES["blocked"]
    assert brain["reason_code"] == "provider_key_invalid"
    assert brain["action"] == {
        "label": "open thinking",
        "href": "/app/thinking/#main",
    }


def test_get_providers_brain_unknown_is_check_again(settings_client, monkeypatch):
    _patch_brain(
        monkeypatch,
        _brain_payload(
            state="unknown",
            headline=HEADLINES["unknown"],
            reason_code="brain_record_unavailable",
            reason_text="brain record unavailable",
            action={"label": "check again", "refresh": True},
        ),
    )

    response = settings_client.get("/app/thinking/api/providers")

    assert response.status_code == 200
    brain = response.get_json()["brain"]
    assert brain["state"] == "unknown"
    assert brain["headline"] == HEADLINES["unknown"]
    assert brain["action"] == {"label": "check again", "refresh": True}


def test_get_providers_brain_unknown_does_not_change_status_payload(
    settings_client, monkeypatch
):
    sentinel = {
        "configured": True,
        "selected": True,
        "generate_ready": True,
        "cogitate_ready": True,
        "issues": ["sentinel"],
    }
    monkeypatch.setattr(
        "solstone.think.providers.state.local_status_dict",
        lambda: sentinel,
    )
    _patch_selected_providers(monkeypatch)
    _patch_brain(
        monkeypatch,
        _brain_payload(
            state="unknown",
            headline=HEADLINES["unknown"],
            reason_code="brain_record_unavailable",
            reason_text="brain record unavailable",
            action={"label": "check again", "refresh": True},
        ),
    )

    response = settings_client.get("/app/thinking/api/providers")

    assert response.status_code == 200
    payload = response.get_json()
    assert payload["provider_status"]["local"] == sentinel
    assert payload["brain"]["state"] == "unknown"
    assert payload["brain"]["headline"] == HEADLINES["unknown"]


def test_get_providers_brain_keeps_local_backend_on_mlx(settings_client, monkeypatch):
    _patch_selected_providers(monkeypatch)
    _patch_brain(monkeypatch)
    monkeypatch.setattr(routes.local_bootstrap, "_is_mlx_backend", lambda: True)

    response = settings_client.get("/app/thinking/api/providers")

    assert response.status_code == 200
    payload = response.get_json()
    assert payload["local_backend"] == "mlx"
    assert payload["brain"]["state"] == "ready"
