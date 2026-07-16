# SPDX-License-Identifier: AGPL-3.0-only
# Copyright (c) 2026 sol pbc

from __future__ import annotations

import importlib

mod = importlib.import_module(
    "solstone.apps.settings.maint.008_migrate_pairing_home_address"
)


def test_migration_moves_valid_legacy_host_url_to_home_address() -> None:
    config = {
        "pairing": {
            "host_url": "http://192.168.1.44:7657",
            "note": "preserve me",
        },
        "unrelated": {"value": True},
    }

    assert mod.migrate(config) is True

    assert config == {
        "pairing": {
            "home_address": "192.168.1.44:7657",
            "note": "preserve me",
        },
        "unrelated": {"value": True},
    }
    assert mod.migrate(config) is False


def test_migration_removes_invalid_legacy_values_without_home_address() -> None:
    for legacy in (
        "http://localhost:7657",
        "http://127.0.0.1:7657",
        "http://192.168.1.44:5015",
        "not a url",
        None,
    ):
        config = {"pairing": {"host_url": legacy, "note": "preserve me"}}

        assert mod.migrate(config) is True

        assert config == {"pairing": {"note": "preserve me"}}


def test_migration_preserves_existing_new_key_when_legacy_invalid() -> None:
    config = {
        "pairing": {
            "host_url": "http://127.0.0.1:7657",
            "home_address": "192.168.1.44:7657",
        }
    }

    assert mod.migrate(config) is True

    assert config == {"pairing": {"home_address": "192.168.1.44:7657"}}
    assert mod.migrate(config) is False


def test_migration_noops_without_legacy_key() -> None:
    config = {"pairing": {"home_address": "192.168.1.44:7657"}}

    assert mod.migrate(config) is False
    assert config == {"pairing": {"home_address": "192.168.1.44:7657"}}
