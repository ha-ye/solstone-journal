# SPDX-License-Identifier: AGPL-3.0-only
# Copyright (c) 2026 sol pbc

"""Pre-hook: provide template vars for chat prompt context."""

from __future__ import annotations

import logging
from datetime import date, datetime
from typing import Any

from solstone.convey.chat_stream import read_chat_tail, reduce_chat_state
from solstone.convey.sol_initiated.copy import (
    KIND_SOL_CHAT_REQUEST,
    TRIGGER_LABEL_SOL_INITIATED,
)

logger = logging.getLogger(__name__)
STOP_AND_REPORT_CONTRACT = (
    "stop-and-report turn, not a dispatch turn. Do not retry this task or request "
    "another talent for it. Stop here and report to the owner directly using the "
    "{result_field_label} below."
)


def pre_process(context: dict) -> dict:
    """Build chat-context template vars for the chat talent prompt."""

    trigger_kind, trigger_payload = _normalize_trigger(context)
    day = _resolve_day(context, trigger_payload)
    template_vars = {
        "active_talents": "",
        "trigger_kind": "",
        "trigger_context": "",
        "summary": "",
        "message": None,
        "category": "",
        "since_ts": "",
        "trigger_talent": "",
        "location": "",
        "situational": "",
    }
    result = {"template_vars": template_vars}

    messages: list[dict[str, str]] = []
    source_context = ""
    latest_owner_message: dict[str, Any] | None = None
    try:
        tail = read_chat_tail(day, limit=20)
        for event in tail:
            if event["kind"] == "owner_message":
                latest_owner_message = event
                messages.append({"role": "user", "content": event["text"]})
            elif event["kind"] == "sol_message":
                messages.append({"role": "assistant", "content": event["text"]})

        if latest_owner_message and "source" in latest_owner_message:
            source = latest_owner_message["source"]
            template_vars["source"] = source
            if isinstance(source, dict) and source.get("kind") == "needs_you":
                item_text = source.get("item_text")
                if isinstance(item_text, str) and item_text.strip():
                    source_context = (
                        "The owner reached this conversation from their Needs You "
                        f'tile: "{item_text}". Be useful on this topic -- no need '
                        "to call out where it came from."
                    )

        terminal_followup = _render_terminal_followup(trigger_kind, trigger_payload)
        if terminal_followup:
            messages.append({"role": "user", "content": terminal_followup})
    except Exception:
        logger.debug("Chat tail enrichment failed", exc_info=True)

    if trigger_kind == "owner_message":
        trigger_text = str(trigger_payload.get("text") or "").strip()
        if trigger_text and (
            not messages
            or messages[-1].get("role") != "user"
            or messages[-1].get("content") != trigger_text
        ):
            messages.append({"role": "user", "content": trigger_text})

    if messages:
        result["messages"] = messages

    try:
        state = reduce_chat_state(day)
        template_vars["active_talents"] = _render_active_talents(
            state.get("active_talents", [])
        )
    except Exception:
        logger.debug("Active talent enrichment failed", exc_info=True)

    template_vars["situational"] = _render_situational(day)

    _apply_trigger_template_vars(template_vars, trigger_kind, trigger_payload)
    trigger_context = _render_trigger_context(trigger_kind, trigger_payload, context)
    if source_context:
        trigger_context = (
            f"{trigger_context}\n\n{source_context}"
            if trigger_context
            else source_context
        )
    template_vars["trigger_context"] = trigger_context
    template_vars["location"] = _render_location(trigger_payload, context)

    return result


def _render_situational(day: str) -> str:
    """Compact situational read from the latest pulse record."""
    try:
        from solstone.think.day_accumulator import read_latest

        record = read_latest(day, "pulse")
    except Exception:
        logger.debug("Situational pulse enrichment failed", exc_info=True)
        return ""
    if not record:
        return ""

    lines = ["## Situational awareness\n"]
    title = str(record.get("title") or "").strip()
    one = str(record.get("one_sentence") or "").strip()
    details = str(record.get("full_details") or "").strip()
    if title:
        lines.append(f"**{title}**")
    if one:
        lines.append(one)
    if details:
        lines.append(f"\n{details}")

    needs = [str(n).strip() for n in record.get("needs_you", []) if str(n).strip()]
    if needs:
        lines.append("\nNeeds the owner:")
        lines.extend(f"- {need}" for need in needs)
    return "\n".join(lines)


def _normalize_trigger(context: dict) -> tuple[str | None, dict[str, Any]]:
    trigger_info = context.get("trigger")
    kind = None
    payload: dict[str, Any] = {}

    if isinstance(trigger_info, dict):
        kind = trigger_info.get("type")
        payload.update({k: v for k, v in trigger_info.items() if k != "type"})

    location = context.get("location")
    if isinstance(location, dict):
        if "app" not in payload and location.get("app"):
            payload["app"] = location["app"]
        if "path" not in payload and location.get("path"):
            payload["path"] = location["path"]
        if "facet" not in payload and location.get("facet"):
            payload["facet"] = location["facet"]

    if "facet" not in payload and context.get("facet"):
        payload["facet"] = context["facet"]
    if "app" not in payload and context.get("app"):
        payload["app"] = context["app"]
    if "path" not in payload and context.get("path"):
        payload["path"] = context["path"]

    if not kind and context.get("prompt"):
        kind = "owner_message"
    if kind == "owner_message" and "text" not in payload:
        if payload.get("message"):
            payload["text"] = payload["message"]
        elif context.get("prompt"):
            payload["text"] = context["prompt"]
    if kind == KIND_SOL_CHAT_REQUEST:
        return kind, payload

    return kind, payload


def _terminal_result_details(
    trigger_kind: str | None,
    payload: dict[str, Any],
) -> tuple[str, str, str] | None:
    if trigger_kind == "talent_finished":
        return "finished", "result", str(payload.get("summary") or "")
    if trigger_kind == "talent_errored":
        return "errored", "reason", str(payload.get("reason") or "")
    return None


def _render_terminal_followup(
    trigger_kind: str | None,
    payload: dict[str, Any],
) -> str:
    details = _terminal_result_details(trigger_kind, payload)
    if details is None:
        return ""
    kind_label, result_field_label, result_value = details
    field_title = result_field_label.capitalize()
    contract = STOP_AND_REPORT_CONTRACT.format(result_field_label=result_field_label)
    return (
        "[internal follow-up: talent "
        f"{payload.get('name', 'exec')} {kind_label}. This is a {contract} "
        f"{field_title}: {result_value}]"
    )


def _resolve_day(context: dict, trigger_payload: dict[str, Any]) -> str:
    day = context.get("day")
    if isinstance(day, str) and len(day) == 8 and day.isdigit():
        return day

    ts_value = trigger_payload.get("ts")
    if isinstance(ts_value, int):
        return datetime.fromtimestamp(ts_value / 1000).strftime("%Y%m%d")

    return date.today().strftime("%Y%m%d")


def _render_active_talents(active_talents: list[dict[str, Any]]) -> str:
    if not active_talents:
        return ""

    lines = ["## Active Talents\n"]
    for talent in active_talents:
        started_at = _format_started_at(talent.get("started_at"))
        line = f"- **{talent.get('name', 'exec')}** — {talent.get('task', '')}"
        if started_at:
            line += f" (started {started_at})"
        lines.append(line)
    return "\n".join(lines)


def _format_started_at(value: Any) -> str:
    if not isinstance(value, int):
        return ""
    return datetime.fromtimestamp(value / 1000).strftime("%Y-%m-%d %H:%M")


def _render_trigger_context(
    trigger_kind: str | None,
    payload: dict[str, Any],
    context: dict[str, Any],
) -> str:
    if not trigger_kind:
        return ""
    if trigger_kind == "owner_message":
        return ""

    lines = ["## Trigger Context\n", f"- Type: {_prompt_trigger_kind(trigger_kind)}"]
    if trigger_kind == KIND_SOL_CHAT_REQUEST:
        _append_sol_request_trigger_context(lines, payload)
    elif trigger_kind == "talent_finished":
        _append_terminal_trigger_context(lines, trigger_kind, payload)
    elif trigger_kind == "talent_errored":
        _append_terminal_trigger_context(lines, trigger_kind, payload)
    else:
        if payload:
            for key, value in payload.items():
                lines.append(f"- {key}: {value}")

    return "\n".join(lines)


def _prompt_trigger_kind(trigger_kind: str | None) -> str:
    if trigger_kind == KIND_SOL_CHAT_REQUEST:
        return TRIGGER_LABEL_SOL_INITIATED
    return str(trigger_kind or "")


def _apply_trigger_template_vars(
    template_vars: dict[str, Any],
    trigger_kind: str | None,
    payload: dict[str, Any],
) -> None:
    template_vars["trigger_kind"] = _prompt_trigger_kind(trigger_kind)
    if trigger_kind != KIND_SOL_CHAT_REQUEST:
        return
    template_vars["summary"] = str(payload.get("summary") or "")
    message = payload.get("message")
    template_vars["message"] = str(message) if message is not None else None
    template_vars["category"] = str(payload.get("category") or "")
    since_ts = payload.get("since_ts")
    template_vars["since_ts"] = since_ts if isinstance(since_ts, int) else ""
    template_vars["trigger_talent"] = str(payload.get("trigger_talent") or "")


def _append_sol_request_trigger_context(
    lines: list[str],
    payload: dict[str, Any],
) -> None:
    summary = str(payload.get("summary") or "").strip()
    if summary:
        lines.append(f"- Summary: {summary}")
    message = payload.get("message")
    if message:
        lines.append(f"- Message: {message}")
    if payload.get("category"):
        lines.append(f"- Category: {payload['category']}")
    if isinstance(payload.get("since_ts"), int):
        lines.append(f"- Since ts: {payload['since_ts']}")
    if payload.get("trigger_talent"):
        lines.append(f"- Trigger talent: {payload['trigger_talent']}")


def _append_terminal_trigger_context(
    lines: list[str],
    trigger_kind: str,
    payload: dict[str, Any],
) -> None:
    details = _terminal_result_details(trigger_kind, payload)
    if details is None:
        return
    _, result_field_label, result_value = details
    if payload.get("name"):
        lines.append(f"- Talent: {payload['name']}")
    instruction = STOP_AND_REPORT_CONTRACT.format(result_field_label=result_field_label)
    lines.append(f"- Instruction: This is a {instruction}")
    if result_value:
        lines.append(f"- {result_field_label.capitalize()}: {result_value}")


def _render_location(payload: dict[str, Any], context: dict[str, Any]) -> str:
    app = payload.get("app") or context.get("app")
    path = payload.get("path") or context.get("path")
    facet = payload.get("facet") or context.get("facet")

    if not any((app, path, facet)):
        return ""

    lines = ["## Location\n"]
    if app:
        lines.append(f"- App: {app}")
    if path:
        lines.append(f"- Path: {path}")
    if facet:
        lines.append(f"- Facet: {facet}")
    return "\n".join(lines)
