# SPDX-License-Identifier: AGPL-3.0-only
# Copyright (c) 2026 sol pbc

from __future__ import annotations

import importlib

import pytest

from solstone.apps.settings import local_bootstrap
from solstone.convey import create_app
from solstone.think.models import LOCAL_FLASH, LOCAL_PRO
from solstone.think.providers.local import LOCAL_MODEL_SPECS


def _client(journal_path):
    app = create_app(str(journal_path))
    app.config["TESTING"] = True
    return app.test_client()


def _settings_config() -> dict:
    return {
        "setup": {"completed_at": "2026-05-09T00:00:00Z"},
        "convey": {"trust_localhost": True},
        "providers": {
            "generate": {"provider": "google", "tier": 2, "backup": "anthropic"},
            "cogitate": {"provider": "openai", "tier": 2, "backup": "anthropic"},
            "auth": {"google": "api_key", "openai": "api_key"},
        },
    }


@pytest.fixture(autouse=True)
def _reset_local_state(monkeypatch):
    monkeypatch.setattr(
        local_bootstrap,
        "_STATES",
        {name: local_bootstrap.LocalBootstrapState() for name in LOCAL_MODEL_SPECS},
    )


def test_local_availability_payload_exact_shape(settings_env, monkeypatch):
    journal_path, _config = settings_env(_settings_config())
    monkeypatch.setattr(local_bootstrap, "check_binary_present", lambda: True)
    monkeypatch.setattr(local_bootstrap, "check_model_present", lambda _model: True)
    monkeypatch.setattr(local_bootstrap, "_platform_supported", lambda: (True, ""))
    monkeypatch.setattr(
        local_bootstrap.psutil,
        "virtual_memory",
        lambda: type("VMem", (), {"total": 32 * 1024**3})(),
    )
    client = _client(journal_path)

    response = client.get("/app/settings/api/local/availability")

    assert response.status_code == 200
    payload = response.get_json()
    assert set(payload) == {
        "model",
        "platform_supported",
        "total_memory_gb",
        "min_ram_gb",
        "binary_present",
        "model_present",
        "available",
        "reason",
    }
    assert payload == {
        "model": LOCAL_FLASH,
        "platform_supported": True,
        "total_memory_gb": 32.0,
        "min_ram_gb": 12,
        "binary_present": True,
        "model_present": True,
        "available": True,
        "reason": "",
    }


def test_local_models_route_returns_settings_shape(settings_env):
    journal_path, _config = settings_env(_settings_config())
    client = _client(journal_path)

    response = client.get("/app/settings/api/local/models")

    assert response.status_code == 200
    assert response.get_json() == [
        {
            "name": LOCAL_FLASH,
            "label": "qwen 2.5 coder 7B — 12 GB",
            "min_ram_gb": 12,
            "size_bytes": LOCAL_MODEL_SPECS[LOCAL_FLASH].size_bytes,
        },
        {
            "name": LOCAL_PRO,
            "label": "qwen3 coder 30B — 32 GB",
            "min_ram_gb": 32,
            "size_bytes": LOCAL_MODEL_SPECS[LOCAL_PRO].size_bytes,
        },
    ]


@pytest.mark.parametrize(
    ("method", "path"),
    [
        ("get", "/app/settings/api/local/availability"),
        ("post", "/app/settings/api/local/bootstrap"),
        ("get", "/app/settings/api/local/bootstrap/status"),
    ],
)
def test_local_routes_reject_unknown_model(settings_env, method, path):
    journal_path, _config = settings_env(_settings_config())
    client = _client(journal_path)

    response = getattr(client, method)(f"{path}?model=not-real")

    assert response.status_code == 400
    payload = response.get_json()
    assert payload["reason_code"] == "invalid_request_value"
    assert "not-real" in payload["detail"]
    assert LOCAL_FLASH in payload["detail"]
    assert LOCAL_PRO in payload["detail"]


@pytest.mark.parametrize(
    ("method", "path", "helper_name", "return_value"),
    [
        (
            "get",
            "/app/settings/api/local/availability",
            "get_availability_payload",
            {"available": True},
        ),
        (
            "post",
            "/app/settings/api/local/bootstrap",
            "start_bootstrap",
            ({"state": "installed"}, 200),
        ),
        (
            "get",
            "/app/settings/api/local/bootstrap/status",
            "get_state",
            {"state": "idle"},
        ),
    ],
)
def test_local_routes_default_to_flash_model(
    settings_env, monkeypatch, method, path, helper_name, return_value
):
    journal_path, _config = settings_env(_settings_config())
    calls = []

    def fake_helper(model):
        calls.append(model)
        return return_value

    monkeypatch.setattr(local_bootstrap, helper_name, fake_helper)
    client = _client(journal_path)

    response = getattr(client, method)(path)

    assert response.status_code == 200
    assert calls == [LOCAL_FLASH]


def test_local_bootstrap_post_rejects_unqualified_host(settings_env, monkeypatch):
    journal_path, _config = settings_env(_settings_config())
    monkeypatch.setattr(
        local_bootstrap,
        "start_bootstrap",
        lambda _model: (_ for _ in ()).throw(
            local_bootstrap.LocalBootstrapUnavailableError("unsupported platform")
        ),
    )
    client = _client(journal_path)

    response = client.post("/app/settings/api/local/bootstrap")

    assert response.status_code == 400
    payload = response.get_json()
    assert payload["reason_code"] == "invalid_request_value"
    assert payload["detail"] == "unsupported platform"


def test_routes_import_registers_local_endpoints(settings_env):
    routes = importlib.import_module("solstone.apps.settings.routes")
    journal_path, _config = settings_env(_settings_config())
    app = create_app(str(journal_path))
    registered = {rule.rule for rule in app.url_map.iter_rules()}

    assert routes.settings_bp is not None
    assert "/app/settings/api/providers/local/status" in registered
    assert "/app/settings/api/local/availability" in registered
    assert "/app/settings/api/local/bootstrap" in registered
    assert "/app/settings/api/local/bootstrap/status" in registered
    assert "/app/settings/api/local/models" in registered
