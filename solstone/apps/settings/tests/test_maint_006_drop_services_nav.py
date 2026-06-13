# SPDX-License-Identifier: AGPL-3.0-only
# Copyright (c) 2026 sol pbc

from __future__ import annotations

import importlib
import json
from pathlib import Path

import pytest

mod = importlib.import_module("solstone.apps.settings.maint.006_drop_services_nav")


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


def test_drop_services_from_order_and_starred(monkeypatch, tmp_path, capsys):
    journal = tmp_path / "journal"
    _set_convey_journal(monkeypatch, journal)
    _write_convey_config(
        journal,
        {
            "apps": {
                "starred": ["home", "services", "chat"],
                "order": ["home", "news", "services", "chat"],
            }
        },
    )

    mod.main()

    config = _read_convey_config(journal)
    assert config["apps"]["starred"] == ["home", "chat"]
    assert config["apps"]["order"] == ["home", "news", "chat"]
    assert "Removed services from app navigation." in capsys.readouterr().out


def test_drop_services_removes_duplicates(monkeypatch, tmp_path, capsys):
    journal = tmp_path / "journal"
    _set_convey_journal(monkeypatch, journal)
    _write_convey_config(
        journal,
        {
            "apps": {
                "starred": ["services", "home", "services"],
                "order": ["services", "home", "services"],
            }
        },
    )

    mod.main()

    config = _read_convey_config(journal)
    assert config["apps"]["starred"] == ["home"]
    assert config["apps"]["order"] == ["home"]
    assert "Removed services from app navigation." in capsys.readouterr().out


def test_drop_services_already_absent_noops(monkeypatch, tmp_path, capsys):
    journal = tmp_path / "journal"
    _set_convey_journal(monkeypatch, journal)
    _write_convey_config(
        journal,
        {"apps": {"starred": ["home"], "order": ["home", "news"]}},
    )

    config_path = journal / "config" / "convey.json"
    before = config_path.read_bytes()
    mod.main()

    assert config_path.read_bytes() == before
    assert "Services already absent from app navigation." in capsys.readouterr().out


def test_drop_services_ignores_missing_lists(monkeypatch, tmp_path, capsys):
    journal = tmp_path / "journal"
    _set_convey_journal(monkeypatch, journal)
    _write_convey_config(journal, {"apps": {"other": ["services"]}})

    config_path = journal / "config" / "convey.json"
    before = config_path.read_bytes()
    mod.main()

    assert config_path.read_bytes() == before
    assert "Services already absent from app navigation." in capsys.readouterr().out


def test_drop_services_write_failure_exits_nonzero(monkeypatch, tmp_path, capsys):
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
