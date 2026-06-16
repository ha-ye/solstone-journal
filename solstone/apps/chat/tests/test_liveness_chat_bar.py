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
            }
        )
        + "\n",
        encoding="utf-8",
    )
    monkeypatch.setenv("SOLSTONE_JOURNAL", str(journal))
    app = create_app(str(journal))
    app.config["TESTING"] = True
    client = app.test_client()
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
    assert "statusWrap.classList.add('chat-bar-status--thinking');" in chat_html
    assert "statusWrap.classList.remove('chat-bar-status--error');" in chat_html


def test_chat_bar_sets_phase_two_without_blocking_talent_tray(chat_html):
    assert "upsertTalent({" in chat_html
    assert "if (!solRequestState && chatBarPendingPlaceholders.length > 0)" in chat_html
    assert "String(msg.task || '').trim()" in chat_html
    assert (
        "window.solChatCopy.talentLabel(String(msg.name || ''), 'running')" in chat_html
    )
    assert "window.solChatCopy.CHAT_LIVENESS_TASK_FORMAT" in chat_html
    assert "setStatus(composed, composed);" in chat_html


def test_chat_bar_enter_submits(chat_html):
    assert "function handleComposerKeydown(event)" in chat_html
    assert "event.isComposing === true || event.keyCode === 229" in chat_html
    assert "event.key === 'Enter' && event.shiftKey" in chat_html
    assert "event.key === 'Enter'" in chat_html
    assert "form.requestSubmit()" in chat_html
    assert "input.addEventListener('keydown', handleComposerKeydown);" in chat_html
    assert chat_html.count("input.addEventListener('keydown'") == 1


def test_chat_bar_terminal_overwrites_liveness_without_retry_button(chat_html):
    assert "function clearPendingLivenessStatus()" in chat_html
    assert (
        "if (chatBarPendingPlaceholders.length > 0) chatBarPendingPlaceholders.shift();"
        in chat_html
    )
    assert "clearPendingLivenessStatus();" in chat_html
    assert "setStatus(msg.text || '', msg.notes || msg.text || '');" in chat_html
    assert (
        "setStatus(renderedReason.message, renderedReason.message, renderedReason.action);"
        in chat_html
    )
    assert "statusWrap.classList.remove('chat-bar-status--thinking');" in chat_html
    assert "statusWrap.classList.add('chat-bar-status--error');" in chat_html
    assert "statusErrorActive = true;" in chat_html
    assert "window.location.href = '/app/chat/';" in chat_html

    app_template = Path("solstone/convey/templates/app.html").read_text(
        encoding="utf-8"
    )
    retry_class = "-".join(("chat", "error", "retry"))
    assert retry_class not in app_template


def test_chat_bar_talent_terminal_clears_liveness(chat_html):
    assert "if (eventName === 'talent_finished')" in chat_html
    assert "if (eventName === 'talent_errored')" in chat_html
    assert (
        "if (!solRequestState && chatBarPendingPlaceholders.length > 0) {\n"
        "        clearPendingLivenessStatus();\n"
        "        setStatus('', '');\n"
        "      }"
    ) in chat_html
