# SPDX-License-Identifier: AGPL-3.0-only
# Copyright (c) 2026 sol pbc

from __future__ import annotations

from datetime import datetime

from solstone.convey import create_app


def test_api_pulse_includes_needs_you_items_json_shape(journal_copy, monkeypatch):
    import solstone.apps.home.routes as home_routes

    needs_you_item = {
        "text": "Review the launch checklist",
        "kind": "chat",
        "payload": {"prompt": "let's dig into Review the launch checklist"},
    }

    monkeypatch.setattr(
        home_routes,
        "_build_pulse_context",
        lambda: {
            "now": datetime(2026, 5, 24, 12, 0),
            "attention": None,
            "needs_you_items": [needs_you_item],
            "show_welcome": False,
        },
    )

    client = create_app(str(journal_copy)).test_client()
    response = client.get("/app/home/api/pulse")

    assert response.status_code == 200
    payload = response.get_json()
    assert payload["needs_you_items"] == [needs_you_item]
    assert list(payload["needs_you_items"][0]) == ["kind", "payload", "text"]
