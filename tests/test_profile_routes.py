# SPDX-License-Identifier: AGPL-3.0-only
# Copyright (c) 2026 sol pbc

from __future__ import annotations

import json

from solstone.convey import create_app
from solstone.think.surfaces import profile as profile_surface
from tests._baseline_harness import make_test_client
from tests.test_surfaces_profile import (
    _activity_record,
    _append_activity,
    _commitment,
    _minimal_facet_tree,
    _participant,
    _utc_ms,
    _write_facet_relationship,
    _write_story_activity,
)

PROFILE_PREFIX = "/api/profile"
PROFILES_PREFIX = "/api/profiles"


def _assert_error(response, status: int) -> dict:
    assert response.status_code == status
    data = response.get_json()
    assert data["reason_code"]
    if status == 400:
        assert data["detail"]
    return data


def _configure_journal(tmp_path, monkeypatch) -> None:
    monkeypatch.setenv("SOLSTONE_JOURNAL", str(tmp_path))
    config_dir = tmp_path / "config"
    config_dir.mkdir(parents=True, exist_ok=True)
    (config_dir / "journal.json").write_text(
        json.dumps({"setup": {"completed_at": 1}}),
        encoding="utf-8",
    )


def _configure_unset_journal(tmp_path, monkeypatch) -> None:
    monkeypatch.setenv("SOLSTONE_JOURNAL", str(tmp_path))


def _seed_ravi(tmp_path, monkeypatch) -> None:
    _configure_journal(tmp_path, monkeypatch)
    _minimal_facet_tree(
        tmp_path,
        journal_entities=({"id": "ravi", "name": "Ravi", "type": "Person"},),
    )
    _write_facet_relationship(tmp_path, "work", "ravi", description="Customer")
    monkeypatch.setattr(profile_surface, "_today_day", lambda: "20260607")
    _write_story_activity(
        "work",
        "20260605",
        "meeting_090000_300",
        _utc_ms("20260605", 9),
        commitments=[_commitment(counterparty="Ravi", counterparty_entity_id="ravi")],
    )
    _append_activity(
        "work",
        "20260605",
        _activity_record(
            "20260605",
            [_participant("ravi", name="Ravi")],
            record_id="meeting_part",
        ),
    )


def test_profile_full_composed(tmp_path, monkeypatch):
    _seed_ravi(tmp_path, monkeypatch)
    client = make_test_client(tmp_path)

    response = client.get(f"{PROFILE_PREFIX}/Ravi")

    assert response.status_code == 200
    data = response.get_json()
    assert data["name"] == "Ravi"
    assert data["open_with_them"]
    assert "facet" in data["open_with_them"][0]["sources"][0]
    assert data["sources"]
    assert "activity_id" in data["sources"][0]


def test_profile_full_not_found(tmp_path, monkeypatch):
    _configure_journal(tmp_path, monkeypatch)
    _minimal_facet_tree(tmp_path)
    monkeypatch.setattr(profile_surface, "_today_day", lambda: "20260607")
    client = make_test_client(tmp_path)

    data = _assert_error(client.get(f"{PROFILE_PREFIX}/missing"), 404)

    assert data["reason_code"] == "entity_not_found"


def test_profile_brief(tmp_path, monkeypatch):
    _seed_ravi(tmp_path, monkeypatch)
    client = make_test_client(tmp_path)

    response = client.get(f"{PROFILE_PREFIX}/Ravi/brief")

    assert response.status_code == 200
    assert response.get_json()["open_loop_count"] > 0


def test_profile_cadence(tmp_path, monkeypatch):
    _seed_ravi(tmp_path, monkeypatch)
    client = make_test_client(tmp_path)

    response = client.get(f"{PROFILE_PREFIX}/Ravi/cadence")
    mentions_response = client.get(
        f"{PROFILE_PREFIX}/Ravi/cadence?include_mentions=true"
    )

    assert response.status_code == 200
    assert response.get_json()["recent_interactions_count_30d"] >= 1
    assert mentions_response.status_code == 200


def test_profiles_active_collection_envelope(tmp_path, monkeypatch):
    _seed_ravi(tmp_path, monkeypatch)
    client = make_test_client(tmp_path)

    response = client.get(f"{PROFILES_PREFIX}/active")

    assert response.status_code == 200
    data = response.get_json()
    assert isinstance(data, dict)
    assert "items" in data
    assert data["total"] >= 1
    assert "ravi" in data["items"]


def test_profiles_active_bad_window_days_returns_invalid_request_value(
    tmp_path, monkeypatch
):
    _configure_journal(tmp_path, monkeypatch)
    _minimal_facet_tree(tmp_path)
    monkeypatch.setattr(profile_surface, "_today_day", lambda: "20260607")
    client = make_test_client(tmp_path)

    data = _assert_error(client.get(f"{PROFILES_PREFIX}/active?window_days=abc"), 400)

    assert data["reason_code"] == "invalid_request_value"


def test_profile_redirects_to_init_when_setup_incomplete(tmp_path, monkeypatch):
    _configure_unset_journal(tmp_path, monkeypatch)
    app = create_app(journal=str(tmp_path))
    app.config["TESTING"] = True
    client = app.test_client()

    response = client.get(f"{PROFILE_PREFIX}/Ravi")

    assert response.status_code == 302
    assert "/init" in response.headers["Location"]
