# SPDX-License-Identifier: AGPL-3.0-only
# Copyright (c) 2026 sol pbc

"""Self-contained fixtures for settings app tests."""

from __future__ import annotations

import json

import pytest

from solstone.think.importers import local_secrets


@pytest.fixture(autouse=True)
def _skip_supervisor_check(monkeypatch):
    """Allow app CLI tests to run without a live solstone supervisor."""
    monkeypatch.setenv("SOL_SKIP_SUPERVISOR_CHECK", "1")


@pytest.fixture
def settings_env(tmp_path, monkeypatch):
    """Create a temporary journal with settings config."""

    def _create(config: dict | None = None):
        home = tmp_path / "home"
        home.mkdir(exist_ok=True)
        monkeypatch.setenv("HOME", str(home))
        monkeypatch.setenv("SOLSTONE_JOURNAL", str(tmp_path))
        config_dir = tmp_path / "config"
        config_dir.mkdir(parents=True, exist_ok=True)
        config_path = config_dir / "journal.json"
        if config is None:
            config = {
                "identity": {
                    "name": "Test User",
                    "preferred": "Tester",
                    "bio": "A test user",
                    "pronouns": {
                        "subject": "they",
                        "object": "them",
                        "possessive": "their",
                        "reflexive": "themselves",
                    },
                    "aliases": ["tester"],
                    "email_addresses": ["test@example.com"],
                    "timezone": "UTC",
                },
                "env": {
                    "GOOGLE_API_KEY": "test-google-key",
                    "OPENAI_API_KEY": "test-openai-key",
                },
                "providers": {
                    "generate": {
                        "provider": "google",
                        "tier": 2,
                        "backup": "anthropic",
                    },
                    "cogitate": {
                        "provider": "openai",
                        "tier": 2,
                        "backup": "anthropic",
                    },
                    "auth": {
                        "google": "api_key",
                        "openai": "api_key",
                        "anthropic": "platform",
                    },
                    "google_backend": "auto",
                    "key_validation": {},
                },
                "transcribe": {
                    "backend": "parakeet",
                    "enrich": True,
                    "noise_upgrade": False,
                    "parakeet": {
                        "model_version": "v3",
                        "device": "auto",
                        "timeout_sec": 120.0,
                    },
                    "revai": {
                        "model": "fusion",
                    },
                },
                "observe": {"tmux": {"enabled": True, "capture_interval": 5}},
            }
        env = config.get("env")
        if isinstance(env, dict):
            for key in list(env):
                if key in local_secrets.ENV_SECRET_INTEGRATIONS and env.get(key):
                    local_secrets.save_env_secret(
                        key,
                        str(env[key]),
                        journal_path=tmp_path,
                    )
                    env.pop(key, None)
        config_path.write_text(json.dumps(config, indent=2) + "\n", encoding="utf-8")
        return tmp_path, config

    return _create
