# SPDX-License-Identifier: AGPL-3.0-only
# Copyright (c) 2026 sol pbc

from __future__ import annotations

import json
from typing import get_args

import pytest

from solstone.apps.thinking import routes
from solstone.apps.thinking.local_bootstrap import LOCAL_MODEL_SPECS
from solstone.convey import create_app
from solstone.think.models import LOCAL_MODEL
from solstone.think.providers.install_state import InstallState
from solstone.think.providers.state import ProviderState

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
def settings_client(settings_env):
    client, _journal_path = _settings_client_with_journal(settings_env)
    return client


@pytest.fixture
def settings_client_with_journal(settings_env):
    return _settings_client_with_journal(settings_env)


def _settings_client_with_journal(settings_env):
    journal_path, config = settings_env()
    config["setup"] = {"completed_at": "2026-05-23T00:00:00Z"}
    (journal_path / "config" / "journal.json").write_text(
        json.dumps(config, indent=2) + "\n",
        encoding="utf-8",
    )
    app = create_app(str(journal_path))
    app.config["TESTING"] = True
    return app.test_client(), journal_path


def _write_config(journal_path, config: dict) -> None:
    (journal_path / "config" / "journal.json").write_text(
        json.dumps(config, indent=2) + "\n",
        encoding="utf-8",
    )


def _valid_vertex_creds() -> dict:
    return {
        "type": "service_account",
        "project_id": "test-project",
        "client_email": "test@test-project.iam.gserviceaccount.com",
        "private_key": "-----BEGIN RSA PRIVATE KEY-----\nfake\n-----END RSA PRIVATE KEY-----\n",
        "client_id": "123",
        "token_uri": "https://oauth2.googleapis.com/token",
    }


def _assert_install_status(payload: dict) -> None:
    assert INSTALL_STATUS_FIELDS <= set(payload)
    assert payload["install_state"] in CANONICAL_INSTALL_STATES


def _patch_selected_providers(monkeypatch, *, provider: str = "google") -> None:
    monkeypatch.setattr(
        "solstone.think.models.resolve_provider",
        lambda _context, _interface: (provider, f"{provider}-model"),
    )


def _patch_readiness(monkeypatch, reason_code: str, status: str, provider: str) -> None:
    def fake_readiness(selected_provider: str, interface: str, model: str):
        return ProviderState(
            provider=selected_provider,
            interface=interface,
            status=status,
            model=model,
            reason_code=reason_code if status != "ready" else None,
        )

    monkeypatch.setattr(
        "solstone.think.providers.state.readiness_for_provider",
        fake_readiness,
    )
    _patch_selected_providers(monkeypatch, provider=provider)


def _assert_ai_readiness_shape(payload: dict) -> None:
    ai_readiness = payload["ai_readiness"]
    assert set(ai_readiness) >= {"summary", "interfaces", "groups"}
    assert set(ai_readiness["summary"]) == {
        "status",
        "severity",
        "active_groups",
        "blocked_count",
    }
    assert set(ai_readiness["interfaces"]) == {"generate", "cogitate"}
    for view in ai_readiness["interfaces"].values():
        assert set(view) == {
            "semantic_key",
            "work_key",
            "status",
            "severity",
            "reason_code",
            "provider",
            "model",
            "context",
            "interface",
            "summary",
            "detail",
            "recovery_action",
            "operator_detail",
        }
    if ai_readiness.get("local") is not None:
        assert set(ai_readiness["local"]) == {
            "semantic_key",
            "work_key",
            "status",
            "severity",
            "reason_code",
            "provider",
            "model",
            "context",
            "interface",
            "summary",
            "detail",
            "recovery_action",
            "operator_detail",
        }


def test_get_providers_includes_local_install_state(settings_client):
    response = settings_client.get("/app/thinking/api/providers")

    assert response.status_code == 200
    payload = response.get_json()
    assert "bundled" not in payload
    assert "ai_readiness" in payload
    assert isinstance(payload["local"], dict)
    assert payload["local_override"] == {
        "enabled": False,
        "endpoint_url": "",
        "served_model_id": "",
        "credential_configured": False,
    }
    assert REMOVED_PROVIDER not in payload
    _assert_install_status(payload["local"])


def test_get_providers_reports_byo_when_split_cloud_providers_share_lane(
    settings_client,
):
    response = settings_client.get("/app/thinking/api/providers")

    assert response.status_code == 200
    payload = response.get_json()
    assert payload["active_lane"]["lane"] == "byo"
    assert payload["active_lane"]["split"] is False


def test_get_providers_reports_advanced_when_generate_and_cogitate_lanes_split(
    settings_client_with_journal,
):
    client, journal_path = settings_client_with_journal
    config_path = journal_path / "config" / "journal.json"
    config = json.loads(config_path.read_text())
    config["providers"]["generate"]["provider"] = "local"
    config["providers"]["cogitate"]["provider"] = "openai"
    _write_config(journal_path, config)

    response = client.get("/app/thinking/api/providers")

    assert response.status_code == 200
    payload = response.get_json()
    assert payload["active_lane"]["lane"] == "advanced"
    assert payload["active_lane"]["split"] is True


def test_lane_switch_updates_generate_and_cogitate_without_contexts(
    settings_client_with_journal,
):
    client, journal_path = settings_client_with_journal
    config_path = journal_path / "config" / "journal.json"
    before = json.loads(config_path.read_text())
    before_contexts = before["providers"]["contexts"]
    before_models = before["providers"].get("models")

    response = client.put("/app/thinking/api/providers", json={"lane": "local"})

    assert response.status_code == 200
    payload = response.get_json()
    assert payload["active_lane"]["lane"] == "local"
    config = json.loads(config_path.read_text())
    assert config["providers"]["generate"]["provider"] == "local"
    assert config["providers"]["cogitate"]["provider"] == "local"
    assert config["providers"]["contexts"] == before_contexts
    assert config["providers"].get("models") == before_models


def test_scout_lane_is_derived_from_google_provider_and_provenance(
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
    config["providers"]["generate"]["provider"] = "google"
    config["providers"]["cogitate"]["provider"] = "google"
    _write_config(journal_path, config)

    response = client.get("/app/thinking/api/providers")

    assert response.status_code == 200
    payload = response.get_json()
    assert payload["active_lane"]["lane"] == "scout"
    assert payload["active_lane"]["scout_enabled"] is True


def test_byo_gemini_key_write_is_rejected_when_scout_enabled(
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
    _write_config(journal_path, config)

    response = client.put(
        "/app/thinking/api/keys",
        json={"env_var": "GOOGLE_API_KEY", "value": "manual-key"},
    )

    assert response.status_code == 400
    payload = response.get_json()
    assert payload["reason_code"] == "invalid_config_value"
    config = json.loads(config_path.read_text())
    assert config["env"]["GOOGLE_API_KEY"] == "scout-key"


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
    config["providers"]["generate"]["provider"] = "google"
    config["providers"]["cogitate"]["provider"] = "google"
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
        }:
            assert forbidden not in body

    providers_payload = responses[0].get_json()
    assert providers_payload["active_lane"] == {
        "lane": "scout",
        "generate": "scout",
        "cogitate": "scout",
        "split": False,
        "scout_enabled": True,
        "scout_provenance_configured": True,
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
    assert provider_status["local"]["cogitate_cli"] == "llama-server"
    assert REMOVED_PROVIDER not in payload
    _assert_install_status(payload["local"])


def test_providers_payload_omits_auth(settings_client):
    response = settings_client.get("/app/thinking/api/providers")

    assert response.status_code == 200
    payload = response.get_json()
    assert "auth" not in payload


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
        "cogitate_cli": None,
        "cogitate_cli_found": False,
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
        "cogitate_cli": "llama-server",
        "cogitate_cli_found": True,
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


def test_get_providers_ai_readiness_shape(settings_client):
    response = settings_client.get("/app/thinking/api/providers")

    assert response.status_code == 200
    payload = response.get_json()
    _assert_ai_readiness_shape(payload)
    assert payload["local_backend"] == "local"
    assert payload["ai_readiness"]["local"]["provider"] == "local"


def test_get_providers_ai_readiness_surfaces_gpu_probe_failed_from_inspect(
    settings_client, monkeypatch
):
    monkeypatch.setattr(
        "solstone.think.providers.local_install.inspect_readiness",
        lambda _model=None: {
            "install_state": "installed",
            "binary_installed": True,
            "model_installed": True,
            "ram_sufficient": True,
            "gpu_available": True,
            "gpu_probe_ok": False,
            "binary_path": "/tmp/llama-server",
            "model_path": "/tmp/model.gguf",
            "model_id": LOCAL_MODEL,
            "install_error": None,
        },
    )
    monkeypatch.setattr(
        "solstone.think.providers.local_server.probe_state",
        lambda: (_ for _ in ()).throw(AssertionError("server probe not expected")),
    )

    response = settings_client.get("/app/thinking/api/providers")

    assert response.status_code == 200
    local = response.get_json()["ai_readiness"]["local"]
    assert local["status"] == "blocked"
    assert local["reason_code"] == "gpu_probe_failed"
    assert local["provider"] == "local"


def test_get_providers_ai_readiness_missing_key_blocks(settings_client, monkeypatch):
    _patch_readiness(
        monkeypatch,
        reason_code="provider_key_missing",
        status="blocked",
        provider="google",
    )

    response = settings_client.get("/app/thinking/api/providers")

    assert response.status_code == 200
    payload = response.get_json()
    _assert_ai_readiness_shape(payload)
    readiness = payload["ai_readiness"]
    assert readiness["summary"]["severity"] == "blocker"
    assert readiness["summary"]["active_groups"] == 1
    assert readiness["summary"]["blocked_count"] == 1
    group = readiness["groups"][0]
    assert group["reason_code"] == "provider_key_missing"
    assert group["recovery_action"] == {
        "label": "Open Settings",
        "href": "/app/thinking/#main",
    }


def test_get_providers_ai_readiness_cloud_unknown_is_neutral(
    settings_client, monkeypatch
):
    _patch_readiness(
        monkeypatch,
        reason_code="unknown",
        status="unknown",
        provider="anthropic",
    )

    response = settings_client.get("/app/thinking/api/providers")

    assert response.status_code == 200
    readiness = response.get_json()["ai_readiness"]
    assert readiness["summary"]["severity"] == "neutral"
    assert readiness["summary"]["active_groups"] == 0
    assert readiness["summary"]["blocked_count"] == 0
    assert readiness["groups"] == []


@pytest.mark.parametrize(
    ("reason_code", "status", "expected_severity"),
    [
        ("local_model_missing", "blocked", "blocker"),
        ("local_model_installing", "blocked", "blocker"),
        ("local_model_loading", "blocked", "blocker"),
        ("ram_insufficient", "blocked", "blocker"),
        ("gpu_probe_failed", "blocked", "blocker"),
        ("gpu_unavailable", "blocked", "blocker"),
        ("local_server_unhealthy", "unhealthy", "attention"),
        ("local_endpoint_unreachable", "unhealthy", "attention"),
        ("local_endpoint_contract_failed", "unhealthy", "attention"),
    ],
)
def test_get_providers_ai_readiness_local_blockers_group_coherently(
    settings_client, monkeypatch, reason_code, status, expected_severity
):
    _patch_readiness(
        monkeypatch,
        reason_code=reason_code,
        status=status,
        provider="local",
    )

    response = settings_client.get("/app/thinking/api/providers")

    assert response.status_code == 200
    readiness = response.get_json()["ai_readiness"]
    assert readiness["summary"]["severity"] == expected_severity
    assert readiness["summary"]["active_groups"] == 1
    assert readiness["groups"][0]["reason_code"] == reason_code
    assert readiness["groups"][0]["provider"] == "local"


def test_get_providers_ai_readiness_degrades_without_changing_status_payload(
    settings_client, monkeypatch
):
    sentinel = {
        "configured": True,
        "selected": True,
        "generate_ready": True,
        "cogitate_ready": True,
        "cogitate_cli": "llama-server",
        "cogitate_cli_found": True,
        "issues": ["sentinel"],
    }
    monkeypatch.setattr(
        "solstone.think.providers.state.local_status_dict",
        lambda: sentinel,
    )
    _patch_selected_providers(monkeypatch)
    monkeypatch.setattr(
        "solstone.think.providers.state.readiness_for_provider",
        lambda *_args, **_kwargs: (_ for _ in ()).throw(RuntimeError("boom")),
    )

    response = settings_client.get("/app/thinking/api/providers")

    assert response.status_code == 200
    payload = response.get_json()
    assert payload["provider_status"]["local"] == sentinel
    assert payload["ai_readiness"] == {
        "summary": {
            "status": "unknown",
            "severity": "neutral",
            "active_groups": 0,
            "blocked_count": 0,
        },
        "interfaces": {},
        "groups": [],
        "unavailable": True,
    }


def test_get_providers_ai_readiness_omits_local_on_mlx(settings_client, monkeypatch):
    _patch_selected_providers(monkeypatch)
    monkeypatch.setattr(routes.local_bootstrap, "_is_mlx_backend", lambda: True)

    def fake_readiness(provider: str, interface: str, model: str):
        assert provider != "local"
        return ProviderState(
            provider=provider,
            interface=interface,
            status="ready",
            model=model,
        )

    monkeypatch.setattr(
        "solstone.think.providers.state.readiness_for_provider",
        fake_readiness,
    )

    response = settings_client.get("/app/thinking/api/providers")

    assert response.status_code == 200
    payload = response.get_json()
    assert payload["local_backend"] == "mlx"
    assert "local" not in payload["ai_readiness"]


def test_put_providers_imports_and_clears_vertex_credentials(
    settings_client_with_journal, monkeypatch
):
    client, journal_path = settings_client_with_journal
    monkeypatch.setattr(
        "solstone.apps.thinking.routes.validate_vertex_credentials",
        lambda _path: {
            "valid": True,
            "email": "test@test-project.iam.gserviceaccount.com",
        },
    )

    response = client.put(
        "/app/thinking/api/providers",
        json={"vertex_credentials": json.dumps(_valid_vertex_creds())},
    )

    assert response.status_code == 200
    creds_file = journal_path / ".config" / "vertex-credentials.json"
    assert creds_file.exists()
    assert creds_file.stat().st_mode & 0o777 == 0o600
    config_path = journal_path / "config" / "journal.json"
    config = json.loads(config_path.read_text())
    assert config["providers"]["vertex_credentials"] == str(creds_file)

    response = client.put(
        "/app/thinking/api/providers",
        json={"vertex_credentials": ""},
    )

    assert response.status_code == 200
    assert not creds_file.exists()
    config = json.loads(config_path.read_text())
    assert "vertex_credentials" not in config["providers"]


def test_put_providers_clear_refuses_noncanonical_path_but_removes_config(
    settings_client_with_journal,
):
    client, journal_path = settings_client_with_journal
    noncanonical = journal_path / "elsewhere.json"
    noncanonical.write_text("secret", encoding="utf-8")
    config_path = journal_path / "config" / "journal.json"
    config = json.loads(config_path.read_text())
    config["providers"]["vertex_credentials"] = str(noncanonical)
    config["providers"].setdefault("key_validation", {})["google_vertex"] = {
        "valid": True,
    }
    config_path.write_text(json.dumps(config, indent=2) + "\n", encoding="utf-8")

    response = client.put(
        "/app/thinking/api/providers",
        json={"vertex_credentials": ""},
    )

    assert response.status_code == 200
    assert noncanonical.exists()
    config = json.loads(config_path.read_text())
    assert "vertex_credentials" not in config["providers"]
    assert "google_vertex" not in config["providers"]["key_validation"]
