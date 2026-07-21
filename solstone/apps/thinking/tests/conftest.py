# SPDX-License-Identifier: AGPL-3.0-only
# Copyright (c) 2026 sol pbc

"""Self-contained fixtures for Thinking app tests."""

from __future__ import annotations

import json
import os

import pytest

from solstone.think.services import operations, spp, spp_transport


@pytest.fixture(scope="module")
def thinking_app():
    """Build the route registry once; per-test fixtures still isolate journals."""
    from solstone.convey import create_app
    from solstone.think.voice.runtime import stop_all_voice_runtime

    previous = os.environ.get("SOLSTONE_DISABLE_CONVEY_SIDE_RUNTIMES")
    os.environ["SOLSTONE_DISABLE_CONVEY_SIDE_RUNTIMES"] = "1"
    try:
        app = create_app()
    finally:
        if previous is None:
            os.environ.pop("SOLSTONE_DISABLE_CONVEY_SIDE_RUNTIMES", None)
        else:
            os.environ["SOLSTONE_DISABLE_CONVEY_SIDE_RUNTIMES"] = previous
    app.config["TESTING"] = True
    yield app
    stop_all_voice_runtime()


@pytest.fixture(autouse=True)
def _skip_supervisor_check(monkeypatch):
    """Allow app CLI tests to run without a live solstone supervisor."""
    monkeypatch.setenv("SOL_SKIP_SUPERVISOR_CHECK", "1")


@pytest.fixture(autouse=True)
def _restore_convey_journal_root():
    from solstone.convey import state

    original_journal_root = state.journal_root
    yield
    state.journal_root = original_journal_root


@pytest.fixture(autouse=True)
def _clear_service_operations():
    operations.clear_registry()
    yield
    operations.clear_registry()


@pytest.fixture(autouse=True)
def _clear_spp_attestation_state():
    spp.delete_attestation_state()
    yield
    spp.delete_attestation_state()


@pytest.fixture(autouse=True)
def _clear_spp_transport_state():
    spp_transport.teardown_confidential_transport()
    yield
    spp_transport.teardown_confidential_transport()


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
                    "active": {
                        "provider": "google",
                        "model": "gemini-3.5-flash",
                    },
                    "key_validation": {},
                },
                "transcribe": {
                    "backend": "parakeet",
                    "parakeet": {
                        "model_version": "v3",
                        "device": "auto",
                        "timeout_sec": 120.0,
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
