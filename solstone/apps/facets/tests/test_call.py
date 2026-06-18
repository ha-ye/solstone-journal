# SPDX-License-Identifier: AGPL-3.0-only
# Copyright (c) 2026 sol pbc

from __future__ import annotations

import json
import sys
from pathlib import Path

import pytest
import requests
from typer.testing import CliRunner

ROOT = Path(__file__).resolve().parents[4]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from solstone.apps.facets.call import app
from solstone.convey.reasons import ENTITY_BUSY
from solstone.think.convey_client import ConveyClient
from solstone.think.facet_review_candidates import (
    dismiss_candidate,
    record_facet_candidate,
)
from solstone.think.journal_io import LockTimeout
from tests._baseline_harness import make_test_client, mark_setup_complete


@pytest.fixture
def journal(tmp_path, monkeypatch):
    monkeypatch.setenv("SOLSTONE_JOURNAL", str(tmp_path))
    mark_setup_complete(tmp_path)
    return tmp_path


@pytest.fixture
def runner(journal, monkeypatch):
    client = ConveyClient(session=make_test_client(journal), base_url="")
    monkeypatch.setattr("solstone.apps.facets.call.get_client", lambda: client)
    return CliRunner()


def _seed_candidate(
    *,
    name: str = "Home Reno",
    name_key: str = "home reno",
    count: int = 3,
    day: str = "20260602",
) -> dict:
    return record_facet_candidate(
        name,
        name_key,
        count,
        14,
        [{"day": day, "stream": "archon", "segment": "090000_300"}],
        day,
    )


def test_list_candidates_text_and_json_byte_exact(runner):
    row = _seed_candidate()

    text = runner.invoke(app, ["list-candidates"])
    payload = runner.invoke(app, ["list-candidates", "--json"])

    assert text.exit_code == 0
    assert text.output == "Home Reno  [open]  count=3  last=20260602\n"
    assert payload.exit_code == 0
    assert json.loads(payload.stdout) == [row]


def test_list_candidates_filters_status(runner):
    _seed_candidate()
    _seed_candidate(name="Office Move", name_key="office move", count=2)
    dismiss_candidate("office move")

    result = runner.invoke(app, ["list-candidates", "--status", "open"])

    assert result.exit_code == 0
    assert result.output == "Home Reno  [open]  count=3  last=20260602\n"


def test_list_candidates_empty(runner):
    result = runner.invoke(app, ["list-candidates"])

    assert result.exit_code == 0
    assert result.output == "No facet candidates found.\n"


def test_accept_candidate_success_byte_exact(runner):
    _seed_candidate()

    result = runner.invoke(app, ["accept", "home reno"])

    assert result.exit_code == 0
    assert result.output == "Accepted facet candidate 'home reno' as 'home-reno'.\n"


def test_dismiss_candidate_success_byte_exact(runner):
    _seed_candidate()

    result = runner.invoke(app, ["dismiss", "home reno"])

    assert result.exit_code == 0
    assert result.output == "Dismissed facet candidate 'home reno'.\n"


def test_dismiss_candidate_already_dismissed_byte_exact(runner):
    _seed_candidate()
    dismiss_candidate("home reno")

    result = runner.invoke(app, ["dismiss", "home reno"])

    assert result.exit_code == 0
    assert result.output == "Facet candidate 'home reno' already dismissed.\n"


def test_accept_missing_candidate_prints_http_error(runner):
    result = runner.invoke(app, ["accept", "missing"])

    assert result.exit_code == 1
    assert result.stderr == "candidate not found\n"
    assert result.stdout == ""


def test_accept_busy_prints_owner_voice_error(runner, monkeypatch):
    def _raise_locktimeout(_name_key):
        raise LockTimeout(Path("busy"), 0.01)

    monkeypatch.setattr(
        "solstone.apps.curation.routes.accept_facet_candidate", _raise_locktimeout
    )

    result = runner.invoke(app, ["accept", "home reno"])

    assert result.exit_code == 1
    assert result.stderr == f"{ENTITY_BUSY.message}\n"
    assert result.stdout == ""


def test_convey_down_prints_require_solstone_message(journal, monkeypatch):
    class DownSession:
        def get(self, _url):
            raise requests.exceptions.ConnectionError()

        def post(self, _url, json=None):
            raise requests.exceptions.ConnectionError()

    client = ConveyClient(session=DownSession(), base_url="http://localhost:5015")
    monkeypatch.setattr("solstone.apps.facets.call.get_client", lambda: client)
    monkeypatch.delenv("SOL_SKIP_SUPERVISOR_CHECK", raising=False)
    monkeypatch.delenv("SOL_SUPERVISOR_SPAWNED", raising=False)

    result = CliRunner().invoke(app, ["list-candidates"])

    assert result.exit_code == 1
    assert (
        result.stderr
        == "sol: solstone isn't running. Start it with 'journal up' and retry.\n"
    )
    assert result.stdout == ""
