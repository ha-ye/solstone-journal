# SPDX-License-Identifier: AGPL-3.0-only
# Copyright (c) 2026 sol pbc

from __future__ import annotations

import json
from pathlib import Path

import pytest

from solstone.convey import create_app


@pytest.fixture
def chat_client(tmp_path, monkeypatch):
    journal = tmp_path / "journal"
    config_dir = journal / "config"
    config_dir.mkdir(parents=True)
    (config_dir / "journal.json").write_text(
        json.dumps(
            {
                "setup": {"completed_at": "2026-05-09T00:00:00Z"},
                "convey": {"trust_localhost": True},
            }
        )
        + "\n",
        encoding="utf-8",
    )
    monkeypatch.setenv("SOLSTONE_JOURNAL", str(journal))
    app = create_app(str(journal))
    app.config["TESTING"] = True
    client = app.test_client()
    with client.session_transaction() as session:
        session["logged_in"] = True
        session.permanent = True
    return client


@pytest.fixture
def chat_html(chat_client):
    response = chat_client.get("/app/chat/20990109")
    assert response.status_code == 200
    return response.get_data(as_text=True)


def test_chat_bar_sets_phase_one_from_owner_message(chat_html):
    assert "const chatBarPendingPlaceholders = [];" in chat_html
    assert (
        "if (!solRequestState) {\n        chatBarPendingPlaceholders.push({"
    ) in chat_html
    assert "window.solChatCopy.CHAT_LIVENESS_THINKING" in chat_html
    assert (
        "setStatus(window.solChatCopy.CHAT_LIVENESS_THINKING, "
        "window.solChatCopy.CHAT_LIVENESS_THINKING);"
    ) in chat_html


def test_chat_bar_sets_phase_two_without_blocking_talent_tray(chat_html):
    assert "upsertTalent({" in chat_html
    assert "if (!solRequestState && chatBarPendingPlaceholders.length > 0)" in chat_html
    assert "String(msg.task || '').trim()" in chat_html
    assert (
        "window.solChatCopy.talentLabel(String(msg.name || ''), 'running')" in chat_html
    )
    assert "window.solChatCopy.CHAT_LIVENESS_TASK_FORMAT" in chat_html
    assert "setStatus(composed, composed);" in chat_html


def test_chat_bar_terminal_overwrites_liveness_without_retry_button(chat_html):
    assert (
        "if (chatBarPendingPlaceholders.length > 0) chatBarPendingPlaceholders.shift();"
        in chat_html
    )
    assert "setStatus(msg.text || '', msg.notes || msg.text || '');" in chat_html
    assert (
        "setStatus(renderedReason.message, detail, renderedReason.action);" in chat_html
    )

    app_template = Path("solstone/convey/templates/app.html").read_text(
        encoding="utf-8"
    )
    assert "chat-error-retry" not in app_template
