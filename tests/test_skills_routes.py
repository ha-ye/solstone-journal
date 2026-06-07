# SPDX-License-Identifier: AGPL-3.0-only
# Copyright (c) 2026 sol pbc

from __future__ import annotations

from solstone.apps import AppRegistry
from solstone.think.skills import save_patterns, save_profile
from tests._baseline_harness import make_logged_in_test_client

PREFIX = "/app/skills"


def _assert_error(response, status: int) -> dict:
    assert response.status_code == status
    data = response.get_json()
    assert data["reason_code"]
    if status == 400:
        assert data["detail"]
    return data


def _pattern(slug: str, status: str, **over) -> dict:
    pattern = {
        "slug": slug,
        "name": slug.replace("-", " ").title(),
        "status": status,
        "observations": [
            {
                "day": "2026-01-01",
                "facet": "work",
                "activity_ids": ["a"],
                "notes": "",
                "recorded_at": "2026-01-01T00:00:00Z",
            }
        ],
        "facets_touched": ["work"],
        "first_seen": "2026-01-01",
        "last_seen": "2026-01-01",
        "needs_profile": False,
        "needs_refresh": False,
        "profile_generated_at": None,
        "created_at": "2026-01-01T00:00:00Z",
        "updated_at": "2026-01-01T00:00:00Z",
    }
    pattern.update(over)
    return pattern


def test_skills_api_only_discovery_registers_blueprint_outside_menu():
    registry = AppRegistry()
    registry.discover()

    assert "skills" not in registry.apps
    assert any(bp.name == "app:skills" for bp in registry.api_blueprints)


def test_skills_index_404(journal_copy):
    client = make_logged_in_test_client(journal_copy)

    response = client.get(f"{PREFIX}/")

    assert response.status_code == 404


def test_skills_collection_returns_items_and_total(journal_copy):
    save_patterns(
        [
            _pattern("one", "emerging"),
            _pattern("two", "mature"),
            _pattern("three", "dormant"),
        ]
    )
    client = make_logged_in_test_client(journal_copy)

    response = client.get(f"{PREFIX}/api/patterns")

    assert response.status_code == 200
    data = response.get_json()
    assert "items" in data
    assert data["total"] == 3


def test_skills_collection_limit_bounds_page_size(journal_copy):
    save_patterns(
        [
            _pattern("one", "emerging"),
            _pattern("two", "mature"),
            _pattern("three", "dormant"),
        ]
    )
    client = make_logged_in_test_client(journal_copy)

    response = client.get(f"{PREFIX}/api/patterns?limit=2")

    assert response.status_code == 200
    data = response.get_json()
    assert len(data["items"]) == 2
    assert data["total"] == 3


def test_skills_collection_status_filter(journal_copy):
    save_patterns(
        [
            _pattern("one", "emerging"),
            _pattern("two", "dormant"),
        ]
    )
    client = make_logged_in_test_client(journal_copy)

    response = client.get(f"{PREFIX}/api/patterns?status=dormant")

    assert response.status_code == 200
    data = response.get_json()
    assert data["total"] == 1
    assert all(item["status"] == "dormant" for item in data["items"])

    response = client.get(f"{PREFIX}/api/patterns?status=emerging,dormant")

    assert response.status_code == 200
    assert response.get_json()["total"] == 2


def test_skills_item_read_includes_profile(journal_copy):
    save_patterns([_pattern("one", "emerging")])
    save_profile("one", "# md")
    client = make_logged_in_test_client(journal_copy)

    response = client.get(f"{PREFIX}/api/patterns/one")

    assert response.status_code == 200
    data = response.get_json()
    assert data["pattern"]["slug"] == "one"
    assert data["profile"] == "# md"


def test_skills_item_read_profile_null(journal_copy):
    save_patterns([_pattern("one", "emerging")])
    client = make_logged_in_test_client(journal_copy)

    response = client.get(f"{PREFIX}/api/patterns/one")

    assert response.status_code == 200
    assert response.get_json()["profile"] is None


def test_skills_item_read_miss_404(journal_copy):
    client = make_logged_in_test_client(journal_copy)

    response = client.get(f"{PREFIX}/api/patterns/nope")

    data = _assert_error(response, 404)
    assert data["reason_code"] == "skill_not_found"


def test_skills_seed_creates_and_persists_pattern(journal_copy):
    client = make_logged_in_test_client(journal_copy)

    response = client.post(
        f"{PREFIX}/api/patterns",
        json={
            "slug": "seeded",
            "name": "Seeded",
            "day": "2026-01-01",
            "facet": "work",
            "activity_ids": ["a"],
            "notes": "first",
        },
    )

    assert response.status_code == 201
    data = response.get_json()
    assert data["status"] == "emerging"
    assert data["facets_touched"] == ["work"]
    assert data["first_seen"] == "2026-01-01"

    response = client.get(f"{PREFIX}/api/patterns/seeded")

    assert response.status_code == 200


def test_skills_seed_existing_slug_409(journal_copy):
    save_patterns([_pattern("seeded", "emerging")])
    client = make_logged_in_test_client(journal_copy)

    response = client.post(
        f"{PREFIX}/api/patterns",
        json={
            "slug": "seeded",
            "name": "Seeded",
            "day": "2026-01-01",
            "facet": "work",
            "activity_ids": ["a"],
        },
    )

    data = _assert_error(response, 409)
    assert data["reason_code"] == "skill_already_exists"


def test_skills_seed_validation_400(journal_copy):
    client = make_logged_in_test_client(journal_copy)

    missing_name = client.post(
        f"{PREFIX}/api/patterns",
        json={
            "slug": "seeded",
            "day": "2026-01-01",
            "facet": "work",
            "activity_ids": ["a"],
        },
    )
    empty_activity_ids = client.post(
        f"{PREFIX}/api/patterns",
        json={
            "slug": "seeded",
            "name": "Seeded",
            "day": "2026-01-01",
            "facet": "work",
            "activity_ids": [],
        },
    )

    data = _assert_error(missing_name, 400)
    assert data["reason_code"] == "missing_required_field"
    data = _assert_error(empty_activity_ids, 400)
    assert data["reason_code"] == "missing_required_field"


def test_skills_observe_new_recomputes_derived_fields(journal_copy):
    save_patterns([_pattern("skill", "emerging")])
    client = make_logged_in_test_client(journal_copy)

    response = client.post(
        f"{PREFIX}/api/patterns/skill/observations",
        json={
            "day": "2026-01-02",
            "facet": "personal",
            "activity_ids": ["b"],
        },
    )

    assert response.status_code == 201
    data = response.get_json()
    assert data["last_seen"] == "2026-01-02"
    assert "personal" in data["facets_touched"]


def test_skills_observe_duplicate_is_idempotent(journal_copy):
    save_patterns([_pattern("skill", "emerging")])
    client = make_logged_in_test_client(journal_copy)
    payload = {
        "day": "2026-01-02",
        "facet": "personal",
        "activity_ids": ["b"],
    }

    first = client.post(f"{PREFIX}/api/patterns/skill/observations", json=payload)
    second = client.post(f"{PREFIX}/api/patterns/skill/observations", json=payload)

    assert first.status_code == 201
    assert second.status_code == 200
    assert len(second.get_json()["observations"]) == len(
        first.get_json()["observations"]
    )


def test_skills_observe_reordered_activity_ids_are_duplicate(journal_copy):
    save_patterns(
        [
            _pattern(
                "skill",
                "emerging",
                observations=[
                    {
                        "day": "2026-01-01",
                        "facet": "work",
                        "activity_ids": ["a", "b"],
                        "recorded_at": "2026-01-01T00:00:00Z",
                    }
                ],
            )
        ]
    )
    client = make_logged_in_test_client(journal_copy)

    response = client.post(
        f"{PREFIX}/api/patterns/skill/observations",
        json={
            "day": "2026-01-01",
            "facet": "work",
            "activity_ids": ["b", "a"],
        },
    )

    assert response.status_code == 200
    assert len(response.get_json()["observations"]) == 1


def test_skills_observe_dormant_becomes_mature(journal_copy):
    save_patterns([_pattern("skill", "dormant")])
    client = make_logged_in_test_client(journal_copy)

    response = client.post(
        f"{PREFIX}/api/patterns/skill/observations",
        json={
            "day": "2026-01-02",
            "facet": "work",
            "activity_ids": ["b"],
        },
    )

    assert response.status_code == 201
    assert response.get_json()["status"] == "mature"


def test_skills_observe_miss_404(journal_copy):
    client = make_logged_in_test_client(journal_copy)

    response = client.post(
        f"{PREFIX}/api/patterns/nope/observations",
        json={
            "day": "2026-01-02",
            "facet": "work",
            "activity_ids": ["b"],
        },
    )

    _assert_error(response, 404)


def test_skills_promote_sets_needs_profile(journal_copy):
    save_patterns([_pattern("skill", "emerging")])
    client = make_logged_in_test_client(journal_copy)

    response = client.post(f"{PREFIX}/api/patterns/skill/promote", json={})

    assert response.status_code == 200
    assert response.get_json()["needs_profile"] is True


def test_skills_promote_idempotent_already_flagged(journal_copy):
    save_patterns([_pattern("skill", "emerging", needs_profile=True)])
    client = make_logged_in_test_client(journal_copy)

    response = client.post(f"{PREFIX}/api/patterns/skill/promote", json={})

    assert response.status_code == 200
    assert response.get_json()["needs_profile"] is True


def test_skills_promote_idempotent_already_mature(journal_copy):
    save_patterns([_pattern("skill", "mature")])
    client = make_logged_in_test_client(journal_copy)

    response = client.post(f"{PREFIX}/api/patterns/skill/promote", json={})

    assert response.status_code == 200


def test_skills_promote_miss_404(journal_copy):
    client = make_logged_in_test_client(journal_copy)

    response = client.post(f"{PREFIX}/api/patterns/nope/promote", json={})

    _assert_error(response, 404)


def test_skills_refresh_not_mature_409(journal_copy):
    save_patterns([_pattern("skill", "emerging")])
    client = make_logged_in_test_client(journal_copy)

    response = client.post(f"{PREFIX}/api/patterns/skill/refresh", json={})

    data = _assert_error(response, 409)
    assert data["reason_code"] == "skill_not_mature"


def test_skills_refresh_sets_needs_refresh(journal_copy):
    save_patterns([_pattern("skill", "mature")])
    client = make_logged_in_test_client(journal_copy)

    response = client.post(f"{PREFIX}/api/patterns/skill/refresh", json={})

    assert response.status_code == 200
    assert response.get_json()["needs_refresh"] is True


def test_skills_refresh_idempotent_already_flagged(journal_copy):
    save_patterns([_pattern("skill", "mature", needs_refresh=True)])
    client = make_logged_in_test_client(journal_copy)

    response = client.post(f"{PREFIX}/api/patterns/skill/refresh", json={})

    assert response.status_code == 200
    assert response.get_json()["needs_refresh"] is True


def test_skills_refresh_miss_404(journal_copy):
    client = make_logged_in_test_client(journal_copy)

    response = client.post(f"{PREFIX}/api/patterns/nope/refresh", json={})

    _assert_error(response, 404)


def test_skills_mark_dormant_sets_status(journal_copy):
    save_patterns([_pattern("skill", "mature")])
    client = make_logged_in_test_client(journal_copy)

    response = client.post(f"{PREFIX}/api/patterns/skill/mark-dormant", json={})

    assert response.status_code == 200
    assert response.get_json()["status"] == "dormant"


def test_skills_mark_dormant_idempotent(journal_copy):
    save_patterns([_pattern("skill", "dormant")])
    client = make_logged_in_test_client(journal_copy)

    response = client.post(f"{PREFIX}/api/patterns/skill/mark-dormant", json={})

    assert response.status_code == 200
    assert response.get_json()["status"] == "dormant"


def test_skills_mark_dormant_miss_404(journal_copy):
    client = make_logged_in_test_client(journal_copy)

    response = client.post(f"{PREFIX}/api/patterns/nope/mark-dormant", json={})

    _assert_error(response, 404)


def test_skills_retire_sets_status(journal_copy):
    save_patterns([_pattern("skill", "mature")])
    client = make_logged_in_test_client(journal_copy)

    response = client.post(f"{PREFIX}/api/patterns/skill/retire", json={})

    assert response.status_code == 200
    assert response.get_json()["status"] == "retired"


def test_skills_retire_idempotent(journal_copy):
    save_patterns([_pattern("skill", "retired")])
    client = make_logged_in_test_client(journal_copy)

    response = client.post(f"{PREFIX}/api/patterns/skill/retire", json={})

    assert response.status_code == 200


def test_skills_retire_miss_404(journal_copy):
    client = make_logged_in_test_client(journal_copy)

    response = client.post(f"{PREFIX}/api/patterns/nope/retire", json={})

    _assert_error(response, 404)


def test_skills_edit_request_creates_request_id(journal_copy):
    save_patterns([_pattern("skill", "mature")])
    client = make_logged_in_test_client(journal_copy)

    response = client.post(
        f"{PREFIX}/api/patterns/skill/edit-requests",
        json={"instructions": "do x"},
    )

    assert response.status_code == 201
    data = response.get_json()
    assert data["request_id"]
    assert data["slug"] == "skill"


def test_skills_edit_request_miss_404(journal_copy):
    client = make_logged_in_test_client(journal_copy)

    response = client.post(
        f"{PREFIX}/api/patterns/nope/edit-requests",
        json={"instructions": "x"},
    )

    _assert_error(response, 404)


def test_skills_edit_request_missing_instructions_400(journal_copy):
    save_patterns([_pattern("skill", "mature")])
    client = make_logged_in_test_client(journal_copy)

    response = client.post(f"{PREFIX}/api/patterns/skill/edit-requests", json={})

    _assert_error(response, 400)


def test_skills_rename_updates_slug(journal_copy):
    save_patterns([_pattern("old", "mature")])
    client = make_logged_in_test_client(journal_copy)

    response = client.post(
        f"{PREFIX}/api/patterns/old/rename",
        json={"new_slug": "new"},
    )

    assert response.status_code == 200
    assert response.get_json()["slug"] == "new"
    assert client.get(f"{PREFIX}/api/patterns/new").status_code == 200
    assert client.get(f"{PREFIX}/api/patterns/old").status_code == 404


def test_skills_rename_miss_404(journal_copy):
    client = make_logged_in_test_client(journal_copy)

    response = client.post(
        f"{PREFIX}/api/patterns/nope/rename",
        json={"new_slug": "x"},
    )

    _assert_error(response, 404)


def test_skills_rename_existing_pattern_409(journal_copy):
    save_patterns([_pattern("old", "mature"), _pattern("new", "mature")])
    client = make_logged_in_test_client(journal_copy)

    response = client.post(
        f"{PREFIX}/api/patterns/old/rename",
        json={"new_slug": "new"},
    )

    data = _assert_error(response, 409)
    assert data["reason_code"] == "skill_already_exists"


def test_skills_rename_orphan_profile_409(journal_copy):
    save_patterns([_pattern("old", "mature")])
    save_profile("new", "# orphan")
    client = make_logged_in_test_client(journal_copy)

    response = client.post(
        f"{PREFIX}/api/patterns/old/rename",
        json={"new_slug": "new"},
    )

    data = _assert_error(response, 409)
    assert data["reason_code"] == "skill_already_exists"


def test_skills_post_endpoints_no_body_400(journal_copy):
    save_patterns([_pattern("skill", "emerging")])
    client = make_logged_in_test_client(journal_copy)

    seed_response = client.post(f"{PREFIX}/api/patterns")
    promote_response = client.post(f"{PREFIX}/api/patterns/skill/promote")

    seed_data = _assert_error(seed_response, 400)
    promote_data = _assert_error(promote_response, 400)
    assert seed_data["reason_code"] == "missing_request_body"
    assert promote_data["reason_code"] == "missing_request_body"


def test_skills_post_endpoints_non_json_400(journal_copy):
    client = make_logged_in_test_client(journal_copy)

    response = client.post(
        f"{PREFIX}/api/patterns",
        data="not json",
        content_type="application/json",
    )

    data = _assert_error(response, 400)
    assert data["reason_code"] == "invalid_json_request"
