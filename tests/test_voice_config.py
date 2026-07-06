# SPDX-License-Identifier: AGPL-3.0-only
# Copyright (c) 2026 sol pbc

from __future__ import annotations

from pathlib import Path

from solstone.think.importers import local_secrets
from solstone.think.voice import config


def _isolate_secret_home(tmp_path: Path, monkeypatch) -> Path:
    home = tmp_path / "home"
    journal = tmp_path / "journal"
    home.mkdir()
    journal.mkdir()
    monkeypatch.setenv("HOME", str(home))
    monkeypatch.setenv("SOLSTONE_JOURNAL", str(journal))
    import solstone.think.utils as think_utils

    think_utils._journal_path_cache = None
    return journal


def test_voice_config_defaults(tmp_path, monkeypatch):
    _isolate_secret_home(tmp_path, monkeypatch)
    monkeypatch.setattr(config, "get_config", lambda: {"agent": {"name": "sol"}})
    monkeypatch.delenv("OPENAI_API_KEY", raising=False)

    assert config.get_openai_api_key() is None
    assert config.get_voice_model() == "gpt-realtime"
    assert config.get_brain_model() == "haiku"


def test_voice_config_prefers_voice_local_secret(tmp_path, monkeypatch):
    journal = _isolate_secret_home(tmp_path, monkeypatch)
    monkeypatch.setattr(
        config,
        "get_config",
        lambda: {
            "voice": {
                "model": "gpt-realtime-mini",
                "brain_model": "sonnet",
            }
        },
    )
    monkeypatch.setenv("OPENAI_API_KEY", "sk-env")
    local_secrets.save_secret(
        "voice",
        "openai_api_key",
        "sk-local-voice",
        journal_path=journal,
    )

    assert config.get_openai_api_key() == "sk-local-voice"
    assert config.get_voice_model() == "gpt-realtime-mini"
    assert config.get_brain_model() == "sonnet"


def test_voice_config_falls_back_to_env(tmp_path, monkeypatch):
    _isolate_secret_home(tmp_path, monkeypatch)
    monkeypatch.setattr(config, "get_config", lambda: {"voice": {}})
    monkeypatch.setenv("OPENAI_API_KEY", "sk-env")

    assert config.get_openai_api_key() == "sk-env"
