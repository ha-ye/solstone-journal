# SPDX-License-Identifier: AGPL-3.0-only
# Copyright (c) 2026 sol pbc

from __future__ import annotations

import hashlib
import json
import stat
from pathlib import Path

import pytest

from solstone.think.importers import local_secrets
from solstone.think.importers.local_secrets import OuraTokens


def _use_home_and_journal(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, journal_name: str = "journal"
) -> Path:
    home = tmp_path / "home"
    journal = tmp_path / journal_name
    home.mkdir()
    journal.mkdir()
    monkeypatch.setenv("HOME", str(home))
    monkeypatch.setenv("SOLSTONE_JOURNAL", str(journal))

    import solstone.think.utils as think_utils

    think_utils._journal_path_cache = None
    return journal


def _expected_secret_path(home: Path, journal: Path) -> Path:
    fingerprint = hashlib.sha256(str(journal.resolve()).encode("utf-8")).hexdigest()[
        :16
    ]
    return (
        home
        / "Library"
        / "Application Support"
        / "Solstone"
        / "secrets"
        / "oura"
        / f"{fingerprint}.json"
    )


def _expected_integration_path(home: Path, journal: Path, integration: str) -> Path:
    fingerprint = hashlib.sha256(str(journal.resolve()).encode("utf-8")).hexdigest()[
        :16
    ]
    return (
        home
        / "Library"
        / "Application Support"
        / "Solstone"
        / "secrets"
        / integration
        / f"{fingerprint}.json"
    )


def test_save_load_delete_oura_tokens_stays_outside_journal_with_private_modes(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
):
    journal = _use_home_and_journal(tmp_path, monkeypatch)
    home = tmp_path / "home"
    secret_path = _expected_secret_path(home, journal)
    tokens = OuraTokens(
        access_token="access-token-sensitive",
        refresh_token="refresh-token-sensitive",
        expires_at=1800000000.0,
    )

    local_secrets.save_oura_tokens(tokens)

    assert local_secrets.load_oura_tokens() == tokens
    assert secret_path.exists()
    assert not list(journal.rglob("*"))
    assert stat.S_IMODE(secret_path.parent.stat().st_mode) == 0o700
    assert stat.S_IMODE(secret_path.stat().st_mode) == 0o600

    payload = json.loads(secret_path.read_text(encoding="utf-8"))
    assert payload["schema"] == "solstone.local_secret.oura_oauth.v1"
    assert payload["provider"] == "oura"
    assert payload["token_type"] == "Bearer"

    local_secrets.delete_oura_tokens()

    assert local_secrets.load_oura_tokens() is None
    assert not secret_path.exists()


def test_missing_oura_tokens_returns_none_without_creating_secret_dir(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
):
    _use_home_and_journal(tmp_path, monkeypatch)

    assert local_secrets.load_oura_tokens() is None
    assert not (tmp_path / "home" / "Library").exists()


def test_corrupt_oura_tokens_fail_closed_without_rewriting(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
):
    journal = _use_home_and_journal(tmp_path, monkeypatch)
    secret_path = _expected_secret_path(tmp_path / "home", journal)
    secret_path.parent.mkdir(parents=True, mode=0o700)
    secret_path.write_text("{not json", encoding="utf-8")
    before = secret_path.read_text(encoding="utf-8")

    assert local_secrets.load_oura_tokens() is None
    assert secret_path.read_text(encoding="utf-8") == before


def test_different_journals_use_different_oura_secret_files(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
):
    journal_a = _use_home_and_journal(tmp_path, monkeypatch, "journal-a")
    home = tmp_path / "home"
    path_a = _expected_secret_path(home, journal_a)
    tokens_a = OuraTokens(
        access_token="access-a",
        refresh_token="refresh-a",
        expires_at=1800000000.0,
    )

    local_secrets.save_oura_tokens(tokens_a)

    journal_b = tmp_path / "journal-b"
    journal_b.mkdir()
    monkeypatch.setenv("SOLSTONE_JOURNAL", str(journal_b))
    import solstone.think.utils as think_utils

    think_utils._journal_path_cache = None
    path_b = _expected_secret_path(home, journal_b)

    assert path_a != path_b
    assert local_secrets.load_oura_tokens() is None

    tokens_b = OuraTokens(
        access_token="access-b",
        refresh_token="refresh-b",
        expires_at=1900000000.0,
        token_type="Bearer",
    )
    local_secrets.save_oura_tokens(tokens_b)

    assert json.loads(path_a.read_text(encoding="utf-8"))["access_token"] == "access-a"
    assert json.loads(path_b.read_text(encoding="utf-8"))["access_token"] == "access-b"


def test_save_load_delete_general_secret_is_journal_fingerprinted_and_private(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
):
    journal = _use_home_and_journal(tmp_path, monkeypatch)
    home = tmp_path / "home"
    secret_path = _expected_integration_path(home, journal, "google")

    local_secrets.save_secret("google", "GOOGLE_API_KEY", "google-sensitive")

    assert local_secrets.load_secret("google", "GOOGLE_API_KEY") == "google-sensitive"
    assert secret_path.exists()
    assert not list(journal.rglob("*"))
    assert stat.S_IMODE(secret_path.parent.stat().st_mode) == 0o700
    assert stat.S_IMODE(secret_path.stat().st_mode) == 0o600

    payload = json.loads(secret_path.read_text(encoding="utf-8"))
    assert payload["schema"] == "solstone.local_secret.v1"
    assert payload["integration"] == "google"
    assert payload["secrets"]["GOOGLE_API_KEY"] == "google-sensitive"

    local_secrets.delete_secret("google", "GOOGLE_API_KEY")

    assert local_secrets.load_secret("google", "GOOGLE_API_KEY") is None
    assert not secret_path.exists()


def test_env_secret_helpers_prefer_local_boundary_over_process_env(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
):
    _use_home_and_journal(tmp_path, monkeypatch)
    monkeypatch.setenv("GOOGLE_API_KEY", "process-key")

    assert local_secrets.load_env_secret("GOOGLE_API_KEY") == "process-key"

    local_secrets.save_env_secret("GOOGLE_API_KEY", "local-key")

    assert local_secrets.load_env_secret("GOOGLE_API_KEY") == "local-key"
    assert (
        local_secrets.load_env_secret("GOOGLE_API_KEY", include_process=False)
        == "local-key"
    )
    assert local_secrets.is_env_secret_configured("GOOGLE_API_KEY") is True

    local_secrets.delete_env_secret("GOOGLE_API_KEY")

    assert local_secrets.load_env_secret("GOOGLE_API_KEY") == "process-key"
    assert (
        local_secrets.load_env_secret("GOOGLE_API_KEY", include_process=False) is None
    )


def test_invalid_integration_name_rejected(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
):
    _use_home_and_journal(tmp_path, monkeypatch)

    with pytest.raises(ValueError, match="invalid local secret integration"):
        local_secrets.save_secret("../journal", "TOKEN", "sensitive")
