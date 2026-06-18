# SPDX-License-Identifier: AGPL-3.0-only
# Copyright (c) 2026 sol pbc

import base64
import json
from datetime import datetime
from pathlib import Path
from types import SimpleNamespace

import pytest

from solstone.apps.chat.copy import talent_label_for
from solstone.convey.chat_stream import (
    append_chat_event,
    find_unresponded_trigger,
    read_chat_events,
    read_chat_tail,
    reduce_chat_state,
)


def _setup_journal(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    journal = tmp_path / "journal"
    journal.mkdir()
    monkeypatch.setenv("SOLSTONE_JOURNAL", str(journal))
    return journal


def _ms(year: int, month: int, day: int, hour: int, minute: int, second: int) -> int:
    return int(datetime(year, month, day, hour, minute, second).timestamp() * 1000)


def test_append_owner_message_creates_segment_and_jsonl(tmp_path, monkeypatch):
    journal = _setup_journal(tmp_path, monkeypatch)
    ts = _ms(2026, 4, 20, 12, 0, 0)

    event = append_chat_event(
        "owner_message",
        ts=ts,
        text="hello",
        app="sol",
        path="/chat",
        facet="work",
    )

    segment_dir = journal / "chronicle" / "20260420" / "chat" / "120000_300"
    chat_path = segment_dir / "chat.jsonl"

    assert event["kind"] == "owner_message"
    assert event["ts"] == ts
    assert segment_dir.is_dir()
    assert (segment_dir / "stream.json").is_file()
    assert chat_path.is_file()
    assert (journal / "streams" / "chat.json").is_file()

    entries = [
        json.loads(line) for line in chat_path.read_text(encoding="utf-8").splitlines()
    ]
    assert entries == [event]


def test_append_is_atomic(tmp_path, monkeypatch):
    journal = _setup_journal(tmp_path, monkeypatch)

    append_chat_event(
        "owner_message",
        ts=_ms(2026, 4, 20, 12, 0, 0),
        text="hello",
        app="sol",
        path="/chat",
        facet="work",
    )

    chat_path = (
        journal / "chronicle" / "20260420" / "chat" / "120000_300" / "chat.jsonl"
    )
    lines = chat_path.read_text(encoding="utf-8").splitlines()

    assert len(lines) == 1
    assert json.loads(lines[0])["text"] == "hello"


def test_append_broadcasts_on_chat_tract_with_stored_event_payload(
    tmp_path, monkeypatch
):
    _setup_journal(tmp_path, monkeypatch)
    import solstone.convey.chat as chat

    calls: list[tuple[str, str, dict]] = []

    class FakeCallosum:
        def emit(self, tract, event, **fields):
            calls.append((tract, event, fields))
            return True

    monkeypatch.setattr(chat, "_runtime", SimpleNamespace(callosum=FakeCallosum()))

    event = append_chat_event(
        "sol_message",
        ts=_ms(2026, 4, 20, 12, 0, 0),
        use_id="1713626000000",
        text="hello",
        notes="ready",
        requested_target=None,
        requested_task=None,
    )

    assert calls == [("chat", "sol_message", event)]


def test_append_broadcast_failure_is_swallowed(tmp_path, monkeypatch):
    journal = _setup_journal(tmp_path, monkeypatch)
    import solstone.convey.chat as chat

    class FakeCallosum:
        def emit(self, tract, event, **fields):
            raise RuntimeError("boom")

    monkeypatch.setattr(chat, "_runtime", SimpleNamespace(callosum=FakeCallosum()))

    event = append_chat_event(
        "owner_message",
        ts=_ms(2026, 4, 20, 12, 0, 0),
        text="hello",
        app="sol",
        path="/chat",
        facet="work",
    )

    chat_path = (
        journal / "chronicle" / "20260420" / "chat" / "120000_300" / "chat.jsonl"
    )

    assert event["kind"] == "owner_message"
    lines = chat_path.read_text(encoding="utf-8").splitlines()
    assert len(lines) == 1
    assert json.loads(lines[0]) == event


def test_append_rejects_unknown_kind(tmp_path, monkeypatch):
    _setup_journal(tmp_path, monkeypatch)

    with pytest.raises(ValueError, match="Unknown chat event kind"):
        append_chat_event("unknown", ts=1)


def test_append_rejects_missing_required_fields(tmp_path, monkeypatch):
    _setup_journal(tmp_path, monkeypatch)

    with pytest.raises(ValueError, match="owner_message requires fields: path, facet"):
        append_chat_event(
            "owner_message",
            ts=1,
            text="hello",
            app="sol",
        )


def test_chat_queue_depth_event_validates_depth(tmp_path, monkeypatch):
    _setup_journal(tmp_path, monkeypatch)
    ts = _ms(2026, 4, 20, 12, 0, 0)

    event = append_chat_event("chat_queue_depth", ts=ts, depth=3)

    assert event["kind"] == "chat_queue_depth"
    assert read_chat_events("20260420") == [event]
    with pytest.raises(ValueError, match="chat_queue_depth requires fields: depth"):
        append_chat_event("chat_queue_depth", ts=ts + 1)
    with pytest.raises(ValueError, match="chat_queue_depth depth must be an int"):
        append_chat_event("chat_queue_depth", ts=ts + 2, depth="3")


def test_support_draft_event_round_trips(tmp_path, monkeypatch):
    _setup_journal(tmp_path, monkeypatch)
    ts = _ms(2026, 4, 20, 12, 0, 0)

    event = append_chat_event(
        "support_draft",
        ts=ts,
        draft_id="draft-1",
        captured_day="20260420",
        verb="create",
        payload={"subject": "help"},
        diagnostics_snapshot=None,
    )

    events = read_chat_events("20260420")
    assert events == [event]
    assert events[0]["kind"] == "support_draft"
    assert events[0]["draft_id"] == "draft-1"

    attach_payload = {
        "ticket_id": 42,
        "filename": "evidence.txt",
        "content_type": "text/plain",
        "byte_size": 5,
        "content_b64": base64.b64encode(b"bytes").decode("ascii"),
    }
    attach_event = append_chat_event(
        "support_draft",
        ts=ts + 1,
        draft_id="draft-attach",
        captured_day="20260420",
        verb="attach",
        payload=attach_payload,
        diagnostics_snapshot=None,
    )

    events = read_chat_events("20260420")
    assert events == [event, attach_event]
    assert events[1]["payload"] == attach_payload


def test_chat_error_preserves_optional_provider(tmp_path, monkeypatch):
    _setup_journal(tmp_path, monkeypatch)
    ts = _ms(2026, 4, 20, 12, 0, 0)

    event = append_chat_event(
        "chat_error",
        ts=ts,
        reason="provider_key_invalid",
        use_id="1713626000000",
        provider="google",
    )

    events = read_chat_events("20260420")
    assert events == [event]
    assert events[0]["provider"] == "google"


def test_chat_error_preserves_optional_detail_verbatim(tmp_path, monkeypatch):
    _setup_journal(tmp_path, monkeypatch)
    ts = _ms(2026, 4, 20, 12, 0, 0)
    detail = "provider raw detail: " + ("x" * 400)

    event = append_chat_event(
        "chat_error",
        ts=ts,
        reason="unknown",
        use_id="1713626000000",
        provider="google",
        detail=detail,
    )

    events = read_chat_events("20260420")
    assert events == [event]
    assert events[0]["detail"] == detail


def test_thinking_payload_round_trips_for_sol_message_and_talent_finished(
    tmp_path, monkeypatch
):
    _setup_journal(tmp_path, monkeypatch)
    ts = _ms(2026, 4, 20, 12, 0, 0)
    thinking = {
        "content": "reasoning text",
        "provider": "openai",
        "model": "gpt-reasoning",
        "tokens": 100,
    }

    sol_event = append_chat_event(
        "sol_message",
        ts=ts,
        use_id="1713626000000",
        text="hello",
        notes="ready",
        requested_target=None,
        requested_task=None,
        thinking=thinking,
    )
    talent_event = append_chat_event(
        "talent_finished",
        ts=ts + 1_000,
        use_id="1713626000001",
        name="exec",
        summary="done",
        thinking=thinking,
    )

    events = read_chat_events("20260420")
    assert events == [sol_event, talent_event]
    assert events[0]["thinking"] == thinking
    assert events[1]["thinking"] == thinking


def test_historical_events_without_thinking_replay_unchanged(tmp_path, monkeypatch):
    journal = _setup_journal(tmp_path, monkeypatch)
    fixture = (
        '{"kind": "sol_message", "ts": 1776708000000, '
        '"use_id": "1713626000000", "text": "hello", "notes": "ready", '
        '"requested_target": null, "requested_task": null}\n'
    )
    chat_dir = journal / "chronicle" / "20260420" / "chat" / "120000_300"
    chat_dir.mkdir(parents=True)
    (chat_dir / "chat.jsonl").write_text(fixture, encoding="utf-8")

    events = read_chat_events("20260420")
    assert events == [json.loads(fixture)]
    assert "thinking" not in events[0]
    replayed = "".join(json.dumps(event, ensure_ascii=False) + "\n" for event in events)
    assert replayed == fixture
    state = reduce_chat_state("20260420")
    assert state["latest_sol_message"] is not None
    assert "thinking" not in state["latest_sol_message"]
    assert state["latest_sol_message"]["offer"] is None

    import solstone.convey.chat_stream as chat_stream

    assert "thinking" not in chat_stream._VALID_KINDS["sol_message"]
    assert "offer" not in chat_stream._VALID_KINDS["sol_message"]
    assert "thinking" not in chat_stream._VALID_KINDS["talent_finished"]


def test_owner_message_preserves_optional_source(tmp_path, monkeypatch):
    _setup_journal(tmp_path, monkeypatch)
    source = {"kind": "needs_you", "item_text": "Review the launch checklist"}

    event = append_chat_event(
        "owner_message",
        ts=_ms(2026, 4, 20, 12, 0, 0),
        text="let's dig into Review the launch checklist",
        app="home",
        path="/app/home",
        facet=None,
        source=source,
    )

    events = read_chat_events("20260420")
    assert events == [event]
    assert events[0]["source"] == source


def test_append_rolls_at_300_seconds(tmp_path, monkeypatch):
    journal = _setup_journal(tmp_path, monkeypatch)
    start = _ms(2026, 4, 20, 12, 0, 0)

    append_chat_event(
        "owner_message",
        ts=start,
        text="first",
        app="sol",
        path="/chat",
        facet="work",
    )
    append_chat_event(
        "owner_message",
        ts=start + 299_999,
        text="second",
        app="sol",
        path="/chat",
        facet="work",
    )
    append_chat_event(
        "owner_message",
        ts=start + 300_000,
        text="third",
        app="sol",
        path="/chat",
        facet="work",
    )

    chat_root = journal / "chronicle" / "20260420" / "chat"
    assert sorted(path.name for path in chat_root.iterdir()) == [
        "120000_300",
        "120500_300",
    ]


def test_append_rolls_at_day_cross(tmp_path, monkeypatch):
    journal = _setup_journal(tmp_path, monkeypatch)
    first = _ms(2026, 4, 20, 23, 59, 59)
    second = _ms(2026, 4, 21, 0, 0, 0)

    append_chat_event(
        "owner_message",
        ts=first,
        text="late",
        app="sol",
        path="/chat",
        facet="work",
    )
    append_chat_event(
        "owner_message",
        ts=second,
        text="next day",
        app="sol",
        path="/chat",
        facet="work",
    )

    assert (
        journal / "chronicle" / "20260420" / "chat" / "235959_300" / "chat.jsonl"
    ).is_file()
    assert (
        journal / "chronicle" / "20260421" / "chat" / "000000_300" / "chat.jsonl"
    ).is_file()


def test_read_chat_events_returns_ordered(tmp_path, monkeypatch):
    _setup_journal(tmp_path, monkeypatch)
    start = _ms(2026, 4, 20, 12, 0, 0)

    append_chat_event(
        "owner_message",
        ts=start,
        text="first",
        app="sol",
        path="/chat",
        facet="work",
    )
    append_chat_event(
        "owner_message",
        ts=start + 300_000,
        text="second",
        app="sol",
        path="/chat",
        facet="work",
    )
    append_chat_event(
        "owner_message",
        ts=start + 600_000,
        text="third",
        app="sol",
        path="/chat",
        facet="work",
    )

    events = read_chat_events("20260420")
    assert [event["text"] for event in events] == ["first", "second", "third"]
    assert [event["ts"] for event in events] == [
        start,
        start + 300_000,
        start + 600_000,
    ]


def test_read_chat_tail_last_n(tmp_path, monkeypatch):
    _setup_journal(tmp_path, monkeypatch)
    start = _ms(2026, 4, 20, 12, 0, 0)

    for index in range(4):
        append_chat_event(
            "owner_message",
            ts=start + (index * 60_000),
            text=f"msg-{index}",
            app="sol",
            path="/chat",
            facet="work",
        )

    tail = read_chat_tail("20260420", limit=2)
    assert [event["text"] for event in tail] == ["msg-2", "msg-3"]


def test_reduce_chat_state_extracts_latest_sol_and_active_talents(
    tmp_path, monkeypatch
):
    _setup_journal(tmp_path, monkeypatch)
    start = _ms(2026, 4, 20, 12, 0, 0)

    append_chat_event(
        "owner_message",
        ts=start,
        text="hello",
        app="sol",
        path="/chat",
        facet="work",
    )
    append_chat_event(
        "sol_message",
        ts=start + 1_000,
        use_id="chat-1",
        text="dispatching",
        notes="planning",
        requested_target="exec",
        requested_task="compare drafts",
    )
    append_chat_event(
        "talent_spawned",
        ts=start + 2_000,
        use_id="exec-1",
        name="exec",
        task="compare drafts",
        started_at=start + 2_000,
    )
    append_chat_event(
        "talent_finished",
        ts=start + 3_000,
        use_id="exec-1",
        name="exec",
        summary="done",
    )
    append_chat_event(
        "talent_spawned",
        ts=start + 4_000,
        use_id="exec-2",
        name="exec",
        task="write summary",
        started_at=start + 4_000,
    )
    append_chat_event(
        "talent_errored",
        ts=start + 5_000,
        use_id="exec-3",
        name="exec",
        reason="bad input",
    )
    append_chat_event(
        "chat_error",
        ts=start + 6_000,
        reason="unknown",
        use_id=None,
    )
    append_chat_event(
        "reflection_ready",
        ts=start + 7_000,
        day="20260308",
        url="/app/reflections/20260308",
    )

    reduced = reduce_chat_state("20260420")

    assert reduced["latest_sol_message"] == {
        "ts": start + 1_000,
        "use_id": "chat-1",
        "text": "dispatching",
        "notes": "planning",
        "requested_target": "exec",
        "requested_task": "compare drafts",
        "offer": None,
        "draft": None,
        "origin": None,
        "sources": [],
        "answer_state": "answered",
    }
    assert reduced["active_talents"] == [
        {
            "use_id": "exec-2",
            "name": "exec",
            "task": "write summary",
            "started_at": start + 4_000,
            "label": talent_label_for("exec", "running"),
        }
    ]
    assert reduced["completed_talents"] == [
        {
            "use_id": "exec-1",
            "name": "exec",
            "task": "compare drafts",
            "summary": "done",
            "finished_at": start + 3_000,
            "label": talent_label_for("exec", "finished"),
        }
    ]
    assert reduced["errored_talents"] == [
        {
            "use_id": "exec-3",
            "name": "exec",
            "finished_at": start + 5_000,
            "label": talent_label_for("exec", "errored"),
        }
    ]
    assert reduced["chat_error"] == {
        "reason": "unknown",
        "provider": "",
        "detail": "",
    }
    assert reduced["queue_depth"] == 0
    assert reduced["queued_talents"] == []


def test_reduce_chat_state_enriches_talent_labels(tmp_path, monkeypatch):
    _setup_journal(tmp_path, monkeypatch)
    start = _ms(2026, 4, 20, 12, 0, 0)

    append_chat_event(
        "talent_spawned",
        ts=start,
        use_id="read-running",
        name="read",
        task="scan today",
        started_at=start,
    )
    append_chat_event(
        "talent_spawned",
        ts=start + 1_000,
        use_id="support-running",
        name="support",
        task="contact support",
        started_at=start + 1_000,
    )
    append_chat_event(
        "talent_spawned",
        ts=start + 2_000,
        use_id="read-finished",
        name="read",
        task="read memory",
        started_at=start + 2_000,
    )
    append_chat_event(
        "talent_finished",
        ts=start + 3_000,
        use_id="read-finished",
        name="read",
        summary="done",
    )
    append_chat_event(
        "talent_spawned",
        ts=start + 4_000,
        use_id="support-finished",
        name="support",
        task="send draft",
        started_at=start + 4_000,
    )
    append_chat_event(
        "talent_finished",
        ts=start + 5_000,
        use_id="support-finished",
        name="support",
        summary="submitted",
    )
    append_chat_event(
        "talent_spawned",
        ts=start + 6_000,
        use_id="read-errored",
        name="read",
        task="find note",
        started_at=start + 6_000,
    )
    append_chat_event(
        "talent_errored",
        ts=start + 7_000,
        use_id="read-errored",
        name="read",
        reason="timeout",
    )
    append_chat_event(
        "talent_spawned",
        ts=start + 8_000,
        use_id="support-errored",
        name="support",
        task="send ticket",
        started_at=start + 8_000,
    )
    append_chat_event(
        "talent_errored",
        ts=start + 9_000,
        use_id="support-errored",
        name="support",
        reason="network",
    )

    reduced = reduce_chat_state("20260420")

    assert reduced["active_talents"] == [
        {
            "use_id": "read-running",
            "name": "read",
            "task": "scan today",
            "started_at": start,
            "label": talent_label_for("read", "running"),
        },
        {
            "use_id": "support-running",
            "name": "support",
            "task": "contact support",
            "started_at": start + 1_000,
            "label": talent_label_for("support", "running"),
        },
    ]
    assert reduced["completed_talents"] == [
        {
            "use_id": "read-finished",
            "name": "read",
            "task": "read memory",
            "summary": "done",
            "finished_at": start + 3_000,
            "label": talent_label_for("read", "finished"),
        },
        {
            "use_id": "support-finished",
            "name": "support",
            "task": "send draft",
            "summary": "submitted",
            "finished_at": start + 5_000,
            "label": talent_label_for("support", "finished"),
        },
    ]
    assert reduced["errored_talents"] == [
        {
            "use_id": "read-errored",
            "name": "read",
            "finished_at": start + 7_000,
            "label": talent_label_for("read", "errored"),
        },
        {
            "use_id": "support-errored",
            "name": "support",
            "finished_at": start + 9_000,
            "label": talent_label_for("support", "errored"),
        },
    ]


def test_reduce_chat_state_unknown_talent_label_falls_back_to_name(
    tmp_path, monkeypatch
):
    _setup_journal(tmp_path, monkeypatch)
    start = _ms(2026, 4, 20, 12, 0, 0)

    append_chat_event(
        "talent_spawned",
        ts=start,
        use_id="mystery-1",
        name="mystery",
        task="unknown task",
        started_at=start,
    )

    reduced = reduce_chat_state("20260420")

    assert reduced["active_talents"] == [
        {
            "use_id": "mystery-1",
            "name": "mystery",
            "task": "unknown task",
            "started_at": start,
            "label": "mystery",
        }
    ]


def test_reduce_chat_state_sol_message_sources_and_answer_state_defaults(
    tmp_path, monkeypatch
):
    _setup_journal(tmp_path, monkeypatch)
    start = _ms(2026, 4, 20, 12, 0, 0)
    sources = [
        {
            "ref": "sol://20260313/archon/091500_300",
            "label": "Mar 13",
            "url": "/app/timeline/20260313",
        }
    ]

    append_chat_event(
        "sol_message",
        ts=start,
        use_id="chat-1",
        text="partial",
        notes="first",
        requested_target=None,
        requested_task=None,
        sources=sources,
        answer_state="partial",
    )

    reduced = reduce_chat_state("20260420")
    assert reduced["latest_sol_message"]["sources"] == sources
    assert reduced["latest_sol_message"]["answer_state"] == "partial"

    append_chat_event(
        "sol_message",
        ts=start + 1_000,
        use_id="chat-2",
        text="answered",
        notes="second",
        requested_target=None,
        requested_task=None,
    )

    reduced = reduce_chat_state("20260420")
    assert reduced["latest_sol_message"]["sources"] == []
    assert reduced["latest_sol_message"]["answer_state"] == "answered"


def test_reduce_chat_state_chat_error_cleared_by_newer_sol_message(
    tmp_path, monkeypatch
):
    _setup_journal(tmp_path, monkeypatch)
    start = _ms(2026, 4, 20, 12, 0, 0)

    append_chat_event(
        "chat_error",
        ts=start,
        reason="provider_response_invalid",
        use_id="chat-1",
        provider="openai",
        detail="bad json",
    )

    reduced = reduce_chat_state("20260420")
    assert reduced["chat_error"] == {
        "reason": "provider_response_invalid",
        "provider": "openai",
        "detail": "bad json",
    }

    append_chat_event(
        "sol_message",
        ts=start + 1_000,
        use_id="chat-1",
        text="recovered",
        notes="ok",
        requested_target=None,
        requested_task=None,
    )

    assert reduce_chat_state("20260420")["chat_error"] is None


def test_reduce_chat_state_includes_offer_and_clears_on_later_sol_message(
    tmp_path, monkeypatch
):
    _setup_journal(tmp_path, monkeypatch)
    start = _ms(2026, 4, 20, 12, 0, 0)

    append_chat_event(
        "sol_message",
        ts=start,
        use_id="chat-offer",
        text="I can bring in solstone support.",
        notes="offer support",
        requested_target=None,
        requested_task=None,
        offer={"kind": "support"},
    )

    reduced = reduce_chat_state("20260420")
    assert reduced["latest_sol_message"]["offer"] == {"kind": "support"}

    append_chat_event(
        "sol_message",
        ts=start + 1_000,
        use_id="chat-answer",
        text="done",
        notes="answered",
        requested_target=None,
        requested_task=None,
    )

    reduced = reduce_chat_state("20260420")
    assert reduced["latest_sol_message"]["offer"] is None


def test_reduce_chat_state_includes_draft_and_clears_on_later_sol_message(
    tmp_path, monkeypatch
):
    _setup_journal(tmp_path, monkeypatch)
    start = _ms(2026, 4, 20, 12, 0, 0)
    draft = {
        "draft_id": "draft-attach",
        "verb": "attach",
        "payload": {
            "ticket_id": 42,
            "filename": "evidence.txt",
            "content_type": "text/plain",
            "byte_size": 5,
        },
        "diagnostics_snapshot": None,
    }

    append_chat_event(
        "sol_message",
        ts=start,
        use_id="chat-draft",
        text="draft ready",
        notes="support draft",
        requested_target=None,
        requested_task=None,
        draft=draft,
    )

    reduced = reduce_chat_state("20260420")
    assert reduced["latest_sol_message"]["draft"] == draft
    assert "content_b64" not in reduced["latest_sol_message"]["draft"]["payload"]

    append_chat_event(
        "sol_message",
        ts=start + 1_000,
        use_id="chat-answer",
        text="done",
        notes="answered",
        requested_target=None,
        requested_task=None,
    )

    reduced = reduce_chat_state("20260420")
    assert reduced["latest_sol_message"]["draft"] is None


def test_reduce_chat_state_returns_last_queue_depth(tmp_path, monkeypatch):
    _setup_journal(tmp_path, monkeypatch)
    start = _ms(2026, 4, 20, 12, 0, 0)

    assert reduce_chat_state("20260420")["queue_depth"] == 0

    append_chat_event("chat_queue_depth", ts=start, depth=2)
    append_chat_event("chat_queue_depth", ts=start + 1_000, depth=5)
    append_chat_event("chat_queue_depth", ts=start + 2_000, depth=1)

    assert reduce_chat_state("20260420")["queue_depth"] == 1


def test_reduce_chat_state_surfaces_sol_message_origin(tmp_path, monkeypatch):
    _setup_journal(tmp_path, monkeypatch)
    start = _ms(2026, 4, 20, 12, 0, 0)
    origin = {"logical_use_id": "chat-dispatch", "ask": "look this up"}

    append_chat_event(
        "sol_message",
        ts=start,
        use_id="chat-fold",
        text="folded answer",
        notes="",
        requested_target=None,
        requested_task=None,
        origin=origin,
    )

    reduced = reduce_chat_state("20260420")
    assert reduced["latest_sol_message"]["origin"] == origin


def test_reduce_chat_state_tracks_queued_talents_until_spawn(tmp_path, monkeypatch):
    _setup_journal(tmp_path, monkeypatch)
    start = _ms(2026, 4, 20, 12, 0, 0)

    append_chat_event(
        "talent_queued",
        ts=start,
        use_id="talent-queued",
        name="exec",
        task="research",
        queued_at=start,
        chat_use_id="chat-dispatch",
        ask="research this",
        context={"scope": "today"},
        location={"app": "sol", "path": "/app/sol", "facet": "work"},
    )

    assert reduce_chat_state("20260420")["queued_talents"] == [
        {
            "use_id": "talent-queued",
            "name": "exec",
            "task": "research",
            "queued_at": start,
        }
    ]

    append_chat_event(
        "talent_spawned",
        ts=start + 1_000,
        use_id="talent-queued",
        name="exec",
        task="research",
        started_at=start + 1_000,
    )

    reduced = reduce_chat_state("20260420")
    assert reduced["queued_talents"] == []
    assert reduced["active_talents"][0]["use_id"] == "talent-queued"


def test_append_reflection_ready_event(tmp_path, monkeypatch):
    _setup_journal(tmp_path, monkeypatch)
    ts = _ms(2026, 4, 20, 12, 0, 0)

    event = append_chat_event(
        "reflection_ready",
        ts=ts,
        day="20260308",
        url="/app/reflections/20260308",
    )

    assert event["kind"] == "reflection_ready"
    assert event["day"] == "20260308"
    assert event["url"] == "/app/reflections/20260308"


def test_find_unresponded_trigger_owner_message(tmp_path, monkeypatch):
    _setup_journal(tmp_path, monkeypatch)
    ts = _ms(2026, 4, 20, 12, 0, 0)

    append_chat_event(
        "owner_message",
        ts=ts,
        text="hello",
        app="sol",
        path="/chat",
        facet="work",
    )

    trigger = find_unresponded_trigger("20260420")
    assert trigger is not None
    assert trigger["kind"] == "owner_message"
    assert trigger["text"] == "hello"


def test_find_unresponded_trigger_talent_finished(tmp_path, monkeypatch):
    _setup_journal(tmp_path, monkeypatch)
    start = _ms(2026, 4, 20, 12, 0, 0)

    append_chat_event(
        "owner_message",
        ts=start,
        text="hello",
        app="sol",
        path="/chat",
        facet="work",
    )
    append_chat_event(
        "sol_message",
        ts=start + 1_000,
        use_id="chat-1",
        text="working",
        notes="",
        requested_target=None,
        requested_task=None,
    )
    append_chat_event(
        "talent_finished",
        ts=start + 2_000,
        use_id="exec-1",
        name="exec",
        summary="done",
    )

    trigger = find_unresponded_trigger("20260420")
    assert trigger is not None
    assert trigger["kind"] == "talent_finished"
    assert trigger["summary"] == "done"


def test_find_unresponded_trigger_after_dispatch_ack_and_spawn(tmp_path, monkeypatch):
    _setup_journal(tmp_path, monkeypatch)
    start = _ms(2026, 4, 20, 12, 0, 0)

    append_chat_event(
        "owner_message",
        ts=start,
        text="look this up",
        app="sol",
        path="/chat",
        facet="work",
    )
    append_chat_event(
        "sol_message",
        ts=start + 1_000,
        use_id="chat-dispatch",
        text="working",
        notes="",
        requested_target="exec",
        requested_task="research",
    )
    append_chat_event(
        "talent_spawned",
        ts=start + 2_000,
        use_id="talent-1",
        name="exec",
        task="research",
        started_at=start + 2_000,
    )
    append_chat_event(
        "talent_finished",
        ts=start + 3_000,
        use_id="talent-1",
        name="exec",
        summary="done",
    )

    trigger = find_unresponded_trigger("20260420")
    assert trigger is not None
    assert trigger["kind"] == "talent_finished"
    assert trigger["use_id"] == "talent-1"


def test_talent_queued_is_not_an_unresponded_trigger(tmp_path, monkeypatch):
    _setup_journal(tmp_path, monkeypatch)
    start = _ms(2026, 4, 20, 12, 0, 0)

    append_chat_event(
        "owner_message",
        ts=start,
        text="look this up",
        app="sol",
        path="/chat",
        facet="work",
    )
    append_chat_event(
        "sol_message",
        ts=start + 1_000,
        use_id="chat-dispatch",
        text="working",
        notes="",
        requested_target="exec",
        requested_task="research",
    )
    append_chat_event(
        "talent_queued",
        ts=start + 2_000,
        use_id="talent-queued",
        name="exec",
        task="research",
        queued_at=start + 2_000,
        chat_use_id="chat-dispatch",
        ask="look this up",
        context={},
        location={"app": "sol", "path": "/chat", "facet": "work"},
    )

    assert find_unresponded_trigger("20260420") is None


def test_find_unresponded_trigger_resolved(tmp_path, monkeypatch):
    _setup_journal(tmp_path, monkeypatch)
    start = _ms(2026, 4, 20, 12, 0, 0)

    append_chat_event(
        "talent_finished",
        ts=start,
        use_id="exec-1",
        name="exec",
        summary="done",
    )
    append_chat_event(
        "sol_message",
        ts=start + 1_000,
        use_id="chat-1",
        text="thanks",
        notes="",
        requested_target=None,
        requested_task=None,
    )

    assert find_unresponded_trigger("20260420") is None
