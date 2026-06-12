# SPDX-License-Identifier: AGPL-3.0-only
# Copyright (c) 2026 sol pbc

from __future__ import annotations

import json
from typing import get_args

import pytest

from solstone.apps.settings import routes
from solstone.apps.settings.local_bootstrap import LOCAL_MODEL_SPECS
from solstone.convey import create_app
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
    config.setdefault("convey", {})["trust_localhost"] = True
    (journal_path / "config" / "journal.json").write_text(
        json.dumps(config, indent=2) + "\n",
        encoding="utf-8",
    )
    app = create_app(str(journal_path))
    app.config["TESTING"] = True
    return app.test_client(), journal_path


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
    response = settings_client.get("/app/settings/api/providers")

    assert response.status_code == 200
    payload = response.get_json()
    assert "bundled" not in payload
    assert "ai_readiness" in payload
    assert isinstance(payload["local"], dict)
    assert REMOVED_PROVIDER not in payload
    _assert_install_status(payload["local"])


def test_providers_payload_omits_bundled_block(settings_client):
    response = settings_client.get("/app/settings/api/providers")

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
    response = settings_client.get("/app/settings/api/providers")

    assert response.status_code == 200
    payload = response.get_json()
    assert "auth" not in payload


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
        "/app/settings/api/providers",
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

    response = settings_client.get("/app/settings/api/providers")

    assert response.status_code == 200
    payload = response.get_json()
    assert payload["provider_status"]["local"] == sentinel


def test_get_providers_ai_readiness_shape(settings_client):
    response = settings_client.get("/app/settings/api/providers")

    assert response.status_code == 200
    payload = response.get_json()
    _assert_ai_readiness_shape(payload)
    assert payload["local_backend"] == "local"
    assert payload["ai_readiness"]["local"]["provider"] == "local"


def test_get_providers_ai_readiness_missing_key_blocks(settings_client, monkeypatch):
    _patch_readiness(
        monkeypatch,
        reason_code="provider_key_missing",
        status="blocked",
        provider="google",
    )

    response = settings_client.get("/app/settings/api/providers")

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
        "href": "/app/settings/#providers",
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

    response = settings_client.get("/app/settings/api/providers")

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
        ("gpu_unavailable", "blocked", "blocker"),
        ("local_server_unhealthy", "unhealthy", "attention"),
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

    response = settings_client.get("/app/settings/api/providers")

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

    response = settings_client.get("/app/settings/api/providers")

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

    response = settings_client.get("/app/settings/api/providers")

    assert response.status_code == 200
    payload = response.get_json()
    assert payload["local_backend"] == "mlx"
    assert "local" not in payload["ai_readiness"]


def test_put_providers_imports_and_clears_vertex_credentials(
    settings_client_with_journal, monkeypatch
):
    client, journal_path = settings_client_with_journal
    monkeypatch.setattr(
        "solstone.apps.settings.routes.validate_vertex_credentials",
        lambda _path: {
            "valid": True,
            "email": "test@test-project.iam.gserviceaccount.com",
        },
    )

    response = client.put(
        "/app/settings/api/providers",
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
        "/app/settings/api/providers",
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
        "/app/settings/api/providers",
        json={"vertex_credentials": ""},
    )

    assert response.status_code == 200
    assert noncanonical.exists()
    config = json.loads(config_path.read_text())
    assert "vertex_credentials" not in config["providers"]
    assert "google_vertex" not in config["providers"]["key_validation"]
