# SPDX-License-Identifier: AGPL-3.0-only
# Copyright (c) 2026 sol pbc

from __future__ import annotations

from solstone.convey import create_app


def test_day_activities_returns_collection_envelope(activities_env):
    journal, _facet, day, _day_path = activities_env(
        [
            {
                "id": "coding_090000_300",
                "activity": "coding",
                "title": "Focused coding",
                "segments": ["090000_300"],
                "created_at": 1,
            }
        ]
    )
    client = create_app(journal=str(journal)).test_client()

    response = client.get(f"/app/activities/api/day/{day}/activities")

    assert response.status_code == 200
    payload = response.get_json()
    assert set(payload) == {"items", "total"}
    assert len(payload["items"]) == 1
    assert payload["total"] == len(payload["items"])


def test_day_activities_empty_day_returns_empty_envelope(activities_env):
    journal, _facet, day, _day_path = activities_env(None)
    client = create_app(journal=str(journal)).test_client()

    response = client.get(f"/app/activities/api/day/{day}/activities")

    assert response.status_code == 200
    assert response.get_json() == {"items": [], "total": 0}
