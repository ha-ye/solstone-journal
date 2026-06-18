# SPDX-License-Identifier: AGPL-3.0-only
# Copyright (c) 2026 sol pbc

"""CLI command for chatting with the journal agent."""

from __future__ import annotations

import argparse
import json
import sys
import threading
import time
from collections.abc import Iterable, Iterator
from typing import Any

import requests

from solstone.apps.chat.copy import (
    CHAT_LIVENESS_THINKING,
    talent_label_for,
)
from solstone.think.convey_client import (
    ConveyClient,
    ConveyClientError,
    ConveyUnreachableError,
    resolve_base_url,
)
from solstone.think.utils import setup_cli

POST_TIMEOUT_SECONDS = 10
POLL_SECONDS = 2
IDLE_CEILING_SECONDS = 240

SERVICE_DOWN_MESSAGE = (
    "sol: solstone isn't running. Start it with 'journal up' and retry."
)
QUEUED_MESSAGE = "Sol is busy right now — your message is queued."
LIVE_PROGRESS_UNAVAILABLE_MESSAGE = "Live progress was unavailable."
LOST_CONTACT_MESSAGE = (
    "sol: Lost contact with Sol before it finished — check 'journal doctor'."
)
EMPTY_ANSWER_MESSAGE = "sol: Sol returned an empty answer."
MALFORMED_RESPONSE_MESSAGE = "I couldn't read the chat response."
COMPOSING_MESSAGE = "Composing your answer…"


class _TimeoutSession(requests.Session):
    def request(self, method, url, **kwargs):
        kwargs.setdefault("timeout", POST_TIMEOUT_SECONDS)
        return super().request(method, url, **kwargs)


def _build_client(base_url: str) -> ConveyClient:
    return ConveyClient(
        base_url=base_url,
        require_service=False,
        session=_TimeoutSession(),
    )


def iter_sse_events(chunks: Iterable[bytes]) -> Iterator[dict]:
    buffer = ""
    data_lines: list[str] = []

    for chunk in chunks:
        buffer += chunk.decode("utf-8", errors="replace")
        while "\n" in buffer:
            line, buffer = buffer.split("\n", 1)
            if line.endswith("\r"):
                line = line[:-1]

            if line == "":
                if data_lines:
                    raw = "\n".join(data_lines)
                    try:
                        obj = json.loads(raw)
                    except json.JSONDecodeError:
                        pass
                    else:
                        if isinstance(obj, dict):
                            yield obj
                data_lines = []
                continue

            if line.startswith(":"):
                continue

            if line.startswith("data:"):
                value = line[len("data:") :]
                if value.startswith(" "):
                    value = value[1:]
                data_lines.append(value)


def _open_sse(base_url: str) -> Iterable[bytes] | None:
    url = base_url.rstrip("/") + "/sse/events"
    try:
        response = requests.get(
            url,
            stream=True,
            timeout=(POST_TIMEOUT_SECONDS, None),
        )
    except requests.exceptions.RequestException:
        return None
    return response.iter_content(chunk_size=None)


def _post_chat(
    client: ConveyClient,
    message: str,
    facet: str | None,
) -> tuple[str, bool]:
    payload = {"message": message}
    if facet:
        payload["facet"] = facet
    try:
        data = client.request("POST", "/api/chat", json=payload)
    except ConveyUnreachableError:
        raise
    except ConveyClientError as exc:
        if exc.status is not None and 200 <= exc.status < 300:
            raise ValueError(MALFORMED_RESPONSE_MESSAGE) from exc
        raise

    if not isinstance(data, dict):
        raise ValueError(MALFORMED_RESPONSE_MESSAGE)
    use_id = str(data.get("use_id") or "").strip()
    if not use_id:
        raise ValueError(MALFORMED_RESPONSE_MESSAGE)
    return use_id, bool(data.get("queued"))


def _render_post_error(exc: ConveyClientError) -> str:
    lines = [f"sol: {exc.error}"]
    detail = str(exc.detail or "").strip()
    if detail:
        lines.append(f"sol: {detail}")
    return "\n".join(lines)


def _origin_logical_use_id(message: dict) -> str:
    origin = message.get("origin")
    if not isinstance(origin, dict):
        return ""
    return str(origin.get("logical_use_id") or "")


def _is_fold_terminal_message(message: dict, use_id: str) -> bool:
    return (
        message.get("requested_target") is None
        and _origin_logical_use_id(message) == use_id
    )


def _session_terminal(client: ConveyClient, use_id: str) -> dict | None:
    try:
        data = client.request("GET", "/api/chat/session")
    except ConveyClientError:
        return None
    latest = (data or {}).get("latest_sol_message") or {}
    if str(latest.get("use_id") or "") != use_id and not _is_fold_terminal_message(
        latest,
        use_id,
    ):
        return None
    if latest.get("requested_target") is not None:
        return None
    return {"kind": "finish", "result": str(latest.get("text") or "")}


def _collapse_whitespace(value: Any) -> str:
    return " ".join(str(value or "").split())


def _truncate(text: str, limit: int) -> str:
    if len(text) <= limit:
        return text
    return text[: limit - 1] + "…"


def _task_suffix(task: Any) -> str:
    collapsed = _collapse_whitespace(task)
    if not collapsed:
        return ""
    return f" ({_truncate(collapsed, 100)})"


def _tool_name(event: dict) -> str:
    return _collapse_whitespace(event.get("tool")) or "unknown"


def _render_progress(event: dict, *, verbose: bool) -> str | None:
    tract = event.get("tract")
    event_name = event.get("event") or event.get("kind")

    if verbose:
        if tract == "cortex" and event_name == "start":
            provider = _collapse_whitespace(event.get("provider")) or "unknown"
            model = _collapse_whitespace(event.get("model")) or "unknown"
            return f"Provider: {provider}; model: {model}"
        if tract == "cortex" and event_name == "thinking":
            summary = _collapse_whitespace(event.get("summary"))
            if summary:
                return f"Thinking: {_truncate(summary, 200)}"
        if tract == "cortex" and event_name == "tool_end":
            return f"· {_tool_name(event)} done"
        return None

    if tract == "cortex":
        if event_name in {"start", "thinking"}:
            return CHAT_LIVENESS_THINKING
        if event_name == "tool_start":
            return f"· {_tool_name(event)}"
        return None

    if tract == "chat" and event_name == "sol_message":
        target = event.get("requested_target")
        try:
            label = talent_label_for(target, "running")
        except ValueError:
            return None
        return label + _task_suffix(event.get("requested_task"))

    if tract == "chat" and event_name == "talent_finished":
        return COMPOSING_MESSAGE

    return None


def _terminal_error_message(
    reason: str,
    provider: str,
) -> str:
    from solstone.convey.provider_readiness import chat_view

    rendered = chat_view(reason or "unknown", provider or "")
    return str(rendered.get("message") or reason or "unknown")


def _print_stderr(message: str) -> None:
    print(message, file=sys.stderr)


def main() -> None:
    """Entry point for ``sol chat``."""
    parser = argparse.ArgumentParser(
        prog="sol chat",
        description="Chat with your journal",
    )
    parser.add_argument("message", nargs="*", help="Chat message")
    parser.add_argument("--facet", help="Facet context")
    args = setup_cli(parser)

    from solstone.think.identity import ensure_identity_directory

    ensure_identity_directory()

    if not args.message:
        parser.print_help()
        return

    message = " ".join(args.message).strip()
    base_url = resolve_base_url()
    client = _build_client(base_url)
    state: dict[str, Any] = {
        "use_id": None,
        "last_event_at": time.monotonic(),
        "terminal": None,
        "last_progress": None,
    }
    lock = threading.Lock()
    done = threading.Event()
    sse_ended = threading.Event()

    def set_terminal(terminal: dict[str, str]) -> None:
        with lock:
            if not done.is_set():
                state["terminal"] = terminal
                done.set()

    def emit_progress(line: str | None) -> None:
        if not line:
            return
        should_print = False
        with lock:
            if line != state["last_progress"]:
                state["last_progress"] = line
                should_print = True
        if should_print:
            _print_stderr(line)

    def render_event_progress(msg: dict[str, Any]) -> None:
        emit_progress(_render_progress(msg, verbose=False))
        if args.verbose:
            emit_progress(_render_progress(msg, verbose=True))

    def callback(msg: dict) -> None:
        event_tract = msg.get("tract")
        event_name = msg.get("event")
        with lock:
            logical_use_id = state["use_id"]

        if (
            event_tract == "cortex"
            and msg.get("chat_proxy") is True
            and logical_use_id is not None
            and msg.get("use_id") == logical_use_id
        ):
            with lock:
                state["last_event_at"] = time.monotonic()
            if event_name == "finish":
                set_terminal(
                    {
                        "kind": "finish",
                        "result": str(msg.get("result") or ""),
                    }
                )
                return
            if event_name == "error":
                set_terminal(
                    {
                        "kind": "error",
                        "reason": str(msg.get("error") or "unknown"),
                        "provider": str(msg.get("provider") or ""),
                        "detail": str(msg.get("detail") or ""),
                    }
                )
                return
            render_event_progress(msg)
            return

        if event_tract != "chat":
            return

        if logical_use_id is not None and msg.get("use_id") == logical_use_id:
            with lock:
                state["last_event_at"] = time.monotonic()
            if event_name == "sol_message" and msg.get("requested_target"):
                render_event_progress(msg)
            return

        if (
            logical_use_id is not None
            and event_name == "sol_message"
            and _is_fold_terminal_message(msg, logical_use_id)
        ):
            with lock:
                state["last_event_at"] = time.monotonic()
            set_terminal(
                {
                    "kind": "finish",
                    "result": str(msg.get("text") or ""),
                }
            )
            return

        if logical_use_id is not None and event_name == "talent_finished":
            with lock:
                state["last_event_at"] = time.monotonic()
            render_event_progress(msg)

    sse_chunks = _open_sse(base_url)
    try:
        use_id, queued = _post_chat(client, message, args.facet)
    except ConveyUnreachableError:
        _print_stderr(SERVICE_DOWN_MESSAGE)
        sys.exit(1)
    except ConveyClientError as exc:
        _print_stderr(_render_post_error(exc))
        sys.exit(1)
    except ValueError as exc:
        _print_stderr(f"sol: {exc}")
        sys.exit(1)

    with lock:
        state["use_id"] = use_id
        state["last_event_at"] = time.monotonic()
    if queued:
        emit_progress(QUEUED_MESSAGE)

    def reader() -> None:
        try:
            for event in iter_sse_events(sse_chunks or []):
                if done.is_set():
                    return
                callback(event)
        finally:
            if not done.is_set():
                sse_ended.set()

    reader_thread = threading.Thread(target=reader, daemon=True)
    reader_thread.start()

    try:
        while not done.wait(POLL_SECONDS):
            with lock:
                idle_for = time.monotonic() - float(state["last_event_at"])
            if sse_ended.is_set() or idle_for >= IDLE_CEILING_SECONDS:
                terminal = _session_terminal(client, use_id)
                if terminal is not None:
                    _print_stderr(LIVE_PROGRESS_UNAVAILABLE_MESSAGE)
                    set_terminal(terminal)
                    break
                if idle_for >= IDLE_CEILING_SECONDS:
                    set_terminal({"kind": "lost_contact"})
                    break
    except KeyboardInterrupt:
        _print_stderr("\nInterrupted.")
        sys.exit(1)

    with lock:
        terminal = state["terminal"]

    if terminal is None or terminal.get("kind") == "lost_contact":
        _print_stderr(LOST_CONTACT_MESSAGE)
        sys.exit(1)

    if terminal.get("kind") == "finish":
        result = str(terminal.get("result") or "")
        if not result.strip():
            _print_stderr(EMPTY_ANSWER_MESSAGE)
            sys.exit(1)
        print(result)
        return

    if terminal.get("kind") == "error":
        reason = str(terminal.get("reason") or "unknown")
        provider = str(terminal.get("provider") or "")
        message = _terminal_error_message(reason, provider)
        _print_stderr(f"sol: {message}")
        sys.exit(1)

    _print_stderr(LOST_CONTACT_MESSAGE)
    sys.exit(1)
