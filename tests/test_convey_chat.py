# SPDX-License-Identifier: AGPL-3.0-only
# Copyright (c) 2026 sol pbc

from __future__ import annotations

import json
import logging
from datetime import date, datetime

import pytest
from flask import Flask

from solstone.convey.chat import ChatSpawnResult, chat_bp
from solstone.convey.chat_stream import append_chat_event, read_chat_events
from solstone.think.cortex_client import CortexSpawnUnavailable


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


def _ms(year: int, month: int, day: int, hour: int, minute: int, second: int) -> int:
    return int(datetime(year, month, day, hour, minute, second).timestamp() * 1000)


def _write_talent_log(
    journal, talent_name: str, filename: str, events: list[dict]
) -> None:
    talent_dir = journal / "talents" / talent_name
    talent_dir.mkdir(parents=True, exist_ok=True)
    log_path = talent_dir / filename
    log_path.write_text(
        "\n".join(json.dumps(event) for event in events) + "\n",
        encoding="utf-8",
    )


def _post_chat_message(client, message: str):
    return client.post(
        "/api/chat",
        json={
            "message": message,
            "app": "sol",
            "path": "/app/sol",
            "facet": "work",
        },
    )


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


def test_cortex_thinking_reaches_sol_message(chat_client, monkeypatch):
    import solstone.convey.chat as chat

    monkeypatch.setattr(
        "solstone.convey.chat._emit_cortex_event", lambda *_args, **_kwargs: None
    )
    _set_current_chat(chat, "logical-chat", "raw-chat")

    chat._handle_callosum_message(
        {
            "tract": "cortex",
            "event": "start",
            "use_id": "raw-chat",
            "provider": "openai",
        }
    )
    for summary in ("first thought", "second thought"):
        chat._handle_callosum_message(
            {
                "tract": "cortex",
                "event": "thinking",
                "use_id": "raw-chat",
                "summary": summary,
            }
        )
    chat._on_cortex_finish(
        {
            "use_id": "raw-chat",
            "model": "gpt-reasoning",
            "usage": {"reasoning_tokens": 100},
            "result": json.dumps(
                {
                    "message": "done",
                    "notes": "ok",
                    "talent_request": None,
                }
            ),
        }
    )

    sol_message = next(
        event
        for event in read_chat_events(date.today().strftime("%Y%m%d"))
        if event["kind"] == "sol_message"
    )
    assert sol_message["thinking"] == {
        "content": "first thought\n\nsecond thought",
        "provider": "openai",
        "model": "gpt-reasoning",
        "tokens": 100,
    }
    assert "raw-chat" not in chat._thinking_buffers
    assert "raw-chat" not in chat._thinking_providers


def test_cortex_thinking_reaches_talent_finished(chat_client, monkeypatch):
    import solstone.convey.chat as chat

    monkeypatch.setattr(
        "solstone.convey.chat._emit_cortex_event", lambda *_args, **_kwargs: None
    )
    monkeypatch.setattr("solstone.convey.chat._run_next_action", lambda _action: None)
    monkeypatch.setattr(
        "solstone.convey.chat._arm_watchdog_locked", lambda *_args, **_kwargs: None
    )
    monkeypatch.setattr(
        "solstone.convey.chat._cancel_watchdog_locked", lambda *_args, **_kwargs: None
    )
    _set_current_chat(chat, "logical-chat", None)
    with chat._state_lock:
        chat._active_talents["talent-raw"] = {
            "chat_use_id": "logical-chat",
            "target": "exec",
            "task": "research",
            "location": {"app": "sol", "path": "/app/sol", "facet": "work"},
        }

    chat._handle_callosum_message(
        {
            "tract": "cortex",
            "event": "start",
            "use_id": "talent-raw",
            "provider": "anthropic",
        }
    )
    chat._handle_callosum_message(
        {
            "tract": "cortex",
            "event": "thinking",
            "use_id": "talent-raw",
            "summary": "talent thought",
        }
    )
    chat._on_cortex_finish(
        {
            "use_id": "talent-raw",
            "model": "claude-reasoning",
            "usage": {"reasoning_tokens": 7},
            "result": "summary",
        }
    )

    finished = next(
        event
        for event in read_chat_events(date.today().strftime("%Y%m%d"))
        if event["kind"] == "talent_finished"
    )
    assert finished["thinking"] == {
        "content": "talent thought",
        "provider": "anthropic",
        "model": "claude-reasoning",
        "tokens": 7,
    }


def test_sol_message_omits_thinking_when_not_emitted(chat_client, monkeypatch, caplog):
    import solstone.convey.chat as chat

    monkeypatch.setattr(
        "solstone.convey.chat._emit_cortex_event", lambda *_args, **_kwargs: None
    )
    _set_current_chat(chat, "logical-chat", "raw-chat")

    with caplog.at_level(logging.WARNING, logger="solstone.convey.chat"):
        chat._on_cortex_finish(
            {
                "use_id": "raw-chat",
                "result": {
                    "message": "done",
                    "notes": "ok",
                    "talent_request": None,
                },
            }
        )

    sol_message = next(
        event
        for event in read_chat_events(date.today().strftime("%Y%m%d"))
        if event["kind"] == "sol_message"
    )
    assert "thinking" not in sol_message
    assert caplog.records == []


def test_empty_thinking_summary_is_suppressed(chat_client, monkeypatch):
    import solstone.convey.chat as chat

    monkeypatch.setattr(
        "solstone.convey.chat._emit_cortex_event", lambda *_args, **_kwargs: None
    )
    _set_current_chat(chat, "logical-chat", "raw-chat")

    chat._handle_callosum_message(
        {
            "tract": "cortex",
            "event": "thinking",
            "use_id": "raw-chat",
            "summary": "   \n",
        }
    )
    chat._on_cortex_finish(
        {
            "use_id": "raw-chat",
            "result": {
                "message": "done",
                "notes": "ok",
                "talent_request": None,
            },
        }
    )

    sol_message = next(
        event
        for event in read_chat_events(date.today().strftime("%Y%m%d"))
        if event["kind"] == "sol_message"
    )
    assert "thinking" not in sol_message


def test_thinking_buffers_are_isolated_and_cleared(chat_client, monkeypatch):
    import solstone.convey.chat as chat

    monkeypatch.setattr(
        "solstone.convey.chat._emit_cortex_event", lambda *_args, **_kwargs: None
    )
    _set_current_chat(chat, "logical-chat", "raw-chat")
    with chat._state_lock:
        chat._active_talents["talent-raw"] = {
            "chat_use_id": "logical-chat",
            "target": "exec",
            "task": "research",
            "location": {"app": "sol", "path": "/app/sol", "facet": "work"},
        }

    chat._handle_callosum_message(
        {
            "tract": "cortex",
            "event": "thinking",
            "use_id": "raw-chat",
            "summary": "chat thought",
        }
    )
    chat._handle_callosum_message(
        {
            "tract": "cortex",
            "event": "thinking",
            "use_id": "talent-raw",
            "summary": "talent thought",
        }
    )
    chat._on_cortex_finish(
        {
            "use_id": "raw-chat",
            "result": {
                "message": "done",
                "notes": "ok",
                "talent_request": None,
            },
        }
    )

    sol_message = next(
        event
        for event in read_chat_events(date.today().strftime("%Y%m%d"))
        if event["kind"] == "sol_message"
    )
    assert sol_message["thinking"]["content"] == "chat thought"
    assert "raw-chat" not in chat._thinking_buffers
    assert chat._thinking_buffers["talent-raw"] == ["talent thought"]


def test_thinking_buffer_evicted_on_retry_rotation(chat_client, monkeypatch):
    import solstone.convey.chat as chat

    monkeypatch.setattr(
        "solstone.convey.chat._emit_cortex_event", lambda *_args, **_kwargs: None
    )
    monkeypatch.setattr("solstone.convey.chat._run_next_action", lambda _action: None)
    monkeypatch.setattr(
        "solstone.convey.chat._arm_watchdog_locked", lambda *_args, **_kwargs: None
    )
    monkeypatch.setattr(
        "solstone.convey.chat._cancel_watchdog_locked", lambda *_args, **_kwargs: None
    )
    _set_current_chat(chat, "logical-chat", "raw-old")
    chat._handle_callosum_message(
        {
            "tract": "cortex",
            "event": "thinking",
            "use_id": "raw-old",
            "summary": "old thought",
        }
    )

    chat._on_cortex_finish({"use_id": "raw-old", "result": "not json"})

    with chat._state_lock:
        raw_new = str(chat._current_chat_state["raw_use_id"])
    assert "raw-old" not in chat._thinking_buffers
    chat._handle_callosum_message(
        {
            "tract": "cortex",
            "event": "thinking",
            "use_id": raw_new,
            "summary": "new thought",
        }
    )
    chat._on_cortex_finish(
        {
            "use_id": raw_new,
            "result": {
                "message": "done",
                "notes": "ok",
                "talent_request": None,
            },
        }
    )

    sol_message = next(
        event
        for event in read_chat_events(date.today().strftime("%Y%m%d"))
        if event["kind"] == "sol_message"
    )
    assert sol_message["thinking"]["content"] == "new thought"


def test_thinking_buffer_clear_across_synthetic_max_active_rotation(
    chat_client, monkeypatch
):
    import solstone.convey.chat as chat

    monkeypatch.setattr(
        "solstone.convey.chat._emit_cortex_event", lambda *_args, **_kwargs: None
    )
    monkeypatch.setattr("solstone.convey.chat._run_next_action", lambda _action: None)
    monkeypatch.setattr(
        "solstone.convey.chat._active_talent_count_for_today_locked",
        lambda: chat.MAX_ACTIVE_TALENTS,
    )
    monkeypatch.setattr(
        "solstone.convey.chat._arm_watchdog_locked", lambda *_args, **_kwargs: None
    )
    monkeypatch.setattr(
        "solstone.convey.chat._cancel_watchdog_locked", lambda *_args, **_kwargs: None
    )
    _set_current_chat(chat, "logical-chat", "raw-old")
    chat._handle_callosum_message(
        {
            "tract": "cortex",
            "event": "thinking",
            "use_id": "raw-old",
            "summary": "old thought",
        }
    )

    chat._on_cortex_finish(
        {
            "use_id": "raw-old",
            "result": {
                "message": "checking",
                "notes": "ok",
                "talent_request": {
                    "target": "exec",
                    "task": "research",
                    "context": "{}",
                },
            },
        }
    )

    with chat._state_lock:
        raw_new = str(chat._current_chat_state["raw_use_id"])
    assert "raw-old" not in chat._thinking_buffers
    chat._handle_callosum_message(
        {
            "tract": "cortex",
            "event": "thinking",
            "use_id": raw_new,
            "summary": "synthetic thought",
        }
    )
    assert chat._thinking_buffers[raw_new] == ["synthetic thought"]


def test_late_thinking_arrival_drops_without_mutating_events(
    chat_client, monkeypatch, caplog
):
    import solstone.convey.chat as chat

    monkeypatch.setattr(
        "solstone.convey.chat._emit_cortex_event", lambda *_args, **_kwargs: None
    )
    events_before = list(read_chat_events(date.today().strftime("%Y%m%d")))

    with caplog.at_level(logging.DEBUG, logger="solstone.convey.chat"):
        chat._handle_callosum_message(
            {
                "tract": "cortex",
                "event": "thinking",
                "use_id": "late-raw",
                "summary": "too late",
            }
        )

    assert "dropping late thinking event use_id=late-raw" in caplog.text
    assert read_chat_events(date.today().strftime("%Y%m%d")) == events_before
    assert chat._thinking_buffers == {}


def test_talent_error_evicts_but_does_not_attach_thinking(chat_client, monkeypatch):
    import solstone.convey.chat as chat

    monkeypatch.setattr(
        "solstone.convey.chat._emit_cortex_event", lambda *_args, **_kwargs: None
    )
    monkeypatch.setattr("solstone.convey.chat._run_next_action", lambda _action: None)
    _set_current_chat(chat, "logical-chat", None)
    with chat._state_lock:
        chat._active_talents["talent-raw"] = {
            "chat_use_id": "logical-chat",
            "target": "exec",
            "task": "research",
            "location": {"app": "sol", "path": "/app/sol", "facet": "work"},
        }
    chat._handle_callosum_message(
        {
            "tract": "cortex",
            "event": "thinking",
            "use_id": "talent-raw",
            "summary": "do not attach",
        }
    )

    chat._on_cortex_error({"use_id": "talent-raw", "error": "boom"})

    errored = next(
        event
        for event in read_chat_events(date.today().strftime("%Y%m%d"))
        if event["kind"] == "talent_errored"
    )
    assert "thinking" not in errored
    assert "talent-raw" not in chat._thinking_buffers


def test_chat_watchdog_timeout_evicts_thinking_buffers(chat_client, monkeypatch):
    import solstone.convey.chat as chat

    monkeypatch.setattr(
        "solstone.convey.chat._emit_cortex_event", lambda *_args, **_kwargs: None
    )
    monkeypatch.setattr("solstone.convey.chat._run_next_action", lambda _action: None)
    _set_current_chat(chat, "logical-chat", "raw-timeout")

    chat._handle_callosum_message(
        {
            "tract": "cortex",
            "event": "start",
            "use_id": "raw-timeout",
            "provider": "openai",
        }
    )
    chat._handle_callosum_message(
        {
            "tract": "cortex",
            "event": "thinking",
            "use_id": "raw-timeout",
            "summary": "thinking before timeout",
        }
    )

    assert chat._thinking_buffers["raw-timeout"] == ["thinking before timeout"]
    assert chat._thinking_providers["raw-timeout"] == "openai"

    chat._on_watchdog_timeout("raw-timeout", "chat", "logical-chat")

    assert "raw-timeout" not in chat._thinking_buffers
    assert "raw-timeout" not in chat._thinking_providers


def test_post_chat_appends_owner_message_and_returns_reserved_use_id(
    chat_client, monkeypatch
):
    starts: list[dict] = []
    monkeypatch.setattr(
        "solstone.think.identity.ensure_identity_directory", lambda: None
    )
    monkeypatch.setattr(
        "solstone.convey.chat._spawn_chat_generate",
        lambda action: starts.append(action) or ChatSpawnResult(ok=True),
    )

    response = chat_client.post(
        "/api/chat",
        json={
            "message": "hello there",
            "app": "sol",
            "path": "/app/sol",
            "facet": "work",
        },
    )

    assert response.status_code == 200
    payload = response.get_json()
    assert payload["queued"] is False
    assert payload["use_id"].isdigit()
    assert starts and starts[-1]["logical_use_id"] == payload["use_id"]


def test_post_chat_dispatches_queued_messages_fifo(chat_client, monkeypatch):
    import solstone.convey.chat as chat

    starts: list[dict] = []
    monkeypatch.setattr(
        "solstone.think.identity.ensure_identity_directory", lambda: None
    )
    monkeypatch.setattr(
        "solstone.convey.chat._spawn_chat_generate",
        lambda action: starts.append(action) or ChatSpawnResult(ok=True),
    )
    monkeypatch.setattr(
        "solstone.convey.chat._emit_cortex_event", lambda *_args, **_kwargs: None
    )

    responses = [_post_chat_message(chat_client, f"msg {idx}") for idx in range(5)]

    assert [response.status_code for response in responses] == [200] * 5
    assert [response.get_json()["queued"] for response in responses] == [
        False,
        True,
        True,
        True,
        True,
    ]
    assert len(starts) == 1

    index = 0
    while index < len(starts):
        action = starts[index]
        message = action["trigger"]["message"]
        chat._on_cortex_finish(
            {
                "use_id": action["raw_use_id"],
                "result": json.dumps(
                    {
                        "message": f"reply {message}",
                        "notes": "ok",
                        "talent_request": None,
                    }
                ),
            }
        )
        index += 1

    assert [action["trigger"]["message"] for action in starts] == [
        "msg 0",
        "msg 1",
        "msg 2",
        "msg 3",
        "msg 4",
    ]
    events = read_chat_events(date.today().strftime("%Y%m%d"))
    replies = [event["text"] for event in events if event["kind"] == "sol_message"]
    assert replies == [
        "reply msg 0",
        "reply msg 1",
        "reply msg 2",
        "reply msg 3",
        "reply msg 4",
    ]


def test_post_chat_rejects_when_queue_depth_cap_reached(chat_client, monkeypatch):
    import solstone.convey.chat as chat

    monkeypatch.setattr(
        "solstone.think.identity.ensure_identity_directory", lambda: None
    )
    with chat._state_lock:
        chat._current_chat_use_id = "current"
        chat._current_chat_state = {
            "raw_use_id": "raw-current",
            "raw_use_ids_seen": {"raw-current"},
            "trigger": {"type": "owner_message", "message": "busy"},
            "location": {"app": "sol", "path": "/app/sol", "facet": "work"},
            "retry_count": 0,
        }
        for index in range(10):
            chat._queued_triggers.append(
                {
                    "use_id": str(index + 1),
                    "trigger": {"type": "owner_message", "message": f"queued {index}"},
                    "location": {"app": "sol", "path": "/app/sol", "facet": "work"},
                }
            )

    response = _post_chat_message(chat_client, "one too many")

    assert response.status_code == 429
    assert response.get_json() == {
        "error": "Chat queue full",
        "reason_code": "chat_queue_full",
        "detail": "",
    }
    events = read_chat_events(date.today().strftime("%Y%m%d"))
    assert [event for event in events if event["kind"] == "owner_message"] == []
    assert [event for event in events if event["kind"] == "chat_queue_depth"] == []


def test_chat_error_starts_next_queued_message(chat_client, monkeypatch):
    import solstone.convey.chat as chat

    starts: list[dict] = []
    monkeypatch.setattr(
        "solstone.think.identity.ensure_identity_directory", lambda: None
    )
    monkeypatch.setattr(
        "solstone.convey.chat._spawn_chat_generate",
        lambda action: starts.append(action) or ChatSpawnResult(ok=True),
    )
    monkeypatch.setattr(
        "solstone.convey.chat._emit_cortex_event", lambda *_args, **_kwargs: None
    )

    assert _post_chat_message(chat_client, "first").status_code == 200
    assert _post_chat_message(chat_client, "second").status_code == 200
    assert len(starts) == 1

    chat._handle_chat_failure(starts[0]["logical_use_id"], "unknown")

    assert [action["trigger"]["message"] for action in starts] == [
        "first",
        "second",
    ]
    errors = [
        event
        for event in read_chat_events(date.today().strftime("%Y%m%d"))
        if event["kind"] == "chat_error"
    ]
    assert errors[-1]["use_id"] == starts[0]["logical_use_id"]
    assert errors[-1]["reason"] == "unknown"


def test_queue_depth_events_emit_on_enqueue_and_dequeue(chat_client, monkeypatch):
    import solstone.convey.chat as chat

    starts: list[dict] = []
    monkeypatch.setattr(
        "solstone.think.identity.ensure_identity_directory", lambda: None
    )
    monkeypatch.setattr(
        "solstone.convey.chat._spawn_chat_generate",
        lambda action: starts.append(action) or ChatSpawnResult(ok=True),
    )
    monkeypatch.setattr(
        "solstone.convey.chat._emit_cortex_event", lambda *_args, **_kwargs: None
    )

    for message in ("first", "second", "third"):
        assert _post_chat_message(chat_client, message).status_code == 200

    for index in range(2):
        action = starts[index]
        chat._on_cortex_finish(
            {
                "use_id": action["raw_use_id"],
                "result": {
                    "message": f"reply {action['trigger']['message']}",
                    "notes": "ok",
                    "talent_request": None,
                },
            }
        )

    events = read_chat_events(date.today().strftime("%Y%m%d"))
    depths = [event["depth"] for event in events if event["kind"] == "chat_queue_depth"]
    assert depths == [1, 2, 1, 0]


def test_handle_chat_failure_threads_pipeline_unavailable(chat_client, monkeypatch):
    monkeypatch.setattr(
        "solstone.think.identity.ensure_identity_directory", lambda: None
    )

    def fail_spawn(*_args, **_kwargs):
        raise CortexSpawnUnavailable(detail="FileNotFoundError")

    monkeypatch.setattr("solstone.convey.utils.spawn_agent", fail_spawn)

    response = chat_client.post(
        "/api/chat",
        json={
            "message": "hello there",
            "app": "sol",
            "path": "/app/sol",
            "facet": "work",
        },
    )

    assert response.status_code != 200
    errors = [
        event
        for event in read_chat_events(date.today().strftime("%Y%m%d"))
        if event["kind"] == "chat_error"
    ]
    assert errors[-1]["reason"] == "chat_pipeline_unavailable"
    assert errors[-1]["detail"] == "FileNotFoundError"


def test_chat_event_error_persists_and_emits_detail(tmp_path, monkeypatch):
    import solstone.convey.chat as chat

    _setup_journal(tmp_path, monkeypatch)
    _reset_chat_state(chat)
    emitted: list[tuple[str, dict]] = []
    monkeypatch.setattr(
        "solstone.convey.chat._emit_cortex_event",
        lambda event, **fields: emitted.append((event, fields)),
    )

    chat._handle_chat_failure(
        "1713626000000",
        "chat_pipeline_unavailable",
        detail=" FileNotFoundError \n",
    )

    errors = [
        event
        for event in read_chat_events(date.today().strftime("%Y%m%d"))
        if event["kind"] == "chat_error"
    ]
    assert errors[-1]["reason"] == "chat_pipeline_unavailable"
    assert errors[-1]["detail"] == "FileNotFoundError"
    assert emitted == [
        (
            "error",
            {
                "use_id": "1713626000000",
                "error": "chat_pipeline_unavailable",
                "provider": "",
                "detail": "FileNotFoundError",
                "chat_proxy": True,
            },
        )
    ]


def test_session_endpoint_reduces_from_chat_stream(chat_client, monkeypatch):
    day = "20260420"
    monkeypatch.setattr("solstone.convey.chat._today_day", lambda: day)
    started_at = _ms(2026, 4, 20, 12, 1, 0)
    finished_at = _ms(2026, 4, 20, 12, 2, 0)
    append_chat_event(
        "sol_message",
        ts=_ms(2026, 4, 20, 12, 0, 0),
        use_id="1713626000000",
        text="hello",
        notes="ready",
        requested_target=None,
        requested_task=None,
    )
    append_chat_event(
        "talent_spawned",
        ts=started_at,
        use_id="1713626000001",
        name="exec",
        task="research",
        started_at=started_at,
    )
    append_chat_event(
        "talent_finished",
        ts=finished_at,
        use_id="1713626000001",
        name="exec",
        summary="done",
    )

    response = chat_client.get("/api/chat/session")
    assert response.status_code == 200
    payload = response.get_json()
    assert payload["latest_sol_message"]["text"] == "hello"
    assert payload["active_talents"] == []
    assert payload["completed_talents"] == [
        {
            "finished_at": finished_at,
            "name": "exec",
            "summary": "done",
            "task": "research",
            "use_id": "1713626000001",
        }
    ]


def test_chat_session_retries_unresolved_trigger_when_idle(chat_client, monkeypatch):
    day = "20260420"
    monkeypatch.setattr("solstone.convey.chat._today_day", lambda: day)
    append_chat_event(
        "owner_message",
        ts=_ms(2026, 4, 20, 12, 0, 0),
        text="retry me",
        app="sol",
        path="/app/sol",
        facet="work",
    )

    starts: list[dict] = []
    monkeypatch.setattr(
        "solstone.convey.chat._spawn_chat_generate",
        lambda action: starts.append(action) or ChatSpawnResult(ok=True),
    )

    response = chat_client.get("/api/chat/session")

    assert response.status_code == 200
    assert len(starts) == 1
    assert starts[0]["trigger"]["type"] == "owner_message"


def test_chat_session_retries_again_when_spawn_fails_and_trigger_remains_unresolved(
    chat_client, monkeypatch
):
    day = "20260420"
    monkeypatch.setattr("solstone.convey.chat._today_day", lambda: day)
    append_chat_event(
        "owner_message",
        ts=_ms(2026, 4, 20, 12, 0, 0),
        text="retry me again",
        app="sol",
        path="/app/sol",
        facet="work",
    )

    starts: list[dict] = []

    def fake_spawn(action):
        starts.append(action)
        if len(starts) > 1:
            return ChatSpawnResult(ok=True)
        return ChatSpawnResult(ok=False, reason="unknown")

    monkeypatch.setattr("solstone.convey.chat._spawn_chat_generate", fake_spawn)
    monkeypatch.setattr(
        "solstone.convey.chat._emit_error", lambda *_args, **_kwargs: None
    )

    first = chat_client.get("/api/chat/session")
    second = chat_client.get("/api/chat/session")

    assert first.status_code == 200
    assert second.status_code == 200
    assert len(starts) == 2
    assert starts[0]["trigger"]["type"] == "owner_message"
    assert starts[1]["trigger"]["type"] == "owner_message"


def test_talent_log_endpoint_returns_completed_run(chat_client, tmp_path):
    use_id = "1700000000001"
    _write_talent_log(
        tmp_path / "journal",
        "default",
        f"{use_id}.jsonl",
        [
            {
                "event": "request",
                "ts": 1700000000001,
                "use_id": use_id,
                "prompt": "Search for meetings about project updates",
                "name": "default",
                "provider": "openai",
            },
            {
                "event": "start",
                "ts": 1700000000100,
                "use_id": use_id,
                "model": "gpt-4o",
                "provider": "openai",
            },
            {
                "event": "thinking",
                "ts": 1700000000300,
                "use_id": use_id,
                "content": "reasoning",
                "raw": {"provider": "openai"},
            },
            {
                "event": "finish",
                "ts": 1700000000600,
                "use_id": use_id,
                "result": "done",
            },
        ],
    )

    response = chat_client.get(f"/api/chat/talent-log/{use_id}")

    assert response.status_code == 200
    payload = response.get_json()
    assert payload["use_id"] == use_id
    assert payload["status"] == "completed"
    assert payload["task"] == "Search for meetings about project updates"
    assert payload["started_at"] == 1700000000100
    assert payload["finished_at"] == 1700000000600
    assert len(payload["events"]) == 3
    assert payload["events"][1]["event"] == "thinking"
    assert "raw" not in payload["events"][1]


def test_talent_log_endpoint_returns_running_active_run(chat_client, tmp_path):
    use_id = "1700000000002"
    _write_talent_log(
        tmp_path / "journal",
        "default",
        f"{use_id}_active.jsonl",
        [
            {
                "event": "request",
                "ts": 1700000000002,
                "use_id": use_id,
                "task": "Analyze conversation flow",
            },
            {
                "event": "start",
                "ts": 1700000000102,
                "use_id": use_id,
                "model": "gpt-4o-mini",
            },
            {
                "event": "thinking",
                "ts": 1700000000202,
                "use_id": use_id,
                "content": "still working",
            },
        ],
    )

    response = chat_client.get(f"/api/chat/talent-log/{use_id}")

    assert response.status_code == 200
    payload = response.get_json()
    assert payload["status"] == "running"
    assert payload["task"] == "Analyze conversation flow"
    assert payload["finished_at"] is None
    assert payload["events"][-1]["event"] == "thinking"


def test_talent_log_endpoint_prefers_active_log(chat_client, tmp_path):
    use_id = "1700000000003"
    journal = tmp_path / "journal"
    _write_talent_log(
        journal,
        "default",
        f"{use_id}_active.jsonl",
        [
            {
                "event": "request",
                "ts": 1700000000003,
                "use_id": use_id,
                "prompt": "active prompt",
            },
            {
                "event": "thinking",
                "ts": 1700000000103,
                "use_id": use_id,
                "content": "active content",
            },
        ],
    )
    _write_talent_log(
        journal,
        "flow",
        f"{use_id}.jsonl",
        [
            {
                "event": "request",
                "ts": 1700000000003,
                "use_id": use_id,
                "prompt": "completed prompt",
            },
            {
                "event": "finish",
                "ts": 1700000000203,
                "use_id": use_id,
                "result": "completed result",
            },
        ],
    )

    response = chat_client.get(f"/api/chat/talent-log/{use_id}")

    assert response.status_code == 200
    payload = response.get_json()
    assert payload["status"] == "running"
    assert payload["task"] == "active prompt"
    assert payload["events"][0]["content"] == "active content"


def test_talent_log_endpoint_returns_errored_run(chat_client, tmp_path):
    use_id = "1700000000004"
    _write_talent_log(
        tmp_path / "journal",
        "flow",
        f"{use_id}.jsonl",
        [
            {
                "event": "request",
                "ts": 1700000000004,
                "use_id": use_id,
                "prompt": "Analyze flow",
            },
            {
                "event": "error",
                "ts": 1700000000204,
                "use_id": use_id,
                "error": "Rate limit exceeded",
            },
        ],
    )

    response = chat_client.get(f"/api/chat/talent-log/{use_id}")

    assert response.status_code == 200
    payload = response.get_json()
    assert payload["status"] == "errored"
    assert payload["finished_at"] == 1700000000204
    assert payload["events"][-1]["event"] == "error"


def test_talent_log_endpoint_returns_missing(chat_client):
    use_id = "1700000000999"

    response = chat_client.get(f"/api/chat/talent-log/{use_id}")

    assert response.status_code == 404
    payload = response.get_json()
    assert payload["error"] == "I couldn't find that talent run."
    assert payload["reason_code"] == "talent_not_found"
    assert payload["detail"] == f"Talent log not found for use_id {use_id}"


def test_talent_log_endpoint_task_falls_back_to_prompt(chat_client, tmp_path):
    use_id = "1700000000005"
    _write_talent_log(
        tmp_path / "journal",
        "default",
        f"{use_id}.jsonl",
        [
            {
                "event": "request",
                "ts": 1700000000005,
                "use_id": use_id,
                "prompt": "Fallback prompt",
            },
            {
                "event": "finish",
                "ts": 1700000000305,
                "use_id": use_id,
                "result": "done",
            },
        ],
    )

    response = chat_client.get(f"/api/chat/talent-log/{use_id}")

    assert response.status_code == 200
    assert response.get_json()["task"] == "Fallback prompt"
