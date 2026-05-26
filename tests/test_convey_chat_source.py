# SPDX-License-Identifier: AGPL-3.0-only
# Copyright (c) 2026 sol pbc

from __future__ import annotations

from datetime import date

import pytest
from flask import Flask

from solstone.convey.chat import ChatSpawnResult, chat_bp
from solstone.convey.chat_stream import read_chat_events


def _setup_journal(tmp_path, monkeypatch):
    journal = tmp_path / "journal"
    journal.mkdir()
    monkeypatch.setenv("SOLSTONE_JOURNAL", str(journal))
    return journal


def _reset_chat_state(chat_module) -> None:
    chat_module.stop_all_chat_runtime()
    with chat_module._state_lock:
        chat_module._current_chat_use_id = None
        chat_module._current_chat_state = None
        chat_module._queued_triggers.clear()
        chat_module._active_talents.clear()
        chat_module._reserved_use_ids.clear()
        for timer in chat_module._watchdog_timers.values():
            timer.cancel()
        chat_module._watchdog_timers.clear()
        chat_module._last_use_id = 0


@pytest.fixture
def chat_client(tmp_path, monkeypatch):
    import solstone.convey.chat as chat

    _setup_journal(tmp_path, monkeypatch)
    _reset_chat_state(chat)
    monkeypatch.setattr(
        "solstone.think.identity.ensure_identity_directory",
        lambda: None,
    )
    monkeypatch.setattr(
        "solstone.convey.chat._spawn_chat_generate",
        lambda action: ChatSpawnResult(ok=True),
    )

    app = Flask(__name__)
    app.config["TESTING"] = True
    app.register_blueprint(chat_bp)
    return app.test_client()


def _latest_owner_message() -> dict:
    events = read_chat_events(date.today().strftime("%Y%m%d"))
    owner_messages = [event for event in events if event["kind"] == "owner_message"]
    assert owner_messages
    return owner_messages[-1]


def test_post_chat_persists_needs_you_source(chat_client):
    source = {"kind": "needs_you", "item_text": "Review the launch checklist"}

    response = chat_client.post(
        "/api/chat",
        json={
            "message": "let's dig into Review the launch checklist",
            "app": "home",
            "path": "/app/home",
            "facet": "work",
            "source": source,
        },
    )

    assert response.status_code == 200
    assert _latest_owner_message()["source"] == source


def test_post_chat_omits_source_when_absent(chat_client):
    response = chat_client.post(
        "/api/chat",
        json={
            "message": "hello there",
            "app": "home",
            "path": "/app/home",
            "facet": "work",
        },
    )

    assert response.status_code == 200
    assert "source" not in _latest_owner_message()
