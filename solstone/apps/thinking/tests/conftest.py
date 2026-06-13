# SPDX-License-Identifier: AGPL-3.0-only
# Copyright (c) 2026 sol pbc

"""Self-contained fixtures for Thinking app tests."""

from __future__ import annotations

import json

import pytest

from solstone.think.services import operations


@pytest.fixture(autouse=True)
def _skip_supervisor_check(monkeypatch):
    """Allow app CLI tests to run without a live solstone supervisor."""
    monkeypatch.setenv("SOL_SKIP_SUPERVISOR_CHECK", "1")


@pytest.fixture(autouse=True)
def _clear_service_operations():
    operations.clear_registry()
    yield
    operations.clear_registry()


@pytest.fixture
def settings_env(tmp_path, monkeypatch):
    """Create a temporary journal with provider config."""

    def _create(config: dict | None = None):
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
                    "contexts": {
                        "work": {
                            "provider": "google",
                            "tier": 2,
                        }
                    },
                    "models": {
                        "generate": "gemini-2.5-pro",
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
                    "whisper": {
                        "device": "auto",
                        "model": "medium.en",
                        "compute_type": "default",
                    },
                    "revai": {
                        "model": "fusion",
                    },
                },
                "observe": {"tmux": {"enabled": True, "capture_interval": 5}},
            }
        config_path.write_text(json.dumps(config, indent=2) + "\n", encoding="utf-8")
        monkeypatch.setenv("SOLSTONE_JOURNAL", str(tmp_path))
        return tmp_path, config

    return _create


@pytest.fixture
def journal_copy(settings_env):
    journal_path, _config = settings_env()
    return journal_path
