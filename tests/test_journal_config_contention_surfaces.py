# SPDX-License-Identifier: AGPL-3.0-only
# Copyright (c) 2026 sol pbc

from __future__ import annotations

import importlib
import logging
import sys
from pathlib import Path
from types import SimpleNamespace

import pytest

from solstone.think.journal_io import LockTimeout
from tests.helpers.journal_config import seed_journal_config


def _busy_mutate() -> LockTimeout:
    return LockTimeout(path=Path("busy.lock"), timeout=0.01)


def _raise_busy(*_args, **_kwargs):
    raise _busy_mutate()


@pytest.mark.parametrize(
    ("module_name", "seed", "success_text", "argv"),
    [
        (
            "solstone.apps.observer.maint.000_migrate_remote_to_observer",
            {"observe": {"remote": {"enabled": True}}},
            "Config updated:",
            "observer-maint",
        ),
        (
            "solstone.apps.settings.maint.008_migrate_pairing_home_address",
            {"pairing": {"host_url": "https://home.example"}},
            "Migrated pairing home address config.",
            "settings-maint",
        ),
        (
            "solstone.apps.thinking.maint.000_unify_provider_config",
            {
                "providers": {
                    "google_vertex": {"project_id": "p"},
                    "key_validation": {"google_vertex": {"valid": True}},
                }
            },
            "Unified thinking provider config",
            "thinking-maint",
        ),
    ],
)
def test_maintenance_scripts_propagate_busy_without_success_print(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
    module_name: str,
    seed: dict,
    success_text: str,
    argv: str,
) -> None:
    monkeypatch.setenv("SOLSTONE_JOURNAL", str(tmp_path))
    seed_journal_config(seed, tmp_path)
    module = importlib.import_module(module_name)
    monkeypatch.setattr(module, "mutate_journal_config", _raise_busy)
    monkeypatch.setattr(sys, "argv", [argv])

    with pytest.raises(LockTimeout):
        module.main()

    assert success_text not in capsys.readouterr().out


def test_spl_busy_paths_propagate_without_success_effect_or_log(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
) -> None:
    monkeypatch.setenv("SOLSTONE_JOURNAL", str(tmp_path))
    spl = importlib.import_module("solstone.think.services.spl")
    monkeypatch.setattr(spl, "mutate_journal_config", _raise_busy)
    monkeypatch.setattr(
        spl,
        "LinkState",
        SimpleNamespace(
            load_or_create=lambda: SimpleNamespace(instance_id="i", home_label="h")
        ),
    )
    monkeypatch.setattr(
        spl,
        "load_or_generate_ca",
        lambda *_args, **_kwargs: SimpleNamespace(pubkey_spki_pem="pem"),
    )
    monkeypatch.setattr(spl, "enroll_home", lambda *_args, **_kwargs: "token")
    saved_tokens: list[str] = []
    monkeypatch.setattr(spl, "save_service_token", saved_tokens.append)

    seed_journal_config({"link": {"posture": "direct"}}, tmp_path)
    with caplog.at_level(logging.DEBUG, logger=spl.log.name):
        with pytest.raises(LockTimeout):
            spl.enable_spl()
    assert saved_tokens == []
    assert "enabled sol private link" not in caplog.text

    caplog.clear()
    seed_journal_config({"link": {"posture": "spl"}}, tmp_path)
    with caplog.at_level(logging.DEBUG, logger=spl.log.name):
        with pytest.raises(LockTimeout):
            spl.disable_spl()
    assert "disabled sol private link" not in caplog.text
