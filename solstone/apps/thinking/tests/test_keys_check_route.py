# SPDX-License-Identifier: AGPL-3.0-only
# Copyright (c) 2026 sol pbc

from __future__ import annotations

import json
import os
from datetime import datetime
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


def _read_config(journal_path):
    return json.loads((journal_path / "config" / "journal.json").read_text())


def _write_config(journal_path, config: dict) -> None:
    (journal_path / "config" / "journal.json").write_text(
        json.dumps(config, indent=2) + "\n",
        encoding="utf-8",
    )


def _assert_check_did_not_mutate(journal_path, before_bytes, before_env) -> None:
    assert (journal_path / "config" / "journal.json").read_bytes() == before_bytes
    assert dict(os.environ) == before_env
    assert not (journal_path / "config" / "journal.json.lock").exists()
    today = datetime.now().strftime("%Y%m%d")
    assert not (journal_path / "config" / "actions" / f"{today}.jsonl").exists()


def test_keys_check_validates_without_mutation(settings_client_with_journal):
    client, journal_path = settings_client_with_journal
    config_path = journal_path / "config" / "journal.json"

    for validation in (
        {"valid": True, "backend": "aistudio"},
        {
            "valid": False,
            "error": "bad key",
            "reason_code": "provider_key_invalid",
        },
    ):
        before_bytes = config_path.read_bytes()
        before_env = dict(os.environ)
        with patch(
            "solstone.apps.thinking.routes.validate_key",
            return_value=validation,
        ) as mock_validate:
            response = client.post(
                "/app/thinking/api/keys/check",
                json={"env_var": "GOOGLE_API_KEY", "value": " candidate-key "},
            )

        assert response.status_code == 200
        payload = response.get_json()
        assert payload["valid"] is validation["valid"]
        assert payload["provider"] == "google"
        assert "error" not in payload
        assert "backend" not in payload
        mock_validate.assert_called_once_with("google", "candidate-key")
        _assert_check_did_not_mutate(journal_path, before_bytes, before_env)


def test_keys_check_bad_env_var_and_empty_candidate(settings_client_with_journal):
    client, _journal_path = settings_client_with_journal

    bad_env = client.post(
        "/app/thinking/api/keys/check",
        json={"env_var": "NOT_A_KEY", "value": "candidate"},
    )
    assert bad_env.status_code == 400
    assert bad_env.get_json()["reason_code"] == "invalid_config_value"

    empty = client.post(
        "/app/thinking/api/keys/check",
        json={"env_var": "GOOGLE_API_KEY", "value": " \n "},
    )
    assert empty.status_code == 400
    assert empty.get_json()["detail"] == "value must not be empty"


def test_keys_check_rejects_non_string_value(settings_client_with_journal):
    client, _journal_path = settings_client_with_journal

    response = client.post(
        "/app/thinking/api/keys/check",
        json={"env_var": "GOOGLE_API_KEY", "value": 123},
    )

    assert response.status_code == 400
    assert response.get_json()["detail"] == "value must be a string"


def test_keys_check_validates_stripped_candidate(settings_client_with_journal):
    client, _journal_path = settings_client_with_journal

    with patch(
        "solstone.apps.thinking.routes.validate_key",
        return_value={"valid": True, "backend": "aistudio"},
    ) as mock_validate:
        response = client.post(
            "/app/thinking/api/keys/check",
            json={"env_var": "GOOGLE_API_KEY", "value": "  live-key\n"},
        )

    assert response.status_code == 200
    mock_validate.assert_called_once_with("google", "live-key")


def test_keys_check_invalid_candidate_does_not_change_active_lane(
    settings_client_with_journal,
):
    client, journal_path = settings_client_with_journal
    config = _read_config(journal_path)
    config["env"]["ANTHROPIC_API_KEY"] = "working-key-a"
    config["providers"]["active"] = {
        "provider": "anthropic",
        "model": "claude-opus-4-8",
    }
    config["providers"]["key_validation"]["anthropic"] = {
        "valid": True,
        "timestamp": "2026-07-13T12:00:00+00:00",
    }
    _write_config(journal_path, config)

    before_config = _read_config(journal_path)
    before_providers = client.get("/app/thinking/api/providers").get_json()

    with patch(
        "solstone.apps.thinking.routes.validate_key",
        return_value={
            "valid": False,
            "error": "bad candidate b",
            "reason_code": "provider_key_invalid",
        },
    ):
        response = client.post(
            "/app/thinking/api/keys/check",
            json={"env_var": "ANTHROPIC_API_KEY", "value": "garbage-key-b"},
        )

    assert response.status_code == 200
    assert response.get_json()["valid"] is False
    after_config = _read_config(journal_path)
    after_providers = client.get("/app/thinking/api/providers").get_json()
    assert after_config["env"] == before_config["env"]
    assert (
        after_config["providers"]["key_validation"]
        == before_config["providers"]["key_validation"]
    )
    assert after_providers["active_lane"] == before_providers["active_lane"]
