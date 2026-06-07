# SPDX-License-Identifier: AGPL-3.0-only
# Copyright (c) 2026 sol pbc

from __future__ import annotations

from solstone.convey import create_app
from tests._baseline_harness import make_logged_in_test_client
from tests.test_surfaces_ledger import (
    _commitment,
    _minimal_facet_tree,
    _utc_ms,
    _write_story_activity,
)

PREFIX = "/api/ledger"


def _assert_error(response, status: int) -> dict:
    assert response.status_code == status
    data = response.get_json()
    assert data["reason_code"]
    if status == 400:
        assert data["detail"]
    return data


def _configure_journal(tmp_path, monkeypatch) -> None:
    monkeypatch.setenv("SOLSTONE_JOURNAL", str(tmp_path))


def _decision(
    *,
    owner: str = "Mina",
    owner_entity_id: str | None = "mina",
    action: str = "move launch review",
    context: str = "Decision context.",
) -> dict:
    return {
        "owner": owner,
        "owner_entity_id": owner_entity_id,
        "action": action,
        "context": context,
    }


def test_ledger_list_collection_envelope_and_bound(tmp_path, monkeypatch):
    _configure_journal(tmp_path, monkeypatch)
    _minimal_facet_tree(tmp_path)
    for index in range(25):
        _write_story_activity(
            "work",
            "20260410",
            f"meeting_{index:06d}_300",
            _utc_ms(f"2026-04-10T09:{index:02d}:00Z"),
            commitments=[_commitment(action=f"action number {index}")],
        )
    client = make_logged_in_test_client(tmp_path)

    response = client.get(PREFIX)

    assert response.status_code == 200
    data = response.get_json()
    assert isinstance(data, dict)
    assert len(data["items"]) == 20
    assert data["total"] == 25


def test_ledger_list_counterparty_filter_narrows_total(tmp_path, monkeypatch):
    _configure_journal(tmp_path, monkeypatch)
    _minimal_facet_tree(tmp_path)
    for index in range(3):
        _write_story_activity(
            "work",
            "20260410",
            f"ravi_{index:06d}_300",
            _utc_ms(f"2026-04-10T09:{index:02d}:00Z"),
            commitments=[_commitment(action=f"ravi action {index}")],
        )
    for index in range(2):
        _write_story_activity(
            "work",
            "20260410",
            f"imani_{index:06d}_300",
            _utc_ms(f"2026-04-10T10:{index:02d}:00Z"),
            commitments=[
                _commitment(
                    action=f"imani action {index}",
                    counterparty="Imani",
                    counterparty_entity_id="imani",
                )
            ],
        )
    client = make_logged_in_test_client(tmp_path)

    all_response = client.get(PREFIX)
    filtered_response = client.get(f"{PREFIX}?counterparty=Ravi")

    assert all_response.status_code == 200
    assert filtered_response.status_code == 200
    assert filtered_response.get_json()["total"] < all_response.get_json()["total"]


def test_ledger_list_bad_state_returns_invalid_request_value(tmp_path, monkeypatch):
    _configure_journal(tmp_path, monkeypatch)
    _minimal_facet_tree(tmp_path)
    client = make_logged_in_test_client(tmp_path)

    data = _assert_error(client.get(f"{PREFIX}?state=bogus"), 400)

    assert data["reason_code"] == "invalid_request_value"


def test_ledger_list_bad_sort_returns_invalid_request_value(tmp_path, monkeypatch):
    _configure_journal(tmp_path, monkeypatch)
    _minimal_facet_tree(tmp_path)
    client = make_logged_in_test_client(tmp_path)

    data = _assert_error(client.get(f"{PREFIX}?sort=bogus"), 400)

    assert data["reason_code"] == "invalid_request_value"


def test_ledger_list_bad_age_days_gte_returns_invalid_request_value(
    tmp_path, monkeypatch
):
    _configure_journal(tmp_path, monkeypatch)
    _minimal_facet_tree(tmp_path)
    client = make_logged_in_test_client(tmp_path)

    data = _assert_error(client.get(f"{PREFIX}?age_days_gte=abc"), 400)

    assert data["reason_code"] == "invalid_request_value"


def test_ledger_list_bad_closed_since_returns_invalid_day(tmp_path, monkeypatch):
    _configure_journal(tmp_path, monkeypatch)
    _minimal_facet_tree(tmp_path)
    client = make_logged_in_test_client(tmp_path)

    notaday = _assert_error(client.get(f"{PREFIX}?closed_since=notaday"), 400)
    bad_month = _assert_error(client.get(f"{PREFIX}?closed_since=20261301"), 400)

    assert notaday["reason_code"] == "invalid_day"
    assert bad_month["reason_code"] == "invalid_day"


def test_ledger_decisions_collection_envelope_and_routing(tmp_path, monkeypatch):
    _configure_journal(tmp_path, monkeypatch)
    _minimal_facet_tree(tmp_path)
    _write_story_activity(
        "work",
        "20260410",
        "meeting_090000_300",
        _utc_ms("2026-04-10T09:00:00Z"),
        decisions=[_decision(action="move launch review")],
    )
    client = make_logged_in_test_client(tmp_path)

    response = client.get(f"{PREFIX}/decisions")

    assert response.status_code == 200
    data = response.get_json()
    assert isinstance(data, dict)
    assert data["total"] == 1
    assert data["items"][0]["action"] == "move launch review"


def test_ledger_decisions_bad_since_returns_invalid_day(tmp_path, monkeypatch):
    _configure_journal(tmp_path, monkeypatch)
    _minimal_facet_tree(tmp_path)
    client = make_logged_in_test_client(tmp_path)

    data = _assert_error(client.get(f"{PREFIX}/decisions?since=bad"), 400)

    assert data["reason_code"] == "invalid_day"


def test_ledger_get_item_returns_recursive_dataclass_dict(tmp_path, monkeypatch):
    _configure_journal(tmp_path, monkeypatch)
    _minimal_facet_tree(tmp_path)
    _write_story_activity(
        "work",
        "20260410",
        "meeting_090000_300",
        _utc_ms("2026-04-10T09:00:00Z"),
        commitments=[_commitment()],
    )
    client = make_logged_in_test_client(tmp_path)
    item_id = client.get(PREFIX).get_json()["items"][0]["id"]

    response = client.get(f"{PREFIX}/{item_id}")

    assert response.status_code == 200
    data = response.get_json()
    assert data["id"] == item_id
    assert data["sources"]
    assert "facet" in data["sources"][0]


def test_ledger_get_item_missing_returns_ledger_item_not_found(tmp_path, monkeypatch):
    _configure_journal(tmp_path, monkeypatch)
    _minimal_facet_tree(tmp_path)
    client = make_logged_in_test_client(tmp_path)

    data = _assert_error(client.get(f"{PREFIX}/does-not-exist"), 404)

    assert data["reason_code"] == "ledger_item_not_found"


def test_ledger_close_flips_state(tmp_path, monkeypatch):
    _configure_journal(tmp_path, monkeypatch)
    _minimal_facet_tree(tmp_path)
    _write_story_activity(
        "work",
        "20260410",
        "meeting_090000_300",
        _utc_ms("2026-04-10T09:00:00Z"),
        commitments=[_commitment()],
    )
    client = make_logged_in_test_client(tmp_path)
    item_id = client.get(PREFIX).get_json()["items"][0]["id"]

    closed_response = client.post(f"{PREFIX}/{item_id}/close", json={"note": "done"})
    dropped_response = client.post(
        f"{PREFIX}/{item_id}/close",
        json={"note": "drop", "as_state": "dropped"},
    )

    assert closed_response.status_code == 200
    assert closed_response.get_json()["state"] == "closed"
    assert dropped_response.status_code == 200
    assert dropped_response.get_json()["state"] == "dropped"


def test_ledger_close_decision_id_returns_ledger_item_not_found(tmp_path, monkeypatch):
    _configure_journal(tmp_path, monkeypatch)
    _minimal_facet_tree(tmp_path)
    _write_story_activity(
        "work",
        "20260410",
        "meeting_090000_300",
        _utc_ms("2026-04-10T09:00:00Z"),
        decisions=[_decision()],
    )
    client = make_logged_in_test_client(tmp_path)
    decision_id = client.get(f"{PREFIX}/decisions").get_json()["items"][0]["id"]

    data = _assert_error(
        client.post(f"{PREFIX}/{decision_id}/close", json={"note": "x"}), 404
    )

    assert data["reason_code"] == "ledger_item_not_found"


def test_ledger_close_no_body_returns_missing_request_body(tmp_path, monkeypatch):
    _configure_journal(tmp_path, monkeypatch)
    _minimal_facet_tree(tmp_path)
    client = make_logged_in_test_client(tmp_path)

    data = _assert_error(client.post(f"{PREFIX}/does-not-exist/close"), 400)

    assert data["reason_code"] == "missing_request_body"


def test_ledger_close_non_json_returns_invalid_json_request(tmp_path, monkeypatch):
    _configure_journal(tmp_path, monkeypatch)
    _minimal_facet_tree(tmp_path)
    client = make_logged_in_test_client(tmp_path)

    data = _assert_error(
        client.post(
            f"{PREFIX}/does-not-exist/close",
            data="not json",
            content_type="application/json",
        ),
        400,
    )

    assert data["reason_code"] == "invalid_json_request"


def test_ledger_close_empty_note_returns_missing_required_field(tmp_path, monkeypatch):
    _configure_journal(tmp_path, monkeypatch)
    _minimal_facet_tree(tmp_path)
    client = make_logged_in_test_client(tmp_path)

    data = _assert_error(
        client.post(f"{PREFIX}/does-not-exist/close", json={"note": "  "}), 400
    )

    assert data["reason_code"] == "missing_required_field"


def test_ledger_close_bad_as_state_returns_invalid_request_value(tmp_path, monkeypatch):
    _configure_journal(tmp_path, monkeypatch)
    _minimal_facet_tree(tmp_path)
    client = make_logged_in_test_client(tmp_path)

    data = _assert_error(
        client.post(
            f"{PREFIX}/does-not-exist/close",
            json={"note": "x", "as_state": "weird"},
        ),
        400,
    )

    assert data["reason_code"] == "invalid_request_value"


def test_ledger_requires_login(tmp_path, monkeypatch):
    _configure_journal(tmp_path, monkeypatch)
    app = create_app(journal=str(tmp_path))
    app.config["TESTING"] = True
    client = app.test_client()

    response = client.get(PREFIX)

    assert response.status_code == 302
