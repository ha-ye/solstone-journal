# SPDX-License-Identifier: AGPL-3.0-only
# Copyright (c) 2026 sol pbc

"""Route and CLI parity tests for targeted entity write owners."""

from __future__ import annotations

import json
from pathlib import Path

from typer.testing import CliRunner

from solstone.convey import create_app
from solstone.think.call import call_app
from solstone.think.entities import (
    attach_or_reactivate_entity,
    block_journal_entity,
    detach_facet_entity,
    entity_slug,
    save_entities,
)

FACET = "personal"
TIMESTAMP_KEYS = {"attached_at", "created_at", "updated_at"}


def _make_journal(root: Path, monkeypatch) -> Path:
    journal = root / "journal"
    facet_dir = journal / "facets" / FACET
    facet_dir.mkdir(parents=True)
    (facet_dir / "facet.json").write_text(
        json.dumps({"title": "Personal", "description": "Personal facet"}),
        encoding="utf-8",
    )
    config_dir = journal / "config"
    config_dir.mkdir()
    (config_dir / "journal.json").write_text(
        json.dumps(
            {
                "convey": {"trust_localhost": True},
                "setup": {"completed_at": 1700000000000},
            }
        ),
        encoding="utf-8",
    )
    monkeypatch.setenv("SOLSTONE_JOURNAL", str(journal))
    monkeypatch.setenv("SOL_SKIP_SUPERVISOR_CHECK", "1")
    return journal


def _normalized_pairs(path: Path) -> list[tuple[str, object]]:
    pairs = json.loads(path.read_text(encoding="utf-8"), object_pairs_hook=list)
    return [
        (key, "<timestamp>" if key in TIMESTAMP_KEYS else value) for key, value in pairs
    ]


def _assert_entity_files_match(route_journal: Path, cli_journal: Path, entity_id: str):
    for relpath in [
        Path("entities") / entity_id / "entity.json",
        Path("facets") / FACET / "entities" / entity_id / "entity.json",
    ]:
        assert _normalized_pairs(route_journal / relpath) == _normalized_pairs(
            cli_journal / relpath
        )


def _post_attach(journal: Path, payload: dict) -> tuple[int, dict]:
    app = create_app(str(journal))
    response = app.test_client().post(f"/app/entities/api/{FACET}", json=payload)
    return response.status_code, response.get_json()


def _cli_attach(payload: dict) -> tuple[int, str]:
    result = CliRunner().invoke(
        call_app,
        [
            "entities",
            "attach",
            payload["type"],
            payload["name"],
            payload["description"],
            "-f",
            FACET,
        ],
    )
    return result.exit_code, result.output


def _seed_detached(name: str, description: str = "Removed") -> str:
    relationship, _ = attach_or_reactivate_entity(
        FACET,
        entity_type="Person",
        name=name,
        description=description,
    )
    detach_facet_entity(FACET, relationship["entity_id"])
    return relationship["entity_id"]


def _seed_active_people() -> None:
    save_entities(
        FACET,
        [
            {"type": "Person", "name": "Alice Johnson", "description": "Friend"},
            {"type": "Person", "name": "Bob Smith", "description": "Neighbor"},
        ],
    )


def test_fresh_attach_route_cli_byte_parity(tmp_path, monkeypatch):
    payload = {
        "type": "Person",
        "name": "Alice Johnson",
        "description": "Friend",
    }
    route_journal = _make_journal(tmp_path / "route", monkeypatch)
    route_status, route_json = _post_attach(route_journal, payload)

    cli_journal = _make_journal(tmp_path / "cli", monkeypatch)
    cli_exit, cli_output = _cli_attach(payload)

    assert route_status == 201
    assert route_json["id"] == "alice_johnson"
    assert "success" not in route_json
    assert cli_exit == 0, cli_output
    _assert_entity_files_match(route_journal, cli_journal, "alice_johnson")


def test_reattach_detached_route_cli_byte_parity(tmp_path, monkeypatch):
    payload = {
        "type": "Person",
        "name": "Alice Johnson",
        "description": "Friend again",
    }
    route_journal = _make_journal(tmp_path / "route", monkeypatch)
    route_entity_id = _seed_detached("Alice Johnson")
    route_status, route_json = _post_attach(route_journal, payload)

    cli_journal = _make_journal(tmp_path / "cli", monkeypatch)
    cli_entity_id = _seed_detached("Alice Johnson")
    cli_exit, cli_output = _cli_attach(payload)

    assert route_entity_id == cli_entity_id == "alice_johnson"
    assert route_status == 200
    assert route_json["success"] is True
    assert route_json["reattached"] is True
    assert cli_exit == 0, cli_output
    _assert_entity_files_match(route_journal, cli_journal, "alice_johnson")


def test_name_collision_against_detached_reactivates_route_cli_byte_parity(
    tmp_path, monkeypatch
):
    payload = {
        "type": "Person",
        "name": "alice johnson",
        "description": "Case-insensitive reattach",
    }
    route_journal = _make_journal(tmp_path / "route", monkeypatch)
    route_entity_id = _seed_detached("Alice Johnson")
    route_status, route_json = _post_attach(route_journal, payload)

    cli_journal = _make_journal(tmp_path / "cli", monkeypatch)
    cli_entity_id = _seed_detached("Alice Johnson")
    cli_exit, cli_output = _cli_attach(payload)

    assert route_entity_id == cli_entity_id == "alice_johnson"
    assert route_status == 200
    assert route_json["reattached"] is True
    assert cli_exit == 0, cli_output
    _assert_entity_files_match(route_journal, cli_journal, "alice_johnson")


def test_aka_conflict_route_cli_typed_error_parity(tmp_path, monkeypatch):
    route_journal = _make_journal(tmp_path / "route", monkeypatch)
    _seed_active_people()
    route_app = create_app(str(route_journal))
    route_response = route_app.test_client().put(
        f"/app/entities/api/{FACET}/update",
        json={
            "old_name": "Alice Johnson",
            "new_name": "Alice Johnson",
            "type": "Person",
            "aka_list": "Bob Smith",
        },
    )

    _make_journal(tmp_path / "cli", monkeypatch)
    _seed_active_people()
    cli_result = CliRunner().invoke(
        call_app,
        ["entities", "aka", "Alice Johnson", "Bob Smith", "-f", FACET],
    )

    assert route_response.status_code == 409
    assert route_response.get_json()["reason_code"] == "entity_alias_conflict"
    assert cli_result.exit_code == 1
    assert "conflicts with entity 'Bob Smith'" in cli_result.output


def test_blocked_name_route_cli_typed_error_parity(tmp_path, monkeypatch):
    payload = {
        "type": "Person",
        "name": "Alice Johnson",
        "description": "Friend",
    }
    route_journal = _make_journal(tmp_path / "route", monkeypatch)
    save_entities(
        FACET,
        [{"type": "Person", "name": "Alice Johnson", "description": "Friend"}],
    )
    block_journal_entity(entity_slug("Alice Johnson"))
    route_status, route_json = _post_attach(route_journal, payload)

    _make_journal(tmp_path / "cli", monkeypatch)
    save_entities(
        FACET,
        [{"type": "Person", "name": "Alice Johnson", "description": "Friend"}],
    )
    block_journal_entity(entity_slug("Alice Johnson"))
    cli_exit, cli_output = _cli_attach(payload)

    assert route_status == 400
    assert route_json["reason_code"] == "entity_blocked"
    assert cli_exit == 1
    assert "blocked" in cli_output
