# SPDX-License-Identifier: AGPL-3.0-only
# Copyright (c) 2026 sol pbc

"""HTTP-client parity tests for ``sol call entities``."""

from __future__ import annotations

import json
import os
from pathlib import Path

import pytest
from typer.testing import CliRunner

from solstone.apps.entities.call import app
from solstone.convey.reasons import ENTITY_BUSY
from solstone.think.convey_client import ConveyClient
from solstone.think.entities import (
    block_journal_entity,
    detach_facet_entity,
    entity_slug,
    load_facet_relationship,
    save_entities,
)
from solstone.think.entities.errors import EntityNotFoundError
from solstone.think.entities.journal import load_journal_entity, save_journal_entity
from solstone.think.entities.observations import add_observation, save_observations
from solstone.think.entities.review_candidates import save_candidates
from solstone.think.journal_io import LockTimeout
from tests._baseline_harness import make_test_client

runner = CliRunner()


@pytest.fixture(autouse=True)
def _entities_client(monkeypatch: pytest.MonkeyPatch) -> None:
    def client() -> ConveyClient:
        journal = Path(os.environ["SOLSTONE_JOURNAL"])
        return ConveyClient(
            session=make_test_client(journal),
            base_url="",
        )

    monkeypatch.setattr("solstone.apps.entities.call.get_client", client)
    monkeypatch.setenv("SOL_SKIP_SUPERVISOR_CHECK", "1")


@pytest.fixture
def entity_env(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    monkeypatch.setenv("SOLSTONE_JOURNAL", str(tmp_path))
    _reset_journal_cache()
    for facet in ["personal", "work", "other"]:
        _ensure_facet(tmp_path, facet)
    _ensure_config(tmp_path)

    def create(
        attached: list[dict] | None = None,
        detected: list[dict] | None = None,
        day: str | None = None,
        facet: str = "personal",
        observations: list[str] | None = None,
        observation_entity: str | None = None,
    ) -> Path:
        _ensure_facet(tmp_path, facet)
        if attached:
            save_entities(facet, attached, day=None)
        if detected and day:
            save_entities(facet, detected, day=day)
        if observations and observation_entity:
            for index, content in enumerate(observations, 1):
                add_observation(facet, observation_entity, content, str(index))
        return tmp_path

    yield create
    _reset_journal_cache()


@pytest.fixture
def entity_move_env(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    monkeypatch.setenv("SOLSTONE_JOURNAL", str(tmp_path))
    _reset_journal_cache()
    _ensure_config(tmp_path)

    def create(
        entity_name: str = "Alice Johnson",
        src_facet: str = "work",
        dst_facet: str = "personal",
        src_observations: list[dict] | None = None,
        dst_observations: list[dict] | None = None,
        create_dst_entity: bool = False,
    ) -> tuple[Path, str, str, str]:
        _ensure_facet(tmp_path, src_facet)
        _ensure_facet(tmp_path, dst_facet)
        entity = {
            "type": "Person",
            "name": entity_name,
            "description": "Friend",
            "attached_at": 1000,
            "updated_at": 1000,
        }
        save_entities(src_facet, [entity], day=None)
        if src_observations:
            save_observations(src_facet, entity_name, src_observations)
        if create_dst_entity:
            save_entities(dst_facet, [entity], day=None)
        if dst_observations:
            save_observations(dst_facet, entity_name, dst_observations)
        return tmp_path, src_facet, dst_facet, entity_name

    yield create
    _reset_journal_cache()


def _reset_journal_cache() -> None:
    import solstone.think.utils as think_utils

    think_utils._journal_path_cache = None


def _ensure_config(journal: Path) -> None:
    config_dir = journal / "config"
    config_dir.mkdir(parents=True, exist_ok=True)
    (config_dir / "journal.json").write_text(
        json.dumps(
            {
                "setup": {"completed_at": 1700000000000},
            }
        )
        + "\n",
        encoding="utf-8",
    )


def _ensure_facet(journal: Path, facet: str) -> None:
    facet_dir = journal / "facets" / facet
    facet_dir.mkdir(parents=True, exist_ok=True)
    (facet_dir / "facet.json").write_text(
        json.dumps({"title": facet.title(), "description": f"{facet} facet"}) + "\n",
        encoding="utf-8",
    )


def _candidate_rows(journal: Path) -> list[dict]:
    path = journal / "entities" / "review-candidates.jsonl"
    if not path.exists():
        return []
    return [
        json.loads(line)
        for line in path.read_text(encoding="utf-8").splitlines()
        if line.strip()
    ]


def _seed_merge_entities() -> None:
    save_journal_entity(
        {
            "id": "kognova_inc",
            "name": "Kognova Inc",
            "type": "Company",
            "aka": ["Kognova Incorporated"],
        }
    )
    save_journal_entity(
        {
            "id": "kognova",
            "name": "Kognova",
            "type": "Company",
            "aka": [],
        }
    )


def _seed_merge_candidate(status: str = "open") -> None:
    save_candidates(
        [
            {
                "facet": "work",
                "source": "Kognova Inc",
                "source_slug": "kognova_inc",
                "target": "Kognova",
                "target_slug": "kognova",
                "status": status,
                "evidence": {
                    "basis": "name-variant",
                    "summary": "Kognova Inc / Kognova",
                    "detection_count": 4,
                    "needs": 0,
                },
            }
        ]
    )


def _busy(journal: Path) -> LockTimeout:
    return LockTimeout(journal / "busy.lock", 0.01)


def _mark_blocked_without_detach(name: str) -> None:
    blocked_entity = load_journal_entity(entity_slug(name))
    assert blocked_entity is not None
    blocked_entity["blocked"] = True
    save_journal_entity(blocked_entity)


def test_list_attached_detected_empty_and_env_byte_exact(
    entity_env, monkeypatch: pytest.MonkeyPatch
) -> None:
    entity_env(
        attached=[
            {
                "type": "Person",
                "name": "Alice Johnson",
                "description": "Friend",
                "attached_at": 1000,
                "updated_at": 1000,
            },
            {
                "type": "Company",
                "name": "Acme Corp",
                "description": "Client",
                "attached_at": 1001,
                "updated_at": 1001,
            },
        ],
        detected=[
            {"type": "Person", "name": "Alice", "description": "Met at conference"},
            {"type": "Tool", "name": "pytest", "description": "Testing tool"},
        ],
        day="20240101",
    )

    attached = runner.invoke(app, ["list", "personal"])
    detected = runner.invoke(app, ["list", "personal", "--day", "20240101"])

    assert attached.exit_code == 0
    assert attached.stdout == (
        "2 attached entities:\n"
        "  - Acme Corp (Company): Client\n"
        "  - Alice Johnson (Person): Friend\n"
    )
    assert attached.stderr == ""
    assert detected.exit_code == 0
    assert detected.stdout == (
        "2 detected for 20240101 entities:\n"
        "  - Alice (Person): Met at conference\n"
        "  - pytest (Tool): Testing tool\n"
    )

    entity_env(facet="other")
    empty = runner.invoke(app, ["list", "other"])
    assert empty.exit_code == 0
    assert empty.stdout == "No entities found.\n"

    monkeypatch.setenv("SOL_FACET", "personal")
    env_result = runner.invoke(app, ["list"])
    assert env_result.exit_code == 0
    assert "Alice Johnson" in env_result.stdout


def test_missing_sol_env_errors_byte_exact(entity_env, monkeypatch) -> None:
    entity_env()
    monkeypatch.delenv("SOL_FACET", raising=False)
    monkeypatch.delenv("SOL_DAY", raising=False)

    list_result = runner.invoke(app, ["list"])
    detect_result = runner.invoke(
        app,
        ["detect", "Person", "Alice", "Met", "-f", "personal"],
    )

    assert list_result.exit_code == 1
    assert list_result.stderr == (
        "Error: facet is required (pass as argument or set SOL_FACET).\n"
    )
    assert detect_result.exit_code == 1
    assert detect_result.stderr == (
        "Error: day is required (pass as argument or set SOL_DAY).\n"
    )


def test_detect_success_duplicate_invalid_blocked_busy_and_env_byte_exact(
    entity_env,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    journal = entity_env()
    result = runner.invoke(
        app,
        [
            "detect",
            "Person",
            "Alice",
            "Met at conference",
            "--facet",
            "personal",
            "--day",
            "20240101",
        ],
    )
    duplicate = runner.invoke(
        app,
        [
            "detect",
            "Person",
            "Alice",
            "Second",
            "--facet",
            "personal",
            "--day",
            "20240101",
        ],
    )
    invalid = runner.invoke(
        app,
        ["detect", "AB", "Alice", "Met", "--facet", "personal", "--day", "20240101"],
    )

    assert result.exit_code == 0
    assert result.stdout == "Entity 'Alice' detected for 20240101.\n"
    assert duplicate.exit_code == 1
    assert duplicate.stderr == "Error: Entity 'Alice' already detected for 20240101\n"
    assert invalid.exit_code == 1
    assert invalid.stderr == "Error: Invalid entity type 'AB'.\n"

    monkeypatch.setenv("SOL_DAY", "20240102")
    monkeypatch.setenv("SOL_FACET", "personal")
    env_result = runner.invoke(app, ["detect", "Person", "Bob", "Met at party"])
    assert env_result.exit_code == 0
    assert env_result.stdout == "Entity 'Bob' detected for 20240102.\n"

    override_result = runner.invoke(
        app,
        [
            "detect",
            "Person",
            "Carol",
            "Met at lunch",
            "--day",
            "20240105",
        ],
    )
    assert override_result.exit_code == 0
    assert override_result.stdout == "Entity 'Carol' detected for 20240105.\n"

    entity_env(
        attached=[
            {"type": "Person", "name": "Blocked Person", "description": "Blocked"}
        ]
    )
    blocked_entity = load_journal_entity(entity_slug("Blocked Person"))
    assert blocked_entity is not None
    blocked_entity["blocked"] = True
    save_journal_entity(blocked_entity)
    blocked = runner.invoke(
        app,
        [
            "detect",
            "Person",
            "Blocked Person",
            "Met",
            "--facet",
            "personal",
            "--day",
            "20240103",
        ],
    )
    assert blocked.exit_code == 1
    assert blocked.stderr == "Error: Entity 'Blocked Person' is blocked.\n"

    def raise_busy(*args, **kwargs):
        raise _busy(journal)

    monkeypatch.setattr(
        "solstone.apps.entities.routes.save_detected_entity", raise_busy
    )
    busy = runner.invoke(
        app,
        [
            "detect",
            "Person",
            "Charlie",
            "Met",
            "--facet",
            "personal",
            "--day",
            "20240104",
        ],
    )
    assert busy.exit_code == 1
    assert busy.stderr == ENTITY_BUSY.message + "\n"


def test_attach_new_existing_detached_invalid_blocked_busy_byte_exact(
    entity_env,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    journal = entity_env()
    new_result = runner.invoke(
        app,
        ["attach", "Person", "Alice Johnson", "Friend", "-f", "personal"],
    )
    existing = runner.invoke(
        app,
        ["attach", "Person", "Alice Johnson", "Friend", "-f", "personal"],
    )
    invalid = runner.invoke(
        app,
        ["attach", "AB", "Someone", "Friend", "-f", "personal"],
    )

    assert new_result.exit_code == 0
    assert new_result.stdout == "Entity 'Alice Johnson' attached.\n"
    assert existing.exit_code == 0
    assert existing.stdout == "Entity 'Alice Johnson' already attached.\n"
    assert invalid.exit_code == 1
    assert invalid.stderr == "Error: Invalid entity type 'AB'.\n"

    detach_facet_entity("personal", "alice_johnson")
    reattached = runner.invoke(
        app,
        ["attach", "Person", "Alice Johnson", "Friend again", "-f", "personal"],
    )
    relationship = load_facet_relationship("personal", "alice_johnson")
    assert reattached.exit_code == 0
    assert reattached.stdout == "Entity 'Alice Johnson' attached.\n"
    assert relationship is not None
    assert "detached" not in relationship
    assert relationship["description"] == "Friend again"

    entity_env(attached=[{"type": "Person", "name": "Blocked", "description": ""}])
    block_journal_entity(entity_slug("Blocked"))
    blocked = runner.invoke(
        app,
        ["attach", "Person", "Blocked", "Friend", "-f", "personal"],
    )
    assert blocked.exit_code == 1
    assert blocked.stderr == "Error: Entity 'Blocked' is blocked.\n"

    def raise_not_found(*args, **kwargs):
        raise EntityNotFoundError()

    monkeypatch.setattr(
        "solstone.apps.entities.routes.attach_or_reactivate_entity",
        raise_not_found,
    )
    owner_missing = runner.invoke(
        app,
        ["attach", "Person", "Missing Owner", "Friend", "-f", "personal"],
    )
    assert owner_missing.exit_code == 1
    assert owner_missing.stderr == "Error: Entity 'Missing Owner' not found.\n"

    def raise_busy(*args, **kwargs):
        raise _busy(journal)

    monkeypatch.setattr(
        "solstone.apps.entities.routes.attach_or_reactivate_entity",
        raise_busy,
    )
    busy = runner.invoke(
        app,
        ["attach", "Person", "Busy", "Friend", "-f", "personal"],
    )
    assert busy.exit_code == 1
    assert busy.stderr == ENTITY_BUSY.message + "\n"


def test_update_attached_detected_and_resolve_errors_byte_exact(
    entity_env,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    journal = entity_env(
        attached=[
            {
                "type": "Person",
                "name": "Alice Johnson",
                "description": "Old",
                "attached_at": 1000,
                "updated_at": 1000,
            }
        ],
        detected=[{"type": "Person", "name": "Alice", "description": "Old"}],
        day="20240101",
    )

    attached = runner.invoke(
        app,
        ["update", "Alice Johnson", "New description", "-f", "personal"],
    )
    detected = runner.invoke(
        app,
        ["update", "Alice", "New desc", "-f", "personal", "--day", "20240101"],
    )
    missing = runner.invoke(
        app,
        ["update", "Missing", "New description", "-f", "personal"],
    )
    detected_missing = runner.invoke(
        app,
        ["update", "Missing", "New desc", "-f", "personal", "--day", "20240101"],
    )
    missing_plain = runner.invoke(
        app,
        ["update", "Unknown", "New description", "-f", "other"],
    )

    assert attached.exit_code == 0
    assert attached.stdout == "Entity 'Alice Johnson' updated.\n"
    assert detected.exit_code == 0
    assert detected.stdout == "Entity 'Alice' updated for 20240101.\n"
    assert missing.exit_code == 1
    assert missing.stderr == (
        "Error: Entity 'Missing' not found. Did you mean: Alice Johnson\n"
    )
    assert detected_missing.exit_code == 1
    assert detected_missing.stderr == "Error: Entity 'Missing' not found for 20240101\n"
    assert missing_plain.exit_code == 1
    assert (
        missing_plain.stderr == "Error: Entity 'Unknown' not found in facet 'other'.\n"
    )

    def raise_busy(*args, **kwargs):
        raise _busy(journal)

    monkeypatch.setattr(
        "solstone.apps.entities.routes.update_detected_entity",
        raise_busy,
    )
    detected_busy = runner.invoke(
        app,
        ["update", "Alice", "New desc", "-f", "personal", "--day", "20240101"],
    )
    assert detected_busy.exit_code == 1
    assert detected_busy.stderr == ENTITY_BUSY.message + "\n"

    entity_env(
        attached=[
            {"type": "Person", "name": "Blocked Person", "description": "Blocked"}
        ]
    )
    _mark_blocked_without_detach("Blocked Person")
    blocked = runner.invoke(
        app,
        ["update", "Blocked Person", "New description", "-f", "personal"],
    )
    assert blocked.exit_code == 1
    assert blocked.stderr == "Error: Entity 'Blocked Person' is blocked.\n"


def test_move_success_merge_and_error_order_byte_exact(
    entity_move_env,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    journal, src_facet, dst_facet, entity_name = entity_move_env()
    slug = entity_slug(entity_name)
    moved = runner.invoke(
        app,
        ["move", entity_name, "--from", src_facet, "--to", dst_facet],
    )
    assert moved.exit_code == 0
    assert moved.stdout == ("Moved entity 'Alice Johnson' from 'work' to 'personal'.\n")
    assert not (journal / "facets" / src_facet / "entities" / slug).exists()
    assert (journal / "facets" / dst_facet / "entities" / slug).exists()

    journal, src_facet, dst_facet, entity_name = entity_move_env(
        src_observations=[
            {"content": "Prefers async", "observed_at": 1000},
            {"content": "Uses Vim", "observed_at": 1001},
        ],
        dst_observations=[
            {"content": "Prefers async", "observed_at": 1000},
            {"content": "Likes tea", "observed_at": 1002},
        ],
        create_dst_entity=True,
    )
    merge_result = runner.invoke(
        app,
        ["move", entity_name, "--from", src_facet, "--to", dst_facet, "--merge"],
    )
    dst_obs_path = (
        journal / "facets" / dst_facet / "entities" / slug / "observations.jsonl"
    )
    observations = [
        json.loads(line)
        for line in dst_obs_path.read_text(encoding="utf-8").splitlines()
    ]
    assert merge_result.exit_code == 0
    assert not (journal / "facets" / src_facet / "entities" / slug).exists()
    assert len(observations) == 3

    entity_move_env(create_dst_entity=True)
    exists = runner.invoke(
        app,
        ["move", entity_name, "--from", src_facet, "--to", dst_facet],
    )
    missing_to = runner.invoke(
        app,
        ["move", entity_name, "--from", src_facet, "--to", "missing"],
    )
    missing_from = runner.invoke(
        app,
        ["move", entity_name, "--from", "missing", "--to", dst_facet],
    )
    missing_entity = runner.invoke(
        app,
        ["move", "Missing", "--from", src_facet, "--to", dst_facet],
    )
    _ensure_facet(journal, "other")
    missing_plain = runner.invoke(
        app,
        ["move", "Unknown", "--from", "other", "--to", dst_facet],
    )
    entity_move_env(entity_name="Blocked Person")
    _mark_blocked_without_detach("Blocked Person")
    blocked = runner.invoke(
        app,
        ["move", "Blocked Person", "--from", src_facet, "--to", dst_facet],
    )
    assert exists.exit_code == 1
    assert exists.stderr == (
        "Error: Entity already exists in destination facet. Use --merge to merge.\n"
    )
    assert missing_from.exit_code == 1
    assert missing_from.stderr == "Error: Facet 'missing' (--from) does not exist.\n"
    assert missing_to.exit_code == 1
    assert missing_to.stderr == "Error: Facet 'missing' (--to) does not exist.\n"
    assert missing_entity.exit_code == 1
    assert missing_entity.stderr == (
        "Error: Entity 'Missing' not found. Did you mean: Alice Johnson\n"
    )
    assert missing_plain.exit_code == 1
    assert (
        missing_plain.stderr == "Error: Entity 'Unknown' not found in facet 'other'.\n"
    )
    assert blocked.exit_code == 1
    assert blocked.stderr == "Error: Entity 'Blocked Person' is blocked.\n"

    def raise_missing_dir(*args, **kwargs):
        raise EntityNotFoundError()

    monkeypatch.setattr(
        "solstone.apps.entities.routes.move_facet_entity",
        raise_missing_dir,
    )
    source_dir_missing = runner.invoke(
        app,
        ["move", entity_name, "--from", src_facet, "--to", dst_facet, "--merge"],
    )
    assert source_dir_missing.exit_code == 1
    assert source_dir_missing.stderr == (
        "Error: Entity data directory not found in source facet.\n"
    )


def test_aka_success_duplicate_first_word_conflict_and_busy_byte_exact(
    entity_env,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    journal = entity_env(
        attached=[
            {
                "type": "Person",
                "name": "Alice Johnson",
                "description": "Friend",
                "attached_at": 1000,
                "updated_at": 1000,
                "aka": ["Ali"],
            },
            {
                "type": "Person",
                "name": "Bob Smith",
                "description": "Neighbor",
                "attached_at": 1001,
                "updated_at": 1001,
            },
        ]
    )

    added = runner.invoke(app, ["aka", "Alice Johnson", "Ally", "-f", "personal"])
    duplicate = runner.invoke(app, ["aka", "Alice Johnson", "Ali", "-f", "personal"])
    first_word = runner.invoke(
        app,
        ["aka", "Alice Johnson", "Alice", "-f", "personal"],
    )
    conflict = runner.invoke(
        app,
        ["aka", "Alice Johnson", "Bob Smith", "-f", "personal"],
    )

    assert added.exit_code == 0
    assert added.stdout == "Added alias 'Ally' to 'Alice Johnson'.\n"
    assert duplicate.exit_code == 0
    assert duplicate.stdout == "Alias 'Ali' already exists for 'Alice Johnson'.\n"
    assert first_word.exit_code == 0
    assert first_word.stdout == (
        "Alias 'Alice' is the first word of 'Alice Johnson' (skipped).\n"
    )
    assert conflict.exit_code == 1
    assert conflict.stderr == (
        "Error: Alias 'Bob Smith' conflicts with entity 'Bob Smith'.\n"
    )

    missing_plain = runner.invoke(app, ["aka", "Missing", "Miss", "-f", "other"])
    assert missing_plain.exit_code == 1
    assert (
        missing_plain.stderr == "Error: Entity 'Missing' not found in facet 'other'.\n"
    )

    entity_env(
        facet="other",
        attached=[
            {"type": "Person", "name": "Blocked Person", "description": "Blocked"}
        ],
    )
    _mark_blocked_without_detach("Blocked Person")
    blocked = runner.invoke(app, ["aka", "Blocked Person", "BP", "-f", "other"])
    assert blocked.exit_code == 1
    assert blocked.stderr == "Error: Entity 'Blocked Person' is blocked.\n"

    def raise_not_found(*args, **kwargs):
        raise EntityNotFoundError()

    monkeypatch.setattr("solstone.apps.entities.routes.add_entity_aka", raise_not_found)
    owner_missing = runner.invoke(
        app,
        ["aka", "Alice Johnson", "Owner Missing", "-f", "personal"],
    )
    assert owner_missing.exit_code == 1
    assert owner_missing.stderr == "Error: Entity 'Alice Johnson' not found.\n"

    def raise_busy(*args, **kwargs):
        raise _busy(journal)

    monkeypatch.setattr("solstone.apps.entities.routes.add_entity_aka", raise_busy)
    busy = runner.invoke(app, ["aka", "Alice Johnson", "Busy", "-f", "personal"])
    assert busy.exit_code == 1
    assert busy.stderr == ENTITY_BUSY.message + "\n"


def test_update_owner_errors_consolidate_and_observe_busy_byte_exact(
    entity_env,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    journal = entity_env(
        attached=[
            {
                "type": "Person",
                "name": "Alice Johnson",
                "description": "Friend",
                "attached_at": 1000,
                "updated_at": 1000,
            }
        ]
    )

    def raise_not_found(*args, **kwargs):
        raise EntityNotFoundError()

    monkeypatch.setattr(
        "solstone.apps.entities.routes.update_facet_entity_description",
        raise_not_found,
    )
    update_missing = runner.invoke(
        app,
        ["update", "Alice Johnson", "New description", "-f", "personal"],
    )
    assert update_missing.exit_code == 1
    assert update_missing.stderr == "Error: Entity 'Alice Johnson' not found.\n"

    def raise_busy(*args, **kwargs):
        raise _busy(journal)

    monkeypatch.setattr(
        "solstone.apps.entities.routes.update_facet_entity_description",
        raise_busy,
    )
    update_busy = runner.invoke(
        app,
        ["update", "Alice Johnson", "New description", "-f", "personal"],
    )
    assert update_busy.exit_code == 1
    assert update_busy.stderr == ENTITY_BUSY.message + "\n"

    empty_observations = runner.invoke(
        app,
        ["observations", "Alice Johnson", "-f", "personal"],
    )
    assert empty_observations.exit_code == 0
    assert empty_observations.stdout == "No observations for 'Alice Johnson'.\n"

    monkeypatch.setattr("solstone.apps.entities.routes.add_observation", raise_busy)
    observe_busy = runner.invoke(
        app,
        ["observe", "Alice Johnson", "Likes coffee", "-f", "personal"],
    )
    assert observe_busy.exit_code == 1
    assert observe_busy.stderr == ENTITY_BUSY.message + "\n"

    consolidate = runner.invoke(app, ["consolidate"])
    consolidate_full = runner.invoke(app, ["consolidate", "--full"])
    assert consolidate.exit_code == 0
    assert consolidate.stdout.startswith("Wrote ")
    assert consolidate_full.exit_code == 0
    assert consolidate_full.stdout.startswith("Wrote ")


def test_observations_and_observe_byte_exact(entity_env) -> None:
    entity_env(
        attached=[
            {
                "type": "Person",
                "name": "Alice Johnson",
                "description": "Friend",
                "attached_at": 1000,
                "updated_at": 1000,
            }
        ],
        observations=["Likes coffee", "Expert in Python"],
        observation_entity="Alice Johnson",
    )

    listed = runner.invoke(app, ["observations", "Alice Johnson", "-f", "personal"])
    observed = runner.invoke(
        app,
        ["observe", "Alice Johnson", "Prefers morning", "-f", "personal"],
    )
    empty_content = runner.invoke(
        app,
        ["observe", "Alice Johnson", "   ", "-f", "personal"],
    )
    missing = runner.invoke(
        app, ["observe", "Missing", "Likes coffee", "-f", "personal"]
    )
    missing_observations = runner.invoke(
        app, ["observations", "Missing", "-f", "other"]
    )
    entity_env(
        facet="other",
        attached=[
            {"type": "Person", "name": "Blocked Person", "description": "Blocked"}
        ],
    )
    _mark_blocked_without_detach("Blocked Person")
    blocked_observations = runner.invoke(
        app, ["observations", "Blocked Person", "-f", "other"]
    )
    blocked_observe = runner.invoke(
        app, ["observe", "Blocked Person", "Likes coffee", "-f", "other"]
    )

    assert listed.exit_code == 0
    assert listed.stdout == (
        "2 observations for 'Alice Johnson':\n"
        "  1. Likes coffee\n"
        "  2. Expert in Python\n"
    )
    assert observed.exit_code == 0
    assert observed.stdout == "Observation added to 'Alice Johnson'.\n"
    assert empty_content.exit_code == 1
    assert empty_content.stderr == "Error: Observation content cannot be empty\n"
    assert missing.exit_code == 1
    assert missing.stderr == (
        "Error: Entity 'Missing' not found. Did you mean: Alice Johnson\n"
    )
    assert missing_observations.exit_code == 1
    assert missing_observations.stderr == (
        "Error: Entity 'Missing' not found in facet 'other'.\n"
    )
    assert blocked_observations.exit_code == 1
    assert blocked_observations.stderr == "Error: Entity 'Blocked Person' is blocked.\n"
    assert blocked_observe.exit_code == 1
    assert blocked_observe.stderr == "Error: Entity 'Blocked Person' is blocked.\n"


def test_record_merge_candidate_and_json_semantics(
    entity_env,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    journal = entity_env()
    created = runner.invoke(
        app,
        [
            "record-merge-candidate",
            "Kognova Inc",
            "Kognova",
            "--facet",
            "work",
            "--day",
            "20260602",
            "--evidence",
            "Kognova Inc / Kognova - needs 1 more",
            "--detections",
            "3",
            "--needs",
            "1",
        ],
    )
    updated = runner.invoke(
        app,
        [
            "record-merge-candidate",
            "Kognova Inc",
            "Kognova",
            "--facet",
            "work",
            "--day",
            "20260603",
            "--evidence",
            "new summary",
            "--detections",
            "4",
        ],
    )
    json_result = runner.invoke(
        app,
        [
            "record-merge-candidate",
            "Newco Inc",
            "Newco",
            "--facet",
            "work",
            "--day",
            "20260602",
            "--evidence",
            "json row",
            "--json",
        ],
    )
    same_slug = runner.invoke(
        app,
        [
            "record-merge-candidate",
            "Kognova",
            "kognova",
            "--facet",
            "work",
            "--day",
            "20260602",
            "--evidence",
            "same",
        ],
    )

    rows = _candidate_rows(journal)
    assert created.exit_code == 0
    assert created.stdout == "merge candidate recorded: Kognova Inc -> Kognova\n"
    assert updated.exit_code == 0
    assert updated.stdout == (
        "merge candidate updated: Kognova Inc -> Kognova (status: open)\n"
    )
    assert rows[0]["last_surfaced"] == "20260603"
    assert rows[0]["evidence"]["detection_count"] == 4
    assert json_result.exit_code == 0
    parsed = json.loads(json_result.stdout)
    assert parsed["source"] == "Newco Inc"
    assert parsed["target"] == "Newco"
    assert same_slug.exit_code == 1
    assert same_slug.stderr == "Error: source and target resolve to the same entity.\n"

    preserved_row = {
        "facet": "work",
        "source": "Preserve Inc",
        "source_slug": "preserve_inc",
        "target": "Preserve",
        "target_slug": "preserve",
        "status": "dismissed",
        "custom": "keep-me",
        "evidence": {
            "basis": "old-basis",
            "summary": "old summary",
            "detection_count": 1,
            "needs": 2,
        },
    }
    save_candidates(_candidate_rows(journal) + [preserved_row])
    preserved = runner.invoke(
        app,
        [
            "record-merge-candidate",
            "Preserve Inc",
            "Preserve",
            "--facet",
            "work",
            "--day",
            "20260604",
            "--evidence",
            "preserved",
            "--detections",
            "5",
        ],
    )
    preserved_rows = _candidate_rows(journal)
    preserved_after = next(
        row for row in preserved_rows if row["source_slug"] == "preserve_inc"
    )
    assert preserved.exit_code == 0
    assert preserved.stdout == (
        "merge candidate updated: Preserve Inc -> Preserve (status: dismissed)\n"
    )
    assert preserved_after["status"] == "dismissed"
    assert preserved_after["custom"] == "keep-me"
    assert preserved_after["evidence"]["summary"] == "preserved"

    def raise_busy(*args, **kwargs):
        raise _busy(journal)

    monkeypatch.setattr(
        "solstone.apps.entities.routes.record_entity_merge_candidate",
        raise_busy,
    )
    busy = runner.invoke(
        app,
        [
            "record-merge-candidate",
            "Busy Inc",
            "Busy",
            "--facet",
            "work",
            "--day",
            "20260602",
            "--evidence",
            "busy",
        ],
    )
    assert busy.exit_code == 1
    assert busy.stderr == ENTITY_BUSY.message + "\n"


def test_merge_candidates_json_semantic_and_text_byte_exact(entity_env) -> None:
    entity_env()
    empty = runner.invoke(app, ["merge-candidates"])
    _seed_merge_candidate()

    json_result = runner.invoke(app, ["merge-candidates", "--json"])
    filtered = runner.invoke(app, ["merge-candidates", "--facet", "other", "--json"])
    status_filtered = runner.invoke(
        app, ["merge-candidates", "--status", "open", "--json"]
    )
    text = runner.invoke(app, ["merge-candidates"])

    assert empty.exit_code == 0
    assert empty.stdout == "No merge candidates found.\n"
    assert json_result.exit_code == 0
    assert json.loads(json_result.stdout)[0]["source"] == "Kognova Inc"
    assert filtered.exit_code == 0
    assert json.loads(filtered.stdout) == []
    assert status_filtered.exit_code == 0
    assert json.loads(status_filtered.stdout)[0]["status"] == "open"
    assert text.exit_code == 0
    assert text.stdout == (
        "Kognova Inc -> Kognova  [open]  facet=work  detections=4  needs=0  last=\n"
    )


def test_accept_and_dismiss_merge_candidate_statuses(entity_env) -> None:
    journal = entity_env()
    _seed_merge_entities()
    _seed_merge_candidate()

    preview = runner.invoke(
        app,
        ["accept-merge-candidate", "kognova_inc", "kognova", "--facet", "work"],
    )
    accepted = runner.invoke(
        app,
        [
            "accept-merge-candidate",
            "kognova_inc",
            "kognova",
            "--facet",
            "work",
            "--commit",
        ],
    )
    accepted_again = runner.invoke(
        app,
        [
            "accept-merge-candidate",
            "kognova_inc",
            "kognova",
            "--facet",
            "work",
            "--commit",
        ],
    )

    assert preview.exit_code == 0
    assert preview.stdout == (
        "Merge preview:\n"
        "  aliases added: Kognova Inc, Kognova Incorporated\n"
        "  emails added: 0\n"
        "  facet links: 0 moved, 0 merged\n"
        "  observations moved: 0\n"
        "  speaker labels updated: 0 labels, 0 corrections\n"
        "  voice samples moved: 0 added, 0 total\n"
    )
    assert accepted.exit_code == 0
    assert accepted.stdout == "Accepted merge candidate: kognova_inc -> kognova\n"
    assert accepted_again.exit_code == 0
    assert accepted_again.stdout == (
        "Merge candidate already accepted: kognova_inc -> kognova\n"
    )
    assert _candidate_rows(journal)[0]["status"] == "accepted"

    _seed_merge_candidate()
    dismissed = runner.invoke(
        app,
        ["dismiss-merge-candidate", "kognova_inc", "kognova", "--facet", "work"],
    )
    dismissed_again = runner.invoke(
        app,
        ["dismiss-merge-candidate", "kognova_inc", "kognova", "--facet", "work"],
    )
    assert dismissed.exit_code == 0
    assert dismissed.stdout == "Dismissed merge candidate: kognova_inc -> kognova\n"
    assert dismissed_again.exit_code == 0
    assert dismissed_again.stdout == (
        "Merge candidate already dismissed: kognova_inc -> kognova\n"
    )


def test_merge_candidate_domain_errors_and_busy_byte_exact(
    entity_env,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    journal = entity_env()

    accept_missing = runner.invoke(
        app,
        ["accept-merge-candidate", "kognova_inc", "kognova", "--facet", "work"],
    )
    dismiss_missing = runner.invoke(
        app,
        ["dismiss-merge-candidate", "kognova_inc", "kognova", "--facet", "work"],
    )
    assert accept_missing.exit_code == 1
    assert accept_missing.stderr == "Error: candidate not found\n"
    assert dismiss_missing.exit_code == 1
    assert dismiss_missing.stderr == "Error: candidate not found\n"

    _seed_merge_candidate(status="accepted")
    preview_accepted = runner.invoke(
        app,
        ["accept-merge-candidate", "kognova_inc", "kognova", "--facet", "work"],
    )
    assert preview_accepted.exit_code == 1
    assert preview_accepted.stderr == (
        "Error: cannot preview candidate with status accepted\n"
    )

    def raise_busy(*args, **kwargs):
        raise _busy(journal)

    monkeypatch.setattr(
        "solstone.apps.entities.routes.accept_entity_candidate",
        raise_busy,
    )
    accept_busy = runner.invoke(
        app,
        [
            "accept-merge-candidate",
            "kognova_inc",
            "kognova",
            "--facet",
            "work",
            "--commit",
        ],
    )
    assert accept_busy.exit_code == 1
    assert accept_busy.stderr == ENTITY_BUSY.message + "\n"

    monkeypatch.setattr(
        "solstone.apps.entities.routes.dismiss_entity_candidate",
        raise_busy,
    )
    dismiss_busy = runner.invoke(
        app,
        ["dismiss-merge-candidate", "kognova_inc", "kognova", "--facet", "work"],
    )
    assert dismiss_busy.exit_code == 1
    assert dismiss_busy.stderr == ENTITY_BUSY.message + "\n"


def test_merge_json_semantics_and_search(entity_env) -> None:
    entity_env(
        attached=[
            {
                "type": "Person",
                "name": "Alice Johnson",
                "description": "Friend",
                "attached_at": 1000,
                "updated_at": 1000,
            }
        ]
    )
    _seed_merge_entities()
    search = runner.invoke(app, ["search", "--type", "Person", "--limit", "1"])
    search_without_facets = runner.invoke(
        app, ["search", "--type", "Company", "--limit", "1"]
    )
    no_results = runner.invoke(app, ["search", "--query", "NoSuchName"])

    save_journal_entity(
        {
            "id": "blocked_source",
            "name": "Blocked Source",
            "type": "Company",
            "blocked": True,
        }
    )
    save_journal_entity(
        {
            "id": "first_principal",
            "name": "First Principal",
            "type": "Person",
            "is_principal": True,
        }
    )
    save_journal_entity(
        {
            "id": "second_principal",
            "name": "Second Principal",
            "type": "Person",
            "is_principal": True,
        }
    )
    save_journal_entity(
        {"id": "cross_source", "name": "Cross Source", "type": "Company"}
    )
    save_journal_entity(
        {"id": "cross_target", "name": "Cross Target", "type": "Company"}
    )
    save_journal_entity(
        {
            "id": "cross_watcher",
            "name": "Cross Watcher",
            "type": "Company",
            "aka": ["cross_source"],
        }
    )

    merge_dry_run = runner.invoke(app, ["merge", "kognova_inc", "kognova"])
    merge_error = runner.invoke(app, ["merge", "missing", "kognova", "--commit"])
    merge_missing_target = runner.invoke(
        app, ["merge", "kognova", "missing_target", "--commit"]
    )
    merge_same = runner.invoke(app, ["merge", "kognova", "kognova", "--commit"])
    merge_blocked = runner.invoke(
        app, ["merge", "blocked_source", "kognova", "--commit"]
    )
    merge_principal = runner.invoke(
        app, ["merge", "first_principal", "second_principal", "--commit"]
    )
    merge_aka_xref = runner.invoke(
        app, ["merge", "cross_source", "cross_target", "--commit"]
    )
    merge_commit = runner.invoke(app, ["merge", "kognova_inc", "kognova", "--commit"])

    assert merge_dry_run.exit_code == 0
    assert json.loads(merge_dry_run.stdout)["merged"] is False
    assert merge_error.exit_code == 1
    assert json.loads(merge_error.stderr) == {
        "error": "Source entity not found: missing"
    }
    assert merge_missing_target.exit_code == 1
    assert json.loads(merge_missing_target.stderr) == {
        "error": "Target entity not found: missing_target"
    }
    assert merge_same.exit_code == 1
    assert json.loads(merge_same.stderr) == {
        "error": "Source and target must be different entities."
    }
    assert merge_blocked.exit_code == 1
    assert json.loads(merge_blocked.stderr) == {
        "error": "Cannot merge blocked entity: blocked_source"
    }
    assert merge_principal.exit_code == 1
    assert json.loads(merge_principal.stderr) == {
        "error": "Cannot merge two principal entities."
    }
    assert merge_aka_xref.exit_code == 1
    assert json.loads(merge_aka_xref.stderr) == {
        "error": "Cannot merge 'cross_source': referenced in aka lists of entity ids: cross_watcher"
    }
    assert search.exit_code == 0
    assert search.stdout == (
        "1 entities:\n  - Alice Johnson (Person): Friend\n    facets: personal\n"
    )
    assert search_without_facets.exit_code == 0
    assert search_without_facets.stdout == "1 entities:\n  - Kognova (Company): \n"
    assert no_results.exit_code == 0
    assert no_results.stdout == "No entities found.\n"
    assert merge_commit.exit_code == 0
    assert json.loads(merge_commit.stdout)["merged"] is True
