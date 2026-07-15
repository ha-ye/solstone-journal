# SPDX-License-Identifier: AGPL-3.0-only
# Copyright (c) 2026 sol pbc

from __future__ import annotations

from solstone.convey import create_app
from solstone.convey.reasons import INVALID_CONFIG_VALUE


def _base_config() -> dict:
    return {
        "setup": {"completed_at": 1700000000000},
    }


def _client(journal_path):
    app = create_app(str(journal_path))
    app.config["TESTING"] = True
    return app.test_client()


def _post_processing(client, data: dict):
    return client.post(
        "/app/settings/api/config",
        json={"section": "processing", "data": data},
    )


def test_processing_api_get_returns_effective_defaults(settings_env):
    journal_path, _config = settings_env(_base_config())
    client = _client(journal_path)

    response = client.get("/app/settings/api/processing")

    assert response.status_code == 200
    payload = response.get_json()
    assert payload["mode"] == "realtime"
    assert payload["gate"]["time_window"]["start"] == "02:00"


def test_processing_api_post_round_trips_nested_window(settings_env):
    journal_path, _config = settings_env(_base_config())
    client = _client(journal_path)

    response = _post_processing(
        client,
        {
            "mode": "deferred",
            "gate": {"time_window": {"start": "01:00", "end": "05:00"}},
        },
    )

    assert response.status_code == 200
    payload = client.get("/app/settings/api/processing").get_json()
    assert payload["mode"] == "deferred"
    assert payload["gate"]["time_window"] == {
        "enabled": True,
        "start": "01:00",
        "end": "05:00",
    }
    assert payload["gate"]["display_powersave"] == {"enabled": False}


def test_processing_api_post_bad_hhmm_returns_invalid_config(settings_env):
    journal_path, _config = settings_env(_base_config())
    client = _client(journal_path)

    response = _post_processing(
        client,
        {"gate": {"time_window": {"start": "25:99"}}},
    )

    assert response.status_code == 400
    payload = response.get_json()
    assert payload["reason_code"] == INVALID_CONFIG_VALUE.code
    assert "HH:MM" in payload["detail"]


def test_processing_api_accepts_deferred_without_enabled_conditions(settings_env):
    journal_path, _config = settings_env(_base_config())
    client = _client(journal_path)

    response = _post_processing(
        client,
        {
            "mode": "deferred",
            "gate": {
                "time_window": {"enabled": False},
                "display_powersave": {"enabled": False},
            },
        },
    )

    assert response.status_code == 200
    payload = client.get("/app/settings/api/processing").get_json()
    assert payload["mode"] == "deferred"
    assert payload["gate"]["time_window"]["enabled"] is False
    assert payload["gate"]["display_powersave"]["enabled"] is False


def test_processing_api_deep_merges_partial_updates(settings_env):
    journal_path, _config = settings_env(_base_config())
    client = _client(journal_path)

    first = _post_processing(
        client,
        {
            "mode": "deferred",
            "gate": {"time_window": {"start": "01:00", "end": "05:00"}},
        },
    )
    second = _post_processing(client, {"mode": "realtime"})

    assert first.status_code == 200
    assert second.status_code == 200
    payload = client.get("/app/settings/api/processing").get_json()
    assert payload["mode"] == "realtime"
    assert payload["gate"]["time_window"]["start"] == "01:00"
    assert payload["gate"]["time_window"]["end"] == "05:00"
