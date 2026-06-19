# SPDX-License-Identifier: AGPL-3.0-only
# Copyright (c) 2026 sol pbc

from __future__ import annotations

import json
from datetime import datetime
from pathlib import Path

import pytest

from solstone.convey.chat_stream import append_chat_event
from solstone.convey.sol_initiated.copy import (
    KIND_OWNER_CHAT_DISMISSED,
    KIND_OWNER_CHAT_OPEN,
    KIND_SOL_CHAT_REQUEST,
)
from solstone.think.push import triggers

_FOLD_TS_MS = int(datetime(2026, 3, 31, 12, 0, 0).timestamp() * 1000)


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


def _fold_message(
    *,
    use_id: str = "fold-synth-1",
    route_id: str = "dispatch-1",
    ask: str = "what happened?",
    ts: int = _FOLD_TS_MS,
) -> dict[str, object]:
    return {
        "tract": "chat",
        "event": "sol_message",
        "use_id": use_id,
        "origin": {"logical_use_id": route_id, "ask": ask},
        "requested_target": None,
        "ts": ts,
    }


def _fail_dispatch_via_portal(**kwargs):
    raise AssertionError("dispatch_via_portal should not be called")


def _fail_dedup_dispatch(**kwargs):
    raise AssertionError("dispatch_dedup_via_portal should not be called")


def _install_fold_success(monkeypatch, calls: list[dict[str, str]]) -> None:
    monkeypatch.setattr(triggers, "load_devices", lambda: [_device_row()])
    monkeypatch.setattr(triggers, "dispatch_via_portal", _fail_dispatch_via_portal)
    monkeypatch.setattr(
        triggers,
        "dispatch_dedup_via_portal",
        lambda **kwargs: calls.append(kwargs) or {"ok": True},
    )


def _install_chat_seed(monkeypatch) -> None:
    monkeypatch.setattr(
        "solstone.think.indexer.journal.index_file",
        lambda *_args: True,
    )


def test_handle_sol_chat_request_routes_via_portal(monkeypatch, tmp_path):
    monkeypatch.setenv("SOLSTONE_JOURNAL", str(tmp_path))
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


def test_handle_sol_chat_request_dispatch_none_logs_portal_unavailable(
    monkeypatch, tmp_path
):
    monkeypatch.setenv("SOLSTONE_JOURNAL", str(tmp_path))
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


def test_handle_sol_chat_request_no_devices_skips_without_dispatch(
    monkeypatch, tmp_path
):
    monkeypatch.setenv("SOLSTONE_JOURNAL", str(tmp_path))
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


def test_handle_chat_lifecycle_dispatch_none_logs_portal_unavailable(
    monkeypatch, tmp_path
):
    monkeypatch.setenv("SOLSTONE_JOURNAL", str(tmp_path))
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


def test_handle_chat_lifecycle_portal_unavailable_logs_skip(monkeypatch, tmp_path):
    monkeypatch.setenv("SOLSTONE_JOURNAL", str(tmp_path))
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


def test_handle_chat_fold_routes_content_free_via_dedup(monkeypatch, tmp_path):
    monkeypatch.setenv("SOLSTONE_JOURNAL", str(tmp_path))
    calls: list[dict[str, str]] = []
    _install_fold_success(monkeypatch, calls)

    triggers.handle_chat_fold(
        _fold_message(use_id="fold-synth-1", route_id="dispatch-1")
    )

    assert calls == [{"request_id": "dispatch-1", "action": triggers.FOLD_PUSH_ACTION}]
    assert _read_log(tmp_path) == [
        {
            "ts": _read_log(tmp_path)[0]["ts"],
            "kind": "chat_fold_push",
            "dedupe_key": "dispatch-1",
            "category": triggers.FOLD_PUSH_ACTION,
            "outcome": "dispatched",
            "via": "portal",
        }
    ]


def test_handle_chat_fold_recovery_shape_stays_content_free(monkeypatch, tmp_path):
    monkeypatch.setenv("SOLSTONE_JOURNAL", str(tmp_path))
    calls: list[dict[str, str]] = []
    _install_fold_success(monkeypatch, calls)
    message = _fold_message(
        use_id="recovered-fold-synth",
        route_id="dispatch-1",
        ask="private owner question",
    )
    message.update(
        {
            "text": "private answer",
            "sources": [{"kind": "journal", "title": "private source"}],
            "notes": "private notes",
        }
    )

    triggers.handle_chat_fold(message)

    assert calls == [{"request_id": "dispatch-1", "action": triggers.FOLD_PUSH_ACTION}]
    row = _read_log(tmp_path)[0]
    assert set(row) == {"ts", "kind", "dedupe_key", "category", "outcome", "via"}
    assert row == {
        "ts": row["ts"],
        "kind": "chat_fold_push",
        "dedupe_key": "dispatch-1",
        "category": triggers.FOLD_PUSH_ACTION,
        "outcome": "dispatched",
        "via": "portal",
    }


@pytest.mark.parametrize(
    "message",
    [
        {
            "tract": "chat",
            "event": "sol_message",
            "use_id": "ack-1",
            "requested_target": "exec",
            "ts": _FOLD_TS_MS,
        },
        {
            "tract": "chat",
            "event": "sol_message",
            "use_id": "direct-1",
            "requested_target": None,
            "ts": _FOLD_TS_MS,
        },
        {
            "tract": "chat",
            "event": "other",
            "use_id": "fold-synth-1",
            "origin": {"logical_use_id": "dispatch-1", "ask": "what happened?"},
            "requested_target": None,
            "ts": _FOLD_TS_MS,
        },
        {
            "tract": "chat",
            "event": "sol_message",
            "use_id": "fold-synth-1",
            "origin": {},
            "requested_target": None,
            "ts": _FOLD_TS_MS,
        },
        {
            "tract": "chat",
            "event": "sol_message",
            "use_id": "fold-synth-1",
            "origin": {"logical_use_id": " ", "ask": "what happened?"},
            "requested_target": None,
            "ts": _FOLD_TS_MS,
        },
    ],
)
def test_handle_chat_fold_noop_shapes_do_not_dispatch(monkeypatch, tmp_path, message):
    monkeypatch.setenv("SOLSTONE_JOURNAL", str(tmp_path))
    monkeypatch.setattr(triggers, "load_devices", lambda: [_device_row()])
    monkeypatch.setattr(triggers, "dispatch_via_portal", _fail_dispatch_via_portal)
    monkeypatch.setattr(triggers, "dispatch_dedup_via_portal", _fail_dedup_dispatch)

    triggers.handle_chat_fold(message)

    assert _read_log(tmp_path) == []


def test_handle_chat_fold_suppresses_when_owner_viewing(monkeypatch, tmp_path):
    monkeypatch.setenv("SOLSTONE_JOURNAL", str(tmp_path))
    _install_chat_seed(monkeypatch)
    append_chat_event(
        KIND_OWNER_CHAT_OPEN,
        request_id="visible-request",
        surface="convey",
        ts=_FOLD_TS_MS - 5 * 60 * 1000,
    )
    monkeypatch.setattr(triggers, "load_devices", lambda: [_device_row()])
    monkeypatch.setattr(triggers, "dispatch_via_portal", _fail_dispatch_via_portal)
    monkeypatch.setattr(triggers, "dispatch_dedup_via_portal", _fail_dedup_dispatch)

    triggers.handle_chat_fold(_fold_message(route_id="dispatch-1"))

    assert _read_log(tmp_path) == [
        {
            "ts": _read_log(tmp_path)[0]["ts"],
            "kind": "chat_fold_push",
            "dedupe_key": "dispatch-1",
            "category": triggers.FOLD_PUSH_ACTION,
            "outcome": "skipped",
            "reason": "owner_viewing_chat",
        }
    ]


def test_handle_chat_fold_dispatches_after_stale_open(monkeypatch, tmp_path):
    monkeypatch.setenv("SOLSTONE_JOURNAL", str(tmp_path))
    _install_chat_seed(monkeypatch)
    append_chat_event(
        KIND_OWNER_CHAT_OPEN,
        request_id="stale-request",
        surface="convey",
        ts=_FOLD_TS_MS - 30 * 60 * 1000,
    )
    calls: list[dict[str, str]] = []
    _install_fold_success(monkeypatch, calls)

    triggers.handle_chat_fold(_fold_message(route_id="dispatch-1"))

    assert calls == [{"request_id": "dispatch-1", "action": triggers.FOLD_PUSH_ACTION}]


def test_handle_chat_fold_dispatches_after_dismissed_open(monkeypatch, tmp_path):
    monkeypatch.setenv("SOLSTONE_JOURNAL", str(tmp_path))
    _install_chat_seed(monkeypatch)
    append_chat_event(
        KIND_OWNER_CHAT_OPEN,
        request_id="dismissed-request",
        surface="convey",
        ts=_FOLD_TS_MS - 5 * 60 * 1000,
    )
    append_chat_event(
        KIND_OWNER_CHAT_DISMISSED,
        request_id="dismissed-request",
        surface="convey",
        reason=None,
        ts=_FOLD_TS_MS - 60 * 1000,
    )
    calls: list[dict[str, str]] = []
    _install_fold_success(monkeypatch, calls)

    triggers.handle_chat_fold(_fold_message(route_id="dispatch-1"))

    assert calls == [{"request_id": "dispatch-1", "action": triggers.FOLD_PUSH_ACTION}]


def test_handle_chat_fold_uses_origin_logical_id_for_dedup(monkeypatch, tmp_path):
    monkeypatch.setenv("SOLSTONE_JOURNAL", str(tmp_path))
    calls: list[dict[str, str]] = []
    _install_fold_success(monkeypatch, calls)

    triggers.handle_chat_fold(
        _fold_message(use_id="fold-synth-1", route_id="dispatch-1")
    )
    triggers.handle_chat_fold(
        _fold_message(use_id="fold-synth-2", route_id="dispatch-1")
    )

    assert calls == [
        {"request_id": "dispatch-1", "action": triggers.FOLD_PUSH_ACTION},
        {"request_id": "dispatch-1", "action": triggers.FOLD_PUSH_ACTION},
    ]


def test_handle_chat_fold_dispatch_none_logs_portal_unavailable(monkeypatch, tmp_path):
    monkeypatch.setenv("SOLSTONE_JOURNAL", str(tmp_path))
    monkeypatch.setattr(triggers, "load_devices", lambda: [_device_row()])
    monkeypatch.setattr(triggers, "dispatch_via_portal", _fail_dispatch_via_portal)
    monkeypatch.setattr(triggers, "dispatch_dedup_via_portal", lambda **kwargs: None)

    triggers.handle_chat_fold(_fold_message(route_id="dispatch-1"))

    assert _read_log(tmp_path) == [
        {
            "ts": _read_log(tmp_path)[0]["ts"],
            "kind": "chat_fold_push",
            "dedupe_key": "dispatch-1",
            "category": triggers.FOLD_PUSH_ACTION,
            "outcome": "skipped",
            "reason": "portal_unavailable",
        }
    ]


def test_handle_chat_fold_no_devices_skips_without_dispatch(monkeypatch, tmp_path):
    monkeypatch.setenv("SOLSTONE_JOURNAL", str(tmp_path))
    monkeypatch.setattr(triggers, "load_devices", lambda: [])
    monkeypatch.setattr(triggers, "dispatch_via_portal", _fail_dispatch_via_portal)
    monkeypatch.setattr(triggers, "dispatch_dedup_via_portal", _fail_dedup_dispatch)

    triggers.handle_chat_fold(_fold_message(route_id="dispatch-1"))

    assert _read_log(tmp_path) == [
        {
            "ts": _read_log(tmp_path)[0]["ts"],
            "kind": "chat_fold_push",
            "dedupe_key": "dispatch-1",
            "category": triggers.FOLD_PUSH_ACTION,
            "outcome": "skipped",
            "reason": "no_devices",
        }
    ]


def test_handle_chat_fold_portal_unavailable_logs_skip(monkeypatch, tmp_path):
    monkeypatch.setenv("SOLSTONE_JOURNAL", str(tmp_path))
    monkeypatch.setattr(triggers, "load_devices", lambda: [_device_row()])
    monkeypatch.setattr(triggers, "dispatch_via_portal", _fail_dispatch_via_portal)
    monkeypatch.setattr(triggers, "dispatch_dedup_via_portal", lambda **kwargs: None)

    triggers.handle_chat_fold(_fold_message(route_id="dispatch-1"))

    assert _read_log(tmp_path) == [
        {
            "ts": _read_log(tmp_path)[0]["ts"],
            "kind": "chat_fold_push",
            "dedupe_key": "dispatch-1",
            "category": triggers.FOLD_PUSH_ACTION,
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
    monkeypatch.setattr(triggers, "dispatch_via_portal", _fail_dispatch_via_portal)
    monkeypatch.setattr(triggers, "dispatch_dedup_via_portal", _fail_dedup_dispatch)

    triggers.handle_sol_chat_request(message)
    triggers.handle_chat_lifecycle(message)
    triggers.handle_chat_fold(message)

    assert _read_log(tmp_path) == []


def test_removed_handlers_are_gone():
    assert not hasattr(triggers, "handle_briefing_finish")
    assert not hasattr(triggers, "_eligible_devices")
