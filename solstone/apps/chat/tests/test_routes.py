# SPDX-License-Identifier: AGPL-3.0-only
# Copyright (c) 2026 sol pbc

from __future__ import annotations

import json
import os
import shutil
import subprocess
from dataclasses import dataclass
from datetime import date, datetime
from pathlib import Path
from typing import Any

import pytest

from solstone.apps.chat.config import DEFAULT_THINKING_SURFACES
from solstone.convey import create_app
from solstone.convey.chat_stream import append_chat_event, read_chat_events
from solstone.convey.sol_initiated.copy import (
    CATEGORIES,
    KIND_OWNER_CHAT_OPEN,
    KIND_SOL_CHAT_REQUEST,
    KIND_SOL_CHAT_REQUEST_SUPERSEDED,
)


def _ms(year: int, month: int, day: int, hour: int, minute: int) -> int:
    return int(datetime(year, month, day, hour, minute).timestamp() * 1000)


@dataclass
class ChatTestEnv:
    client: Any
    journal: Any


@pytest.fixture
def journal_copy(tmp_path, monkeypatch):
    src = Path("tests/fixtures/journal").resolve()
    dst = tmp_path / "journal"
    copytree_tracked(src, dst)
    monkeypatch.setenv("SOLSTONE_JOURNAL", str(dst.resolve()))
    return dst


def _make_env(journal, monkeypatch) -> ChatTestEnv:
    monkeypatch.setenv("SOLSTONE_JOURNAL", str(journal))
    app = create_app(str(journal))
    app.config["TESTING"] = True
    client = app.test_client()
    return ChatTestEnv(client=client, journal=journal)


def _set_today(monkeypatch, day: str) -> None:
    import solstone.apps.chat.routes as chat_routes

    class FixedDate(date):
        @classmethod
        def today(cls) -> date:
            return cls(int(day[:4]), int(day[4:6]), int(day[6:8]))

    monkeypatch.setattr(chat_routes, "date", FixedDate)


def _set_chat_stream_now(
    monkeypatch, day: str, hour: int = 10, minute: int = 1
) -> None:
    monkeypatch.setattr(
        "solstone.convey.chat_stream.time.time",
        lambda: _ms(int(day[:4]), int(day[4:6]), int(day[6:8]), hour, minute) / 1000,
    )


def copytree_tracked(src: Path, dst: Path) -> None:
    result = subprocess.run(
        ["git", "ls-files", "."],
        cwd=str(src),
        capture_output=True,
        text=True,
        check=True,
    )
    for rel in result.stdout.splitlines():
        if not rel:
            continue
        src_file = src / rel
        dst_file = dst / rel
        dst_file.parent.mkdir(parents=True, exist_ok=True)
        if src_file.is_symlink():
            os.symlink(os.readlink(src_file), dst_file)
        else:
            shutil.copy2(src_file, dst_file)


def _append_sol_request(day: str, request_id: str = "req") -> None:
    append_chat_event(
        KIND_SOL_CHAT_REQUEST,
        ts=_ms(int(day[:4]), int(day[4:6]), int(day[6:8]), 10, 0),
        request_id=request_id,
        summary="Notice this",
        message=None,
        category=CATEGORIES[0],
        dedupe=request_id,
        dedupe_window="24h",
        since_ts=1,
        trigger_talent="reflection",
    )


def _write_chat_config(journal: Path, thinking_surfaces: str) -> None:
    config_path = journal / "config" / "chat.json"
    config_path.parent.mkdir(parents=True, exist_ok=True)
    config_path.write_text(
        json.dumps({"thinking_surfaces": thinking_surfaces}) + "\n",
        encoding="utf-8",
    )


def _state_json(env: ChatTestEnv, day: str) -> tuple[Any, dict[str, Any]]:
    response = env.client.get(f"/app/chat/api/state?day={day}")
    return response, response.get_json()


def _owner_chat_open_events(day: str) -> list[dict[str, Any]]:
    return [
        event
        for event in read_chat_events(day)
        if event.get("kind") == KIND_OWNER_CHAT_OPEN
    ]


def test_chat_index_redirects_to_today(journal_copy, monkeypatch):
    today = "20990101"
    _set_today(monkeypatch, today)
    env = _make_env(journal_copy, monkeypatch)

    response = env.client.get("/app/chat/")

    assert response.status_code == 302
    assert response.headers["Location"].endswith(f"/app/chat/{today}")


def test_chat_api_index_counts_fixture_events(journal_copy, monkeypatch):
    env = _make_env(journal_copy, monkeypatch)

    response = env.client.get("/app/chat/api/index")
    data = response.get_json()

    assert response.status_code == 200
    assert data == {
        "coverage": {"start": "20260508", "end": "20260508"},
        "months": {"202605": 2},
    }


def test_chat_invalid_days_return_404(journal_copy, monkeypatch):
    _set_today(monkeypatch, "20990101")
    env = _make_env(journal_copy, monkeypatch)

    assert env.client.get("/app/chat/abcd1234").status_code == 404
    assert env.client.get("/app/chat/20260101extra").status_code == 404
    assert env.client.get("/app/chat/api/state?day=abcd1234").status_code == 404
    assert env.client.get("/app/chat/api/state?day=20260101extra").status_code == 404


def test_chat_day_serves_shell_with_chat_bar(journal_copy, monkeypatch):
    today = "20990102"
    past_day = "20990101"
    _set_today(monkeypatch, today)
    env = _make_env(journal_copy, monkeypatch)

    today_response = env.client.get(f"/app/chat/{today}")
    past_response = env.client.get(f"/app/chat/{past_day}")

    assert today_response.status_code == 200
    assert past_response.status_code == 200
    for response in (today_response, past_response):
        html = response.get_data(as_text=True)
        assert 'id="chatBarForm"' in html
        assert html.count('id="chatBarForm"') == 1


def _css_declaration_block(css: str, selector: str) -> str:
    start = css.index(f"{selector} {{") + len(f"{selector} {{")
    end = css.index("}", start)
    return css[start:end]


def test_chat_bubble_markdown_css_is_wired():
    css = Path("solstone/convey/static/app.css").read_text(encoding="utf-8")

    markdown_block = _css_declaration_block(css, ".chat-bubble-text--markdown")
    pre_block = _css_declaration_block(css, ".chat-bubble-text--markdown pre")

    assert "white-space: normal" in markdown_block
    assert "max-width: 100%" in pre_block
    assert "overflow-x: auto" in pre_block


def test_chat_thinking_css_selector_is_wired():
    css = Path("solstone/convey/static/app.css").read_text(encoding="utf-8")

    assert ".chat-thinking-content" in css
    assert "opacity: 0.7" in css
    assert "font-style: italic" in css
    assert "white-space: pre-wrap" in css


def test_chat_error_detail_css_selector_is_wired():
    css = Path("solstone/convey/static/app.css").read_text(encoding="utf-8")

    assert ".chat-error-detail-content" in css
    assert "opacity: 0.7" in css
    assert "font-style: italic" in css
    assert "white-space: pre-wrap" in css


def test_state_returns_all_event_kinds(journal_copy, monkeypatch):
    day = "20990102"
    _set_today(monkeypatch, "20990103")
    env = _make_env(journal_copy, monkeypatch)
    append_chat_event(
        "owner_message",
        ts=_ms(2099, 1, 2, 9, 0),
        text="owner hello",
        app="chat",
        path=f"/app/chat/{day}",
        facet="work",
    )
    append_chat_event(
        "sol_message",
        ts=_ms(2099, 1, 2, 9, 1),
        use_id="use-1",
        text="sol reply",
        notes="full note",
        requested_target=None,
        requested_task=None,
    )
    append_chat_event(
        "talent_spawned",
        ts=_ms(2099, 1, 2, 9, 2),
        use_id="use-2",
        name="exec",
        task="find updates",
        started_at=_ms(2099, 1, 2, 9, 2),
    )
    append_chat_event(
        "talent_finished",
        ts=_ms(2099, 1, 2, 9, 3),
        use_id="use-2",
        name="exec",
        summary="done",
    )
    append_chat_event(
        "talent_errored",
        ts=_ms(2099, 1, 2, 9, 4),
        use_id="use-3",
        name="exec",
        reason="bad args",
    )
    append_chat_event(
        "chat_error",
        ts=_ms(2099, 1, 2, 9, 5),
        reason="network_unreachable",
        use_id="use-4",
    )
    append_chat_event(
        "reflection_ready",
        ts=_ms(2099, 1, 2, 9, 6),
        day="20981228",
        url="/app/reflections/20981228",
    )

    response, state = _state_json(env, day)

    assert response.status_code == 200
    assert {
        "events",
        "owner_name",
        "agent_name",
        "thinking_surfaces",
        "today_day",
        "sol_open_request_id",
        "sol_message_origins",
    } <= set(state)
    events = state["events"]
    assert [event["kind"] for event in events] == [
        "owner_message",
        "sol_message",
        "talent_spawned",
        "talent_finished",
        "talent_errored",
        "chat_error",
        "reflection_ready",
    ]
    assert events[0]["text"] == "owner hello"
    assert events[1]["notes"] == "full note"
    assert [events[index]["use_id"] for index in (2, 3, 4)] == [
        "use-2",
        "use-2",
        "use-3",
    ]
    assert events[5]["reason"] == "network_unreachable"
    assert events[6]["url"] == "/app/reflections/20981228"


def test_state_empty_day_returns_200_empty(journal_copy, monkeypatch):
    day = "20990104"
    _set_today(monkeypatch, "20990105")
    (journal_copy / "chronicle" / day / "chat").mkdir(parents=True)
    env = _make_env(journal_copy, monkeypatch)

    response, state = _state_json(env, day)

    assert response.status_code == 200
    assert state["events"] == []
    assert state["sol_open_request_id"] is None


def test_state_missing_events_file_returns_empty(journal_copy, monkeypatch):
    day = "20990131"
    _set_today(monkeypatch, "20990201")
    env = _make_env(journal_copy, monkeypatch)

    response, state = _state_json(env, day)

    assert response.status_code == 200
    assert state["events"] == []


def test_state_corrupt_chat_stream_returns_200_empty(journal_copy, monkeypatch):
    day = "20990104"
    _set_today(monkeypatch, "20990105")

    def raise_chat_events(_day: str) -> list[dict[str, Any]]:
        raise ValueError("corrupt stream")

    monkeypatch.setattr("solstone.apps.chat.routes.read_chat_events", raise_chat_events)
    env = _make_env(journal_copy, monkeypatch)

    response, state = _state_json(env, day)

    # Intentional improvement over the old SSR behavior: a corrupt stream falls
    # back to an empty client state instead of returning a 500.
    assert response.status_code == 200
    assert state["events"] == []
    assert state["sol_message_origins"] == {}
    assert {
        "owner_name",
        "agent_name",
        "thinking_surfaces",
        "today_day",
        "sol_open_request_id",
    } <= set(state)


def test_state_thinking_surfaces_reflects_config(journal_copy, monkeypatch):
    day = "20990106"
    thinking = {
        "content": "reasoning text",
        "provider": "openai",
        "model": "gpt",
        "tokens": 10,
    }
    _set_today(monkeypatch, "20990107")
    env = _make_env(journal_copy, monkeypatch)
    append_chat_event(
        "sol_message",
        ts=_ms(2099, 1, 6, 9, 1),
        use_id="use-thinking",
        text="sol reply",
        notes="",
        requested_target=None,
        requested_task=None,
        thinking=thinking,
    )

    for thinking_surfaces in ("always", "never", "on_tap"):
        _write_chat_config(journal_copy, thinking_surfaces)
        response, state = _state_json(env, day)

        assert response.status_code == 200
        assert state["thinking_surfaces"] == thinking_surfaces
        assert state["events"][0]["thinking"] == thinking


def test_state_load_chat_config_failure_falls_back(journal_copy, monkeypatch):
    day = "20990108"
    _set_today(monkeypatch, "20990109")

    def raise_load_chat_config() -> dict[str, Any]:
        raise RuntimeError("boom")

    monkeypatch.setattr(
        "solstone.apps.chat.routes.load_chat_config", raise_load_chat_config
    )
    env = _make_env(journal_copy, monkeypatch)

    response, state = _state_json(env, day)

    assert response.status_code == 200
    assert state["thinking_surfaces"] == DEFAULT_THINKING_SURFACES


def test_state_missing_identity_returns_default_names(tmp_path, monkeypatch):
    journal = tmp_path / "journal"
    config_dir = journal / "config"
    config_dir.mkdir(parents=True)
    (config_dir / "journal.json").write_text(
        json.dumps({"setup": {"completed_at": 1700000000000}}) + "\n",
        encoding="utf-8",
    )
    _set_today(monkeypatch, "20990110")
    env = _make_env(journal, monkeypatch)

    response, state = _state_json(env, "20990109")

    assert response.status_code == 200
    assert state["owner_name"] == "Owner"
    assert state["agent_name"] == "Sol"


def test_state_today_unresolved_request_sets_open_id(journal_copy, monkeypatch):
    today = "20990102"
    _set_today(monkeypatch, today)
    _set_chat_stream_now(monkeypatch, today)
    env = _make_env(journal_copy, monkeypatch)
    _append_sol_request(today, "req")

    response, state = _state_json(env, today)

    assert response.status_code == 200
    assert state["sol_open_request_id"] == "req"
    assert _owner_chat_open_events(today) == []


def test_state_no_unresolved_request_open_id_null(journal_copy, monkeypatch):
    today = "20990102"
    _set_today(monkeypatch, today)
    _set_chat_stream_now(monkeypatch, today)
    env = _make_env(journal_copy, monkeypatch)

    response, state = _state_json(env, today)

    assert response.status_code == 200
    assert state["sol_open_request_id"] is None
    assert _owner_chat_open_events(today) == []


def test_state_past_day_request_open_id_null(journal_copy, monkeypatch):
    today = "20990103"
    past_day = "20990102"
    _set_today(monkeypatch, today)
    _set_chat_stream_now(monkeypatch, today)
    env = _make_env(journal_copy, monkeypatch)
    _append_sol_request(past_day, "req")

    response, state = _state_json(env, past_day)

    assert response.status_code == 200
    assert state["sol_open_request_id"] is None
    assert _owner_chat_open_events(past_day) == []


def test_state_repeated_loads_are_read_only(journal_copy, monkeypatch):
    today = "20990102"
    _set_today(monkeypatch, today)
    _set_chat_stream_now(monkeypatch, today)
    env = _make_env(journal_copy, monkeypatch)
    _append_sol_request(today, "req")

    first_response, first_state = _state_json(env, today)
    second_response, second_state = _state_json(env, today)

    assert first_response.status_code == 200
    assert second_response.status_code == 200
    assert first_state["sol_open_request_id"] == "req"
    assert second_state["sol_open_request_id"] == "req"
    assert _owner_chat_open_events(today) == []


def test_state_sol_message_origins_keyed_by_raw_index(journal_copy, monkeypatch):
    day = "20990113"
    _set_today(monkeypatch, "20990114")
    env = _make_env(journal_copy, monkeypatch)
    base_ts = _ms(2099, 1, 13, 9, 0)
    dispatch_origin = {"logical_use_id": "use-x", "ask": "follow up?"}

    append_chat_event(
        "owner_message",
        ts=base_ts,
        text="owner",
        app="chat",
        path=f"/app/chat/{day}",
        facet="work",
    )
    append_chat_event(
        KIND_SOL_CHAT_REQUEST,
        ts=base_ts + 1,
        request_id="r1",
        summary="notice",
        message="",
        category=CATEGORIES[0],
        dedupe="r1",
        dedupe_window="24h",
        since_ts=123,
        trigger_talent="reflection",
    )
    append_chat_event(
        "sol_message",
        ts=base_ts + 2,
        use_id="use-provenance",
        text="provenance reply",
        notes="",
        requested_target=None,
        requested_task=None,
    )
    append_chat_event(
        KIND_SOL_CHAT_REQUEST_SUPERSEDED,
        ts=base_ts + 3,
        request_id="r1",
        replaced_by="r2",
    )
    append_chat_event(
        KIND_OWNER_CHAT_OPEN,
        ts=base_ts + 4,
        request_id="r1",
        surface="convey",
    )
    append_chat_event(
        "sol_message",
        ts=base_ts + 5,
        use_id="use-dispatch",
        text="dispatch reply",
        notes="",
        requested_target=None,
        requested_task=None,
        origin=dispatch_origin,
    )
    append_chat_event(
        KIND_SOL_CHAT_REQUEST,
        ts=base_ts + 6,
        request_id="r3",
        summary="later notice",
        message="",
        category=CATEGORIES[1],
        dedupe="r3",
        dedupe_window="24h",
        since_ts=456,
        trigger_talent="support",
    )
    append_chat_event(
        "sol_message",
        ts=base_ts + 7,
        use_id="use-post-control",
        text="post-control reply",
        notes="",
        requested_target=None,
        requested_task=None,
    )

    response, state = _state_json(env, day)

    assert response.status_code == 200
    events = state["events"]
    assert len(events) == 8
    assert [event["kind"] for event in events] == [
        "owner_message",
        KIND_SOL_CHAT_REQUEST,
        "sol_message",
        KIND_SOL_CHAT_REQUEST_SUPERSEDED,
        KIND_OWNER_CHAT_OPEN,
        "sol_message",
        KIND_SOL_CHAT_REQUEST,
        "sol_message",
    ]
    origins = state["sol_message_origins"]
    assert set(origins) == {"2", "7"}
    assert origins["2"]["trigger_talent"] == "reflection"
    assert origins["2"]["superseded_by_id"] == "r2"
    assert origins["2"]["superseded_at"] == base_ts + 3
    assert origins["2"]["superseded_time"]
    assert origins["7"]["request_id"] == "r3"
    assert events[5]["origin"] == dispatch_origin
