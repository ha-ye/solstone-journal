# SPDX-License-Identifier: AGPL-3.0-only
# Copyright (c) 2026 sol pbc

from __future__ import annotations

import importlib

from solstone.think.models import CLAUDE_SONNET_4, LOCAL_MODEL

migration = importlib.import_module(
    "solstone.apps.thinking.maint.000_unify_provider_config"
)


def test_migration_prefers_cogitate_and_removes_legacy_provider_state(tmp_path):
    secret = tmp_path / ".config" / "vertex-credentials.json"
    secret.parent.mkdir()
    secret.write_text("secret", encoding="utf-8")
    config = {
        "providers": {
            "generate": {"provider": "google", "model": "gemini-custom"},
            "cogitate": {"provider": "anthropic", "model": "claude-custom"},
            "tier": 2,
            "backup": "openai",
            "models": {"google": {"2": "old"}},
            "contexts": {
                "talent.timeline.segment_summary": {
                    "provider": "openai",
                    "tier": 3,
                    "disabled": True,
                    "extract": False,
                }
            },
            "google_backend": "vertex",
            "vertex_credentials": str(secret),
            "key_validation": {
                "google": {"valid": True},
                "google_vertex": {"valid": True},
                "revai": {"valid": True},
            },
        }
    }

    assert migration.migrate(config, tmp_path) is True

    assert config["providers"] == {
        "active": {"provider": "anthropic", "model": "claude-custom"},
        "key_validation": {"google": {"valid": True}},
    }
    assert config["talent_overrides"] == {
        "talent.timeline.segment_summary": {
            "disabled": True,
            "extract": False,
        }
    }
    assert config["service_key_validation"] == {"revai": {"valid": True}}
    assert secret.exists()
    assert migration.migrate(config, tmp_path) is False


def test_migration_materializes_key_only_personal_cloud_config(tmp_path):
    config = {"env": {"ANTHROPIC_API_KEY": "key"}, "providers": {}}

    assert migration.migrate(config, tmp_path) is True
    assert config["providers"]["active"] == {
        "provider": "anthropic",
        "model": CLAUDE_SONNET_4,
    }


def test_migration_materializes_local_default_and_removes_broken_vertex_link(tmp_path):
    secret = tmp_path / ".config" / "vertex-credentials.json"
    secret.parent.mkdir()
    secret.symlink_to(tmp_path / "missing-secret")
    config = {}

    assert migration.migrate(config, tmp_path) is True
    assert config["providers"]["active"] == {
        "provider": "local",
        "model": LOCAL_MODEL,
    }
    assert secret.is_symlink()
    assert migration.migrate(config, tmp_path) is False


def test_migration_recovers_malformed_legacy_env_and_profile(tmp_path):
    config = {
        "env": ["not", "a", "mapping"],
        "providers": {"generate": {"provider": ["not", "hashable"]}},
    }

    assert migration.migrate(config, tmp_path) is True
    assert config["providers"] == {
        "active": {"provider": "local", "model": LOCAL_MODEL}
    }


def test_migration_preserves_confidential_restore_profile(tmp_path):
    config = {
        "providers": {
            "generate": {"provider": "local"},
            "cogitate": {"provider": "local"},
        },
        "services": {
            "confidential": {
                "prior_generate_provider": "google",
                "prior_cogitate_provider": "anthropic",
            }
        },
    }

    assert migration.migrate(config, tmp_path) is True
    assert config["providers"]["active"] == {
        "provider": "local",
        "model": LOCAL_MODEL,
    }
    assert config["services"]["confidential"] == {
        "prior_active": {
            "provider": "anthropic",
            "model": CLAUDE_SONNET_4,
        }
    }
