# SPDX-License-Identifier: AGPL-3.0-only
# Copyright (c) 2026 sol pbc

from __future__ import annotations

import json

from solstone.convey import create_app


def _settings_client(settings_env):
    journal_path, config = settings_env()
    config["setup"] = {"completed_at": "2026-05-23T00:00:00Z"}
    config_path = journal_path / "config" / "journal.json"
    config_path.write_text(json.dumps(config, indent=2) + "\n", encoding="utf-8")
    facet_dir = journal_path / "facets" / "known"
    facet_dir.mkdir(parents=True, exist_ok=True)
    (facet_dir / "facet.json").write_text(
        json.dumps(
            {"title": "Known", "description": "Known facet", "emoji": "K"},
            indent=2,
        )
        + "\n",
        encoding="utf-8",
    )
    app = create_app(str(journal_path))
    app.config["TESTING"] = True
    return app.test_client()


def test_settings_spa_routes_resolve(settings_env):
    client = _settings_client(settings_env)

    index = client.get("/app/settings/")
    assert index.status_code == 200
    assert 'data-solstone-shell="spa"' in index.get_data(as_text=True)

    workspace = client.get("/app/settings/workspace")
    assert workspace.status_code == 200
    assert 'id="settings-index-view"' in workspace.get_data(as_text=True)

    state = client.get("/app/settings/api/state")
    assert state.status_code == 200
    assert set(state.get_json()) == {
        "settings_copy",
        "install_copy",
        "chat_copy",
        "sol_voice_copy",
        "thinking_surfaces",
    }

    static = client.get("/app/settings/static/settings.js")
    assert static.status_code == 200

    detail = client.get("/app/settings/facets/known")
    assert detail.status_code == 200
    assert 'data-solstone-shell="spa"' in detail.get_data(as_text=True)

    missing = client.get("/app/settings/api/facet/unknown")
    assert missing.status_code == 404
