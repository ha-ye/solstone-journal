# SPDX-License-Identifier: AGPL-3.0-only
# Copyright (c) 2026 sol pbc

from __future__ import annotations

import json

import pytest
from flask import Flask

from solstone.apps.chat.copy import CHAT_DEFERRED_NOT_ANALYZED
from solstone.convey.chat import chat_bp, compose_honest_degradation
from solstone.convey.chat_stream import read_chat_events
from solstone.think.pipeline_health import SegmentBacklog, SegmentCompletion
from solstone.think.processing import (
    format_awaiting_analysis,
    parse_processing_settings,
)


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
        chat_module._thinking_buffers.clear()
        chat_module._thinking_providers.clear()
        for timer in chat_module._watchdog_timers.values():
            timer.cancel()
        chat_module._watchdog_timers.clear()
        chat_module._last_use_id = 0


def _set_current_chat(chat_module, logical_use_id: str, raw_use_id: str | None) -> None:
    with chat_module._state_lock:
        chat_module._current_chat_use_id = logical_use_id
        chat_module._current_chat_state = {
            "raw_use_id": raw_use_id,
            "raw_use_ids_seen": {raw_use_id} if raw_use_id else set(),
            "trigger": {"type": "owner_message", "message": "help"},
            "location": {"app": "sol", "path": "/app/sol", "facet": "work"},
            "retry_count": 0,
        }


@pytest.fixture
def chat_client(tmp_path, monkeypatch):
    import solstone.convey.chat as chat

    _setup_journal(tmp_path, monkeypatch)
    _reset_chat_state(chat)

    app = Flask(__name__)
    app.config["TESTING"] = True
    app.register_blueprint(chat_bp)
    return app.test_client()


def _settings(mode: str):
    return parse_processing_settings({"mode": mode}, strict=False)


def _backlog(
    day: str,
    *,
    not_sensed: int,
    not_thought: int,
    errors: tuple[str, ...] = (),
) -> SegmentBacklog:
    total = not_sensed + not_thought
    completion = SegmentCompletion(
        blockers=[],
        not_sensed=not_sensed,
        not_thought=not_thought,
        total=total,
        capped=0,
    )
    return SegmentBacklog(
        days=(day,),
        not_thought=not_thought,
        not_sensed=not_sensed,
        total=total,
        per_day={day: completion},
        errors=errors,
    )


def _empty_backlog(day: str) -> SegmentBacklog:
    return SegmentBacklog(
        days=(day,),
        not_thought=0,
        not_sensed=0,
        total=0,
        per_day={},
        errors=(),
    )


def _patch_backlog_reads(
    monkeypatch,
    *,
    mode: str,
    backlog: SegmentBacklog,
) -> None:
    monkeypatch.setattr(
        "solstone.convey.chat.load_processing_settings",
        lambda: _settings(mode),
    )
    monkeypatch.setattr(
        "solstone.convey.chat.read_segment_backlog",
        lambda: backlog,
    )


def _patch_finish_side_effects(monkeypatch):
    emitted: dict[str, list] = {"finish": [], "error": [], "cortex": []}
    monkeypatch.setattr(
        "solstone.convey.chat._emit_cortex_event",
        lambda *args, **kwargs: emitted["cortex"].append((args, kwargs)),
    )
    monkeypatch.setattr(
        "solstone.convey.chat._emit_finish",
        lambda *args: emitted["finish"].append(args),
    )
    monkeypatch.setattr(
        "solstone.convey.chat._emit_error",
        lambda *args, **kwargs: emitted["error"].append((args, kwargs)),
    )
    monkeypatch.setattr("solstone.convey.chat._run_next_action", lambda _action: None)
    return emitted


def _finish_chat(chat_module, message_text: str) -> None:
    _set_current_chat(chat_module, "logical-chat", "raw-chat")
    chat_module._on_cortex_finish(
        {
            "use_id": "raw-chat",
            "result": json.dumps(
                {
                    "message": message_text,
                    "notes": "ok",
                    "talent_request": None,
                }
            ),
        }
    )


def _events_of_kind(day: str, kind: str) -> list[dict]:
    return [event for event in read_chat_events(day) if event["kind"] == kind]


def test_compose_honest_degradation_fires_for_deferred_today_pending():
    import solstone.convey.chat as chat

    today = chat._today_day()

    result = compose_honest_degradation(
        _settings("deferred"),
        _backlog(today, not_sensed=2, not_thought=1),
    )

    assert result is not None
    assert CHAT_DEFERRED_NOT_ANALYZED in result
    assert format_awaiting_analysis(3) in result


def test_compose_honest_degradation_ignores_untracked_queried_day():
    import solstone.convey.chat as chat

    today = chat._today_day()

    result = compose_honest_degradation(
        _settings("deferred"),
        _backlog(today, not_sensed=2, not_thought=1),
        queried_day="20260101",
    )

    assert result is None


def test_compose_honest_degradation_ignores_empty_backlog():
    import solstone.convey.chat as chat

    today = chat._today_day()

    result = compose_honest_degradation(
        _settings("deferred"),
        _backlog(today, not_sensed=0, not_thought=0),
    )

    assert result is None


def test_compose_honest_degradation_ignores_realtime_mode():
    import solstone.convey.chat as chat

    today = chat._today_day()

    result = compose_honest_degradation(
        _settings("realtime"),
        _backlog(today, not_sensed=2, not_thought=1),
    )

    assert result is None


def test_compose_honest_degradation_ignores_indeterminate_backlog():
    import solstone.convey.chat as chat

    today = chat._today_day()

    result = compose_honest_degradation(
        _settings("deferred"),
        _backlog(today, not_sensed=2, not_thought=1, errors=(today,)),
    )

    assert result is None


def test_empty_chat_finish_substitutes_honest_degradation(chat_client, monkeypatch):
    import solstone.convey.chat as chat

    today = chat._today_day()
    _patch_backlog_reads(
        monkeypatch,
        mode="deferred",
        backlog=_backlog(today, not_sensed=2, not_thought=1),
    )
    emitted = _patch_finish_side_effects(monkeypatch)

    _finish_chat(chat, "")

    sol_message = _events_of_kind(today, "sol_message")[0]
    assert CHAT_DEFERRED_NOT_ANALYZED in sol_message["text"]
    assert format_awaiting_analysis(3) in sol_message["text"]
    assert [
        event
        for event in read_chat_events(today)
        if event["kind"] == "chat_error"
        and event["reason"] == "provider_response_invalid"
    ] == []
    assert emitted["finish"] == [("logical-chat", sol_message["text"])]
    assert emitted["error"] == []


def test_empty_chat_finish_remains_invalid_in_realtime(chat_client, monkeypatch):
    import solstone.convey.chat as chat

    today = chat._today_day()
    _patch_backlog_reads(
        monkeypatch,
        mode="realtime",
        backlog=_backlog(today, not_sensed=2, not_thought=1),
    )
    emitted = _patch_finish_side_effects(monkeypatch)

    _finish_chat(chat, "")

    sol_message = _events_of_kind(today, "sol_message")[0]
    assert sol_message["text"] == ""
    chat_errors = _events_of_kind(today, "chat_error")
    assert chat_errors[0]["reason"] == "provider_response_invalid"
    assert emitted["finish"] == []
    assert emitted["error"] == [(("logical-chat", "provider_response_invalid"), {})]


def test_empty_chat_finish_remains_invalid_with_empty_backlog(chat_client, monkeypatch):
    import solstone.convey.chat as chat

    today = chat._today_day()
    _patch_backlog_reads(
        monkeypatch,
        mode="deferred",
        backlog=_empty_backlog(today),
    )
    emitted = _patch_finish_side_effects(monkeypatch)

    _finish_chat(chat, "")

    sol_message = _events_of_kind(today, "sol_message")[0]
    assert sol_message["text"] == ""
    assert _events_of_kind(today, "chat_error")[0]["reason"] == (
        "provider_response_invalid"
    )
    assert emitted["finish"] == []
    assert emitted["error"] == [(("logical-chat", "provider_response_invalid"), {})]


def test_empty_chat_finish_remains_invalid_with_backlog_errors(
    chat_client, monkeypatch
):
    import solstone.convey.chat as chat

    today = chat._today_day()
    _patch_backlog_reads(
        monkeypatch,
        mode="deferred",
        backlog=_backlog(today, not_sensed=2, not_thought=1, errors=(today,)),
    )
    emitted = _patch_finish_side_effects(monkeypatch)

    _finish_chat(chat, "")

    sol_message = _events_of_kind(today, "sol_message")[0]
    assert sol_message["text"] == ""
    assert _events_of_kind(today, "chat_error")[0]["reason"] == (
        "provider_response_invalid"
    )
    assert emitted["finish"] == []
    assert emitted["error"] == [(("logical-chat", "provider_response_invalid"), {})]


def test_non_empty_chat_finish_is_not_replaced(chat_client, monkeypatch):
    import solstone.convey.chat as chat

    today = chat._today_day()
    _patch_backlog_reads(
        monkeypatch,
        mode="deferred",
        backlog=_backlog(today, not_sensed=2, not_thought=1),
    )
    emitted = _patch_finish_side_effects(monkeypatch)

    _finish_chat(chat, "Here is your answer.")

    sol_message = _events_of_kind(today, "sol_message")[0]
    assert sol_message["text"] == "Here is your answer."
    assert emitted["finish"] == [("logical-chat", "Here is your answer.")]
    assert emitted["error"] == []
