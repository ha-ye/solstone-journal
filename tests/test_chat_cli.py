# SPDX-License-Identifier: AGPL-3.0-only
# Copyright (c) 2026 sol pbc

from __future__ import annotations

import json
import sys
import time
from collections import deque
from collections.abc import Iterable
from typing import Any

import pytest

from solstone.think import chat_cli
from solstone.think.convey_client import (
    ConveyClientError,
    ConveyUnreachableError,
)

USE_ID = "1713626000000"
FOREIGN_USE_ID = "1713626000001"


class FakeClient:
    def __init__(self, responses: Iterable[Any]) -> None:
        self.responses = deque(responses)
        self.calls: list[tuple[str, str, Any]] = []
        self.base_url = ""

    def request(
        self,
        method: str,
        path: str,
        *,
        params: Any = None,
        json: Any = None,
    ) -> Any:
        self.calls.append((method, path, json))
        if not self.responses:
            raise AssertionError(f"unexpected request: {method} {path}")
        response = self.responses.popleft()
        if isinstance(response, BaseException):
            raise response
        return response


class FakeSseResponse:
    def __init__(self, chunks: list[bytes]) -> None:
        self.chunks = chunks
        self.chunk_size = None

    def iter_content(self, chunk_size=None):
        self.chunk_size = chunk_size
        return iter(self.chunks)


def _patch_identity(monkeypatch: pytest.MonkeyPatch) -> None:
    import solstone.think.identity as identity

    monkeypatch.setattr(identity, "ensure_identity_directory", lambda: None)


def _frame(event: dict[str, Any]) -> bytes:
    return b"data: " + json.dumps(event).encode("utf-8") + b"\n\n"


def _install_main_fakes(
    monkeypatch: pytest.MonkeyPatch,
    *,
    argv: list[str],
    client: FakeClient,
    sse_chunks: Iterable[bytes] | None,
    base_url: str = "http://home.example:9999",
) -> list[str]:
    monkeypatch.setattr(sys, "argv", ["sol chat", *argv])
    _patch_identity(monkeypatch)
    monkeypatch.setattr(chat_cli, "resolve_base_url", lambda: base_url)

    def build_client(resolved_base_url: str) -> FakeClient:
        client.base_url = resolved_base_url
        return client

    open_calls: list[str] = []

    def open_sse(resolved_base_url: str) -> Iterable[bytes] | None:
        open_calls.append(resolved_base_url)
        if sse_chunks is None:
            return None
        return iter(sse_chunks)

    monkeypatch.setattr(chat_cli, "_build_client", build_client)
    monkeypatch.setattr(chat_cli, "_open_sse", open_sse)
    return open_calls


def _post_success(*, queued: bool = False) -> dict[str, Any]:
    return {"use_id": USE_ID, "queued": queued}


def _session_finish(
    *,
    use_id: str = USE_ID,
    text: str = "Recovered answer",
    requested_target: str | None = None,
) -> dict[str, Any]:
    return {
        "latest_sol_message": {
            "use_id": use_id,
            "text": text,
            "requested_target": requested_target,
        }
    }


def test_iter_sse_events_handles_comments_chunking_multiline_and_bad_json() -> None:
    chunks = [
        b": heartbeat\n\n",
        b'data: {"a":',
        b"1}\n\n",
        b'data: {"b": 2}\n\ndata: {"c": 3}\n\n',
        b'data: {"multi":\n',
        b"data: true}\n\n",
        b"data: not json\n\n",
        b'data: {"incomplete": true}',
    ]

    assert list(chat_cli.iter_sse_events(chunks)) == [
        {"a": 1},
        {"b": 2},
        {"c": 3},
        {"multi": True},
    ]


def test_open_sse_uses_resolved_events_url_and_stream_timeout(monkeypatch) -> None:
    response = FakeSseResponse([b": heartbeat\n\n"])
    captured: dict[str, Any] = {}

    def fake_get(url: str, **kwargs: Any) -> FakeSseResponse:
        captured["url"] = url
        captured.update(kwargs)
        return response

    monkeypatch.setattr(chat_cli.requests, "get", fake_get)

    chunks = chat_cli._open_sse("http://home.example:9999/")

    assert list(chunks or []) == [b": heartbeat\n\n"]
    assert captured == {
        "url": "http://home.example:9999/sse/events",
        "stream": True,
        "timeout": (chat_cli.POST_TIMEOUT_SECONDS, None),
    }
    assert response.chunk_size is None


def test_timeout_session_uses_monkeypatched_post_timeout(monkeypatch) -> None:
    captured: dict[str, Any] = {}
    monkeypatch.setattr(chat_cli, "POST_TIMEOUT_SECONDS", 0.25)

    def fake_request(self, method, url, **kwargs):
        captured.update(kwargs)
        return "ok"

    monkeypatch.setattr(chat_cli.requests.Session, "request", fake_request)

    assert chat_cli._TimeoutSession().request("GET", "http://example.test") == "ok"
    assert captured["timeout"] == 0.25


def test_post_chat_includes_facet_when_supplied() -> None:
    client = FakeClient([_post_success()])

    assert chat_cli._post_chat(client, "find this", "work") == (USE_ID, False)
    assert client.calls == [
        ("POST", "/api/chat", {"message": "find this", "facet": "work"})
    ]


def test_post_chat_omits_empty_facet() -> None:
    client = FakeClient([_post_success()])

    assert chat_cli._post_chat(client, "hello", None) == (USE_ID, False)
    assert client.calls == [("POST", "/api/chat", {"message": "hello"})]


def test_render_post_error_uses_error_and_detail() -> None:
    exc = ConveyClientError(
        "Missing required field.",
        detail="message is required",
        status=400,
    )

    assert chat_cli._render_post_error(exc) == (
        "sol: Missing required field.\nsol: message is required"
    )


@pytest.mark.parametrize(
    ("event", "verbose", "expected"),
    [
        (
            {"tract": "cortex", "event": "start", "provider": "google"},
            False,
            "sol is thinking…",
        ),
        (
            {"tract": "cortex", "event": "start", "provider": "google"},
            True,
            "Provider: google; model: unknown",
        ),
        (
            {"tract": "cortex", "event": "thinking", "summary": "reading context"},
            False,
            "sol is thinking…",
        ),
        (
            {"tract": "cortex", "event": "thinking", "summary": "reading context"},
            True,
            "Thinking: reading context",
        ),
        (
            {"tract": "cortex", "event": "tool_start", "tool": "journal_search"},
            False,
            "· journal_search",
        ),
        (
            {"tract": "cortex", "event": "tool_end", "tool": "journal_search"},
            False,
            None,
        ),
        (
            {"tract": "cortex", "event": "tool_end", "tool": "journal_search"},
            True,
            "· journal_search done",
        ),
        (
            {
                "tract": "chat",
                "event": "sol_message",
                "requested_target": "exec",
                "requested_task": " find \n the Adrian quote ",
            },
            False,
            "Making that change… (find the Adrian quote)",
        ),
        (
            {
                "tract": "chat",
                "event": "sol_message",
                "requested_target": "read",
                "requested_task": "patterns this month",
            },
            False,
            "Reading your journal… (patterns this month)",
        ),
        (
            {"tract": "chat", "event": "talent_finished", "use_id": "talent-1"},
            False,
            "Composing your answer…",
        ),
    ],
)
def test_render_progress_rows(event, verbose, expected) -> None:
    assert chat_cli._render_progress(event, verbose=verbose) == expected


def test_render_progress_truncates_task_suffix_to_100_chars() -> None:
    line = chat_cli._render_progress(
        {
            "tract": "chat",
            "event": "sol_message",
            "requested_target": "exec",
            "requested_task": "x" * 101,
        },
        verbose=False,
    )

    assert line == f"Making that change… ({'x' * 99}…)"


def test_render_progress_truncates_verbose_thinking_to_200_chars() -> None:
    line = chat_cli._render_progress(
        {
            "tract": "cortex",
            "event": "thinking",
            "summary": "x" * 201,
        },
        verbose=True,
    )

    assert line == f"Thinking: {'x' * 199}…"


def test_render_progress_lines_are_deduped_by_caller_pattern() -> None:
    lines: list[str] = []
    last = None
    for event in [
        {"tract": "cortex", "event": "start"},
        {"tract": "cortex", "event": "thinking"},
    ]:
        line = chat_cli._render_progress(event, verbose=False)
        if line != last:
            lines.append(line)
            last = line

    assert lines == ["sol is thinking…"]


def test_terminal_error_message_uses_chat_view_for_known_reason() -> None:
    assert chat_cli._terminal_error_message("chat_timeout", "") == (
        "chat took too long"
    )


def test_session_terminal_finish_only_current_use_id() -> None:
    assert chat_cli._session_terminal(FakeClient([_session_finish()]), USE_ID) == {
        "kind": "finish",
        "result": "Recovered answer",
    }
    assert (
        chat_cli._session_terminal(
            FakeClient([_session_finish(use_id=FOREIGN_USE_ID)]),
            USE_ID,
        )
        is None
    )
    assert (
        chat_cli._session_terminal(
            FakeClient([_session_finish(requested_target="exec")]),
            USE_ID,
        )
        is None
    )
    assert (
        chat_cli._session_terminal(
            FakeClient([ConveyClientError("bad", status=500)]),
            USE_ID,
        )
        is None
    )


def test_main_live_sse_finish_prints_answer_without_fallback_warning(
    monkeypatch, capsys
) -> None:
    client = FakeClient([_post_success()])
    _install_main_fakes(
        monkeypatch,
        argv=["hello"],
        client=client,
        sse_chunks=[
            _frame(
                {
                    "tract": "cortex",
                    "event": "finish",
                    "chat_proxy": True,
                    "use_id": USE_ID,
                    "result": "Live answer",
                }
            )
        ],
    )

    chat_cli.main()

    captured = capsys.readouterr()
    assert captured.out == "Live answer\n"
    assert captured.err == ""
    assert client.calls == [("POST", "/api/chat", {"message": "hello"})]


def test_main_live_sse_error_prints_terminal_error_without_session_poll(
    monkeypatch, capsys
) -> None:
    client = FakeClient([_post_success()])
    _install_main_fakes(
        monkeypatch,
        argv=["hello"],
        client=client,
        sse_chunks=[
            _frame(
                {
                    "tract": "cortex",
                    "event": "error",
                    "chat_proxy": True,
                    "use_id": USE_ID,
                    "error": "chat_timeout",
                    "provider": "",
                    "detail": "",
                }
            )
        ],
    )

    with pytest.raises(SystemExit) as exc_info:
        chat_cli.main()

    assert exc_info.value.code == 1
    captured = capsys.readouterr()
    assert captured.out == ""
    assert captured.err == "sol: chat took too long\n"
    assert client.calls == [("POST", "/api/chat", {"message": "hello"})]


def test_main_sse_firehose_filtering_preserves_callback_parity(
    monkeypatch, capsys
) -> None:
    client = FakeClient([_post_success()])
    events = [
        {"tract": "cortex", "event": "start", "chat_proxy": True, "use_id": USE_ID},
        {
            "tract": "cortex",
            "event": "thinking",
            "chat_proxy": True,
            "use_id": FOREIGN_USE_ID,
            "summary": "foreign",
        },
        {"tract": "observe", "event": "status"},
        {
            "tract": "cortex",
            "event": "tool_start",
            "chat_proxy": True,
            "use_id": USE_ID,
            "tool": "journal_search",
        },
        {
            "tract": "chat",
            "event": "sol_message",
            "use_id": USE_ID,
            "requested_target": "exec",
            "requested_task": "find it",
        },
        {
            "tract": "cortex",
            "event": "finish",
            "chat_proxy": True,
            "use_id": USE_ID,
            "result": "Done",
        },
    ]
    _install_main_fakes(
        monkeypatch,
        argv=["hello"],
        client=client,
        sse_chunks=[_frame(event) for event in events],
    )

    chat_cli.main()

    captured = capsys.readouterr()
    assert captured.out == "Done\n"
    assert captured.err == (
        "sol is thinking…\n· journal_search\nMaking that change… (find it)\n"
    )


def test_main_preserves_talent_finished_without_use_id_match(
    monkeypatch, capsys
) -> None:
    client = FakeClient([_post_success()])
    _install_main_fakes(
        monkeypatch,
        argv=["hello"],
        client=client,
        sse_chunks=[
            _frame(
                {
                    "tract": "chat",
                    "event": "talent_finished",
                    "use_id": FOREIGN_USE_ID,
                    "name": "exec",
                    "summary": "done",
                }
            ),
            _frame(
                {
                    "tract": "cortex",
                    "event": "finish",
                    "chat_proxy": True,
                    "use_id": USE_ID,
                    "result": "Final",
                }
            ),
        ],
    )

    chat_cli.main()

    captured = capsys.readouterr()
    assert captured.out == "Final\n"
    assert captured.err == f"{chat_cli.COMPOSING_MESSAGE}\n"


def test_main_uses_resolved_base_url_for_post_sse_and_session(
    monkeypatch, capsys
) -> None:
    client = FakeClient([_post_success(), _session_finish(text="Recovered")])
    monkeypatch.setattr(chat_cli, "POLL_SECONDS", 0.01)
    open_calls = _install_main_fakes(
        monkeypatch,
        argv=["hello"],
        client=client,
        sse_chunks=None,
        base_url="http://home.example:9999",
    )

    chat_cli.main()

    captured = capsys.readouterr()
    assert captured.out == "Recovered\n"
    assert captured.err == f"{chat_cli.LIVE_PROGRESS_UNAVAILABLE_MESSAGE}\n"
    assert client.base_url == "http://home.example:9999"
    assert open_calls == ["http://home.example:9999"]
    assert client.calls == [
        ("POST", "/api/chat", {"message": "hello"}),
        ("GET", "/api/chat/session", None),
    ]


def test_main_dead_sse_recovers_current_turn_from_session(monkeypatch, capsys) -> None:
    client = FakeClient([_post_success(), _session_finish(text="Recovered answer")])
    monkeypatch.setattr(chat_cli, "POLL_SECONDS", 0.01)
    _install_main_fakes(
        monkeypatch,
        argv=["hello"],
        client=client,
        sse_chunks=None,
    )

    chat_cli.main()

    captured = capsys.readouterr()
    assert captured.out == "Recovered answer\n"
    assert captured.err == f"{chat_cli.LIVE_PROGRESS_UNAVAILABLE_MESSAGE}\n"


def test_main_dead_sse_ignores_foreign_session_turn_until_lost_contact(
    monkeypatch, capsys
) -> None:
    client = FakeClient([_post_success(), _session_finish(use_id=FOREIGN_USE_ID)])
    monkeypatch.setattr(chat_cli, "POLL_SECONDS", 0.01)
    monkeypatch.setattr(chat_cli, "IDLE_CEILING_SECONDS", 0)
    _install_main_fakes(
        monkeypatch,
        argv=["hello"],
        client=client,
        sse_chunks=None,
    )

    with pytest.raises(SystemExit) as exc_info:
        chat_cli.main()

    assert exc_info.value.code == 1
    captured = capsys.readouterr()
    assert captured.out == ""
    assert captured.err == f"{chat_cli.LOST_CONTACT_MESSAGE}\n"


def test_main_idle_live_sse_polls_session_at_ceiling(monkeypatch, capsys) -> None:
    def live_idle_stream():
        yield _frame(
            {
                "tract": "cortex",
                "event": "start",
                "chat_proxy": True,
                "use_id": USE_ID,
            }
        )
        while True:
            time.sleep(1)

    client = FakeClient([_post_success(), _session_finish(text="Recovered")])
    monkeypatch.setattr(chat_cli, "POLL_SECONDS", 0.01)
    monkeypatch.setattr(chat_cli, "IDLE_CEILING_SECONDS", 0)
    _install_main_fakes(
        monkeypatch,
        argv=["hello"],
        client=client,
        sse_chunks=live_idle_stream(),
    )

    chat_cli.main()

    captured = capsys.readouterr()
    assert captured.out == "Recovered\n"
    assert captured.err in (
        f"{chat_cli.LIVE_PROGRESS_UNAVAILABLE_MESSAGE}\n",
        (
            f"{chat_cli.CHAT_LIVENESS_THINKING}\n"
            f"{chat_cli.LIVE_PROGRESS_UNAVAILABLE_MESSAGE}\n"
        ),
    )


def test_main_dead_sse_no_session_terminal_lost_contact(monkeypatch, capsys) -> None:
    client = FakeClient([_post_success(), {"latest_sol_message": None}])
    monkeypatch.setattr(chat_cli, "POLL_SECONDS", 0.01)
    monkeypatch.setattr(chat_cli, "IDLE_CEILING_SECONDS", 0)
    _install_main_fakes(
        monkeypatch,
        argv=["hello"],
        client=client,
        sse_chunks=None,
    )

    with pytest.raises(SystemExit) as exc_info:
        chat_cli.main()

    assert exc_info.value.code == 1
    captured = capsys.readouterr()
    assert captured.out == ""
    assert captured.err == f"{chat_cli.LOST_CONTACT_MESSAGE}\n"


def test_main_stream_end_no_terminal_lost_contact(monkeypatch, capsys) -> None:
    client = FakeClient([_post_success(), {"latest_sol_message": None}])
    monkeypatch.setattr(chat_cli, "POLL_SECONDS", 0.01)
    monkeypatch.setattr(chat_cli, "IDLE_CEILING_SECONDS", 0)
    _install_main_fakes(
        monkeypatch,
        argv=["hello"],
        client=client,
        sse_chunks=[],
    )

    with pytest.raises(SystemExit) as exc_info:
        chat_cli.main()

    assert exc_info.value.code == 1
    captured = capsys.readouterr()
    assert captured.out == ""
    assert captured.err == f"{chat_cli.LOST_CONTACT_MESSAGE}\n"


def test_main_reader_thread_no_stray_progress_after_done(monkeypatch, capsys) -> None:
    client = FakeClient([_post_success()])
    _install_main_fakes(
        monkeypatch,
        argv=["hello"],
        client=client,
        sse_chunks=[
            _frame(
                {
                    "tract": "cortex",
                    "event": "finish",
                    "chat_proxy": True,
                    "use_id": USE_ID,
                    "result": "Final",
                }
            ),
            _frame(
                {
                    "tract": "cortex",
                    "event": "tool_start",
                    "chat_proxy": True,
                    "use_id": USE_ID,
                    "tool": "late_tool",
                }
            ),
        ],
    )

    chat_cli.main()

    captured = capsys.readouterr()
    assert captured.out == "Final\n"
    assert captured.err == ""


def test_main_empty_result_is_error(monkeypatch, capsys) -> None:
    client = FakeClient([_post_success()])
    _install_main_fakes(
        monkeypatch,
        argv=["hello"],
        client=client,
        sse_chunks=[
            _frame(
                {
                    "tract": "cortex",
                    "event": "finish",
                    "chat_proxy": True,
                    "use_id": USE_ID,
                    "result": "   ",
                }
            )
        ],
    )

    with pytest.raises(SystemExit) as exc_info:
        chat_cli.main()

    assert exc_info.value.code == 1
    captured = capsys.readouterr()
    assert captured.out == ""
    assert captured.err == f"{chat_cli.EMPTY_ANSWER_MESSAGE}\n"


def test_main_queued_response_prints_busy_line(monkeypatch, capsys) -> None:
    client = FakeClient([_post_success(queued=True)])
    _install_main_fakes(
        monkeypatch,
        argv=["hello"],
        client=client,
        sse_chunks=[
            _frame(
                {
                    "tract": "cortex",
                    "event": "finish",
                    "chat_proxy": True,
                    "use_id": USE_ID,
                    "result": "Queued answer",
                }
            )
        ],
    )

    chat_cli.main()

    captured = capsys.readouterr()
    assert captured.out == "Queued answer\n"
    assert captured.err == f"{chat_cli.QUEUED_MESSAGE}\n"


def test_main_post_error_mapping(monkeypatch, capsys) -> None:
    cases = [
        (
            ConveyUnreachableError("down"),
            f"{chat_cli.SERVICE_DOWN_MESSAGE}\n",
        ),
        (
            ConveyClientError(
                "Missing required field.",
                detail="message is required",
                status=400,
            ),
            "sol: Missing required field.\nsol: message is required\n",
        ),
        (
            ConveyClientError("malformed", status=200),
            f"sol: {chat_cli.MALFORMED_RESPONSE_MESSAGE}\n",
        ),
        (
            {},
            f"sol: {chat_cli.MALFORMED_RESPONSE_MESSAGE}\n",
        ),
    ]

    for response, expected_err in cases:
        client = FakeClient([response])
        _install_main_fakes(
            monkeypatch,
            argv=["hello"],
            client=client,
            sse_chunks=[],
        )

        with pytest.raises(SystemExit) as exc_info:
            chat_cli.main()

        assert exc_info.value.code == 1
        captured = capsys.readouterr()
        assert captured.out == ""
        assert captured.err == expected_err


def test_main_rejects_removed_provider_and_talent_options(monkeypatch) -> None:
    monkeypatch.setattr(sys, "argv", ["sol chat", "--provider", "google", "hello"])
    with pytest.raises(SystemExit) as provider_exit:
        chat_cli.main()

    monkeypatch.setattr(sys, "argv", ["sol chat", "--talent", "chat", "hello"])
    with pytest.raises(SystemExit) as talent_exit:
        chat_cli.main()

    assert provider_exit.value.code == 2
    assert talent_exit.value.code == 2
