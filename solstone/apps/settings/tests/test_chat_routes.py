# SPDX-License-Identifier: AGPL-3.0-only
# Copyright (c) 2026 sol pbc

from __future__ import annotations

import logging

from solstone.apps.chat import copy as chat_copy
from solstone.apps.chat.config import load_chat_config, save_chat_config
from solstone.convey import create_app

CHAT_THINKING_COPY_NAMES = (
    "CHAT_THINKING_SETTING_LABEL",
    "CHAT_THINKING_OPT_ON_TAP",
    "CHAT_THINKING_OPT_ALWAYS",
    "CHAT_THINKING_OPT_NEVER",
    "CHAT_THINKING_SETTING_HELP",
)


def _base_config() -> dict:
    return {
        "setup": {"completed_at": 1700000000000},
    }


def _client(journal_path):
    app = create_app(str(journal_path))
    app.config["TESTING"] = True
    return app.test_client()


def test_settings_state_returns_chat_thinking_value(settings_env):
    journal_path, _config = settings_env(_base_config())
    save_chat_config({"thinking_surfaces": "always"})
    client = _client(journal_path)

    response = client.get("/app/settings/api/state")

    assert response.status_code == 200
    payload = response.get_json()
    assert payload["thinking_surfaces"] == "always"
    for name in CHAT_THINKING_COPY_NAMES:
        assert payload["chat_copy"][name] == getattr(chat_copy, name)

    workspace = client.get("/app/settings/workspace")
    assert workspace.status_code == 200
    html = workspace.get_data(as_text=True)
    assert 'id="tab-chat"' in html
    assert 'id="section-chat"' in html
    for name in (
        "CHAT_THINKING_OPT_ON_TAP",
        "CHAT_THINKING_OPT_ALWAYS",
        "CHAT_THINKING_OPT_NEVER",
    ):
        assert f'data-copy="chat_copy.{name}"' in html


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
