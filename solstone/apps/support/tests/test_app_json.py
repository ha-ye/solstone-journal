# SPDX-License-Identifier: AGPL-3.0-only
# Copyright (c) 2026 sol pbc

from __future__ import annotations

import json
from pathlib import Path

import pytest

from solstone.apps import AppRegistry

SUPPORT_APP_JSON = Path(__file__).resolve().parents[1] / "app.json"


@pytest.fixture
def registry(tmp_path, monkeypatch):
    monkeypatch.setenv("SOLSTONE_JOURNAL", str(tmp_path))
    registry = AppRegistry()
    registry.discover()
    return registry


def test_support_app_json_disables_facets_with_metadata_object():
    metadata = json.loads(SUPPORT_APP_JSON.read_text(encoding="utf-8"))

    assert metadata["facets"] == {"disabled": True}


def test_support_app_json_disables_facets_in_registry(registry):
    assert registry.apps["support"].facets_enabled() is False


def test_support_shell_apps_payload_marks_facets_disabled(registry):
    from solstone.convey.shell_data import _build_apps

    support = next(app for app in _build_apps(registry, {}) if app["name"] == "support")
    assert support["facets_enabled"] is False
