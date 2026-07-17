# SPDX-License-Identifier: AGPL-3.0-only
# Copyright (c) 2026 sol pbc

from __future__ import annotations

import json
import time
from types import SimpleNamespace

import pytest

from solstone.convey import create_app


class _FakeEvent:
    def __init__(self, *, name: str, arguments: str, call_id: str) -> None:
        self.type = "response.function_call_arguments.done"
        self.name = name
        self.arguments = arguments
        self.call_id = call_id


class _FakeConversationItem:
    def __init__(self, state) -> None:
        self._state = state

    async def create(self, *, item):
        self._state.outputs.append(item)


class _FakeResponse:
    def __init__(self, state) -> None:
        self._state = state

    async def create(self):
        self._state.response_creates += 1


class _FakeConversation:
    def __init__(self, state) -> None:
        self.item = _FakeConversationItem(state)


class _FakeConn:
    def __init__(self, state) -> None:
        self._state = state
        self.conversation = _FakeConversation(state)
        self.response = _FakeResponse(state)
        events = getattr(
            state,
            "events",
            [
                _FakeEvent(
                    name="journal.get_day",
                    arguments='{"day":"2026-03-04"}',
                    call_id="call-1",
                )
            ],
        )
        self._events = iter(events)

    def __aiter__(self):
        return self

    async def __anext__(self):
        try:
            return next(self._events)
        except StopIteration:
            raise StopAsyncIteration


class _FakeConnManager:
    def __init__(self, state) -> None:
        self._state = state

    async def __aenter__(self):
        return _FakeConn(self._state)

    async def __aexit__(self, exc_type, exc, tb):
        return False


class _FakeClientSecrets:
    def __init__(self, state) -> None:
        self._state = state

    async def create(self, *, session):
        self._state.session_payloads.append(session)
        return SimpleNamespace(value="ek-test")


class _FakeRealtime:
    def __init__(self, state) -> None:
        self._state = state
        self.client_secrets = _FakeClientSecrets(state)

    def connect(self, *, call_id, model):
        self._state.connect_calls.append({"call_id": call_id, "model": model})
        return _FakeConnManager(self._state)


class FakeAsyncOpenAI:
    def __init__(self, *, api_key):
        self.api_key = api_key
        self.realtime = _FakeRealtime(FakeAsyncOpenAI.state)


@pytest.fixture
def integration_client(journal_copy):
    app = create_app(str(journal_copy))
    app.config["TESTING"] = True
    app.voice_brain_instruction = "Ready voice"
    app.voice_brain_session = "session-1"
    app.voice_brain_refreshed_at = time.time()
    return app.test_client(), app


def test_voice_flow_round_trip(integration_client, monkeypatch):
    client, _app = integration_client
    state = SimpleNamespace(
        session_payloads=[],
        connect_calls=[],
        outputs=[],
        response_creates=0,
    )
    FakeAsyncOpenAI.state = state
    futures = []

    def record_voice_task(app, future):
        futures.append(future)
        app.voice_tasks.add(future)
        future.add_done_callback(app.voice_tasks.discard)

    monkeypatch.setattr("solstone.convey.voice.AsyncOpenAI", FakeAsyncOpenAI)
    monkeypatch.setattr("solstone.think.voice.sideband.AsyncOpenAI", FakeAsyncOpenAI)
    monkeypatch.setattr("solstone.convey.voice.get_openai_api_key", lambda: "sk-test")
    monkeypatch.setattr(
        "solstone.think.voice.sideband.get_openai_api_key", lambda: "sk-test"
    )
    monkeypatch.setattr(
        "solstone.convey.voice.brain.wait_until_ready", lambda app, timeout: True
    )
    monkeypatch.setattr("solstone.convey.voice.brain.brain_is_stale", lambda app: False)
    monkeypatch.setattr("solstone.convey.voice.register_voice_task", record_voice_task)

    session_response = client.post("/api/voice/session")
    assert session_response.status_code == 200
    assert session_response.get_json() == {"ephemeral_key": "ek-test"}
    assert state.session_payloads
    assert state.session_payloads[0]["instructions"] == "Ready voice"

    connect_response = client.post("/api/voice/connect", json={"call_id": "call-1"})
    assert connect_response.status_code == 200
    assert connect_response.get_json() == {"status": "connected"}
    assert len(futures) == 1
    futures[0].result(timeout=1)

    assert state.connect_calls == [{"call_id": "call-1", "model": "gpt-realtime"}]
    assert state.outputs
    tool_output = json.loads(state.outputs[0]["output"])
    nav_target_key = "_nav" + "_target"
    assert nav_target_key not in tool_output
    assert state.response_creates == 1
