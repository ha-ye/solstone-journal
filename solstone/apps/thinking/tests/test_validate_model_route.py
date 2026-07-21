# SPDX-License-Identifier: AGPL-3.0-only
# Copyright (c) 2026 sol pbc

from __future__ import annotations

import json
from unittest.mock import patch

import pytest

from solstone.convey import create_app


@pytest.fixture
def settings_client_with_journal(settings_env):
    journal_path, config = settings_env()
    config["setup"] = {"completed_at": 1700000000000}
    config_path = journal_path / "config" / "journal.json"
    config_path.write_text(json.dumps(config, indent=2) + "\n", encoding="utf-8")
    app = create_app(str(journal_path))
    app.config["TESTING"] = True
    return app.test_client(), journal_path


def _write_config(journal_path, config: dict) -> None:
    (journal_path / "config" / "journal.json").write_text(
        json.dumps(config, indent=2) + "\n",
        encoding="utf-8",
    )


def test_validate_model_route_rejects_missing_body(settings_client_with_journal):
    client, _journal_path = settings_client_with_journal

    response = client.post("/app/thinking/api/validate-model", json={})

    assert response.status_code == 400
    payload = response.get_json()
    assert payload["reason_code"] == "missing_request_body"
    assert payload["detail"] == "No data provided"


def test_validate_model_route_rejects_bad_provider(settings_client_with_journal):
    client, _journal_path = settings_client_with_journal

    response = client.post(
        "/app/thinking/api/validate-model",
        json={"provider": "local", "model": "local/qwen3.5-4b"},
    )

    assert response.status_code == 400
    payload = response.get_json()
    assert payload["reason_code"] == "invalid_request_value"
    assert payload["detail"] == (
        "Invalid provider: local. Must be one of: anthropic, google, openai"
    )


def test_validate_model_route_rejects_empty_model(settings_client_with_journal):
    client, _journal_path = settings_client_with_journal

    response = client.post(
        "/app/thinking/api/validate-model",
        json={"provider": "google", "model": " "},
    )

    assert response.status_code == 400
    payload = response.get_json()
    assert payload["reason_code"] == "invalid_request_value"
    assert payload["detail"] == "model must be a non-empty string."


def test_validate_model_route_reports_missing_stored_key(
    settings_client_with_journal,
):
    client, journal_path = settings_client_with_journal
    config_path = journal_path / "config" / "journal.json"
    config = json.loads(config_path.read_text(encoding="utf-8"))
    config["env"].pop("ANTHROPIC_API_KEY", None)
    _write_config(journal_path, config)

    response = client.post(
        "/app/thinking/api/validate-model",
        json={"provider": "anthropic", "model": " claude-opus-4-8 "},
    )

    assert response.status_code == 200
    payload = response.get_json()
    assert payload == {
        "valid": False,
        "provider": "anthropic",
        "model": "claude-opus-4-8",
        "reason_code": "key_missing",
        "message": "No stored API key for provider.",
    }
    assert "error" not in payload


def test_validate_model_route_relays_success_and_does_not_write_config(
    settings_client_with_journal,
):
    client, journal_path = settings_client_with_journal
    config_path = journal_path / "config" / "journal.json"
    before = config_path.read_bytes()

    with patch(
        "solstone.apps.thinking.routes.validate_model",
        return_value={"valid": True, "extra": "dropped"},
    ) as mock_validate:
        response = client.post(
            "/app/thinking/api/validate-model",
            json={"provider": "google", "model": " gemini-3.5-flash "},
        )

    assert response.status_code == 200
    assert response.get_json() == {
        "valid": True,
        "provider": "google",
        "model": "gemini-3.5-flash",
    }
    mock_validate.assert_called_once_with(
        "google",
        "gemini-3.5-flash",
        "test-google-key",
    )
    assert config_path.read_bytes() == before


def test_validate_model_route_relays_failure_with_probe_identity(
    settings_client_with_journal,
):
    client, _journal_path = settings_client_with_journal

    with patch(
        "solstone.apps.thinking.routes.validate_model",
        return_value={
            "valid": False,
            "error": "model missing",
            "reason_code": "model_not_found",
            "extra": "dropped",
        },
    ) as mock_validate:
        response = client.post(
            "/app/thinking/api/validate-model",
            json={"provider": "openai", "model": " gpt-5.5 "},
        )

    assert response.status_code == 200
    assert response.get_json() == {
        "valid": False,
        "provider": "openai",
        "model": "gpt-5.5",
        "reason_code": "model_not_found",
        "message": "model missing",
    }
    assert "error" not in response.get_json()
    mock_validate.assert_called_once_with(
        "openai",
        "gpt-5.5",
        "test-openai-key",
    )
