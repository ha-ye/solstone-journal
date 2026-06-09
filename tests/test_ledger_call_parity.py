# SPDX-License-Identifier: AGPL-3.0-only
# Copyright (c) 2026 sol pbc

from __future__ import annotations

import json
from pathlib import Path
from urllib.parse import urlsplit

import pytest
import requests
from typer.testing import CliRunner

from solstone.convey.reasons import ACTIVITIES_BUSY
from solstone.think.convey_client import ConveyClient
from solstone.think.journal_io import LockTimeout
from solstone.think.tools.ledger import app
from tests._baseline_harness import make_logged_in_test_client
from tests.test_surfaces_ledger import (
    _commitment,
    _decision,
    _minimal_facet_tree,
    _utc_ms,
    _write_story_activity,
)


@pytest.fixture
def journal(tmp_path, monkeypatch):
    monkeypatch.setenv("SOLSTONE_JOURNAL", str(tmp_path))
    return tmp_path


@pytest.fixture
def runner(journal, monkeypatch):
    client = ConveyClient(session=make_logged_in_test_client(journal), base_url="")
    monkeypatch.setattr("solstone.think.tools.ledger.get_client", lambda: client)
    return CliRunner()


class _Response:
    def __init__(self, body: object, status_code: int = 200) -> None:
        self.status_code = status_code
        self.text = json.dumps(body)


class _CaptureSession:
    def __init__(self) -> None:
        self.urls: list[str] = []
        self.posts: list[tuple[str, object]] = []

    def get(self, url: str):
        captured = _path_with_query(url)
        self.urls.append(captured)
        if captured.startswith("/api/ledger/decisions") or captured.startswith(
            "/api/ledger?"
        ):
            return _Response({"items": [], "total": 0})
        return _Response(_item("abc123"))

    def post(self, url: str, json=None):
        self.posts.append((_path_with_query(url), json))
        return _Response(_item("abc123", state=json["as_state"]))


class _MalformedEnvelopeSession:
    def get(self, _url: str):
        return _Response({"items": {}, "total": 1})


def _item(item_id: str, *, state: str = "open", action: str = "send proposal") -> dict:
    return {
        "id": item_id,
        "state": state,
        "owner": "Mina",
        "owner_entity_id": "mina",
        "counterparty": "Ravi",
        "counterparty_entity_id": "ravi",
        "action": action,
        "summary": action,
        "when": None,
        "context": "Context.",
        "opened_at": 1,
        "closed_at": None,
        "age_days": 0,
        "sources": [],
    }


def _path_with_query(url: str) -> str:
    parsed = urlsplit(url)
    return parsed.path + (f"?{parsed.query}" if parsed.query else "")


def _seed_commitments(journal: Path, count: int) -> None:
    _minimal_facet_tree(journal)
    start = _utc_ms("2026-04-10T09:00:00Z")
    for index in range(count):
        _write_story_activity(
            "work",
            "20260410",
            f"meeting_{index:06d}_300",
            start + index,
            commitments=[_commitment(action=f"action number {index}")],
        )


def test_ledger_request_mapping(monkeypatch):
    session = _CaptureSession()
    client = ConveyClient(session=session, base_url="")
    monkeypatch.setattr("solstone.think.tools.ledger.get_client", lambda: client)
    runner = CliRunner()

    list_result = runner.invoke(
        app,
        [
            "list",
            "--state",
            "all",
            "--owner",
            "Mina",
            "--counterparty",
            "Ravi",
            "--age-days-gte",
            "3",
            "--closed-since",
            "20260401",
            "--sort",
            "opened_at_desc",
            "--facets",
            "work,personal",
            "--top",
            "5",
            "--json",
        ],
    )
    decisions_result = runner.invoke(
        app,
        [
            "decisions",
            "--owner",
            "Mina",
            "--since",
            "20260401",
            "--involving",
            "Ravi",
            "--top",
            "2",
            "--facets",
            "work,personal",
            "--json",
        ],
    )
    get_result = runner.invoke(app, ["get", "abc123", "--json"])
    close_result = runner.invoke(
        app, ["close", "abc123", "--note", "done", "--as", "dropped", "--json"]
    )

    assert list_result.exit_code == 0
    assert decisions_result.exit_code == 0
    assert get_result.exit_code == 0
    assert close_result.exit_code == 0
    assert session.urls == [
        "/api/ledger?state=all&owner=Mina&counterparty=Ravi&age_days_gte=3&closed_since=20260401&sort=opened_at_desc&facets=work%2Cpersonal&limit=100&offset=0",
        "/api/ledger/decisions?owner=Mina&since=20260401&involving=Ravi&facets=work%2Cpersonal&limit=100&offset=0",
        "/api/ledger/abc123",
    ]
    assert session.posts == [
        ("/api/ledger/abc123/close", {"note": "done", "as_state": "dropped"})
    ]
    assert "top" not in session.urls[0]
    assert "top" not in session.urls[1]


def test_ledger_list_pages_past_boundary_and_top_preserves_order(runner, journal):
    _seed_commitments(journal, 125)

    full = runner.invoke(app, ["list", "--json"])
    top = runner.invoke(app, ["list", "--top", "7", "--json"])

    assert full.exit_code == 0
    assert top.exit_code == 0
    full_items = json.loads(full.stdout)
    top_items = json.loads(top.stdout)
    assert len(full_items) == 125
    assert len(top_items) == 7
    assert top_items == full_items[:7]


def test_ledger_get_and_close_json_wrap_single_object(runner, journal):
    _seed_commitments(journal, 1)
    listed = runner.invoke(app, ["list", "--json"])
    item_id = json.loads(listed.stdout)[0]["id"]

    fetched = runner.invoke(app, ["get", item_id, "--json"])
    closed = runner.invoke(app, ["close", item_id, "--note", "done", "--json"])

    assert fetched.exit_code == 0
    assert closed.exit_code == 0
    fetched_payload = json.loads(fetched.stdout)
    closed_payload = json.loads(closed.stdout)
    assert isinstance(fetched_payload, list)
    assert isinstance(closed_payload, list)
    assert fetched_payload[0]["id"] == item_id
    assert closed_payload[0]["state"] == "closed"


def test_ledger_decisions_json_array(runner, journal):
    _minimal_facet_tree(journal)
    _write_story_activity(
        "work",
        "20260410",
        "meeting_090000_300",
        _utc_ms("2026-04-10T09:00:00Z"),
        decisions=[_decision()],
    )

    result = runner.invoke(app, ["decisions", "--json"])

    assert result.exit_code == 0
    payload = json.loads(result.stdout)
    assert isinstance(payload, list)
    assert payload[0]["action"] == "move launch review"


def test_ledger_malformed_envelope_exits_without_stdout(monkeypatch):
    client = ConveyClient(session=_MalformedEnvelopeSession(), base_url="")
    monkeypatch.setattr("solstone.think.tools.ledger.get_client", lambda: client)

    result = CliRunner().invoke(app, ["list", "--json"])

    assert result.exit_code == 1
    assert "I couldn't read the journal response." in result.stderr
    assert result.stdout == ""


def test_ledger_missing_item_error(runner, journal):
    _minimal_facet_tree(journal)

    result = runner.invoke(app, ["get", "missing", "--json"])

    assert result.exit_code == 1
    assert result.stderr == "ledger item not found: missing\n"
    assert result.stdout == ""


def test_ledger_close_busy_uses_owner_voice_error(runner, monkeypatch):
    def _raise_busy(*_args, **_kwargs):
        raise LockTimeout(Path("busy"), 0.01)

    monkeypatch.setattr("solstone.convey.ledger.ledger.close", _raise_busy)

    result = runner.invoke(app, ["close", "abc123", "--note", "done", "--json"])

    assert result.exit_code == 1
    assert result.stderr == f"{ACTIVITIES_BUSY.message}\n"
    assert result.stdout == ""


def test_ledger_convey_down_prints_require_solstone_message(journal, monkeypatch):
    class DownSession:
        def get(self, _url):
            raise requests.exceptions.ConnectionError()

        def post(self, _url, json=None):
            raise requests.exceptions.ConnectionError()

    client = ConveyClient(session=DownSession(), base_url="http://localhost:5015")
    monkeypatch.setattr("solstone.think.tools.ledger.get_client", lambda: client)
    monkeypatch.delenv("SOL_SKIP_SUPERVISOR_CHECK", raising=False)
    monkeypatch.delenv("SOL_SUPERVISOR_SPAWNED", raising=False)

    result = CliRunner().invoke(app, ["list"])

    assert result.exit_code == 1
    assert (
        result.stderr
        == "sol: solstone isn't running. Start it with 'journal up' and retry.\n"
    )
    assert result.stdout == ""
