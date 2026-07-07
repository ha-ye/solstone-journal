# SPDX-License-Identifier: AGPL-3.0-only
# Copyright (c) 2026 sol pbc

from __future__ import annotations

import logging
import math
from dataclasses import dataclass
from typing import Any, Literal

from solstone.convey.chat_sources import parse_sol_sources
from solstone.convey.utils import format_month_day

logger = logging.getLogger(__name__)

NeedsYouKind = Literal["chat", "confirm", "route"]
DISABLED_INVALID_ROUTE_REASON = "this link isn't available from here."
DISABLED_EMPTY_PROMPT_REASON = "this item needs a prompt before chat can open."


@dataclass(frozen=True)
class NeedsYouItem:
    text: str
    kind: NeedsYouKind
    payload: dict[str, Any]
    disabled: bool = False
    reason: str = ""

    def to_dict(self) -> dict[str, Any]:
        return {
            "text": self.text,
            "kind": self.kind,
            "payload": self.payload,
            "disabled": self.disabled,
            "reason": self.reason,
        }


def classify_needs_you(
    attention: Any,
    pulse_needs: list[Any],
) -> list[NeedsYouItem]:
    items: list[NeedsYouItem] = []

    if attention:
        item = _classify_safely("attention", attention, _classify_attention)
        if item is not None:
            items.append(item)

    for pulse_need in pulse_needs:
        item = _classify_safely("pulse need", pulse_need, _classify_pulse_need)
        if item is not None:
            items.append(item)

    return items


def _normalize_item(text: str) -> str:
    return " ".join(text.lower().split())


def _need_display_text(item: Any) -> str:
    if isinstance(item, dict):
        return str(item.get("text") or item.get("placeholder_text") or "")
    placeholder = getattr(item, "placeholder_text", None)
    if isinstance(placeholder, str):
        return placeholder
    return str(item)


def _need_source_identity(item: Any) -> str | None:
    if isinstance(item, dict):
        source_id = item.get("source_id")
        payload = item.get("payload")
        href = payload.get("href") if isinstance(payload, dict) else None
    else:
        source_id = getattr(item, "source_id", None)
        href = None

    if isinstance(source_id, str) and source_id.strip():
        return source_id.strip()

    refs = parse_sol_sources(_need_display_text(item))
    if refs:
        ref = refs[0].get("ref")
        if isinstance(ref, str) and ref:
            return ref

    if isinstance(href, str) and href.strip():
        return href.strip()
    return None


def needs_dedup_key(item: Any) -> str:
    """Stable dedup key for a need item: source identity if present, else normalized text.

    Identity-bearing items dedup only by identity; legacy plain-string items dedup by
    normalized text. The text fallback is namespaced so it can never collide with a
    bare identity string.
    """
    source = _need_source_identity(item)
    if source:
        return source
    return "text:" + _normalize_item(_need_display_text(item))


def _finite_number(value: Any) -> bool:
    return (
        isinstance(value, (int, float))
        and not isinstance(value, bool)
        and math.isfinite(value)
    )


def format_degraded_capture_line(capture_health: dict) -> str | None:
    """Single combined owner line for a degraded capture state, else None.

    Consumed by both the home vitals strip and the needs-you item -- the copy
    lives here once.
    """
    if (
        not isinstance(capture_health, dict)
        or capture_health.get("status") != "degraded"
    ):
        return None
    observers = capture_health.get("observers")
    candidates = (
        [
            o
            for o in observers
            if isinstance(o, dict)
            and o.get("status") == "degraded"
            and isinstance(o.get("ingest_rejection"), dict)
        ]
        if isinstance(observers, list)
        else []
    )
    if not candidates:
        return "an observer isn't reaching your journal"
    first = candidates[0]
    more = len(candidates) - 1
    suffix = f", and {more} more" if more > 0 else ""
    name = first.get("name")
    name = name.strip() if isinstance(name, str) else ""
    if not name:
        return "an observer isn't reaching your journal" + suffix
    rejection = first["ingest_rejection"]
    count = rejection.get("active_count")
    first_ts = rejection.get("first_ts")
    if _finite_number(count) and _finite_number(first_ts):
        clause = f" — {int(count)} rejected since {format_month_day(first_ts)}"
    else:
        clause = ""
    return f"{name} isn't reaching your journal{clause}{suffix}"


def _classify_safely(
    label: str,
    value: Any,
    classifier: Any,
) -> NeedsYouItem | None:
    try:
        return classifier(value)
    except (TypeError, ValueError) as exc:
        logger.warning("omitting malformed needs-you %s: %s", label, exc)
        return None


def _classify_attention(attention: Any) -> NeedsYouItem:
    if isinstance(attention, dict):
        placeholder_text = attention.get("placeholder_text")
    else:
        placeholder_text = getattr(attention, "placeholder_text", None)
    text = _require_text(placeholder_text, "attention placeholder_text")
    return _chat_item(text, f"what happened with {text}?")


def _classify_pulse_need(item: Any) -> NeedsYouItem | None:
    if isinstance(item, dict):
        return _classify_generated_item(
            item,
            default_prompt=lambda text: f"let's dig into {text}",
        )
    text = _require_text(item, "pulse need")
    return _chat_item(text, f"let's dig into {text}")


def _classify_generated_item(
    item: dict[str, Any],
    *,
    default_prompt: Any,
) -> NeedsYouItem | None:
    text = _require_text(item.get("text"), "generated item text")
    kind = item.get("kind")
    raw_payload = item.get("payload") or {}

    if kind == "chat":
        if not isinstance(raw_payload, dict):
            raise TypeError("generated item payload must be an object")
        payload = raw_payload
        prompt = payload.get("prompt")
        if not isinstance(prompt, str) or not prompt.strip():
            prompt = default_prompt(text)
        return _chat_item(text, prompt)

    if kind == "confirm":
        return _chat_item(text, default_prompt(text))

    if kind == "route":
        route_payload = _normalize_route_payload(raw_payload)
        if route_payload is None:
            return _disabled_item(text, "route", DISABLED_INVALID_ROUTE_REASON)
        return NeedsYouItem(text=text, kind="route", payload=route_payload)

    raise ValueError(f"unknown kind: {kind}")


def _normalize_route_payload(payload: Any) -> dict[str, str] | None:
    if not isinstance(payload, dict):
        logger.warning("needs-you route unavailable with malformed payload")
        return None
    href = payload.get("href")
    if not isinstance(href, str) or not href.startswith("/") or href.startswith("//"):
        logger.warning("needs-you route unavailable with off-origin href: %r", href)
        return None
    return {"href": href}


def _chat_item(text: str, prompt: str) -> NeedsYouItem:
    if not isinstance(prompt, str) or not prompt.strip():
        return _disabled_item(text, "chat", DISABLED_EMPTY_PROMPT_REASON)
    return NeedsYouItem(text=text, kind="chat", payload={"prompt": prompt})


def _disabled_item(text: str, kind: NeedsYouKind, reason: str) -> NeedsYouItem:
    return NeedsYouItem(text=text, kind=kind, payload={}, disabled=True, reason=reason)


def _require_text(value: Any, label: str) -> str:
    if not isinstance(value, str):
        raise TypeError(f"{label} must be a string")
    text = value.strip()
    if not text:
        raise ValueError(f"{label} is empty")
    return text
