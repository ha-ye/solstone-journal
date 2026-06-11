# SPDX-License-Identifier: AGPL-3.0-only
# Copyright (c) 2026 sol pbc

from __future__ import annotations

import json
import re
from pathlib import Path

import pytest
import requests
from typer.testing import CliRunner

from solstone.apps.sol.call import app
from solstone.convey.reasons import IDENTITY_BUSY
from solstone.think.convey_client import ConveyClient
from solstone.think.journal_config import read_journal_config, write_journal_config
from solstone.think.journal_io import LockTimeout
from tests._baseline_harness import make_logged_in_test_client

SELF_MD = (
    "# self\n\n"
    "I am sol. this is a new journal — we're just getting started.\n\n"
    "## my name\n"
    "sol (default)\n\n"
    "## who I'm here for\n"
    "[getting to know you]\n"
)


@pytest.fixture
def journal(tmp_path, monkeypatch):
    monkeypatch.setenv("SOLSTONE_JOURNAL", str(tmp_path))
    return tmp_path


@pytest.fixture
def runner(journal, monkeypatch):
    client = ConveyClient(session=make_logged_in_test_client(journal), base_url="")
    monkeypatch.setattr("solstone.apps.sol.call.get_client", lambda: client)
    return CliRunner()


def _write_self_md(journal: Path) -> Path:
    identity_dir = journal / "identity"
    identity_dir.mkdir(parents=True, exist_ok=True)
    path = identity_dir / "self.md"
    path.write_text(SELF_MD, encoding="utf-8")
    return path


def test_name_empty_config_returns_rich_default(runner) -> None:
    result = runner.invoke(app, ["name"])

    assert result.exit_code == 0
    assert json.loads(result.stdout) == {
        "name": "sol",
        "name_status": "default",
        "named_date": None,
        "proposal_count": 0,
    }


def test_thickness_returns_json(runner, monkeypatch) -> None:
    mock_result = {
        "entity_depth": 5,
        "conversation_count": 3,
        "recall_success": 1,
        "facet_count": 2,
        "journal_days": 4,
        "ready": False,
    }
    monkeypatch.setattr(
        "solstone.think.awareness.compute_thickness", lambda: mock_result
    )

    result = runner.invoke(app, ["thickness"])

    assert result.exit_code == 0
    assert json.loads(result.stdout) == mock_result


def test_set_name_updates_config_and_self_md(runner, journal) -> None:
    write_journal_config({})
    self_path = _write_self_md(journal)

    result = runner.invoke(app, ["set-name", "aria", "--status", "chosen"])

    assert result.exit_code == 0
    output = json.loads(result.stdout)
    assert output["name"] == "aria"
    assert output["name_status"] == "chosen"
    assert re.fullmatch(r"\d{4}-\d{2}-\d{2}", output["named_date"])
    assert read_journal_config()["agent"] == output
    self_content = self_path.read_text(encoding="utf-8")
    assert "I am aria." in self_content
    assert "I am sol." not in self_content
    assert f"aria (named {output['named_date']})" in self_content
    assert "sol (default)" not in self_content
    assert "[getting to know you]" in self_content


def test_reset_updates_agent(runner) -> None:
    write_journal_config(
        {
            "agent": {
                "name": "aria",
                "name_status": "chosen",
                "named_date": "2026-04-19",
                "proposal_count": 2,
            }
        }
    )

    result = runner.invoke(app, ["reset"])

    assert result.exit_code == 0
    assert json.loads(result.stdout) == {
        "name": "sol",
        "name_status": "default",
        "named_date": None,
        "proposal_count": 2,
    }
    assert read_journal_config()["agent"]["name"] == "sol"


def test_set_owner_name_only_and_bio(runner, journal) -> None:
    write_journal_config({})
    self_path = _write_self_md(journal)

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
    self_content = self_path.read_text(encoding="utf-8")
    assert "Jer" in self_content
    assert "Building solstone" in self_content
    assert "[getting to know you]" not in self_content


def test_sol_init_creates_identity_files(runner, journal) -> None:
    result = runner.invoke(app, ["sol-init"])

    assert result.exit_code == 0
    assert json.loads(result.stdout) == {
        "identity_dir": str(journal / "identity"),
        "status": "ok",
    }
    assert (journal / "identity" / "self.md").exists()


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

    result = CliRunner().invoke(app, ["name"])

    assert result.exit_code == 1
    assert (
        result.stderr
        == "sol: solstone isn't running. Start it with 'journal up' and retry.\n"
    )
    assert result.stdout == ""


def test_identity_busy_prints_owner_voice_error(runner, journal, monkeypatch) -> None:
    write_journal_config({})
    _write_self_md(journal)

    def raise_timeout(*_args, **_kwargs):
        raise LockTimeout(Path("identity.lock"), 0.01)

    monkeypatch.setattr(
        "solstone.apps.sol.routes.update_self_md_section", raise_timeout
    )

    result = runner.invoke(app, ["set-owner", "Jer"])

    assert result.exit_code == 1
    assert result.stderr == f"{IDENTITY_BUSY.message}\n"
