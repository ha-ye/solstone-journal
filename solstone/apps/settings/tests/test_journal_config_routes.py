# SPDX-License-Identifier: AGPL-3.0-only
# Copyright (c) 2026 sol pbc

from __future__ import annotations

from pathlib import Path

from solstone.convey import create_app
from solstone.convey.reasons import INVALID_CONFIG_VALUE
from solstone.think.utils import get_config


def _base_config() -> dict:
    return {
        "setup": {"completed_at": 1700000000000},
    }


def _client(journal_path: Path):
    app = create_app(str(journal_path))
    app.config["TESTING"] = True
    return app.test_client()


def test_journal_name_round_trips_through_settings_config(settings_env):
    journal_path, _config = settings_env(_base_config())
    client = _client(journal_path)

    response = client.put(
        "/app/settings/api/config",
        json={"section": "journal", "data": {"name": "Home Base"}},
    )

    assert response.status_code == 200
    assert response.get_json()["config"]["journal"]["name"] == "Home Base"

    get_response = client.get("/app/settings/api/config")
    assert get_response.status_code == 200
    assert get_response.get_json()["journal"]["name"] == "Home Base"
    assert get_config()["journal"]["name"] == "Home Base"

    legacy_response = client.put(
        "/app/settings/api/config",
        json={"section": "journal", "key": "name", "value": "Legacy"},
    )

    assert legacy_response.status_code == 200
    assert get_config()["journal"]["name"] == "Legacy"


def test_journal_config_filters_unknown_fields(settings_env):
    journal_path, _config = settings_env(_base_config())
    client = _client(journal_path)

    response = client.put(
        "/app/settings/api/config",
        json={"section": "journal", "data": {"name": "Named", "bogus": "Y"}},
    )

    assert response.status_code == 200
    journal_config = response.get_json()["config"]["journal"]
    assert journal_config["name"] == "Named"
    assert "bogus" not in journal_config

    persisted_journal_config = get_config()["journal"]
    assert persisted_journal_config["name"] == "Named"
    assert "bogus" not in persisted_journal_config


def test_journal_name_rejects_empty_strings_but_allows_null(settings_env):
    journal_path, _config = settings_env(_base_config())
    client = _client(journal_path)

    empty_response = client.put(
        "/app/settings/api/config",
        json={"section": "journal", "data": {"name": ""}},
    )

    assert empty_response.status_code == 400
    empty_payload = empty_response.get_json()
    assert empty_payload["reason_code"] == INVALID_CONFIG_VALUE.code
    assert empty_payload["detail"] == "Journal name cannot be empty"

    whitespace_response = client.put(
        "/app/settings/api/config",
        json={"section": "journal", "data": {"name": "   "}},
    )

    assert whitespace_response.status_code == 400
    whitespace_payload = whitespace_response.get_json()
    assert whitespace_payload["reason_code"] == INVALID_CONFIG_VALUE.code
    assert whitespace_payload["detail"] == "Journal name cannot be empty"

    null_response = client.put(
        "/app/settings/api/config",
        json={"section": "journal", "data": {"name": None}},
    )

    assert null_response.status_code == 200
    assert get_config()["journal"]["name"] is None


def test_journal_config_get_without_journal_section(settings_env):
    journal_path, _config = settings_env(_base_config())
    client = _client(journal_path)

    response = client.get("/app/settings/api/config")

    assert response.status_code == 200
    assert "journal" not in response.get_json()
