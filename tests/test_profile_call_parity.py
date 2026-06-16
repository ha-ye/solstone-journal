# SPDX-License-Identifier: AGPL-3.0-only
# Copyright (c) 2026 sol pbc

from __future__ import annotations

import json
from urllib.parse import urlsplit

import pytest
import requests
from typer.testing import CliRunner

from solstone.think.convey_client import ConveyClient
from solstone.think.surfaces import profile as profile_surface
from solstone.think.tools.profile import app
from tests._baseline_harness import make_test_client, mark_setup_complete
from tests.test_surfaces_profile import (
    _activity_record,
    _append_activity,
    _configure_env,
    _minimal_facet_tree,
    _participant,
    _utc_ms,
    _write_facet_relationship,
    _write_story_activity,
)


@pytest.fixture
def journal(tmp_path, monkeypatch):
    _configure_env(tmp_path, monkeypatch)
    mark_setup_complete(tmp_path)
    return tmp_path


@pytest.fixture
def runner(journal, monkeypatch):
    client = ConveyClient(session=make_test_client(journal), base_url="")
    monkeypatch.setattr("solstone.think.tools.profile.get_client", lambda: client)
    return CliRunner()


class _Response:
    def __init__(self, body: object, status_code: int = 200) -> None:
        self.status_code = status_code
        self.text = json.dumps(body)


class _Html404Response:
    status_code = 404
    text = "<!doctype html><title>404 Not Found</title>"


class _CaptureSession:
    def __init__(self) -> None:
        self.urls: list[str] = []

    def get(self, url: str):
        captured = _path_with_query(url)
        self.urls.append(captured)
        if captured.startswith("/api/profiles/active"):
            return _Response({"items": [], "total": 0})
        return _Response({})


class _MalformedEnvelopeSession:
    def get(self, _url: str):
        return _Response({"items": {}, "total": 1})


def _seed_ravi(journal, monkeypatch) -> None:
    _minimal_facet_tree(
        journal,
        journal_entities=({"id": "ravi", "name": "Ravi", "type": "Person"},),
    )
    _write_facet_relationship(journal, "work", "ravi", description="Customer")
    monkeypatch.setattr(profile_surface, "_today_day", lambda: "20260420")
    _append_activity(
        "work",
        "20260418",
        _activity_record(
            "20260418",
            [_participant("ravi", name="Ravi")],
            record_id="meeting_cli",
        ),
    )
    _write_story_activity(
        "work",
        "20260418",
        "meeting_story",
        _utc_ms("20260418", 9),
        commitments=[
            {
                "owner": "Mina",
                "owner_entity_id": "mina",
                "action": "send proposal",
                "counterparty": "Ravi",
                "counterparty_entity_id": "ravi",
                "when": "tomorrow",
                "context": "Follow-up.",
            }
        ],
    )


def _path_with_query(url: str) -> str:
    parsed = urlsplit(url)
    return parsed.path + (f"?{parsed.query}" if parsed.query else "")


def _seed_active_people(journal, monkeypatch, count: int) -> None:
    _minimal_facet_tree(journal)
    monkeypatch.setattr(profile_surface, "_today_day", lambda: "20260420")
    for index in range(count):
        entity_id = f"person_{index:03d}"
        _append_activity(
            "work",
            "20260418",
            _activity_record(
                "20260418",
                [_participant(entity_id, name=entity_id.title())],
                record_id=f"meeting_{index:03d}",
            ),
        )


def test_profile_request_mapping(monkeypatch):
    session = _CaptureSession()
    client = ConveyClient(session=session, base_url="")
    monkeypatch.setattr("solstone.think.tools.profile.get_client", lambda: client)
    runner = CliRunner()

    full = runner.invoke(
        app,
        ["full", "Ravi", "--facets", "work", "--include-mentions", "--json"],
    )
    brief = runner.invoke(app, ["brief", "Ravi", "--json"])
    cadence = runner.invoke(app, ["cadence", "Ravi", "--include-mentions", "--json"])
    active = runner.invoke(app, ["list-active", "--window-days", "45", "--json"])

    assert full.exit_code == 0
    assert brief.exit_code == 0
    assert cadence.exit_code == 0
    assert active.exit_code == 0
    assert session.urls == [
        "/api/profile/Ravi?facets=work&include_mentions=true",
        "/api/profile/Ravi/brief",
        "/api/profile/Ravi/cadence?include_mentions=true",
        "/api/profiles/active?window_days=45&limit=100&offset=0",
    ]


def test_profile_name_path_encoding(monkeypatch):
    session = _CaptureSession()
    client = ConveyClient(session=session, base_url="")
    monkeypatch.setattr("solstone.think.tools.profile.get_client", lambda: client)
    runner = CliRunner()

    names = ["space name", "percent%name", "question?name", "hash#name"]
    for name in names:
        result = runner.invoke(app, ["full", name, "--json"])
        assert result.exit_code == 0

    assert session.urls == [
        "/api/profile/space%20name",
        "/api/profile/percent%25name",
        "/api/profile/question%3Fname",
        "/api/profile/hash%23name",
    ]


def test_profile_full_json_and_plain(runner, journal, monkeypatch):
    _seed_ravi(journal, monkeypatch)

    plain = runner.invoke(app, ["full", "Ravi"])
    json_result = runner.invoke(app, ["full", "Ravi", "--json"])

    assert plain.exit_code == 0
    assert "Cadence:" in plain.stdout
    assert json_result.exit_code == 0
    payload = json.loads(json_result.stdout)
    assert payload["entity_id"] == "ravi"
    assert "cadence" in payload


def test_profile_not_found_json_exits_without_stdout(runner, journal):
    _minimal_facet_tree(journal)

    result = runner.invoke(app, ["full", "missing", "--json"])

    assert result.exit_code == 1
    assert result.stderr == "profile not found: missing\n"
    assert result.stdout == ""


def test_profile_bare_404_maps_to_not_found(monkeypatch):
    class SlashSession:
        def get(self, _url: str):
            return _Html404Response()

    client = ConveyClient(session=SlashSession(), base_url="")
    monkeypatch.setattr("solstone.think.tools.profile.get_client", lambda: client)

    result = CliRunner().invoke(app, ["full", "slash/name", "--json"])

    assert result.exit_code == 1
    assert result.stderr == "profile not found: slash/name\n"
    assert result.stdout == ""


def test_profile_list_active_pages_past_boundary(runner, journal, monkeypatch):
    _seed_active_people(journal, monkeypatch, 125)

    result = runner.invoke(app, ["list-active", "--json"])

    assert result.exit_code == 0
    ids = json.loads(result.stdout)
    assert len(ids) == 125
    assert ids == sorted(ids)


def test_profile_list_active_malformed_envelope_exits_without_stdout(monkeypatch):
    client = ConveyClient(session=_MalformedEnvelopeSession(), base_url="")
    monkeypatch.setattr("solstone.think.tools.profile.get_client", lambda: client)

    result = CliRunner().invoke(app, ["list-active", "--json"])

    assert result.exit_code == 1
    assert "I couldn't read the journal response." in result.stderr
    assert result.stdout == ""


def test_profile_convey_down_prints_require_solstone_message(journal, monkeypatch):
    class DownSession:
        def get(self, _url):
            raise requests.exceptions.ConnectionError()

        def post(self, _url, json=None):
            raise requests.exceptions.ConnectionError()

    client = ConveyClient(session=DownSession(), base_url="http://localhost:5015")
    monkeypatch.setattr("solstone.think.tools.profile.get_client", lambda: client)
    monkeypatch.delenv("SOL_SKIP_SUPERVISOR_CHECK", raising=False)
    monkeypatch.delenv("SOL_SUPERVISOR_SPAWNED", raising=False)

    result = CliRunner().invoke(app, ["list-active"])

    assert result.exit_code == 1
    assert (
        result.stderr
        == "sol: solstone isn't running. Start it with 'journal up' and retry.\n"
    )
    assert result.stdout == ""
