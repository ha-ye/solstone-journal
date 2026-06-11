# SPDX-License-Identifier: AGPL-3.0-only
# Copyright (c) 2026 sol pbc

from __future__ import annotations

import logging
from dataclasses import dataclass
from typing import Any, Literal

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
