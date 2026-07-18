# SPDX-License-Identifier: AGPL-3.0-only
# Copyright (c) 2026 sol pbc

from __future__ import annotations

import asyncio
import threading

from flask import Flask

from solstone.think.voice import brain
from solstone.think.voice.runtime import start_voice_runtime, stop_voice_runtime

COVENANT_LINE_TEMPLATES = (
    "{agent_name} is sol's spoken voice.",
    "sol keeps the owner's journal as its memory and runs on the owner's device.",
    "call the person whose journal this is the owner, not the user.",
    "in owner-facing speech, describe what sol does with take in, experience, and keep.",
    "never call sol or its live sensing an observer, listener, recorder, "
    "capture system, assistant, or keeper.",
)

RETIRED_COVENANT_PHRASES = (
    "spoken identity of this solstone journal",
    "Use the words observer and listen",
    "Never use the words keeper, assistant, record, or capture.",
)


def _covenant_lines(agent_name: str) -> list[str]:
    return [line.format(agent_name=agent_name) for line in COVENANT_LINE_TEMPLATES]


def _assert_voice_covenant(prompt: str, agent_name: str) -> None:
    for line in _covenant_lines(agent_name):
        assert line in prompt
    for phrase in RETIRED_COVENANT_PHRASES:
        assert phrase not in prompt


def test_extract_instruction():
    text = "before<voice_instruction>Hello there</voice_instruction>after"
    assert brain.extract_instruction(text) == "Hello there"
    assert brain.extract_instruction("no tags here") is None


def test_build_init_prompt_carries_voice_covenant(monkeypatch):
    monkeypatch.setattr(brain, "get_config", lambda: {"agent": {"name": "Astra"}})

    prompt = brain._build_init_prompt()

    _assert_voice_covenant(prompt, "Astra")
    assert "voice-session instruction" in prompt
    assert "Astra is sol's spoken voice." in prompt


def test_build_refresh_prompt_carries_voice_covenant(monkeypatch):
    monkeypatch.setattr(brain, "get_config", lambda: {"agent": {"name": "Astra"}})

    prompt = brain._build_refresh_prompt()

    _assert_voice_covenant(prompt, "Astra")
    assert "Astra is sol's spoken voice." in prompt


def test_start_brain_persists_session(monkeypatch, journal_copy):
    async def fake_run_claude(message, extra_args, *, timeout):
        assert "voice-session instruction" in message
        assert extra_args == ["-n", "voice-brain"]
        assert timeout == 300
        return "<voice_instruction>Speak clearly</voice_instruction>", "session-1"

    monkeypatch.setattr(brain, "_run_claude", fake_run_claude)

    session_id, instruction = asyncio.run(brain.start_brain())

    assert session_id == "session-1"
    assert instruction == "Speak clearly"
    assert (journal_copy / "health" / "voice-brain-session").read_text(
        encoding="utf-8"
    ) == "session-1"


def test_refresh_brain_touches_session_file(monkeypatch, journal_copy):
    session_file = journal_copy / "health" / "voice-brain-session"
    session_file.parent.mkdir(parents=True, exist_ok=True)
    session_file.write_text("session-1", encoding="utf-8")

    async def fake_run_claude(message, extra_args, *, timeout):
        _assert_voice_covenant(message, "sol")
        assert extra_args == ["--resume", "session-1"]
        assert timeout == 300
        return "<voice_instruction>Fresh voice</voice_instruction>", "session-1"

    monkeypatch.setattr(brain, "_run_claude", fake_run_claude)
    before = session_file.stat().st_mtime

    instruction = asyncio.run(brain.refresh_brain("session-1"))

    assert instruction == "Fresh voice"
    assert session_file.stat().st_mtime >= before


def test_ask_brain_uses_resume(monkeypatch):
    async def fake_run_claude(message, extra_args, *, timeout):
        assert message == "What changed?"
        assert extra_args == ["--resume", "session-1"]
        assert timeout == 120
        return "Short answer", "session-1"

    monkeypatch.setattr(brain, "_run_claude", fake_run_claude)

    assert asyncio.run(brain.ask_brain("session-1", "What changed?")) == "Short answer"


def test_schedule_start_and_wait_until_ready(monkeypatch, journal_copy):
    brain.clear_brain_state()
    app = Flask(__name__)

    async def fake_run_claude(message, extra_args, *, timeout):
        return "<voice_instruction>Ready voice</voice_instruction>", "session-2"

    monkeypatch.setattr(brain, "_run_claude", fake_run_claude)

    start_voice_runtime(app)
    try:
        assert brain.wait_until_ready(app, 1.0) is True
        assert app.voice_brain_session == "session-2"
        assert app.voice_brain_instruction == "Ready voice"
        assert isinstance(brain.brain_age_seconds(app), int)
    finally:
        stop_voice_runtime(app)
        brain.clear_brain_state()


def test_schedule_refresh_updates_instruction(monkeypatch, journal_copy):
    brain.clear_brain_state()
    app = Flask(__name__)
    app.voice_brain_session = "session-3"
    app.voice_brain_instruction = "Old voice"
    app.voice_brain_refreshed_at = None
    (journal_copy / "health").mkdir(parents=True, exist_ok=True)
    (journal_copy / "health" / "voice-brain-session").write_text(
        "session-3", encoding="utf-8"
    )

    async def fake_run_claude(message, extra_args, *, timeout):
        return "<voice_instruction>New voice</voice_instruction>", "session-3"

    monkeypatch.setattr(brain, "_run_claude", fake_run_claude)
    refresh_applied = threading.Event()
    original_complete = brain._complete_future

    def complete_and_signal(app_arg, attr_name, future):
        original_complete(app_arg, attr_name, future)
        if attr_name == "refresh_future":
            refresh_applied.set()

    monkeypatch.setattr(brain, "_complete_future", complete_and_signal)

    start_voice_runtime(app)
    try:
        future = brain.schedule_refresh(app, force=True)
        assert future.result(timeout=1.0) == ("session-3", "New voice")
        assert refresh_applied.wait(timeout=2.0)
        assert app.voice_brain_instruction == "New voice"
    finally:
        stop_voice_runtime(app)
        brain.clear_brain_state()


def test_brain_session_file_stays_on_bound_journal(monkeypatch, tmp_path):
    brain.clear_brain_state()
    initial_journal = tmp_path / "initial-journal"
    later_journal = tmp_path / "later-journal"
    monkeypatch.setenv("SOLSTONE_JOURNAL", str(initial_journal))
    app = Flask(__name__)

    async def fake_run_claude(message, extra_args, *, timeout):
        await asyncio.sleep(0.01)
        return "<voice_instruction>Ready voice</voice_instruction>", "session-4"

    monkeypatch.setattr(brain, "_run_claude", fake_run_claude)

    start_voice_runtime(app)
    try:
        monkeypatch.setenv("SOLSTONE_JOURNAL", str(later_journal))
        assert brain.wait_until_ready(app, 1.0) is True
        assert (initial_journal / "health" / "voice-brain-session").read_text(
            encoding="utf-8"
        ) == "session-4"
        assert not (later_journal / "health" / "voice-brain-session").exists()
    finally:
        stop_voice_runtime(app)
        brain.clear_brain_state()
