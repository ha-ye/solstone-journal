# SPDX-License-Identifier: AGPL-3.0-only
# Copyright (c) 2026 sol pbc

from datetime import datetime

from flask import Flask

from solstone.convey.chat_stream import append_chat_event, read_chat_events
from solstone.convey.sol_initiated.copy import (
    CATEGORIES,
    KIND_OWNER_CHAT_DISMISSED,
    KIND_OWNER_CHAT_OPEN,
    KIND_SOL_CHAT_REQUEST,
    SURFACE_CONVEY,
)


def _setup_journal(tmp_path, monkeypatch):
    journal = tmp_path / "journal"
    journal.mkdir()
    monkeypatch.setenv("SOLSTONE_JOURNAL", str(journal))
    monkeypatch.setattr("solstone.convey.chat_stream.index_file", lambda *_args: True)
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


def _append_request() -> None:
    append_chat_event(
        KIND_SOL_CHAT_REQUEST,
        request_id="req",
        summary="Notice this",
        message=None,
        category=CATEGORIES[0],
        dedupe="k",
        dedupe_window="24h",
        since_ts=1,
        trigger_talent="reflection",
    )


def test_recover_wakes_chat_from_sol_request(tmp_path, monkeypatch) -> None:
    import solstone.convey.chat as chat

    _setup_journal(tmp_path, monkeypatch)
    _reset_chat_state(chat)
    day = datetime.now().strftime("%Y%m%d")
    monkeypatch.setattr(chat, "_today_day", lambda: day)
    _append_request()
    starts: list[dict] = []
    monkeypatch.setattr(
        chat,
        "_spawn_chat_generate",
        lambda action: starts.append(action) or chat.ChatSpawnResult(ok=True),
    )

    chat._recover_chat_if_needed()

    assert len(starts) == 1
    assert starts[0]["trigger"]["type"] == KIND_SOL_CHAT_REQUEST
    with chat._state_lock:
        assert chat._current_chat_use_id is not None


def test_owner_message_queues_while_sol_request_generates(
    tmp_path, monkeypatch
) -> None:
    import solstone.convey.chat as chat

    _setup_journal(tmp_path, monkeypatch)
    _reset_chat_state(chat)
    day = datetime.now().strftime("%Y%m%d")
    monkeypatch.setattr(chat, "_today_day", lambda: day)
    monkeypatch.setattr(
        "solstone.think.identity.ensure_identity_directory", lambda: None
    )
    _append_request()
    starts: list[dict] = []
    monkeypatch.setattr(
        chat,
        "_spawn_chat_generate",
        lambda action: starts.append(action) or chat.ChatSpawnResult(ok=True),
    )
    chat._recover_chat_if_needed()

    app = Flask(__name__)
    app.config["TESTING"] = True
    app.register_blueprint(chat.chat_bp)
    response = app.test_client().post(
        "/api/chat",
        json={
            "message": "hello",
            "app": "chat",
            "path": "/app/chat",
            "facet": "work",
        },
    )

    assert response.status_code == 200
    assert response.get_json()["queued"] is True
    assert len(starts) == 1


def test_sol_request_open_endpoint_records_open(tmp_path, monkeypatch) -> None:
    import solstone.convey.chat as chat

    _setup_journal(tmp_path, monkeypatch)
    _reset_chat_state(chat)
    app = Flask(__name__)
    app.config["TESTING"] = True
    app.register_blueprint(chat.chat_bp)

    response = app.test_client().post(
        f"/api/chat/{KIND_SOL_CHAT_REQUEST}/open",
        json={"request_id": "req"},
    )

    assert response.status_code == 200
    assert response.get_json() == {"ok": True}
    events = read_chat_events(datetime.now().strftime("%Y%m%d"))
    assert events[-1]["kind"] == KIND_OWNER_CHAT_OPEN
    assert events[-1]["request_id"] == "req"
    assert events[-1]["surface"] == SURFACE_CONVEY


def test_sol_request_open_endpoint_broadcast_suppresses_push(
    tmp_path,
    monkeypatch,
) -> None:
    import solstone.convey.chat as chat
    from solstone.think.push import triggers

    _setup_journal(tmp_path, monkeypatch)
    _reset_chat_state(chat)
    emitted = []

    class FakeCallosum:
        def emit(self, tract, event, **kwargs):
            emitted.append((tract, event, kwargs))
            return True

    class FakeRuntime:
        callosum = FakeCallosum()

    monkeypatch.setattr(chat, "_runtime", FakeRuntime(), raising=False)
    app = Flask(__name__)
    app.config["TESTING"] = True
    app.register_blueprint(chat.chat_bp)

    response = app.test_client().post(
        f"/api/chat/{KIND_SOL_CHAT_REQUEST}/open",
        json={"request_id": "req-1"},
    )

    assert response.status_code == 200
    assert len(emitted) == 1
    tract, kind, kwargs = emitted[0]
    assert (tract, kind) == ("chat", KIND_OWNER_CHAT_OPEN)
    assert kwargs["request_id"] == "req-1"

    monkeypatch.setattr(triggers, "is_configured", lambda: True)
    devices = [{"token": "a" * 64}]
    monkeypatch.setattr(triggers, "_eligible_devices", lambda: devices)
    monkeypatch.setattr(triggers.time, "time", lambda: 123.0)
    sent_calls = []

    def fake_send_many(push_devices, payload, *, collapse_id, priority, push_type):
        sent_calls.append(
            {
                "devices": push_devices,
                "payload": payload,
                "collapse_id": collapse_id,
                "priority": priority,
                "push_type": push_type,
            }
        )
        return 1, 0

    monkeypatch.setattr(triggers, "send_many", fake_send_many)
    message = {"tract": tract, "event": kind, **kwargs}

    triggers.handle_chat_lifecycle(message)

    assert sent_calls == [
        {
            "devices": devices,
            "payload": {
                "aps": {"mutable-content": 1, "content-available": 1},
                "data": {"action": KIND_OWNER_CHAT_OPEN, "request_id": "req-1"},
            },
            "collapse_id": f"sol_chat_lifecycle:req-1:{KIND_OWNER_CHAT_OPEN}",
            "priority": 5,
            "push_type": "background",
        }
    ]


def test_sol_request_open_endpoint_requires_request_id(tmp_path, monkeypatch) -> None:
    import solstone.convey.chat as chat

    _setup_journal(tmp_path, monkeypatch)
    _reset_chat_state(chat)
    app = Flask(__name__)
    app.config["TESTING"] = True
    app.register_blueprint(chat.chat_bp)

    response = app.test_client().post(
        f"/api/chat/{KIND_SOL_CHAT_REQUEST}/open",
        json={"request_id": " "},
    )

    assert response.status_code == 400
    assert read_chat_events(datetime.now().strftime("%Y%m%d")) == []


def test_sol_request_dismissed_endpoint_records_dismissal_without_reason(
    tmp_path, monkeypatch
) -> None:
    import solstone.convey.chat as chat

    _setup_journal(tmp_path, monkeypatch)
    _reset_chat_state(chat)
    app = Flask(__name__)
    app.config["TESTING"] = True
    app.register_blueprint(chat.chat_bp)

    response = app.test_client().post(
        f"/api/chat/{KIND_SOL_CHAT_REQUEST}/dismissed",
        json={"request_id": "req"},
    )

    assert response.status_code == 200
    assert response.get_json() == {"ok": True}
    events = read_chat_events(datetime.now().strftime("%Y%m%d"))
    assert events[-1]["kind"] == KIND_OWNER_CHAT_DISMISSED
    assert events[-1]["request_id"] == "req"
    assert events[-1]["surface"] == SURFACE_CONVEY
    assert events[-1]["reason"] is None


def test_sol_request_dismissed_endpoint_records_reason(tmp_path, monkeypatch) -> None:
    import solstone.convey.chat as chat

    _setup_journal(tmp_path, monkeypatch)
    _reset_chat_state(chat)
    app = Flask(__name__)
    app.config["TESTING"] = True
    app.register_blueprint(chat.chat_bp)

    response = app.test_client().post(
        f"/api/chat/{KIND_SOL_CHAT_REQUEST}/dismissed",
        json={"request_id": "req", "reason": "not now"},
    )

    assert response.status_code == 200
    events = read_chat_events(datetime.now().strftime("%Y%m%d"))
    assert events[-1]["kind"] == KIND_OWNER_CHAT_DISMISSED
    assert events[-1]["request_id"] == "req"
    assert events[-1]["surface"] == SURFACE_CONVEY
    assert events[-1]["reason"] == "not now"


def test_sol_request_dismissed_endpoint_requires_request_id(
    tmp_path, monkeypatch
) -> None:
    import solstone.convey.chat as chat

    _setup_journal(tmp_path, monkeypatch)
    _reset_chat_state(chat)
    app = Flask(__name__)
    app.config["TESTING"] = True
    app.register_blueprint(chat.chat_bp)

    response = app.test_client().post(
        f"/api/chat/{KIND_SOL_CHAT_REQUEST}/dismissed",
        json={"request_id": ""},
    )

    assert response.status_code == 400
    assert read_chat_events(datetime.now().strftime("%Y%m%d")) == []
