# SPDX-License-Identifier: AGPL-3.0-only
# Copyright (c) 2026 sol pbc

from __future__ import annotations

import json
from pathlib import Path

from scripts import migrate_config_secrets as migrate_script
from solstone.think.config_secrets_migration import (
    migrate_config_secrets,
    replicated_secret_paths,
)
from solstone.think.importers import local_secrets
from solstone.think.journal_config import read_journal_config, write_journal_config


def _synthetic_config() -> dict:
    return {
        "identity": {"name": "Test"},
        "convey": {
            "secret": "convey-secret-sensitive",
            "password_hash": "hashed-password-sensitive",
            "allow_network_access": False,
        },
        "env": {
            "GOOGLE_API_KEY": "google-sensitive",
            "OPENAI_API_KEY": "openai-sensitive",
            "ANTHROPIC_API_KEY": "anthropic-sensitive",
            "REVAI_ACCESS_TOKEN": "revai-sensitive",
            "PLAUD_ACCESS_TOKEN": "plaud-sensitive",
            "SOL_DAY": "20260706",
        },
        "oura": {"client_id": "public-client-id"},
        "voice": {"openai_api_key": "voice-openai-sensitive", "model": "gpt-rt"},
        "providers": {"key_validation": {"google": {"valid": True}}},
    }


def _journal(tmp_path: Path, monkeypatch) -> Path:
    home = tmp_path / "home"
    home.mkdir()
    journal = tmp_path / "journal"
    (journal / "config").mkdir(parents=True)
    monkeypatch.setenv("HOME", str(home))
    monkeypatch.setenv("SOLSTONE_JOURNAL", str(journal))
    import solstone.think.utils as think_utils

    think_utils._journal_path_cache = None
    write_journal_config(_synthetic_config(), journal)
    return journal


def test_config_secret_migration_dry_run_is_read_only(tmp_path, monkeypatch):
    journal = _journal(tmp_path, monkeypatch)
    before = (journal / "config" / "journal.json").read_text(encoding="utf-8")

    result = migrate_config_secrets(journal_path=journal)

    assert result.applied is False
    assert {move.path for move in result.moves} == {
        "convey.secret",
        "convey.password_hash",
        "env.GOOGLE_API_KEY",
        "env.OPENAI_API_KEY",
        "env.ANTHROPIC_API_KEY",
        "env.REVAI_ACCESS_TOKEN",
        "env.PLAUD_ACCESS_TOKEN",
        "voice.openai_api_key",
    }
    assert (journal / "config" / "journal.json").read_text(encoding="utf-8") == before
    assert not (tmp_path / "home" / "Library").exists()


def test_config_secret_migration_apply_moves_values_and_removes_config_keys(
    tmp_path, monkeypatch
):
    journal = _journal(tmp_path, monkeypatch)

    result = migrate_config_secrets(journal_path=journal, apply=True)

    assert result.applied is True
    saved = read_journal_config(journal)
    assert saved["env"] == {"SOL_DAY": "20260706"}
    assert saved["convey"] == {"allow_network_access": False}
    assert saved["oura"]["client_id"] == "public-client-id"
    assert saved["voice"] == {"model": "gpt-rt"}
    assert replicated_secret_paths(saved) == []

    assert (
        local_secrets.load_env_secret(
            "GOOGLE_API_KEY",
            journal_path=journal,
            include_process=False,
        )
        == "google-sensitive"
    )
    assert (
        local_secrets.load_env_secret(
            "PLAUD_ACCESS_TOKEN",
            journal_path=journal,
            include_process=False,
        )
        == "plaud-sensitive"
    )
    assert (
        local_secrets.load_secret("convey", "password_hash", journal_path=journal)
        == "hashed-password-sensitive"
    )
    assert (
        local_secrets.load_secret("voice", "openai_api_key", journal_path=journal)
        == "voice-openai-sensitive"
    )


def test_config_secret_migration_apply_is_idempotent(tmp_path, monkeypatch):
    journal = _journal(tmp_path, monkeypatch)

    first = migrate_config_secrets(journal_path=journal, apply=True)
    second = migrate_config_secrets(journal_path=journal, apply=True)

    assert len(first.moves) == 8
    assert second.moves == ()
    assert replicated_secret_paths(read_journal_config(journal)) == []
    assert (
        local_secrets.load_env_secret(
            "OPENAI_API_KEY",
            journal_path=journal,
            include_process=False,
        )
        == "openai-sensitive"
    )


def test_migration_script_defaults_to_dry_run(tmp_path, monkeypatch, capsys):
    journal = _journal(tmp_path, monkeypatch)

    assert migrate_script.main(["--journal", str(journal)]) == 0

    payload = json.loads(capsys.readouterr().out)
    assert payload["applied"] is False
    assert payload["moves"][0]["action"] == "would_move"
    assert read_journal_config(journal)["env"]["GOOGLE_API_KEY"] == "google-sensitive"
