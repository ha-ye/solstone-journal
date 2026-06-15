# SPDX-License-Identifier: AGPL-3.0-only
# Copyright (c) 2026 sol pbc

from __future__ import annotations

import json
from pathlib import Path

import pytest

from solstone.convey.sol_initiated.copy import (
    KIND_OWNER_CHAT_DISMISSED,
    KIND_OWNER_CHAT_OPEN,
    KIND_SOL_CHAT_REQUEST,
)
from solstone.think.push import triggers


def _log_path(tmp_path: Path) -> Path:
    return tmp_path / "push" / "nudge_log.jsonl"


def _read_log(tmp_path: Path) -> list[dict[str, object]]:
    path = _log_path(tmp_path)
    if not path.exists():
        return []
    return [json.loads(line) for line in path.read_text(encoding="utf-8").splitlines()]


def _device_row() -> dict[str, object]:
    return {
        "fingerprint": "fp-1",
        "token": "a" * 64,
        "bundle_id": "org.solpbc.solstone-swift",
        "environment": "development",
        "platform": "ios",
        "registered_at": 1,
    }


def test_handle_sol_chat_request_routes_via_portal(monkeypatch, tmp_path):
    monkeypatch.setenv("SOLSTONE_JOURNAL", str(tmp_path))
    monkeypatch.setattr(triggers, "push_relay_token", lambda: "tok")
    monkeypatch.setattr(triggers, "load_devices", lambda: [_device_row()])
    calls: list[dict[str, str]] = []
    monkeypatch.setattr(
        triggers,
        "dispatch_via_portal",
        lambda **kwargs: calls.append(kwargs) or {"ok": True},
    )

    triggers.handle_sol_chat_request(
        {
            "tract": "chat",
            "event": KIND_SOL_CHAT_REQUEST,
            "request_id": "req-1",
            "summary": "hello",
            "category": "notice",
        }
    )

    assert calls == [{"request_id": "req-1", "summary": "hello", "category": "notice"}]
    assert _read_log(tmp_path) == [
        {
            "ts": _read_log(tmp_path)[0]["ts"],
            "kind": f"{KIND_SOL_CHAT_REQUEST}_push",
            "dedupe_key": "req-1",
            "category": "notice",
            "outcome": "dispatched",
            "via": "portal",
        }
    ]


def test_handle_sol_chat_request_no_token_skips_without_dispatch(monkeypatch, tmp_path):
    monkeypatch.setenv("SOLSTONE_JOURNAL", str(tmp_path))
    monkeypatch.setattr(triggers, "push_relay_token", lambda: "")

    def fail_dispatch(**kwargs):
        raise AssertionError("dispatch should not be called")

    monkeypatch.setattr(triggers, "dispatch_via_portal", fail_dispatch)

    triggers.handle_sol_chat_request(
        {
            "tract": "chat",
            "event": KIND_SOL_CHAT_REQUEST,
            "request_id": "req-1",
            "summary": "hello",
            "category": "notice",
        }
    )

    assert _read_log(tmp_path) == [
        {
            "ts": _read_log(tmp_path)[0]["ts"],
            "kind": f"{KIND_SOL_CHAT_REQUEST}_push",
            "dedupe_key": "req-1",
            "category": "notice",
            "outcome": "skipped",
            "reason": "no_relay_token",
        }
    ]


def test_handle_sol_chat_request_no_devices_skips_without_dispatch(
    monkeypatch, tmp_path
):
    monkeypatch.setenv("SOLSTONE_JOURNAL", str(tmp_path))
    monkeypatch.setattr(triggers, "push_relay_token", lambda: "tok")
    monkeypatch.setattr(triggers, "load_devices", lambda: [])

    def fail_dispatch(**kwargs):
        raise AssertionError("dispatch should not be called")

    monkeypatch.setattr(triggers, "dispatch_via_portal", fail_dispatch)

    triggers.handle_sol_chat_request(
        {
            "tract": "chat",
            "event": KIND_SOL_CHAT_REQUEST,
            "request_id": "req-1",
            "summary": "hello",
            "category": "notice",
        }
    )

    assert _read_log(tmp_path) == [
        {
            "ts": _read_log(tmp_path)[0]["ts"],
            "kind": f"{KIND_SOL_CHAT_REQUEST}_push",
            "dedupe_key": "req-1",
            "category": "notice",
            "outcome": "skipped",
            "reason": "no_devices",
        }
    ]


def test_handle_sol_chat_request_portal_unavailable_logs_skip(monkeypatch, tmp_path):
    monkeypatch.setenv("SOLSTONE_JOURNAL", str(tmp_path))
    monkeypatch.setattr(triggers, "push_relay_token", lambda: "tok")
    monkeypatch.setattr(triggers, "load_devices", lambda: [_device_row()])
    monkeypatch.setattr(triggers, "dispatch_via_portal", lambda **kwargs: None)

    triggers.handle_sol_chat_request(
        {
            "tract": "chat",
            "event": KIND_SOL_CHAT_REQUEST,
            "request_id": "req-1",
            "summary": "hello",
            "category": "notice",
        }
    )

    assert _read_log(tmp_path) == [
        {
            "ts": _read_log(tmp_path)[0]["ts"],
            "kind": f"{KIND_SOL_CHAT_REQUEST}_push",
            "dedupe_key": "req-1",
            "category": "notice",
            "outcome": "skipped",
            "reason": "portal_unavailable",
        }
    ]


@pytest.mark.parametrize("event", [KIND_OWNER_CHAT_OPEN, KIND_OWNER_CHAT_DISMISSED])
def test_handle_chat_lifecycle_routes_via_portal(monkeypatch, tmp_path, event):
    monkeypatch.setenv("SOLSTONE_JOURNAL", str(tmp_path))
    monkeypatch.setattr(triggers, "push_relay_token", lambda: "tok")
    monkeypatch.setattr(triggers, "load_devices", lambda: [_device_row()])
    calls: list[dict[str, str]] = []
    monkeypatch.setattr(
        triggers,
        "dispatch_dedup_via_portal",
        lambda **kwargs: calls.append(kwargs) or {"ok": True},
    )

    triggers.handle_chat_lifecycle(
        {"tract": "chat", "event": event, "request_id": "req-1"}
    )

    assert calls == [{"request_id": "req-1", "action": event}]
    assert _read_log(tmp_path) == [
        {
            "ts": _read_log(tmp_path)[0]["ts"],
            "kind": "sol_chat_lifecycle_push",
            "dedupe_key": "req-1",
            "category": event,
            "outcome": "dispatched",
            "via": "portal",
        }
    ]


def test_handle_chat_lifecycle_no_token_skips_without_dispatch(monkeypatch, tmp_path):
    monkeypatch.setenv("SOLSTONE_JOURNAL", str(tmp_path))
    monkeypatch.setattr(triggers, "push_relay_token", lambda: "")

    def fail_dispatch(**kwargs):
        raise AssertionError("dispatch should not be called")

    monkeypatch.setattr(triggers, "dispatch_dedup_via_portal", fail_dispatch)

    triggers.handle_chat_lifecycle(
        {"tract": "chat", "event": KIND_OWNER_CHAT_OPEN, "request_id": "req-1"}
    )

    assert _read_log(tmp_path) == [
        {
            "ts": _read_log(tmp_path)[0]["ts"],
            "kind": "sol_chat_lifecycle_push",
            "dedupe_key": "req-1",
            "category": KIND_OWNER_CHAT_OPEN,
            "outcome": "skipped",
            "reason": "no_relay_token",
        }
    ]


def test_handle_chat_lifecycle_portal_unavailable_logs_skip(monkeypatch, tmp_path):
    monkeypatch.setenv("SOLSTONE_JOURNAL", str(tmp_path))
    monkeypatch.setattr(triggers, "push_relay_token", lambda: "tok")
    monkeypatch.setattr(triggers, "load_devices", lambda: [_device_row()])
    monkeypatch.setattr(triggers, "dispatch_dedup_via_portal", lambda **kwargs: None)

    triggers.handle_chat_lifecycle(
        {"tract": "chat", "event": KIND_OWNER_CHAT_OPEN, "request_id": "req-1"}
    )

    assert _read_log(tmp_path) == [
        {
            "ts": _read_log(tmp_path)[0]["ts"],
            "kind": "sol_chat_lifecycle_push",
            "dedupe_key": "req-1",
            "category": KIND_OWNER_CHAT_OPEN,
            "outcome": "skipped",
            "reason": "portal_unavailable",
        }
    ]


@pytest.mark.parametrize(
    "message",
    [
        {"tract": "cortex", "event": KIND_SOL_CHAT_REQUEST, "request_id": "req-1"},
        {"tract": "chat", "event": "other", "request_id": "req-1"},
        {"tract": "chat", "event": KIND_SOL_CHAT_REQUEST, "request_id": ""},
    ],
)
def test_non_chat_or_wrong_event_messages_are_noops(monkeypatch, tmp_path, message):
    monkeypatch.setenv("SOLSTONE_JOURNAL", str(tmp_path))
    monkeypatch.setattr(triggers, "push_relay_token", lambda: "tok")

    triggers.handle_sol_chat_request(message)
    triggers.handle_chat_lifecycle(message)

    assert _read_log(tmp_path) == []


def test_removed_handlers_are_gone():
    assert not hasattr(triggers, "handle_briefing_finish")
    assert not hasattr(triggers, "_eligible_devices")
