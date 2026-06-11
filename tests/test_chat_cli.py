# SPDX-License-Identifier: AGPL-3.0-only
# Copyright (c) 2026 sol pbc

from __future__ import annotations

import io
import json
import sys
import threading
import urllib.error
from datetime import datetime
from pathlib import Path
from typing import Any

import pytest

from solstone.convey.chat_stream import append_chat_event
from solstone.think import chat_cli

DAY = "20260605"
USE_ID = "1713626000000"


class FakeResponse:
    def __init__(self, body: dict[str, Any] | bytes) -> None:
        if isinstance(body, bytes):
            self._body = body
        else:
            self._body = json.dumps(body).encode("utf-8")

    def __enter__(self):
        return self

    def __exit__(self, _exc_type, _exc, _tb) -> bool:
        return False

    def read(self) -> bytes:
        return self._body


def _setup_journal(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    journal = tmp_path / "journal"
    journal.mkdir()
    monkeypatch.setenv("SOLSTONE_JOURNAL", str(journal))
    return journal


def _ms(hour: int, minute: int, second: int) -> int:
    return int(datetime(2026, 6, 5, hour, minute, second).timestamp() * 1000)


def _append_sol_message(
    use_id: str,
    text: str,
    *,
    ts: int | None = None,
    target: str | None = None,
    task: str | None = None,
) -> dict[str, Any]:
    return append_chat_event(
        "sol_message",
        ts=ts or _ms(12, 0, 0),
        use_id=use_id,
        text=text,
        notes="ready",
        requested_target=target,
        requested_task=task,
    )


def _append_chat_error(
    use_id: str,
    reason: str,
    *,
    ts: int | None = None,
    provider: str = "",
    detail: str = "",
) -> dict[str, Any]:
    return append_chat_event(
        "chat_error",
        ts=ts or _ms(12, 0, 0),
        use_id=use_id,
        reason=reason,
        provider=provider,
        detail=detail,
    )


def _http_error(
    body: dict[str, Any] | bytes, code: int = 400
) -> urllib.error.HTTPError:
    if isinstance(body, bytes):
        raw = body
    else:
        raw = json.dumps(body).encode("utf-8")
    return urllib.error.HTTPError(
        "http://127.0.0.1:5015/api/chat",
        code,
        "bad request",
        hdrs=None,
        fp=io.BytesIO(raw),
    )


def _install_urlopen(
    monkeypatch: pytest.MonkeyPatch,
    item: Any,
    *,
    calls: list[tuple[Any, float]] | None = None,
    order: list[str] | None = None,
):
    def fake_urlopen(request, timeout):
        if order is not None:
            order.append("post")
        if calls is not None:
            calls.append((request, timeout))
        if isinstance(item, BaseException):
            raise item
        if callable(item):
            return item(request, timeout)
        return item

    monkeypatch.setattr(chat_cli.urllib.request, "urlopen", fake_urlopen)


def _install_main_fakes(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    *,
    argv: list[str],
    urlopen_item: Any,
    order: list[str] | None = None,
) -> tuple[Path, list[Any], list[tuple[Any, float]]]:
    journal = _setup_journal(tmp_path, monkeypatch)
    monkeypatch.setattr(sys, "argv", ["sol chat", *argv])
    monkeypatch.setattr(chat_cli, "require_solstone", lambda: None)
    monkeypatch.setattr(chat_cli, "read_service_port", lambda service: 5015)
    monkeypatch.setattr(chat_cli, "_today", lambda: DAY)

    import solstone.think.identity as identity

    monkeypatch.setattr(identity, "ensure_identity_directory", lambda: None)

    instances: list[Any] = []

    class FakeCallosumConnection:
        def __init__(self) -> None:
            self.callback = None
            self.stopped = False
            instances.append(self)

        def start(self, callback=None) -> None:
            if order is not None:
                order.append("start")
            self.callback = callback

        def stop(self) -> None:
            self.stopped = True

    monkeypatch.setattr(chat_cli, "CallosumConnection", FakeCallosumConnection)
    calls: list[tuple[Any, float]] = []
    _install_urlopen(monkeypatch, urlopen_item, calls=calls, order=order)
    return journal, instances, calls


def _success_response(use_id: str = USE_ID, *, queued: bool = False) -> FakeResponse:
    return FakeResponse({"use_id": use_id, "queued": queued})


def test_post_chat_includes_facet_when_supplied(monkeypatch) -> None:
    calls: list[tuple[Any, float]] = []
    monkeypatch.setattr(chat_cli, "read_service_port", lambda service: 5015)
    _install_urlopen(monkeypatch, _success_response(), calls=calls)

    assert chat_cli._post_chat("find this", "work") == (USE_ID, False)

    request, timeout = calls[0]
    assert timeout == chat_cli.POST_TIMEOUT_SECONDS
    assert request.full_url == "http://127.0.0.1:5015/api/chat"
    assert json.loads(request.data.decode("utf-8")) == {
        "message": "find this",
        "facet": "work",
    }


def test_post_chat_omits_empty_facet(monkeypatch) -> None:
    calls: list[tuple[Any, float]] = []
    monkeypatch.setattr(chat_cli, "read_service_port", lambda service: 5015)
    _install_urlopen(monkeypatch, _success_response(), calls=calls)

    assert chat_cli._post_chat("hello", None) == (USE_ID, False)

    request, _timeout = calls[0]
    assert json.loads(request.data.decode("utf-8")) == {"message": "hello"}


def test_http_error_message_uses_error_and_detail() -> None:
    exc = _http_error(
        {
            "error": "Sol is thinking right now.",
            "reason_code": "chat_queue_full",
            "detail": "Try again in a moment.",
        }
    )

    assert chat_cli._http_error_message(exc) == (
        "sol: Sol is thinking right now.\nsol: Try again in a moment."
    )


def test_http_error_message_falls_back_for_non_json_body() -> None:
    exc = _http_error(b"plain failure")

    assert chat_cli._http_error_message(exc) == "sol: bad request"


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


def test_persisted_terminal_returns_last_finish(tmp_path, monkeypatch) -> None:
    _setup_journal(tmp_path, monkeypatch)
    _append_sol_message(USE_ID, "first", ts=_ms(12, 0, 0))
    _append_sol_message(USE_ID, "second", ts=_ms(12, 0, 1))

    assert chat_cli._persisted_terminal(USE_ID, DAY) == {
        "kind": "finish",
        "result": "second",
    }


def test_persisted_terminal_skips_bridge_sol_message(tmp_path, monkeypatch) -> None:
    _setup_journal(tmp_path, monkeypatch)
    _append_sol_message(
        USE_ID,
        "checking",
        ts=_ms(12, 0, 0),
        target="exec",
        task="find the note",
    )
    _append_sol_message(USE_ID, "answer", ts=_ms(12, 0, 1))

    assert chat_cli._persisted_terminal(USE_ID, DAY) == {
        "kind": "finish",
        "result": "answer",
    }


def test_persisted_terminal_returns_error(tmp_path, monkeypatch) -> None:
    _setup_journal(tmp_path, monkeypatch)
    _append_chat_error(
        USE_ID,
        "chat_timeout",
        provider="google",
        detail="raw detail",
    )

    assert chat_cli._persisted_terminal(USE_ID, DAY) == {
        "kind": "error",
        "reason": "chat_timeout",
        "provider": "google",
        "detail": "raw detail",
    }


def test_persisted_terminal_returns_none_when_only_bridge(
    tmp_path, monkeypatch
) -> None:
    _setup_journal(tmp_path, monkeypatch)
    _append_sol_message(
        USE_ID,
        "checking",
        target="exec",
        task="find the note",
    )

    assert chat_cli._persisted_terminal(USE_ID, DAY) is None


def test_persisted_terminal_keeps_empty_finish_text(tmp_path, monkeypatch) -> None:
    _setup_journal(tmp_path, monkeypatch)
    _append_sol_message(USE_ID, "")

    assert chat_cli._persisted_terminal(USE_ID, DAY) == {
        "kind": "finish",
        "result": "",
    }


def test_terminal_error_message_uses_chat_view_for_known_reason() -> None:
    assert (
        chat_cli._terminal_error_message(
            "chat_timeout",
            "",
            use_id="",
            day=DAY,
        )
        == "chat took too long"
    )


def test_terminal_error_message_enriches_provider_from_persisted_error(
    tmp_path, monkeypatch
) -> None:
    _setup_journal(tmp_path, monkeypatch)
    _append_chat_error(
        USE_ID,
        "provider_response_invalid",
        provider="google",
    )

    assert chat_cli._terminal_error_message(
        "provider_response_invalid",
        "",
        use_id=USE_ID,
        day=DAY,
    ) == (
        "Gemini's response didn't match the expected shape — try rephrasing "
        "or asking something more specific."
    )


def test_main_prints_initial_persisted_finish_to_stdout_only(
    tmp_path, monkeypatch, capsys
) -> None:
    _install_main_fakes(
        monkeypatch,
        tmp_path,
        argv=["hello"],
        urlopen_item=_success_response(),
    )
    _append_sol_message(USE_ID, "Final answer")

    chat_cli.main()

    captured = capsys.readouterr()
    assert captured.out == "Final answer\n"
    assert captured.err == ""


def test_main_starts_listener_before_post(tmp_path, monkeypatch, capsys) -> None:
    order: list[str] = []
    _install_main_fakes(
        monkeypatch,
        tmp_path,
        argv=["hello"],
        urlopen_item=_success_response(),
        order=order,
    )
    _append_sol_message(USE_ID, "Final answer")

    chat_cli.main()

    assert order == ["start", "post"]
    assert capsys.readouterr().out == "Final answer\n"


def test_main_bridge_is_not_printed_as_terminal_answer(
    tmp_path, monkeypatch, capsys
) -> None:
    _install_main_fakes(
        monkeypatch,
        tmp_path,
        argv=["find", "it"],
        urlopen_item=_success_response(),
    )
    _append_sol_message(
        USE_ID,
        "checking the journal",
        ts=_ms(12, 0, 0),
        target="exec",
        task="find it",
    )
    _append_sol_message(USE_ID, "Real answer", ts=_ms(12, 0, 1))

    chat_cli.main()

    captured = capsys.readouterr()
    assert captured.out == "Real answer\n"
    assert "checking the journal" not in captured.out


def test_main_persisted_chat_error_maps_to_chat_view(
    tmp_path, monkeypatch, capsys
) -> None:
    _install_main_fakes(
        monkeypatch,
        tmp_path,
        argv=["hello"],
        urlopen_item=_success_response(),
    )
    _append_chat_error(USE_ID, "chat_timeout")

    with pytest.raises(SystemExit) as exc_info:
        chat_cli.main()

    assert exc_info.value.code == 1
    captured = capsys.readouterr()
    assert captured.out == ""
    assert captured.err == "sol: chat took too long\n"


def test_main_empty_result_is_error(tmp_path, monkeypatch, capsys) -> None:
    _install_main_fakes(
        monkeypatch,
        tmp_path,
        argv=["hello"],
        urlopen_item=_success_response(),
    )
    _append_sol_message(USE_ID, "   ")

    with pytest.raises(SystemExit) as exc_info:
        chat_cli.main()

    assert exc_info.value.code == 1
    captured = capsys.readouterr()
    assert captured.out == ""
    assert captured.err == f"{chat_cli.EMPTY_ANSWER_MESSAGE}\n"


def test_main_queued_response_prints_busy_line(tmp_path, monkeypatch, capsys) -> None:
    _install_main_fakes(
        monkeypatch,
        tmp_path,
        argv=["hello"],
        urlopen_item=_success_response(queued=True),
    )
    _append_sol_message(USE_ID, "Queued answer")

    chat_cli.main()

    captured = capsys.readouterr()
    assert captured.out == "Queued answer\n"
    assert captured.err == f"{chat_cli.QUEUED_MESSAGE}\n"


def test_main_fast_answer_race_prints_no_live_progress_warning(
    tmp_path, monkeypatch, capsys
) -> None:
    _install_main_fakes(
        monkeypatch,
        tmp_path,
        argv=["hello"],
        urlopen_item=_success_response(),
    )
    _append_sol_message(USE_ID, "Fast answer")

    chat_cli.main()

    assert chat_cli.LIVE_PROGRESS_UNAVAILABLE_MESSAGE not in capsys.readouterr().err


def test_main_post_http_error_prints_error_response_body(
    tmp_path, monkeypatch, capsys
) -> None:
    _install_main_fakes(
        monkeypatch,
        tmp_path,
        argv=["hello"],
        urlopen_item=_http_error(
            {"error": "Missing required field.", "detail": "message is required"}
        ),
    )

    with pytest.raises(SystemExit) as exc_info:
        chat_cli.main()

    assert exc_info.value.code == 1
    captured = capsys.readouterr()
    assert captured.out == ""
    assert captured.err == "sol: Missing required field.\nsol: message is required\n"


def test_main_post_transport_error_uses_service_down_copy(
    tmp_path, monkeypatch, capsys
) -> None:
    _install_main_fakes(
        monkeypatch,
        tmp_path,
        argv=["hello"],
        urlopen_item=urllib.error.URLError("refused"),
    )

    with pytest.raises(SystemExit) as exc_info:
        chat_cli.main()

    assert exc_info.value.code == 1
    captured = capsys.readouterr()
    assert captured.out == ""
    assert captured.err == f"{chat_cli.SERVICE_DOWN_MESSAGE}\n"


def test_main_malformed_post_response_is_error(tmp_path, monkeypatch, capsys) -> None:
    _install_main_fakes(
        monkeypatch,
        tmp_path,
        argv=["hello"],
        urlopen_item=FakeResponse({}),
    )

    with pytest.raises(SystemExit) as exc_info:
        chat_cli.main()

    assert exc_info.value.code == 1
    captured = capsys.readouterr()
    assert captured.out == ""
    assert captured.err == f"sol: {chat_cli.MALFORMED_RESPONSE_MESSAGE}\n"


def test_main_idle_ceiling_uses_final_persisted_terminal_with_warning(
    tmp_path, monkeypatch, capsys
) -> None:
    _install_main_fakes(
        monkeypatch,
        tmp_path,
        argv=["hello"],
        urlopen_item=_success_response(),
    )
    monkeypatch.setattr(chat_cli, "POLL_SECONDS", 0.01)
    monkeypatch.setattr(chat_cli, "IDLE_CEILING_SECONDS", 0)
    calls = 0

    def persisted(use_id: str, day: str):
        nonlocal calls
        calls += 1
        if calls == 1:
            return None
        return {"kind": "finish", "result": "Recovered answer"}

    monkeypatch.setattr(chat_cli, "_persisted_terminal", persisted)

    chat_cli.main()

    captured = capsys.readouterr()
    assert captured.out == "Recovered answer\n"
    assert captured.err == f"{chat_cli.LIVE_PROGRESS_UNAVAILABLE_MESSAGE}\n"
    assert calls == 2


def test_main_idle_ceiling_without_persisted_terminal_loses_contact(
    tmp_path, monkeypatch, capsys
) -> None:
    _install_main_fakes(
        monkeypatch,
        tmp_path,
        argv=["hello"],
        urlopen_item=_success_response(),
    )
    monkeypatch.setattr(chat_cli, "POLL_SECONDS", 0.01)
    monkeypatch.setattr(chat_cli, "IDLE_CEILING_SECONDS", 0)
    monkeypatch.setattr(chat_cli, "_persisted_terminal", lambda use_id, day: None)

    with pytest.raises(SystemExit) as exc_info:
        chat_cli.main()

    assert exc_info.value.code == 1
    captured = capsys.readouterr()
    assert captured.out == ""
    assert captured.err == f"{chat_cli.LOST_CONTACT_MESSAGE}\n"


def test_main_rejects_removed_provider_and_talent_options(monkeypatch) -> None:
    monkeypatch.setattr(sys, "argv", ["sol chat", "--provider", "google", "hello"])
    with pytest.raises(SystemExit) as provider_exit:
        chat_cli.main()

    monkeypatch.setattr(sys, "argv", ["sol chat", "--talent", "chat", "hello"])
    with pytest.raises(SystemExit) as talent_exit:
        chat_cli.main()

    assert provider_exit.value.code == 2
    assert talent_exit.value.code == 2


def test_main_live_proxy_progress_and_finish(tmp_path, monkeypatch, capsys) -> None:
    posted = threading.Event()

    def urlopen_item(_request, _timeout):
        posted.set()
        return _success_response()

    _journal, instances, _calls = _install_main_fakes(
        monkeypatch,
        tmp_path,
        argv=["hello"],
        urlopen_item=urlopen_item,
    )
    monkeypatch.setattr(chat_cli, "POLL_SECONDS", 0.01)
    monkeypatch.setattr(chat_cli, "IDLE_CEILING_SECONDS", 5)
    outcome: dict[str, BaseException] = {}

    def run_main() -> None:
        try:
            chat_cli.main()
        except BaseException as exc:  # noqa: BLE001
            outcome["exc"] = exc

    worker = threading.Thread(target=run_main)
    worker.start()
    assert posted.wait(1)

    for _ in range(200):
        if instances and instances[0].callback is not None:
            callback = instances[0].callback
            callback(
                {
                    "tract": "cortex",
                    "event": "start",
                    "chat_proxy": True,
                    "use_id": USE_ID,
                }
            )
            callback(
                {
                    "tract": "cortex",
                    "event": "thinking",
                    "chat_proxy": True,
                    "use_id": USE_ID,
                    "summary": "working",
                }
            )
            callback(
                {
                    "tract": "cortex",
                    "event": "finish",
                    "chat_proxy": True,
                    "use_id": USE_ID,
                    "result": "Live answer",
                }
            )
        worker.join(0.01)
        if not worker.is_alive():
            break

    assert not worker.is_alive()
    assert outcome == {}
    captured = capsys.readouterr()
    assert captured.out == "Live answer\n"
    assert captured.err == "sol is thinking…\n"
