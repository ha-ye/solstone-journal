# SPDX-License-Identifier: AGPL-3.0-only
# Copyright (c) 2026 sol pbc

from __future__ import annotations

import argparse
import json
from importlib import import_module

import pytest
from flask import Flask

import solstone.convey.state as convey_state
import solstone.think.utils as think_utils

journal_sources = import_module("solstone.apps.import.journal_sources")
import_routes = import_module("solstone.apps.import.routes")
journal_source_cli = import_module("solstone.think.importers.journal_source_cli")

create_state_directory = journal_sources.create_state_directory
generate_key = journal_sources.generate_key
journal_source_state_prefix = journal_sources.journal_source_state_prefix
load_journal_source_by_fingerprint = journal_sources.load_journal_source_by_fingerprint
save_journal_source = journal_sources.save_journal_source

FINGERPRINT = "sha256:" + "e" * 64


@pytest.fixture
def journal_env(tmp_path, monkeypatch):
    monkeypatch.setattr(convey_state, "journal_root", str(tmp_path), raising=False)
    monkeypatch.setenv("SOLSTONE_JOURNAL", str(tmp_path))
    think_utils._journal_path_cache = None
    (tmp_path / "apps" / "import" / "journal_sources").mkdir(
        parents=True, exist_ok=True
    )
    return tmp_path


def _dl_source() -> dict:
    return {
        "key": generate_key(),
        "name": "alpha",
        "created_at": 1000,
        "enabled": True,
        "revoked": False,
        "revoked_at": None,
        "stats": {
            "segments_received": 0,
            "entities_received": 0,
            "facets_received": 0,
            "imports_received": 0,
            "config_received": 0,
        },
    }


def _pl_source() -> dict:
    return {
        "pair_mode": "pl",
        "fingerprint": FINGERPRINT,
        "device_label": "peer laptop",
        "paired_at": "2026-05-20T00:00:00Z",
        "created_at": 2000,
        "enabled": True,
        "revoked": False,
        "revoked_at": None,
        "stats": {
            "segments_received": 0,
            "entities_received": 0,
            "facets_received": 0,
            "imports_received": 0,
            "config_received": 0,
        },
    }


def _save_dl_and_pl() -> dict:
    dl_source = _dl_source()
    assert save_journal_source(dl_source) is True
    assert save_journal_source(_pl_source()) is True
    return dl_source


def _client():
    app = Flask(__name__)
    app.register_blueprint(import_routes.import_bp)
    return app.test_client()


def _write_json(path, payload) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(payload), encoding="utf-8")


def _stage_all_areas(journal_root) -> dict:
    dl_source = _dl_source()
    assert save_journal_source(dl_source) is True
    prefix = journal_source_state_prefix(dl_source)
    state_dir = create_state_directory(journal_root, prefix)

    entity_item = {
        "area": "entities",
        "source_id": "ent-1",
        "reason": "new",
        "source_entity": {"name": "Ada"},
        "match_candidates": [{"entity_id": "ada"}],
        "staged_at": "2026-06-07T00:00:00Z",
    }
    _write_json(
        state_dir / "entities" / "staged" / "ent-1.json",
        {
            "reason": entity_item["reason"],
            "source_entity": entity_item["source_entity"],
            "match_candidates": entity_item["match_candidates"],
            "staged_at": entity_item["staged_at"],
            "junk": "ignored",
        },
    )
    _write_json(state_dir / "entities" / "staged" / "bad.json", [])

    facet_payload = {"source_id": "foo", "reason": "missing_target"}
    facet_item = {
        "area": "facets",
        "staged_file": "work/entity_observations/foo.staged.json",
        "facet": "work",
        "file_type": "entity_observations",
        **facet_payload,
    }
    _write_json(
        state_dir
        / "facets"
        / "staged"
        / "work"
        / "entity_observations"
        / "foo.staged.json",
        facet_payload,
    )
    _write_json(state_dir / "facets" / "staged" / "shallow.staged.json", {})

    config_item = {"area": "config", "diff": {"field.a": {"category": "x"}}}
    _write_json(state_dir / "config" / "diff.json", config_item["diff"])

    return {
        "entities": entity_item,
        "facets": facet_item,
        "config": config_item,
    }


def test_api_journal_source_list_excludes_pl_records(journal_env) -> None:
    dl_source = _save_dl_and_pl()
    app = Flask(__name__)
    app.register_blueprint(import_routes.import_bp)

    response = app.test_client().get("/app/import/api/journal-sources/list")

    assert response.status_code == 200
    assert response.get_json() == {
        "items": [
            {
                "name": "alpha",
                "prefix": journal_source_state_prefix(dl_source),
                "status": "active",
                "created_at": 1000,
            }
        ],
        "total": 1,
    }


def test_cli_status_and_revoke_cannot_target_pl_fingerprint_by_name(
    journal_env, capsys
) -> None:
    _save_dl_and_pl()

    status_rc = journal_source_cli.cmd_status(
        argparse.Namespace(name=FINGERPRINT, json_output=True)
    )
    status = capsys.readouterr()

    revoke_rc = journal_source_cli.cmd_revoke(
        argparse.Namespace(name=FINGERPRINT, json_output=True)
    )
    revoke = capsys.readouterr()

    assert status_rc == 1
    assert f"journal source '{FINGERPRINT}' not found" in status.err
    assert revoke_rc == 1
    assert f"journal source '{FINGERPRINT}' not found" in revoke.err
    pl_record = load_journal_source_by_fingerprint(FINGERPRINT)
    assert pl_record is not None
    assert pl_record["revoked"] is False


def test_api_journal_source_staged_returns_all_areas_and_skips_invalid(
    journal_env,
) -> None:
    expected = _stage_all_areas(journal_env)

    response = _client().get("/app/import/api/journal-sources/alpha/staged")

    assert response.status_code == 200
    body = response.get_json()
    assert body["total"] == len(body["items"])
    assert body["items"] == [
        expected["entities"],
        expected["facets"],
        expected["config"],
    ]
    assert all(item.get("source_id") != "bad" for item in body["items"])
    assert all(
        item.get("staged_file") != "shallow.staged.json" for item in body["items"]
    )


def test_api_journal_source_staged_area_filter(journal_env) -> None:
    expected = _stage_all_areas(journal_env)
    client = _client()

    entities = client.get("/app/import/api/journal-sources/alpha/staged?area=entities")
    facets = client.get("/app/import/api/journal-sources/alpha/staged?area=facets")
    config = client.get("/app/import/api/journal-sources/alpha/staged?area=config")

    assert entities.status_code == 200
    assert entities.get_json() == {"items": [expected["entities"]], "total": 1}
    assert facets.status_code == 200
    assert facets.get_json() == {"items": [expected["facets"]], "total": 1}
    assert config.status_code == 200
    assert config.get_json() == {"items": [expected["config"]], "total": 1}


def test_api_journal_source_staged_empty_valid_and_unknown(journal_env) -> None:
    dl_source = _dl_source()
    assert save_journal_source(dl_source) is True
    create_state_directory(journal_env, journal_source_state_prefix(dl_source))
    client = _client()

    empty = client.get("/app/import/api/journal-sources/alpha/staged")
    unknown = client.get("/app/import/api/journal-sources/ghost/staged")

    assert empty.status_code == 200
    assert empty.get_json() == {"items": [], "total": 0}
    assert unknown.status_code == 404
    assert unknown.get_json()["reason_code"] == "journal_source_problem"


def test_api_journal_source_staged_invalid_area_returns_error(journal_env) -> None:
    dl_source = _dl_source()
    assert save_journal_source(dl_source) is True

    response = _client().get("/app/import/api/journal-sources/alpha/staged?area=bogus")

    assert response.status_code == 400
    body = response.get_json()
    assert body["reason_code"] == "invalid_request_value"
    assert "items" not in body


def test_api_journal_source_staged_omits_non_dict_config(journal_env) -> None:
    dl_source = _dl_source()
    assert save_journal_source(dl_source) is True
    state_dir = create_state_directory(
        journal_env, journal_source_state_prefix(dl_source)
    )
    _write_json(state_dir / "config" / "diff.json", [])

    response = _client().get("/app/import/api/journal-sources/alpha/staged?area=config")

    assert response.status_code == 200
    assert response.get_json() == {"items": [], "total": 0}
