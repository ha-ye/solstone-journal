# SPDX-License-Identifier: AGPL-3.0-only
# Copyright (c) 2026 sol pbc

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import pytest
import requests
from typer.testing import CliRunner

from solstone.apps.skills.call import app
from solstone.convey.reasons import (
    SKILL_ALREADY_EXISTS,
    SKILL_NOT_FOUND,
    SKILL_NOT_MATURE,
    SKILLS_BUSY,
)
from solstone.think.convey_client import ConveyClient
from solstone.think.journal_io import LockTimeout
from solstone.think.skills import (
    load_edit_requests,
    load_patterns,
    locked_modify_patterns,
    profile_path,
    save_patterns,
    save_profile,
)
from tests._baseline_harness import make_logged_in_test_client


@pytest.fixture
def journal(tmp_path, monkeypatch):
    monkeypatch.setenv("SOLSTONE_JOURNAL", str(tmp_path))
    return tmp_path


@pytest.fixture
def runner(journal, monkeypatch):
    client = ConveyClient(session=make_logged_in_test_client(journal), base_url="")
    monkeypatch.setattr("solstone.apps.skills.call.get_client", lambda: client)
    return CliRunner()


def _make_pattern(
    *,
    slug: str = "alpha-skill",
    name: str = "Alpha Skill",
    status: str = "emerging",
    day: str = "2026-04-19",
    facet: str = "work",
    activity_ids: list[str] | None = None,
    notes: str = "",
    needs_profile: bool = False,
    needs_refresh: bool = False,
    profile_generated_at: str | None = None,
    created_at: str = "2026-04-19T14:22:00Z",
    updated_at: str = "2026-04-19T14:22:00Z",
) -> dict[str, Any]:
    ids = ["act_abc"] if activity_ids is None else activity_ids
    observations = [
        {
            "day": day,
            "facet": facet,
            "activity_ids": ids,
            "notes": notes,
            "recorded_at": created_at,
        }
    ]
    return {
        "slug": slug,
        "name": name,
        "status": status,
        "observations": observations,
        "facets_touched": [facet],
        "first_seen": day,
        "last_seen": day,
        "needs_profile": needs_profile,
        "needs_refresh": needs_refresh,
        "profile_generated_at": profile_generated_at,
        "created_at": created_at,
        "updated_at": updated_at,
    }


def _seed_patterns(*rows: dict[str, Any]) -> None:
    save_patterns(list(rows))


def test_skills_app_has_ten_registered_commands() -> None:
    command_names = {command.name for command in app.registered_commands}

    assert len(app.registered_commands) == 10
    assert command_names == {
        "list",
        "show",
        "observe",
        "seed",
        "promote",
        "refresh",
        "mark-dormant",
        "retire",
        "edit-request",
        "rename",
    }


def test_list_empty_text_and_json(runner) -> None:
    text = runner.invoke(app, ["list"])
    json_result = runner.invoke(app, ["list", "--json"])

    assert text.exit_code == 0
    assert text.stdout == ""
    assert json_result.exit_code == 0
    assert json.loads(json_result.stdout) == []


def test_list_filters_by_status_and_fetches_all_pages(runner) -> None:
    rows = [
        _make_pattern(slug=f"skill-{index:03d}", status="mature")
        for index in range(105)
    ]
    rows.append(_make_pattern(slug="dormant-skill", status="dormant"))
    _seed_patterns(*rows)

    all_rows = runner.invoke(app, ["list", "--json"])
    dormant = runner.invoke(app, ["list", "--status", "dormant"])

    assert all_rows.exit_code == 0
    assert len(json.loads(all_rows.stdout)) == 106
    assert dormant.exit_code == 0
    assert "dormant-skill" in dormant.stdout
    assert "skill-000" not in dormant.stdout


def test_show_text_json_and_not_found(runner) -> None:
    _seed_patterns(
        _make_pattern(
            slug="alpha-skill",
            name="Alpha Skill",
            activity_ids=["act_abc", "act_def"],
            notes="Observed in review",
        )
    )
    save_profile("alpha-skill", "# Alpha Skill\n")

    text = runner.invoke(app, ["show", "alpha-skill"])
    json_result = runner.invoke(app, ["show", "alpha-skill", "--json"])
    missing = runner.invoke(app, ["show", "missing-skill"])

    assert text.exit_code == 0
    assert "name: Alpha Skill" in text.stdout
    assert "slug: alpha-skill" in text.stdout
    assert (
        "- 2026-04-19 [work] activity_ids=act_abc,act_def notes=Observed in review"
        in text.stdout
    )
    assert "# Alpha Skill" in text.stdout
    assert json_result.exit_code == 0
    assert json.loads(json_result.stdout) == {
        "pattern": load_patterns()[0],
        "profile": "# Alpha Skill\n",
    }
    assert missing.exit_code == 1
    assert missing.stderr == f"{SKILL_NOT_FOUND.message}\n"


def test_show_sorts_observations_for_text_output(runner) -> None:
    _seed_patterns(_make_pattern(day="2026-04-20", created_at="2026-04-20T10:00:00Z"))

    def mutate(rows):
        rows = list(rows)
        rows[0]["observations"].append(
            {
                "day": "2026-04-19",
                "facet": "solpbc",
                "activity_ids": ["act_older"],
                "notes": "Earlier observation",
                "recorded_at": "2026-04-19T09:00:00Z",
            }
        )
        rows[0]["first_seen"] = "2026-04-19"
        rows[0]["last_seen"] = "2026-04-20"
        return rows

    locked_modify_patterns(mutate)

    result = runner.invoke(app, ["show", "alpha-skill"])

    assert result.exit_code == 0
    first_index = result.stdout.index(
        "- 2026-04-19 [solpbc] activity_ids=act_older notes=Earlier observation"
    )
    second_index = result.stdout.index(
        "- 2026-04-20 [work] activity_ids=act_abc notes="
    )
    assert first_index < second_index


def test_observe_success_json_resurrects_and_duplicate_collapses(runner) -> None:
    _seed_patterns(_make_pattern(status="dormant", activity_ids=["act_a", "act_b"]))

    created = runner.invoke(
        app,
        [
            "observe",
            "alpha-skill",
            "--day",
            "2026-04-20",
            "--facet",
            "personal",
            "--activity-ids",
            "act_new",
            "--notes",
            "Later observation",
            "--json",
        ],
    )
    duplicate = runner.invoke(
        app,
        [
            "observe",
            "alpha-skill",
            "--day",
            "2026-04-19",
            "--facet",
            "work",
            "--activity-ids",
            "act_b,act_a",
        ],
    )

    payload = json.loads(created.stdout)
    assert created.exit_code == 0
    assert payload["status"] == "mature"
    assert payload["facets_touched"] == ["personal", "work"]
    assert payload["last_seen"] == "2026-04-20"
    assert duplicate.exit_code == 0
    assert duplicate.stdout == "observation saved: alpha-skill\n"
    assert duplicate.stderr == ""
    assert "already observed" not in duplicate.output
    assert len(load_patterns()[0]["observations"]) == 2


def test_observe_requires_activity_ids_before_http(runner) -> None:
    result = runner.invoke(
        app,
        [
            "observe",
            "alpha-skill",
            "--day",
            "2026-04-20",
            "--facet",
            "work",
            "--activity-ids",
            " , ",
        ],
    )

    assert result.exit_code == 1
    assert result.stderr == "Error: --activity-ids requires at least one id.\n"
    assert result.stdout == ""


def test_seed_text_json_duplicate_and_activity_validation(runner) -> None:
    text = runner.invoke(
        app,
        [
            "seed",
            "alpha-skill",
            "--name",
            "Alpha Skill",
            "--day",
            "2026-04-19",
            "--facet",
            "work",
            "--activity-ids",
            "act_abc",
        ],
    )
    json_result = runner.invoke(
        app,
        [
            "seed",
            "beta-skill",
            "--name",
            "Beta Skill",
            "--day",
            "2026-04-20",
            "--facet",
            "personal",
            "--activity-ids",
            "act_def,act_ghi",
            "--notes",
            "Initial seed",
            "--json",
        ],
    )
    duplicate = runner.invoke(
        app,
        [
            "seed",
            "alpha-skill",
            "--name",
            "Alpha Skill",
            "--day",
            "2026-04-19",
            "--facet",
            "work",
            "--activity-ids",
            "act_abc",
        ],
    )
    invalid = runner.invoke(
        app,
        [
            "seed",
            "gamma-skill",
            "--name",
            "Gamma Skill",
            "--day",
            "2026-04-19",
            "--facet",
            "work",
            "--activity-ids",
            "",
        ],
    )

    payload = json.loads(json_result.stdout)
    assert text.exit_code == 0
    assert text.stdout == "created skill: alpha-skill\n"
    assert json_result.exit_code == 0
    assert payload["slug"] == "beta-skill"
    assert payload["observations"][0]["activity_ids"] == ["act_def", "act_ghi"]
    assert payload["observations"][0]["notes"] == "Initial seed"
    assert duplicate.exit_code == 1
    assert duplicate.stderr == f"{SKILL_ALREADY_EXISTS.message}\n"
    assert invalid.exit_code == 1
    assert invalid.stderr == "Error: --activity-ids requires at least one id.\n"


def test_promote_success_and_noops_collapse_to_success(runner) -> None:
    _seed_patterns(
        _make_pattern(slug="alpha-skill"),
        _make_pattern(slug="flagged-skill", needs_profile=True),
        _make_pattern(slug="mature-skill", status="mature"),
    )

    promoted = runner.invoke(app, ["promote", "alpha-skill", "--json"])
    already_flagged = runner.invoke(app, ["promote", "flagged-skill"])
    already_mature = runner.invoke(app, ["promote", "mature-skill"])

    assert promoted.exit_code == 0
    assert json.loads(promoted.stdout)["needs_profile"] is True
    assert already_flagged.exit_code == 0
    assert already_flagged.stdout == "flagged for profile: flagged-skill\n"
    assert already_flagged.stderr == ""
    assert "already flagged" not in already_flagged.output
    assert already_mature.exit_code == 0
    assert already_mature.stdout == "flagged for profile: mature-skill\n"
    assert already_mature.stderr == ""
    assert "already mature" not in already_mature.output


def test_refresh_success_noop_and_not_mature(runner) -> None:
    _seed_patterns(
        _make_pattern(slug="mature-skill", status="mature"),
        _make_pattern(slug="flagged-skill", status="mature", needs_refresh=True),
        _make_pattern(slug="emerging-skill", status="emerging"),
    )

    refreshed = runner.invoke(app, ["refresh", "mature-skill", "--json"])
    already_flagged = runner.invoke(app, ["refresh", "flagged-skill"])
    not_mature = runner.invoke(app, ["refresh", "emerging-skill"])

    assert refreshed.exit_code == 0
    assert json.loads(refreshed.stdout)["needs_refresh"] is True
    assert already_flagged.exit_code == 0
    assert already_flagged.stdout == "flagged for refresh: flagged-skill\n"
    assert already_flagged.stderr == ""
    assert "already flagged" not in already_flagged.output
    assert not_mature.exit_code == 1
    assert not_mature.stderr == f"{SKILL_NOT_MATURE.message}\n"


def test_mark_dormant_and_retire_success_and_noops(runner) -> None:
    _seed_patterns(
        _make_pattern(slug="active-skill"),
        _make_pattern(slug="dormant-skill", status="dormant"),
        _make_pattern(slug="retire-skill"),
        _make_pattern(slug="retired-skill", status="retired"),
    )

    dormant = runner.invoke(app, ["mark-dormant", "active-skill", "--json"])
    already_dormant = runner.invoke(app, ["mark-dormant", "dormant-skill"])
    retired = runner.invoke(app, ["retire", "retire-skill", "--json"])
    already_retired = runner.invoke(app, ["retire", "retired-skill"])

    assert dormant.exit_code == 0
    assert json.loads(dormant.stdout)["status"] == "dormant"
    assert already_dormant.exit_code == 0
    assert already_dormant.stdout == "marked dormant: dormant-skill\n"
    assert already_dormant.stderr == ""
    assert "already flagged" not in already_dormant.output
    assert retired.exit_code == 0
    assert json.loads(retired.stdout)["status"] == "retired"
    assert already_retired.exit_code == 0
    assert already_retired.stdout == "retired skill: retired-skill\n"
    assert already_retired.stderr == ""
    assert "already flagged" not in already_retired.output


def test_edit_request_text_json_and_not_found(runner) -> None:
    _seed_patterns(_make_pattern(slug="alpha-skill"), _make_pattern(slug="beta-skill"))

    text = runner.invoke(
        app,
        ["edit-request", "alpha-skill", "--instructions", "revise opening"],
    )
    json_result = runner.invoke(
        app,
        [
            "edit-request",
            "beta-skill",
            "--instructions",
            "expand examples",
            "--requested-by",
            "owner",
            "--json",
        ],
    )
    missing = runner.invoke(
        app,
        ["edit-request", "missing-skill", "--instructions", "revise this"],
    )

    assert text.exit_code == 0
    assert text.stdout.startswith("request_id: req_")
    assert json_result.exit_code == 0
    payload = json.loads(json_result.stdout)
    assert set(payload) == {"request_id", "slug"}
    assert payload["slug"] == "beta-skill"
    rows = load_edit_requests()
    assert len(rows) == 2
    assert rows[0]["id"] != rows[1]["id"]
    assert rows[1]["requested_by"] == "owner"
    assert missing.exit_code == 1
    assert missing.stderr == f"{SKILL_NOT_FOUND.message}\n"


def test_rename_success_not_found_and_target_exists(runner) -> None:
    _seed_patterns(_make_pattern(slug="alpha-skill"), _make_pattern(slug="beta-skill"))
    save_profile("alpha-skill", "# Alpha Skill\n")

    renamed = runner.invoke(app, ["rename", "alpha-skill", "renamed-skill", "--json"])
    missing = runner.invoke(app, ["rename", "missing-skill", "new-skill"])
    target_exists = runner.invoke(app, ["rename", "renamed-skill", "beta-skill"])

    payload = json.loads(renamed.stdout)
    assert renamed.exit_code == 0
    assert payload["slug"] == "renamed-skill"
    assert not profile_path("alpha-skill").exists()
    assert (
        profile_path("renamed-skill").read_text(encoding="utf-8") == "# Alpha Skill\n"
    )
    assert load_patterns()[0]["slug"] == "renamed-skill"
    assert missing.exit_code == 1
    assert missing.stderr == f"{SKILL_NOT_FOUND.message}\n"
    assert target_exists.exit_code == 1
    assert target_exists.stderr == f"{SKILL_ALREADY_EXISTS.message}\n"


def test_convey_down_prints_require_solstone_message(journal, monkeypatch) -> None:
    class DownSession:
        def get(self, _url):
            raise requests.exceptions.ConnectionError()

        def post(self, _url, json=None):
            raise requests.exceptions.ConnectionError()

    client = ConveyClient(session=DownSession(), base_url="http://localhost:5015")
    monkeypatch.setattr("solstone.apps.skills.call.get_client", lambda: client)
    monkeypatch.delenv("SOL_SKIP_SUPERVISOR_CHECK", raising=False)
    monkeypatch.delenv("SOL_SUPERVISOR_SPAWNED", raising=False)

    result = CliRunner().invoke(app, ["list"])

    assert result.exit_code == 1
    assert (
        result.stderr
        == "sol: solstone isn't running. Start it with 'journal up' and retry.\n"
    )
    assert result.stdout == ""


def test_busy_prints_owner_voice_error(runner, monkeypatch) -> None:
    def raise_timeout(_mutate):
        raise LockTimeout(Path("patterns.jsonl"), 0.01)

    monkeypatch.setattr(
        "solstone.apps.skills.routes.locked_modify_patterns", raise_timeout
    )

    result = runner.invoke(
        app,
        [
            "seed",
            "alpha-skill",
            "--name",
            "Alpha Skill",
            "--day",
            "2026-04-19",
            "--facet",
            "work",
            "--activity-ids",
            "act_abc",
        ],
    )

    assert result.exit_code == 1
    assert result.stderr == f"{SKILLS_BUSY.message}\n"
