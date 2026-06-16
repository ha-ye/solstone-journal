# SPDX-License-Identifier: AGPL-3.0-only
# Copyright (c) 2026 sol pbc

from __future__ import annotations

import json

from solstone.apps.settings import routes as settings_routes
from solstone.convey import create_app
from solstone.think import facets


def _settings_client(settings_env):
    journal_path, _config = settings_env()
    config_path = journal_path / "config" / "journal.json"
    config = json.loads(config_path.read_text(encoding="utf-8"))
    config["setup"] = {"completed_at": "2026-05-23T00:00:00Z"}
    config_path.write_text(json.dumps(config, indent=2) + "\n", encoding="utf-8")
    app = create_app(str(journal_path))
    app.config["TESTING"] = True
    return journal_path, app.test_client()


def _facet_json_bytes(payload: dict[str, object]) -> str:
    return json.dumps(payload, indent=2, ensure_ascii=False) + "\n"


def _reason_code(response) -> str:
    return response.get_json()["reason_code"]


def test_create_facet_route_creates_owner_bytes_and_response_shape(settings_env):
    journal, client = _settings_client(settings_env)
    payload = {"title": "Personal", "emoji": "\U0001f3e0", "color": "#007bff"}

    response = client.post("/app/settings/api/facet", json=payload)

    assert response.status_code == 201
    body = response.get_json()
    expected_config = {
        "title": "Personal",
        "description": "",
        "color": "#007bff",
        "emoji": "\U0001f3e0",
    }
    assert body == {"success": True, "facet": "personal", "config": expected_config}
    assert "path" not in body["config"]
    assert "muted" not in body["config"]
    assert (journal / "facets" / "personal" / "facet.json").read_text(
        encoding="utf-8"
    ) == _facet_json_bytes(expected_config)


def test_create_facet_route_validation_order_and_slug_rule(settings_env):
    _journal, client = _settings_client(settings_env)

    empty = client.post("/app/settings/api/facet", json={"title": ""})
    symbols = client.post("/app/settings/api/facet", json={"title": "***"})
    leading_digit = client.post(
        "/app/settings/api/facet", json={"title": "2026 Planning"}
    )
    facets.create_facet("Personal")
    duplicate = client.post("/app/settings/api/facet", json={"title": "Personal"})

    assert empty.status_code == 400
    assert _reason_code(empty) == "missing_required_field"
    assert empty.get_json()["detail"] == "Title is required"
    assert symbols.status_code == 400
    assert _reason_code(symbols) == "invalid_request_value"
    assert leading_digit.status_code == 400
    assert _reason_code(leading_digit) == "invalid_request_value"
    assert leading_digit.get_json()["detail"] == "Title must start with a letter."
    assert duplicate.status_code == 409
    assert _reason_code(duplicate) == "facet_already_exists"


def test_create_facet_route_maps_owner_value_error(settings_env, monkeypatch):
    _journal, client = _settings_client(settings_env)

    def raise_value_error(*_args, **_kwargs):
        raise ValueError("boom")

    monkeypatch.setattr(settings_routes.facets, "create_facet", raise_value_error)

    response = client.post("/app/settings/api/facet", json={"title": "Boomtown"})

    assert response.status_code == 400
    assert _reason_code(response) == "invalid_request_value"


def test_update_facet_route_updates_field_and_returns_config_without_path(settings_env):
    journal, client = _settings_client(settings_env)
    slug = facets.create_facet("Research", emoji="R", color="#667eea")

    response = client.put(
        f"/app/settings/api/facet/{slug}",
        json={"color": "#123456"},
    )

    assert response.status_code == 200
    body = response.get_json()
    assert body["success"] is True
    assert body["facet"] == slug
    assert set(body["config"]) == {"title", "description", "color", "emoji", "muted"}
    assert "path" not in body["config"]
    assert body["config"]["muted"] is False
    expected_file = {
        "title": "Research",
        "description": "",
        "color": "#123456",
        "emoji": "R",
    }
    assert (journal / "facets" / slug / "facet.json").read_text(
        encoding="utf-8"
    ) == _facet_json_bytes(expected_file)


def test_update_facet_route_muted_only_regression(settings_env, monkeypatch):
    journal, client = _settings_client(settings_env)
    slug = facets.create_facet("Quiet", emoji="Q", color="#667eea")

    def fail_update_facet(*_args, **_kwargs):
        raise AssertionError("update_facet should not be called for muted-only bodies")

    monkeypatch.setattr(settings_routes.facets, "update_facet", fail_update_facet)

    mute_response = client.put(
        f"/app/settings/api/facet/{slug}",
        json={"muted": True},
    )
    mute_payload = json.loads(
        (journal / "facets" / slug / "facet.json").read_text(encoding="utf-8")
    )
    unmute_response = client.put(
        f"/app/settings/api/facet/{slug}",
        json={"muted": False},
    )
    unmute_payload = json.loads(
        (journal / "facets" / slug / "facet.json").read_text(encoding="utf-8")
    )

    assert mute_response.status_code == 200
    assert mute_payload["muted"] is True
    assert mute_response.get_json()["config"]["muted"] is True
    assert unmute_response.status_code == 200
    assert "muted" not in unmute_payload
    assert unmute_response.get_json()["config"]["muted"] is False


def test_update_facet_route_not_found(settings_env):
    _journal, client = _settings_client(settings_env)

    missing_with_field = client.put(
        "/app/settings/api/facet/missing",
        json={"color": "#000000"},
    )
    missing_with_unknown = client.put(
        "/app/settings/api/facet/missing",
        json={"bogus": 1},
    )

    assert missing_with_field.status_code == 404
    assert _reason_code(missing_with_field) == "facet_not_found"
    assert missing_with_unknown.status_code == 404
    assert _reason_code(missing_with_unknown) == "facet_not_found"


def test_update_facet_route_delete_race_maps_not_found(settings_env, monkeypatch):
    _journal, client = _settings_client(settings_env)
    slug = facets.create_facet("Race", emoji="R", color="#667eea")

    def raise_not_found(*_args, **_kwargs):
        raise FileNotFoundError("gone")

    monkeypatch.setattr(settings_routes.facets, "update_facet", raise_not_found)

    response = client.put(
        f"/app/settings/api/facet/{slug}",
        json={"color": "#000000"},
    )

    assert response.status_code == 404
    assert _reason_code(response) == "facet_not_found"


def test_facet_routes_single_audit_log_entry(settings_env):
    journal, client = _settings_client(settings_env)

    response = client.post(
        "/app/settings/api/facet",
        json={"title": "Personal", "emoji": "\U0001f3e0", "color": "#007bff"},
    )

    assert response.status_code == 201
    log_files = sorted((journal / "facets" / "personal" / "logs").glob("*.jsonl"))
    assert len(log_files) == 1
    lines = log_files[0].read_text(encoding="utf-8").splitlines()
    assert len(lines) == 1
    entry = json.loads(lines[0])
    assert entry["source"] == "call"
    assert entry["actor"] == "agent"


def test_update_sync_preserves_unrelated_schedule_entry(settings_env):
    journal, client = _settings_client(settings_env)
    schedules_path = journal / "config" / "schedules.json"
    unrelated = {"cmd": ["journal", "heartbeat"], "every": "daily"}
    schedules_path.write_text(
        json.dumps({"unrelated": unrelated}, indent=2) + "\n",
        encoding="utf-8",
    )

    response = client.put(
        "/app/settings/api/sync",
        json={
            "plaud": {"enabled": True},
            "granola": {"enabled": True},
            "obsidian": {"enabled": True},
        },
    )

    assert response.status_code == 200
    raw = json.loads(schedules_path.read_text(encoding="utf-8"))
    assert raw["unrelated"] == unrelated
    assert raw["sync:plaud"] == {
        "cmd": ["journal", "importer", "--sync", "plaud", "--save"],
        "every": "hourly",
        "enabled": True,
    }
    assert raw["sync:granola"] == {
        "cmd": ["journal", "importer", "--sync", "granola", "--save"],
        "every": "hourly",
        "enabled": True,
    }
    assert raw["sync:obsidian"] == {
        "cmd": ["journal", "importer", "--sync", "obsidian", "--save"],
        "every": "hourly",
        "enabled": True,
    }

    payload = response.get_json()
    assert set(payload) == {"plaud", "granola", "obsidian"}
    assert set(payload["plaud"]) == {"available", "enabled", "configured"}
    assert set(payload["granola"]) == {"enabled", "configured"}
    assert set(payload["obsidian"]) == {"available", "enabled", "configured"}
    assert payload["plaud"]["enabled"] is True
    assert payload["plaud"]["configured"] is True
    assert payload["granola"]["enabled"] is True
    assert payload["granola"]["configured"] is True
    assert payload["obsidian"]["enabled"] is True
    assert payload["obsidian"]["configured"] is True
