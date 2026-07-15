# SPDX-License-Identifier: AGPL-3.0-only
# Copyright (c) 2026 sol pbc

from __future__ import annotations

import json

import pytest

from solstone.apps.settings import install_copy
from solstone.convey import create_app


@pytest.fixture
def settings_client(settings_env):
    journal_path, config = settings_env()
    config["setup"] = {"completed_at": 1700000000000}
    (journal_path / "config" / "journal.json").write_text(
        json.dumps(config, indent=2) + "\n",
        encoding="utf-8",
    )
    app = create_app(str(journal_path))
    app.config["TESTING"] = True
    return app.test_client()


def test_settings_state_serves_install_copy(settings_client):
    response = settings_client.get("/app/settings/api/state")

    assert response.status_code == 200
    payload = response.get_json()["install_copy"]
    assert set(payload) == set(install_copy.__all__)
    for name in install_copy.__all__:
        assert payload[name] == getattr(install_copy, name)
