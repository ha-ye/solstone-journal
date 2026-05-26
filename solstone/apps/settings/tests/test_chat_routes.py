# SPDX-License-Identifier: AGPL-3.0-only
# Copyright (c) 2026 sol pbc

from __future__ import annotations

import logging
import re

from solstone.apps.chat.config import load_chat_config, save_chat_config
from solstone.convey import create_app


def _base_config() -> dict:
    return {
        "setup": {"completed_at": "2026-05-09T00:00:00Z"},
        "convey": {"trust_localhost": True},
    }


def _client(journal_path):
    app = create_app(str(journal_path))
    app.config["TESTING"] = True
    return app.test_client()


def test_settings_page_renders_checked_chat_thinking_value(settings_env):
    journal_path, _config = settings_env(_base_config())
    save_chat_config({"thinking_surfaces": "always"})
    client = _client(journal_path)

    response = client.get("/app/settings/", follow_redirects=True)

    assert response.status_code == 200
    html = response.get_data(as_text=True)
    assert 'data-section="chat" id="tab-chat"' in html
    assert 'id="section-chat"' in html
    assert re.search(
        r'<input[^>]+name="thinking_surfaces"[^>]+value="always"[^>]+checked',
        html,
    )


def test_chat_api_get_returns_config(settings_env):
    journal_path, _config = settings_env(_base_config())
    client = _client(journal_path)

    response = client.get("/app/settings/api/chat")

    assert response.status_code == 200
    assert response.get_json() == {"thinking_surfaces": "on_tap"}


def test_chat_api_put_round_trips(settings_env):
    journal_path, _config = settings_env(_base_config())
    client = _client(journal_path)

    response = client.put(
        "/app/settings/api/chat",
        json={"thinking_surfaces": "never"},
    )

    assert response.status_code == 200
    assert response.get_json() == {"thinking_surfaces": "never"}
    assert load_chat_config() == {"thinking_surfaces": "never"}


def test_chat_api_put_rejects_invalid_value(settings_env, caplog):
    journal_path, _config = settings_env(_base_config())
    client = _client(journal_path)
    caplog.set_level(logging.WARNING, logger="solstone.apps.settings.routes")

    response = client.put(
        "/app/settings/api/chat",
        json={"thinking_surfaces": "bogus"},
    )

    assert response.status_code == 400
    assert load_chat_config() == {"thinking_surfaces": "on_tap"}
    assert "invalid chat thinking_surfaces value" in caplog.text


def test_chat_api_put_rejects_invalid_shape(settings_env):
    journal_path, _config = settings_env(_base_config())
    client = _client(journal_path)

    response = client.put("/app/settings/api/chat", json=["bad"])

    assert response.status_code == 400
