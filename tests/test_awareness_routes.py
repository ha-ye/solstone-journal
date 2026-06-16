# SPDX-License-Identifier: AGPL-3.0-only
# Copyright (c) 2026 sol pbc

from __future__ import annotations

import json

from solstone.apps import AppRegistry
from solstone.think.awareness import append_log
from tests._baseline_harness import make_test_client

PREFIX = "/app/awareness"


def _assert_error(response, status: int) -> dict:
    assert response.status_code == status
    data = response.get_json()
    assert data["reason_code"]
    if status == 400:
        assert data["detail"]
    return data


def test_awareness_api_only_discovery_registers_blueprint_outside_menu():
    registry = AppRegistry()
    registry.discover()

    assert "awareness" not in registry.apps
    assert any(bp.name == "app:awareness" for bp in registry.api_blueprints)


def test_awareness_index_404(journal_copy):
    client = make_test_client(journal_copy)

    response = client.get(f"{PREFIX}/")

    assert response.status_code == 404


def test_awareness_state_empty_journal_returns_empty_dict(tmp_path, monkeypatch):
    monkeypatch.setenv("SOLSTONE_JOURNAL", str(tmp_path))
    config_dir = tmp_path / "config"
    config_dir.mkdir(parents=True, exist_ok=True)
    (config_dir / "journal.json").write_text(
        json.dumps({"setup": {"completed_at": 1}}),
        encoding="utf-8",
    )
    client = make_test_client(tmp_path)

    response = client.get(f"{PREFIX}/api/state")

    assert response.status_code == 200
    assert response.get_json() == {}


def test_awareness_state_full_state_includes_journal(journal_copy):
    client = make_test_client(journal_copy)

    response = client.get(f"{PREFIX}/api/state")

    assert response.status_code == 200
    assert "journal" in response.get_json()


def test_awareness_state_known_section(journal_copy):
    client = make_test_client(journal_copy)

    response = client.get(f"{PREFIX}/api/state?section=journal")

    assert response.status_code == 200
    assert response.get_json()["first_daily_ready"] is True


def test_awareness_state_unknown_section(journal_copy):
    client = make_test_client(journal_copy)

    response = client.get(f"{PREFIX}/api/state?section=nope")

    data = _assert_error(response, 404)
    assert data["reason_code"] == "awareness_section_not_found"


def test_awareness_imports_get_defaults(journal_copy):
    client = make_test_client(journal_copy)

    response = client.get(f"{PREFIX}/api/imports")

    assert response.status_code == 200
    assert response.get_json()["has_imported"] is False


def test_awareness_imports_post_record(journal_copy):
    client = make_test_client(journal_copy)

    response = client.post(f"{PREFIX}/api/imports", json={"record": "chatgpt"})

    assert response.status_code == 200
    data = response.get_json()
    assert data["has_imported"] is True
    assert "chatgpt" in data["sources_used"]


def test_awareness_imports_post_declined(journal_copy):
    client = make_test_client(journal_copy)

    response = client.post(f"{PREFIX}/api/imports", json={"declined": True})

    assert response.status_code == 200
    assert response.get_json()["offer_declined"] is not None


def test_awareness_imports_post_nudge(journal_copy):
    client = make_test_client(journal_copy)

    response = client.post(f"{PREFIX}/api/imports", json={"nudge": True})

    assert response.status_code == 200
    assert response.get_json()["last_nudge"] is not None


def test_awareness_imports_post_multi_action_400(journal_copy):
    client = make_test_client(journal_copy)

    response = client.post(
        f"{PREFIX}/api/imports",
        json={"record": "x", "nudge": True},
    )

    _assert_error(response, 400)


def test_awareness_imports_post_zero_action_400(journal_copy):
    client = make_test_client(journal_copy)

    response = client.post(f"{PREFIX}/api/imports", json={})

    _assert_error(response, 400)


def test_awareness_log_collection_limit_and_kind_filter(journal_copy):
    append_log("observation", message="a")
    append_log("observation", message="b")
    append_log("nudge", message="c")
    client = make_test_client(journal_copy)

    response = client.get(f"{PREFIX}/api/log?limit=2")

    assert response.status_code == 200
    data = response.get_json()
    assert len(data["items"]) == 2
    assert data["total"] == 3

    response = client.get(f"{PREFIX}/api/log?kind=observation")

    assert response.status_code == 200
    data = response.get_json()
    assert data["total"] == 2
    assert all(item["kind"] == "observation" for item in data["items"])


def test_awareness_log_day_param_uses_requested_day(journal_copy):
    append_log("observation", message="old", day="20260101")
    client = make_test_client(journal_copy)

    response = client.get(f"{PREFIX}/api/log?day=20260101")

    assert response.status_code == 200
    data = response.get_json()
    assert data["total"] >= 1
    assert any(item.get("message") == "old" for item in data["items"])

    response = client.get(f"{PREFIX}/api/log")

    assert response.status_code == 200
    assert all(item.get("message") != "old" for item in response.get_json()["items"])


def test_awareness_log_post_creates_201(journal_copy):
    client = make_test_client(journal_copy)

    response = client.post(
        f"{PREFIX}/api/log",
        json={"kind": "observation", "message": "hi"},
    )

    assert response.status_code == 201
    data = response.get_json()
    assert data["kind"] == "observation"
    assert "ts" in data


def test_awareness_log_post_missing_kind_400(journal_copy):
    client = make_test_client(journal_copy)

    response = client.post(f"{PREFIX}/api/log", json={})

    _assert_error(response, 400)


def test_awareness_log_post_empty_kind_400(journal_copy):
    client = make_test_client(journal_copy)

    response = client.post(f"{PREFIX}/api/log", json={"kind": ""})

    _assert_error(response, 400)


def test_awareness_post_endpoints_no_body_400(journal_copy):
    client = make_test_client(journal_copy)

    imports_response = client.post(f"{PREFIX}/api/imports")
    log_response = client.post(f"{PREFIX}/api/log")

    imports_data = _assert_error(imports_response, 400)
    log_data = _assert_error(log_response, 400)
    assert imports_data["reason_code"] == "missing_request_body"
    assert log_data["reason_code"] == "missing_request_body"


def test_awareness_post_endpoints_non_json_400(journal_copy):
    client = make_test_client(journal_copy)

    imports_response = client.post(
        f"{PREFIX}/api/imports",
        data="not json",
        content_type="application/json",
    )
    log_response = client.post(
        f"{PREFIX}/api/log",
        data="not json",
        content_type="application/json",
    )

    imports_data = _assert_error(imports_response, 400)
    log_data = _assert_error(log_response, 400)
    assert imports_data["reason_code"] == "invalid_json_request"
    assert log_data["reason_code"] == "invalid_json_request"
