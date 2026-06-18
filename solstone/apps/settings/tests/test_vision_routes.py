# SPDX-License-Identifier: AGPL-3.0-only
# Copyright (c) 2026 sol pbc

from __future__ import annotations

from solstone.convey import create_app
from solstone.think.utils import get_config


def _base_config() -> dict:
    return {
        "setup": {"completed_at": "2026-05-09T00:00:00Z"},
        "describe": {"max_extractions": 37},
    }


def _client(journal_path):
    app = create_app(str(journal_path))
    app.config["TESTING"] = True
    return app.test_client()


def test_vision_api_returns_persisted_max_extractions(settings_env):
    journal_path, _config = settings_env(_base_config())
    client = _client(journal_path)

    response = client.get("/app/settings/api/vision")

    assert response.status_code == 200
    assert response.get_json()["max_extractions"] == 37


def test_vision_api_put_round_trips_max_extractions(settings_env):
    journal_path, _config = settings_env(_base_config())
    client = _client(journal_path)

    response = client.put("/app/settings/api/vision", json={"max_extractions": 42})

    assert response.status_code == 200
    assert response.get_json()["max_extractions"] == 42
    assert get_config()["describe"]["max_extractions"] == 42


def test_vision_api_rejects_invalid_max_extractions(settings_env):
    journal_path, _config = settings_env(_base_config())
    client = _client(journal_path)

    response = client.put("/app/settings/api/vision", json={"max_extractions": 101})

    assert response.status_code == 400
    assert get_config()["describe"]["max_extractions"] == 37
