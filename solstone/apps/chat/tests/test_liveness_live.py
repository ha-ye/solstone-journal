# SPDX-License-Identifier: AGPL-3.0-only
# Copyright (c) 2026 sol pbc

from __future__ import annotations

import json

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


def test_live_script_creates_phase_one_placeholder(chat_html):
    assert "const pendingPlaceholders = [];" in chat_html
    assert "window.solChatCopy.CHAT_LIVENESS_THINKING" in chat_html
    assert "chat-event--placeholder" in chat_html
    assert "placeholder.dataset.kind = 'sol_placeholder';" in chat_html
    assert "chat-bubble--placeholder" in chat_html


def test_placeholder_is_excluded_from_event_bookkeeping(chat_html):
    assert chat_html.count('.chat-event:not([data-kind="sol_placeholder"])') >= 3


def test_live_script_updates_placeholder_on_talent_spawned(chat_html):
    assert "String(msg.task || '').trim()" in chat_html
    assert (
        "window.solChatCopy.talentLabel(String(msg.name || ''), 'running')" in chat_html
    )
    assert "window.solChatCopy.CHAT_LIVENESS_TASK_FORMAT" in chat_html
    assert "catch (_err)" in chat_html
    assert "unknown talent target" in chat_html


def test_live_script_removes_placeholder_on_terminal_events(chat_html):
    assert "kind === 'sol_message' || kind === 'chat_error'" in chat_html
    assert "pendingPlaceholders.shift()" in chat_html
    assert (
        "placeholder.element.parentNode.removeChild(placeholder.element)" in chat_html
    )
    assert "msg._retryText = placeholder.ownerText;" in chat_html


def test_live_script_renders_and_delegates_retry(chat_html):
    assert "button.className = 'chat-error-retry';" in chat_html
    assert "button.dataset.retryText = event._retryText;" in chat_html
    assert "window.solChatCopy.CHAT_ERROR_RETRY_ARIA_FORMAT" in chat_html
    assert "window.solChatCopy.CHAT_ERROR_RETRY_LABEL" in chat_html
    assert "transcript.addEventListener('click'" in chat_html
    assert "button.chat-error-retry" in chat_html
    assert "fetch('/api/chat'" in chat_html
    assert "if (!response.ok) retryButton.disabled = false;" in chat_html
    assert "message: text" in chat_html
    assert "app: 'chat'" in chat_html
    assert "path: window.location.pathname" in chat_html
    assert "facet: window.selectedFacet || null" in chat_html
