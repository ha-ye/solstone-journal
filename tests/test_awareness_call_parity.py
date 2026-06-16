# SPDX-License-Identifier: AGPL-3.0-only
# Copyright (c) 2026 sol pbc

import json
from pathlib import Path

import pytest
import requests
from typer.testing import CliRunner

import solstone.think.awareness as awareness_mod
from solstone.apps.awareness.call import app
from solstone.convey.reasons import AWARENESS_BUSY
from solstone.think.convey_client import ConveyClient
from solstone.think.journal_io import LockTimeout
from tests._baseline_harness import make_test_client, mark_setup_complete

FROZEN_MS = 1700000000000
FROZEN_ISO = "20260415T12:00:00"
FROZEN_DAY = "20260415"


@pytest.fixture
def journal(tmp_path, monkeypatch):
    # Env must point at the tmp journal so BOTH the seed helpers (append_log/
    # update_state) and the in-process route handlers resolve get_journal() to it.
    monkeypatch.setenv("SOLSTONE_JOURNAL", str(tmp_path))
    mark_setup_complete(tmp_path)
    return tmp_path


@pytest.fixture
def runner(journal, monkeypatch):
    client = ConveyClient(session=make_test_client(journal), base_url="")
    monkeypatch.setattr("solstone.apps.awareness.call.get_client", lambda: client)
    return CliRunner()


@pytest.fixture
def frozen_clock(monkeypatch):
    # Patch the awareness module's private clock helpers so mutation entries are
    # deterministic for both direct seeding and the in-process route handlers.
    monkeypatch.setattr(awareness_mod, "_now_ts", lambda: FROZEN_MS)
    monkeypatch.setattr(awareness_mod, "_now_iso", lambda: FROZEN_ISO)
    monkeypatch.setattr(awareness_mod, "_today", lambda: FROZEN_DAY)


def _write_current(journal, state):
    awareness_dir = journal / "awareness"
    awareness_dir.mkdir(exist_ok=True)
    (awareness_dir / "current.json").write_text(json.dumps(state), encoding="utf-8")


def test_status_full_state_json(runner):
    awareness_mod.update_state("onboarding", {"status": "observing"})

    result = runner.invoke(app, ["status"])

    assert result.exit_code == 0
    assert json.loads(result.stdout) == {"onboarding": {"status": "observing"}}


def test_status_empty_journal_human_line(runner):
    result = runner.invoke(app, ["status"])

    assert result.exit_code == 0
    assert result.stdout == "No awareness state yet.\n"


def test_status_section_json(runner):
    awareness_mod.update_state("onboarding", {"status": "observing"})

    result = runner.invoke(app, ["status", "onboarding"])

    assert result.exit_code == 0
    assert json.loads(result.stdout) == {"status": "observing"}


def test_status_section_non_dict_values(runner, journal):
    _write_current(journal, {"flags": [1, 2, 3], "count": 5})

    flags = runner.invoke(app, ["status", "flags"])
    count = runner.invoke(app, ["status", "count"])

    assert flags.exit_code == 0
    assert json.loads(flags.stdout) == [1, 2, 3]
    assert count.exit_code == 0
    assert json.loads(count.stdout) == 5


def test_status_section_null_is_missing(runner, journal):
    _write_current(journal, {"voiceprint": None})

    result = runner.invoke(app, ["status", "voiceprint"])

    assert result.exit_code == 0
    assert result.stdout == "No 'voiceprint' state.\n"


def test_status_missing_section_human_line(runner):
    awareness_mod.update_state("onboarding", {"status": "observing"})

    result = runner.invoke(app, ["status", "nope"])

    assert result.exit_code == 0
    assert result.stdout == "No 'nope' state.\n"


def test_status_section_empty_journal_uses_empty_state_line(runner):
    result = runner.invoke(app, ["status", "onboarding"])

    assert result.exit_code == 0
    assert result.stdout == "No awareness state yet.\n"


def test_imports_default_state(runner):
    result = runner.invoke(app, ["imports"])

    assert result.exit_code == 0
    assert json.loads(result.stdout) == awareness_mod._default_imports()


def test_imports_record_is_deterministic_without_clock_freeze(runner):
    result = runner.invoke(app, ["imports", "--record", "chatgpt"])

    assert result.exit_code == 0
    assert json.loads(result.stdout) == {
        "has_imported": True,
        "import_count": 1,
        "sources_used": ["chatgpt"],
        "offer_declined": None,
        "last_nudge": None,
    }


def test_imports_declined_uses_frozen_clock(runner, frozen_clock):
    result = runner.invoke(app, ["imports", "--declined"])

    assert result.exit_code == 0
    assert json.loads(result.stdout) == {
        "has_imported": False,
        "import_count": 0,
        "sources_used": [],
        "offer_declined": FROZEN_ISO,
        "last_nudge": None,
    }


def test_imports_nudge_uses_frozen_clock(runner, frozen_clock):
    result = runner.invoke(app, ["imports", "--nudge"])

    assert result.exit_code == 0
    assert json.loads(result.stdout) == {
        "has_imported": False,
        "import_count": 0,
        "sources_used": [],
        "offer_declined": None,
        "last_nudge": FROZEN_ISO,
    }


def test_log_read_empty_journal_human_line(runner):
    result = runner.invoke(app, ["log-read"])

    assert result.exit_code == 0
    assert result.stdout == "No entries found.\n"


def test_log_read_fetches_all_entries_past_default_cap_and_page_boundary(runner):
    for i in range(25):
        awareness_mod.append_log("observation", message=f"m{i}", day="20260101")
    for i in range(150):
        awareness_mod.append_log("observation", message=f"m{i}", day="20260102")

    capped = runner.invoke(app, ["log-read", "20260101"])
    paged = runner.invoke(app, ["log-read", "20260102"])

    assert capped.exit_code == 0
    capped_data = json.loads(capped.stdout)
    assert isinstance(capped_data, list)
    assert len(capped_data) == 25
    assert paged.exit_code == 0
    assert len(json.loads(paged.stdout)) == 150


def test_log_read_kind_filter(runner):
    for i in range(3):
        awareness_mod.append_log("observation", message=f"o{i}", day="20260101")
    for i in range(2):
        awareness_mod.append_log("nudge", message=f"n{i}", day="20260101")

    result = runner.invoke(app, ["log-read", "20260101", "--kind", "observation"])

    assert result.exit_code == 0
    data = json.loads(result.stdout)
    assert len(data) == 3
    assert all(entry["kind"] == "observation" for entry in data)


def test_log_read_limit_is_client_side_last_n_slice(runner):
    for i in range(5):
        awareness_mod.append_log("observation", message=f"m{i}", day="20260101")

    result = runner.invoke(app, ["log-read", "20260101", "--limit", "2"])

    assert result.exit_code == 0
    data = json.loads(result.stdout)
    assert [entry["message"] for entry in data] == ["m3", "m4"]


def test_log_creates_entry_with_message_and_key(runner, frozen_clock):
    result = runner.invoke(
        app, ["log", "observation", "saw a meeting", "--key", "test"]
    )

    assert result.exit_code == 0
    assert json.loads(result.stdout) == {
        "ts": FROZEN_MS,
        "kind": "observation",
        "key": "test",
        "message": "saw a meeting",
    }


def test_log_invalid_data_fails_cli_side_without_post(runner):
    result = runner.invoke(app, ["log", "observation", "--data", "{not valid"])

    assert result.exit_code == 1
    assert result.stderr == "Error: --data must be valid JSON\n"
    assert result.stdout == ""
    assert awareness_mod.read_log() == []


def test_log_creates_entry_with_json_data(runner, frozen_clock):
    result = runner.invoke(app, ["log", "observation", "--data", '{"meetings": 2}'])

    assert result.exit_code == 0
    assert json.loads(result.stdout) == {
        "ts": FROZEN_MS,
        "kind": "observation",
        "data": {"meetings": 2},
    }


def test_convey_down_prints_require_solstone_message(journal, monkeypatch):
    class DownSession:
        def get(self, _url):
            raise requests.exceptions.ConnectionError()

        def post(self, _url, json=None):
            raise requests.exceptions.ConnectionError()

    client = ConveyClient(session=DownSession(), base_url="http://localhost:5015")
    monkeypatch.setattr("solstone.apps.awareness.call.get_client", lambda: client)
    monkeypatch.delenv("SOL_SKIP_SUPERVISOR_CHECK", raising=False)
    monkeypatch.delenv("SOL_SUPERVISOR_SPAWNED", raising=False)

    result = CliRunner().invoke(app, ["status"])

    assert result.exit_code == 1
    assert (
        result.stderr
        == "sol: solstone isn't running. Start it with 'journal up' and retry.\n"
    )
    assert result.stdout == ""


def test_imports_busy_prints_owner_voice_error(runner, monkeypatch):
    def _raises_locktimeout(_source_type):
        raise LockTimeout(Path("busy"), 0.01)

    monkeypatch.setattr(
        "solstone.apps.awareness.routes.record_import", _raises_locktimeout
    )

    result = runner.invoke(app, ["imports", "--record", "x"])

    assert result.exit_code == 1
    assert result.stderr == f"{AWARENESS_BUSY.message}\n"
    assert result.stdout == ""
