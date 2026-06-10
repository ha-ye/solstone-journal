# SPDX-License-Identifier: AGPL-3.0-only
# Copyright (c) 2026 sol pbc

from __future__ import annotations

import importlib
import json
from pathlib import Path

import pytest

from solstone.convey.config import DEFAULT_APP_ORDER, DEFAULT_RAIL_APPS

mod = importlib.import_module(
    "solstone.apps.settings.maint.003_seed_default_app_navigation"
)


def _write_convey_config(journal: Path, payload: dict) -> None:
    config_path = journal / "config" / "convey.json"
    config_path.parent.mkdir(parents=True, exist_ok=True)
    config_path.write_text(json.dumps(payload, indent=2) + "\n", encoding="utf-8")


def _read_convey_config(journal: Path) -> dict:
    return json.loads((journal / "config" / "convey.json").read_text("utf-8"))


def _set_convey_journal(monkeypatch, journal: Path) -> Path:
    from solstone.convey import state

    monkeypatch.setenv("SOLSTONE_JOURNAL", str(journal))
    monkeypatch.setattr(state, "journal_root", str(journal))
    return journal


def test_seed_task_writes_resolved_journal_not_cwd(monkeypatch, tmp_path, capsys):
    journal = tmp_path / "journal"
    cwd = tmp_path / "cwd"
    cwd.mkdir()

    _set_convey_journal(monkeypatch, journal)
    monkeypatch.chdir(cwd)
    mod.main()

    config = _read_convey_config(journal)
    assert config["apps"]["starred"] == DEFAULT_RAIL_APPS
    assert config["apps"]["order"] == DEFAULT_APP_ORDER
    assert not (cwd / "config" / "convey.json").exists()

    out = capsys.readouterr().out
    assert "Seeded default app navigation." in out


def test_seed_task_noops_when_keys_present(monkeypatch, tmp_path, capsys):
    journal = tmp_path / "journal"
    _set_convey_journal(monkeypatch, journal)
    payload = {"apps": {"starred": [], "order": []}}
    _write_convey_config(journal, payload)

    config_path = journal / "config" / "convey.json"
    before = config_path.read_bytes()
    mod.main()

    assert config_path.read_bytes() == before
    assert "Default app navigation already present." in capsys.readouterr().out


def test_seed_task_write_failure_exits_nonzero(monkeypatch, tmp_path, capsys):
    journal = tmp_path / "journal"
    _set_convey_journal(monkeypatch, journal)

    def _raise(*_args, **_kwargs):
        raise OSError("disk full")

    monkeypatch.setattr(mod, "locked_modify_convey_config", _raise)

    with pytest.raises(SystemExit) as exc:
        mod.main()

    assert exc.value.code
    err = capsys.readouterr().err
    assert "PERSIST failed" in err
    assert not (journal / "config" / "convey.json").exists()
