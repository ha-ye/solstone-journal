# SPDX-License-Identifier: AGPL-3.0-only
# Copyright (c) 2026 sol pbc

"""CLI command for chatting with the journal agent."""

from __future__ import annotations

import argparse
import json
import sys
import threading
import time
import urllib.error
import urllib.request
from datetime import datetime
from typing import Any

from solstone.apps.chat.copy import (
    CHAT_LIVENESS_THINKING,
    talent_label_for,
)
from solstone.convey.chat_stream import read_chat_events
from solstone.convey.provider_readiness import chat_view
from solstone.think.callosum import CallosumConnection
from solstone.think.utils import read_service_port, require_solstone, setup_cli

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


def _today() -> str:
    return datetime.now().strftime("%Y%m%d")


def _post_chat(message: str, facet: str | None) -> tuple[str, bool]:
    port = read_service_port("convey")
    if port is None:
        raise urllib.error.URLError("convey port unavailable")

    payload = {"message": message}
    if facet:
        payload["facet"] = facet
    body = json.dumps(payload).encode("utf-8")
    request = urllib.request.Request(
        f"http://127.0.0.1:{port}/api/chat",
        data=body,
        headers={"Content-Type": "application/json"},
        method="POST",
    )
    with urllib.request.urlopen(request, timeout=POST_TIMEOUT_SECONDS) as response:
        response_body = response.read().decode("utf-8", errors="replace")

    try:
        data = json.loads(response_body)
    except ValueError:
        raise ValueError(MALFORMED_RESPONSE_MESSAGE)
    if not isinstance(data, dict):
        raise ValueError(MALFORMED_RESPONSE_MESSAGE)
    use_id = str(data.get("use_id") or "").strip()
    if not use_id:
        raise ValueError(MALFORMED_RESPONSE_MESSAGE)
    return use_id, bool(data.get("queued"))


def _http_error_message(exc: urllib.error.HTTPError) -> str:
    raw_body = exc.read().decode("utf-8", errors="replace").strip()
    status_text = str(
        getattr(exc, "reason", "") or getattr(exc, "msg", "") or f"HTTP {exc.code}"
    )
    try:
        payload = json.loads(raw_body or "{}")
    except ValueError:
        return f"sol: {status_text}"

    if not isinstance(payload, dict):
        return f"sol: {status_text}"

    error = str(payload.get("error") or payload.get("reason_code") or status_text)
    lines = [f"sol: {error}"]
    detail = str(payload.get("detail") or "").strip()
    if detail:
        lines.append(f"sol: {detail}")
    return "\n".join(lines)


def _persisted_terminal(use_id: str, day: str) -> dict[str, str] | None:
    terminal: dict[str, str] | None = None
    for event in read_chat_events(day):
        if str(event.get("use_id") or "") != use_id:
            continue

        kind = event.get("kind")
        if kind == "sol_message":
            if event.get("requested_target") is None:
                terminal = {
                    "kind": "finish",
                    "result": str(event.get("text") or ""),
                }
            continue

        if kind == "chat_error":
            terminal = {
                "kind": "error",
                "reason": str(event.get("reason") or "unknown"),
                "provider": str(event.get("provider") or ""),
                "detail": str(event.get("detail") or ""),
            }
    return terminal


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
    *,
    use_id: str,
    day: str,
) -> str:
    resolved_provider = provider or ""
    if not resolved_provider and use_id:
        persisted = _persisted_terminal(use_id, day)
        if persisted and persisted.get("kind") == "error":
            resolved_provider = persisted.get("provider", "")
    rendered = chat_view(reason or "unknown", resolved_provider)
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
    require_solstone()

    from solstone.think.identity import ensure_identity_directory

    ensure_identity_directory()

    if not args.message:
        parser.print_help()
        return

    message = " ".join(args.message).strip()
    state: dict[str, Any] = {
        "use_id": None,
        "last_event_at": time.monotonic(),
        "terminal": None,
        "last_progress": None,
    }
    lock = threading.Lock()
    done = threading.Event()
    day = _today()

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

        if logical_use_id is not None and event_name == "talent_finished":
            with lock:
                state["last_event_at"] = time.monotonic()
            render_event_progress(msg)

    listener = CallosumConnection()
    listener.start(callback=callback)
    try:
        try:
            use_id, queued = _post_chat(message, args.facet)
        except urllib.error.HTTPError as exc:
            _print_stderr(_http_error_message(exc))
            sys.exit(1)
        except (urllib.error.URLError, OSError, TimeoutError):
            _print_stderr(SERVICE_DOWN_MESSAGE)
            sys.exit(1)
        except ValueError as exc:
            _print_stderr(f"sol: {exc}")
            sys.exit(1)

        with lock:
            state["use_id"] = use_id
            state["last_event_at"] = time.monotonic()
        if queued:
            emit_progress(QUEUED_MESSAGE)

        terminal = _persisted_terminal(use_id, day)
        if terminal is not None:
            set_terminal(terminal)

        try:
            while not done.wait(POLL_SECONDS):
                with lock:
                    idle_for = time.monotonic() - float(state["last_event_at"])
                if idle_for < IDLE_CEILING_SECONDS:
                    continue

                terminal = _persisted_terminal(use_id, day)
                if terminal is not None:
                    _print_stderr(LIVE_PROGRESS_UNAVAILABLE_MESSAGE)
                    set_terminal(terminal)
                else:
                    set_terminal({"kind": "lost_contact"})
                break
        except KeyboardInterrupt:
            _print_stderr("\nInterrupted.")
            sys.exit(1)
    finally:
        listener.stop()

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
        message = _terminal_error_message(
            reason,
            provider,
            use_id=str(state["use_id"] or ""),
            day=day,
        )
        _print_stderr(f"sol: {message}")
        sys.exit(1)

    _print_stderr(LOST_CONTACT_MESSAGE)
    sys.exit(1)
