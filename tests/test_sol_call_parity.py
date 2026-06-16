# SPDX-License-Identifier: AGPL-3.0-only
# Copyright (c) 2026 sol pbc

from __future__ import annotations

import json
import re

import pytest
import requests
from typer.testing import CliRunner

from solstone.apps.sol.call import app
from solstone.think.convey_client import ConveyClient
from solstone.think.journal_config import read_journal_config, write_journal_config
from tests._baseline_harness import make_test_client, mark_setup_complete


@pytest.fixture
def journal(tmp_path, monkeypatch):
    monkeypatch.setenv("SOLSTONE_JOURNAL", str(tmp_path))
    mark_setup_complete(tmp_path)
    return tmp_path


@pytest.fixture
def runner(journal, monkeypatch):
    client = ConveyClient(session=make_test_client(journal), base_url="")
    monkeypatch.setattr("solstone.apps.sol.call.get_client", lambda: client)
    return CliRunner()


def test_set_name_updates_config(runner) -> None:
    write_journal_config({"setup": {"completed_at": 1700000000000}})

    result = runner.invoke(app, ["set-name", "aria", "--status", "chosen"])

    assert result.exit_code == 0
    output = json.loads(result.stdout)
    assert output["name"] == "aria"
    assert output["name_status"] == "chosen"
    assert re.fullmatch(r"\d{4}-\d{2}-\d{2}", output["named_date"])
    assert read_journal_config()["agent"] == output


def test_reset_updates_agent(runner) -> None:
    write_journal_config(
        {
            "setup": {"completed_at": 1700000000000},
            "agent": {
                "name": "aria",
                "name_status": "chosen",
                "named_date": "2026-04-19",
            },
        }
    )

    result = runner.invoke(app, ["reset"])

    assert result.exit_code == 0
    assert json.loads(result.stdout) == {
        "name": "sol",
        "name_status": "default",
        "named_date": None,
    }
    assert read_journal_config()["agent"]["name"] == "sol"


def test_set_owner_name_only_and_bio(runner) -> None:
    write_journal_config({"setup": {"completed_at": 1700000000000}})

    name_only = runner.invoke(app, ["set-owner", "Jer"])
    with_bio = runner.invoke(app, ["set-owner", "Jer", "--bio", "Building solstone"])

    assert name_only.exit_code == 0
    assert json.loads(name_only.stdout) == {"name": "Jer", "bio": ""}
    assert with_bio.exit_code == 0
    assert json.loads(with_bio.stdout) == {
        "name": "Jer",
        "bio": "Building solstone",
    }
    config = read_journal_config()
    assert config["identity"]["name"] == "Jer"
    assert config["identity"]["bio"] == "Building solstone"


def test_sol_init_creates_identity_files(runner, journal) -> None:
    result = runner.invoke(app, ["sol-init"])

    assert result.exit_code == 0
    assert json.loads(result.stdout) == {
        "identity_dir": str(journal / "identity"),
        "status": "ok",
    }
    assert (journal / "identity" / "partner.md").exists()


def test_convey_down_prints_require_solstone_message(journal, monkeypatch) -> None:
    class DownSession:
        def get(self, _url):
            raise requests.exceptions.ConnectionError()

        def post(self, _url, json=None):
            raise requests.exceptions.ConnectionError()

    client = ConveyClient(session=DownSession(), base_url="http://localhost:5015")
    monkeypatch.setattr("solstone.apps.sol.call.get_client", lambda: client)
    monkeypatch.delenv("SOL_SKIP_SUPERVISOR_CHECK", raising=False)
    monkeypatch.delenv("SOL_SUPERVISOR_SPAWNED", raising=False)

    result = CliRunner().invoke(app, ["reset"])

    assert result.exit_code == 1
    assert (
        result.stderr
        == "sol: solstone isn't running. Start it with 'journal up' and retry.\n"
    )
    assert result.stdout == ""
