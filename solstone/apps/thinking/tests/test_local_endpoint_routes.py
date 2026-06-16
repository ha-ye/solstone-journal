# SPDX-License-Identifier: AGPL-3.0-only
# Copyright (c) 2026 sol pbc

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

from solstone.apps.thinking import routes as thinking_routes
from solstone.convey import create_app

PLACEHOLDER_CREDENTIAL = "test-token-PLACEHOLDER"


def _client(journal_path: Path):
    app = create_app(str(journal_path))
    app.config["TESTING"] = True
    return app.test_client()


def _config_path(journal_path: Path) -> Path:
    return journal_path / "config" / "journal.json"


def _read_config(journal_path: Path) -> dict[str, Any]:
    return json.loads(_config_path(journal_path).read_text(encoding="utf-8"))


def _write_config(journal_path: Path, config: dict[str, Any]) -> None:
    _config_path(journal_path).write_text(
        json.dumps(config, indent=2) + "\n",
        encoding="utf-8",
    )


def _ready_settings_env(settings_env) -> tuple[Path, dict[str, Any]]:
    journal_path, config = settings_env()
    config["setup"] = {"completed_at": "2026-05-23T00:00:00Z"}
    _write_config(journal_path, config)
    return journal_path, config


def test_local_endpoint_post_sets_normalized_values_and_masks_credential(
    settings_env,
    monkeypatch,
):
    journal_path, _config = _ready_settings_env(settings_env)
    calls: list[dict[str, Any]] = []
    monkeypatch.setattr(
        thinking_routes, "log_app_action", lambda **kwargs: calls.append(kwargs)
    )
    client = _client(journal_path)

    response = client.post(
        "/app/thinking/api/local/endpoint",
        json={
            "endpoint_url": " http://host.test:8080/openai/v1/ ",
            "served_model_id": " served-model ",
            "credential": PLACEHOLDER_CREDENTIAL,
        },
    )

    assert response.status_code == 200
    payload = response.get_json()
    assert payload["local_endpoint"] == {
        "enabled": True,
        "endpoint_url": "http://host.test:8080/openai",
        "served_model_id": "served-model",
        "credential_configured": True,
    }
    config = _read_config(journal_path)
    assert config["providers"]["local"] == {
        "endpoint_url": "http://host.test:8080/openai",
        "served_model_id": "served-model",
        "credential": PLACEHOLDER_CREDENTIAL,
    }

    public_providers = client.get("/app/thinking/api/providers").get_json()
    assert public_providers["local_override"] == {
        "enabled": True,
        "endpoint_url": "http://host.test:8080/openai",
        "served_model_id": "served-model",
        "credential_configured": True,
    }
    assert PLACEHOLDER_CREDENTIAL not in json.dumps(public_providers)
    assert PLACEHOLDER_CREDENTIAL not in json.dumps(payload)

    audit = json.dumps(calls)
    assert calls[0]["action"] == "local_endpoint_update"
    assert "***" in audit
    assert PLACEHOLDER_CREDENTIAL not in audit


def test_local_endpoint_post_preserves_credential_when_absent(settings_env):
    journal_path, config = _ready_settings_env(settings_env)
    config["providers"]["local"] = {
        "endpoint_url": "http://old.test",
        "served_model_id": "old-model",
        "credential": PLACEHOLDER_CREDENTIAL,
    }
    _write_config(journal_path, config)
    client = _client(journal_path)

    response = client.post(
        "/app/thinking/api/local/endpoint",
        json={
            "endpoint_url": "https://new.test/v1",
            "served_model_id": "new-model",
        },
    )

    assert response.status_code == 200
    local_config = _read_config(journal_path)["providers"]["local"]
    assert local_config == {
        "endpoint_url": "https://new.test",
        "served_model_id": "new-model",
        "credential": PLACEHOLDER_CREDENTIAL,
    }


def test_local_endpoint_post_empty_credential_clears_secret(settings_env):
    journal_path, config = _ready_settings_env(settings_env)
    config["providers"]["local"] = {
        "endpoint_url": "http://old.test",
        "served_model_id": "old-model",
        "credential": PLACEHOLDER_CREDENTIAL,
    }
    _write_config(journal_path, config)
    client = _client(journal_path)

    response = client.post(
        "/app/thinking/api/local/endpoint",
        json={
            "endpoint_url": "http://old.test",
            "served_model_id": "old-model",
            "credential": "",
        },
    )

    assert response.status_code == 200
    assert "credential" not in _read_config(journal_path)["providers"]["local"]
    assert response.get_json()["local_endpoint"]["credential_configured"] is False


def test_local_endpoint_delete_clears_values_and_masks_audit(
    settings_env,
    monkeypatch,
):
    journal_path, config = _ready_settings_env(settings_env)
    config["providers"]["local"] = {
        "endpoint_url": "http://old.test",
        "served_model_id": "old-model",
        "credential": PLACEHOLDER_CREDENTIAL,
    }
    _write_config(journal_path, config)
    calls: list[dict[str, Any]] = []
    monkeypatch.setattr(
        thinking_routes, "log_app_action", lambda **kwargs: calls.append(kwargs)
    )
    client = _client(journal_path)

    response = client.delete("/app/thinking/api/local/endpoint")

    assert response.status_code == 200
    assert response.get_json()["local_endpoint"] == {
        "enabled": False,
        "endpoint_url": "",
        "served_model_id": "",
        "credential_configured": False,
    }
    local_config = _read_config(journal_path)["providers"]["local"]
    assert "endpoint_url" not in local_config
    assert "served_model_id" not in local_config
    assert "credential" not in local_config
    audit = json.dumps(calls)
    assert calls[0]["action"] == "local_endpoint_clear"
    assert "***" in audit
    assert PLACEHOLDER_CREDENTIAL not in audit


def test_local_endpoint_post_rejects_bad_url(settings_env):
    journal_path, _config = _ready_settings_env(settings_env)
    client = _client(journal_path)

    response = client.post(
        "/app/thinking/api/local/endpoint",
        json={"endpoint_url": "localhost:8080", "served_model_id": "model"},
    )

    assert response.status_code == 400
    payload = response.get_json()
    assert payload["reason_code"] == "invalid_config_value"
    assert "endpoint_url" in payload["detail"]
