# SPDX-License-Identifier: AGPL-3.0-only
# Copyright (c) 2026 sol pbc

"""Shared utilities and types for AI providers.

This module contains:
- Event TypedDicts emitted by providers during talent execution
- GenerateResult TypedDict returned by run_generate/run_agenerate
- JSONEventCallback for event emission
- Utility functions for common provider operations
"""

from __future__ import annotations

import json
from typing import Any, Callable, Literal, Optional, Union

from typing_extensions import Required, TypedDict

from solstone.think.utils import now_ms

# ---------------------------------------------------------------------------
# Event Types
# ---------------------------------------------------------------------------


class ToolStartEvent(TypedDict, total=False):
    """Event emitted when a tool starts."""

    event: Literal["tool_start"]
    ts: int
    tool: str
    args: Optional[dict[str, Any]]
    call_id: Optional[str]  # Unique ID to pair with tool_end event
    raw: Optional[list[dict[str, Any]]]  # Original provider JSON event(s)


class ToolEndEvent(TypedDict, total=False):
    """Event emitted when a tool finishes."""

    event: Literal["tool_end"]
    ts: int
    tool: str
    args: Optional[dict[str, Any]]
    result: Any
    call_id: Optional[str]  # Matches the call_id from tool_start
    raw: Optional[list[dict[str, Any]]]  # Original provider JSON event(s)


class StartEvent(TypedDict, total=False):
    """Event emitted when a talent run begins."""

    event: Required[Literal["start"]]
    ts: Required[int]
    prompt: Required[str]
    name: Required[str]
    model: Required[str]
    provider: Required[str]
    session_id: Optional[str]  # solstone-owned session ID for continuation
    chat_id: Optional[str]  # Chat ID for reverse lookup
    raw: Optional[list[dict[str, Any]]]  # Original provider JSON event(s)


class FinishEvent(TypedDict, total=False):
    """Event emitted when a talent run finishes successfully."""

    event: Required[Literal["finish"]]
    ts: Required[int]
    result: Required[str]
    usage: Optional[dict[str, Any]]
    cli_session_id: Optional[
        str
    ]  # solstone-owned session ID persisted under journal/.cache/cogitate-history/
    raw: Optional[list[dict[str, Any]]]  # Original provider JSON event(s)


class ErrorEvent(TypedDict, total=False):
    """Event emitted when an error occurs."""

    event: Literal["error"]
    ts: int
    error: str
    reason: Optional[str]
    reason_code: Optional[str]
    provider: Optional[str]
    trace: Optional[str]
    reset_at_ms: Optional[int]
    terminal: Optional[bool]
    raw: Optional[list[dict[str, Any]]]  # Original provider JSON event(s)


class TalentUpdatedEvent(TypedDict, total=False):
    """Event emitted when the talent context changes."""

    event: Required[Literal["talent_updated"]]
    ts: Required[int]
    talent: Required[str]
    raw: Optional[list[dict[str, Any]]]  # Original provider JSON event(s)


class ThinkingEvent(TypedDict, total=False):
    """Event emitted when thinking/reasoning summaries are available.

    For Anthropic models, may include a signature for verification when
    passing thinking blocks back during tool use continuations.
    For redacted thinking, summary will contain "[redacted]" and
    redacted_data will contain the encrypted content.
    """

    event: Required[Literal["thinking"]]
    ts: Required[int]
    summary: Required[str]
    model: Optional[str]
    signature: Optional[str]  # Anthropic thinking block signature
    redacted_data: Optional[str]  # Encrypted data for redacted thinking
    raw: Optional[list[dict[str, Any]]]  # Original provider JSON event(s)


class TextDeltaEvent(TypedDict, total=False):
    """Event emitted when streamed text content is available."""

    event: Required[Literal["text_delta"]]
    ts: Required[int]
    delta: Required[str]
    model: Optional[str]
    raw: Optional[list[dict[str, Any]]]  # Original provider JSON event(s)


class FallbackEvent(TypedDict, total=False):
    """Event emitted when provider fallback occurs."""

    event: Required[Literal["fallback"]]
    ts: Required[int]
    original_provider: Required[str]
    backup_provider: Required[str]
    reason: Required[str]  # "preflight" or "on_failure"
    error: Optional[str]  # Error message for on_failure case


Event = Union[
    ToolStartEvent,
    ToolEndEvent,
    StartEvent,
    FinishEvent,
    ErrorEvent,
    ThinkingEvent,
    TextDeltaEvent,
    TalentUpdatedEvent,
    FallbackEvent,
]


# ---------------------------------------------------------------------------
# Provider Error Classification
# ---------------------------------------------------------------------------

_CLI_UNAVAILABLE_PATTERNS = ("not installed", "command not found", "missing")
_CLI_TIMEOUT_PATTERNS = ("timed out", "timeout")
_CLI_AUTH_PATTERNS = (
    "authentication",
    "unauthorized",
    " 401",
    " 403",
    "401 ",
    "403 ",
    "401:",
    "403:",
    "permission denied",
    "forbidden",
    "invalid api key",
)
_CONTEXT_WINDOW_PATTERNS = (
    "exceeds the available context size",
    "context size has been exceeded",
    "exceeds the context window",
    "maximum context length",
    "context length exceeded",
)


def _status_code(exc: BaseException) -> int | None:
    for attr in ("status_code", "_status_code", "code"):
        value = getattr(exc, attr, None)
        if isinstance(value, int):
            return value
    response = getattr(exc, "response", None)
    value = getattr(response, "status_code", None)
    return value if isinstance(value, int) else None


def _status_text(exc: BaseException) -> str:
    return str(
        getattr(exc, "status", "")
        or getattr(exc, "_status", "")
        or getattr(exc, "_status_text", "")
        or ""
    ).upper()


def _contains_any(text: str, patterns: tuple[str, ...]) -> bool:
    return any(pattern in text for pattern in patterns)


def _module_matches(module_name: str, package: str) -> bool:
    return module_name == package or module_name.startswith(f"{package}.")


def _exception_name_matches(
    exc_name: str, exc_qualname: str, names: tuple[str, ...]
) -> bool:
    return exc_name in names or any(exc_qualname.endswith(f".{name}") for name in names)


RUNTIME_REASON_CODES = frozenset(
    {
        "context_window_exceeded",
        "context_budget_exceeded",
        "local_capacity_exhausted",
        "provider_quota_exceeded",
        "provider_key_invalid",
        "chat_timeout",
        "local_queue_timeout",
        "max_turns_exhausted",
        "network_unreachable",
        "provider_unavailable",
        "provider_response_invalid",
        "incomplete_json_length",
        "incomplete_text_length",
        "unknown",
    }
)


def classify_provider_error(exc: BaseException, provider: str) -> str:
    """Return a chat reason code for a provider exception."""
    try:
        exc_name = type(exc).__name__
        exc_qualname = type(exc).__qualname__
        exc_module = type(exc).__module__
        exc_name_lower = exc_name.lower()
        exc_identity_lower = f"{exc_module}.{exc_qualname}".lower()
        message_lower = str(exc).lower()
        explicit_reason_code = getattr(exc, "reason_code", None)
        if isinstance(explicit_reason_code, str) and explicit_reason_code:
            return explicit_reason_code

        if exc_name == "QuotaExhaustedError":
            return "provider_quota_exceeded"
        if exc_name == "ContextWindowExceededError":
            return "context_window_exceeded"
        if _contains_any(message_lower, _CONTEXT_WINDOW_PATTERNS):
            return "context_window_exceeded"
        # MaxIterationsReached is OpenHands' event-code spelling of the same limit;
        # our MaxTurnsExhausted (cogitate_policy) is what actually reaches here.
        if exc_name in ("MaxTurnsExhausted", "MaxIterationsReached"):
            return "max_turns_exhausted"

        if isinstance(exc, ValueError) and "no response from model" in message_lower:
            return "provider_response_invalid"

        is_anthropic = _module_matches(exc_module, "anthropic")
        is_openai = _module_matches(exc_module, "openai")
        is_google = _module_matches(exc_module, "google.genai")
        is_httpx = _module_matches(exc_module, "httpx")
        status_code = _status_code(exc)
        status_text = _status_text(exc)

        if (is_anthropic or is_openai) and _exception_name_matches(
            exc_name,
            exc_qualname,
            ("AuthenticationError", "PermissionDeniedError"),
        ):
            return "provider_key_invalid"
        if (
            is_google
            and _exception_name_matches(exc_name, exc_qualname, ("ClientError",))
            and status_code in (401, 403)
        ):
            return "provider_key_invalid"

        if (is_anthropic or is_openai) and _exception_name_matches(
            exc_name, exc_qualname, ("RateLimitError",)
        ):
            return "provider_quota_exceeded"
        if (
            is_google
            and _exception_name_matches(exc_name, exc_qualname, ("ClientError",))
            and (status_code == 429 or status_text == "RESOURCE_EXHAUSTED")
        ):
            return "provider_quota_exceeded"

        if (is_anthropic or is_openai) and _exception_name_matches(
            exc_name, exc_qualname, ("APITimeoutError",)
        ):
            return "chat_timeout"
        if is_httpx and (
            "timeout" in exc_name_lower
            or _exception_name_matches(
                exc_name,
                exc_qualname,
                (
                    "TimeoutException",
                    "ConnectTimeout",
                    "PoolTimeout",
                    "ReadTimeout",
                    "WriteTimeout",
                ),
            )
        ):
            return "chat_timeout"

        if (is_anthropic or is_openai) and _exception_name_matches(
            exc_name, exc_qualname, ("APIConnectionError",)
        ):
            return "network_unreachable"
        if is_httpx and (
            _exception_name_matches(
                exc_name,
                exc_qualname,
                ("NetworkError", "RequestError", "ConnectError"),
            )
            or "connection" in exc_name_lower
            or "connect" in exc_name_lower
        ):
            return "network_unreachable"
        if isinstance(exc, ConnectionError):
            return "network_unreachable"

        if is_openai and _exception_name_matches(
            exc_name, exc_qualname, ("InternalServerError",)
        ):
            return "provider_unavailable"
        if is_google and _exception_name_matches(
            exc_name, exc_qualname, ("ServerError",)
        ):
            return "provider_unavailable"
        if (
            (
                (is_anthropic or is_openai)
                and _exception_name_matches(exc_name, exc_qualname, ("APIStatusError",))
            )
            or (
                is_httpx
                and _exception_name_matches(
                    exc_name, exc_qualname, ("HTTPStatusError",)
                )
            )
        ) and (status_code or 0) >= 500:
            return "provider_unavailable"

        if is_google and _exception_name_matches(
            exc_name, exc_qualname, ("UnknownApiResponseError",)
        ):
            return "provider_response_invalid"

        if isinstance(exc, RuntimeError):
            if _contains_any(message_lower, _CLI_UNAVAILABLE_PATTERNS):
                return "provider_unavailable"
            if _contains_any(message_lower, _CLI_TIMEOUT_PATTERNS):
                return "chat_timeout"
            if _contains_any(message_lower, _CLI_AUTH_PATTERNS):
                return "provider_key_invalid"
            return "unknown"

        if (
            "authenticationerror" in exc_name_lower
            or "permissiondeniederror" in exc_name_lower
            or "unauthorized" in exc_name_lower
            or "forbidden" in exc_name_lower
        ):
            return "provider_key_invalid"
        if (
            "ratelimit" in exc_name_lower
            or "toomanyrequests" in exc_name_lower
            or "resourceexhausted" in exc_name_lower
        ):
            return "provider_quota_exceeded"
        if "timeout" in exc_name_lower:
            return "chat_timeout"
        if "connection" in exc_name_lower or "network" in exc_name_lower:
            return "network_unreachable"
        if (
            "responsevalidation" in exc_name_lower
            or "unknownapiresponse" in exc_identity_lower
        ):
            return "provider_response_invalid"
        if "internalservererror" in exc_name_lower or "servererror" in exc_name_lower:
            return "provider_unavailable"

        return "unknown"
    except Exception:
        return "unknown"


# ---------------------------------------------------------------------------
# Usage Schema
# ---------------------------------------------------------------------------

# Canonical keys for the normalized usage dict returned by all providers.
# log_token_usage() passes through exactly these keys (when present and non-zero).
USAGE_KEYS = frozenset(
    {
        "input_tokens",
        "output_tokens",
        "total_tokens",
        "cached_tokens",
        "reasoning_tokens",
        "cache_creation_tokens",
        "requests",
    }
)

# ---------------------------------------------------------------------------
# GenerateResult
# ---------------------------------------------------------------------------


class GenerateResult(TypedDict, total=False):
    """Result from provider run_generate/run_agenerate functions.

    Structured result that allows the wrapper to handle cross-cutting concerns
    like token logging and JSON validation centrally.

    The thinking field contains dicts with: summary (str), signature (optional str),
    redacted_data (optional str for Anthropic redacted thinking).
    """

    text: Required[str]  # Response text
    model: Optional[str]  # Resolved model identifier when provider returns one
    usage: Optional[dict]  # Normalized usage dict (input_tokens, output_tokens, etc.)
    finish_reason: Optional[str]  # Normalized: "stop", "max_tokens", "safety", etc.
    thinking: Optional[list]  # List of thinking block dicts
    schema_validation: Optional[dict]  # Validation result when json_schema is supplied
    input_budget: Optional[
        dict
    ]  # Out-of-band truncation metadata when the bundled-local input was clipped
    inference: Optional[dict]  # Content-free local inference timing/admission record


# ---------------------------------------------------------------------------
# JSONEventCallback
# ---------------------------------------------------------------------------


class JSONEventCallback:
    """Emit JSON events via a callback."""

    def __init__(self, callback: Optional[Callable[[Event], None]] = None) -> None:
        self.callback = callback

    def emit(self, data: Event) -> None:
        if "ts" not in data:
            data = {**data, "ts": now_ms()}
        if self.callback:
            self.callback(data)

    def close(self) -> None:
        pass


# ---------------------------------------------------------------------------
# Raw Event Trimming
# ---------------------------------------------------------------------------

# Structural keys preserved when trimming oversized raw events.
_RAW_STRUCTURAL_KEYS = frozenset(
    {
        "type",
        "id",
        "tool_id",
        "tool_name",
        "role",
        "event_type",
        "timestamp",
    }
)

_RAW_BYTE_LIMIT = 16_384  # 16 KB


def safe_raw(
    events: list[dict[str, Any]],
    limit: int = _RAW_BYTE_LIMIT,
) -> list[dict[str, Any]]:
    """Return *events* as-is if small enough, otherwise a trimmed version.

    When the JSON-serialized size exceeds *limit* bytes, each event is reduced
    to its structural keys and a ``_raw_trimmed`` dict is appended with the
    original byte count and the limit that was applied.
    """
    serialized = json.dumps(events, ensure_ascii=False)
    if len(serialized.encode("utf-8")) <= limit:
        return events

    trimmed = [
        {k: v for k, v in e.items() if k in _RAW_STRUCTURAL_KEYS} for e in events
    ]
    trimmed.append(
        {"_raw_trimmed": {"original_bytes": len(serialized), "limit": limit}}
    )
    return trimmed


__all__ = [
    "Event",
    "GenerateResult",
    "JSONEventCallback",
    "RUNTIME_REASON_CODES",
    "ThinkingEvent",
    "USAGE_KEYS",
    "classify_provider_error",
    "safe_raw",
]
