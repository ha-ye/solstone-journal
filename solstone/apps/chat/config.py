# SPDX-License-Identifier: AGPL-3.0-only
# Copyright (c) 2026 sol pbc

"""Settings for the chat app surface."""

from __future__ import annotations

import copy
import json
import logging
from pathlib import Path
from typing import Any

from solstone.think.utils import _write_config_atomic, get_journal

logger = logging.getLogger(__name__)

DEFAULT_THINKING_SURFACES = "on_tap"
THINKING_SURFACES_VALUES = {"always", "on_tap", "never"}


def load_chat_config() -> dict[str, Any]:
    """Load chat app config with defaults applied."""
    return _normalize_config(_read_chat_config())


def save_chat_config(updates: dict[str, Any]) -> dict[str, Any]:
    """Deep-merge and persist chat app config."""
    if not isinstance(updates, dict):
        raise ValueError("chat config updates must be an object")

    clean_updates = copy.deepcopy(updates)
    if "thinking_surfaces" in clean_updates and not _valid_thinking_surfaces(
        clean_updates["thinking_surfaces"]
    ):
        logger.warning(
            "dropping invalid chat thinking_surfaces value: %r",
            clean_updates["thinking_surfaces"],
        )
        clean_updates.pop("thinking_surfaces", None)

    merged = _deep_merge(_read_chat_config(), clean_updates)
    config = _normalize_config(merged)
    _write_config_atomic(_chat_config_path(), config)
    return config


def _chat_config_path() -> Path:
    return Path(get_journal()) / "config" / "chat.json"


def _read_chat_config() -> dict[str, Any]:
    path = _chat_config_path()
    try:
        with path.open(encoding="utf-8") as handle:
            raw = json.load(handle)
    except FileNotFoundError:
        return {}
    except json.JSONDecodeError:
        logger.warning("chat config is invalid JSON; using defaults")
        return {}

    if not isinstance(raw, dict):
        logger.warning("chat config root must be an object; using defaults")
        return {}
    return raw


def _normalize_config(raw: dict[str, Any]) -> dict[str, Any]:
    config = copy.deepcopy(raw)
    value = config.get("thinking_surfaces", DEFAULT_THINKING_SURFACES)
    if not _valid_thinking_surfaces(value):
        logger.warning("invalid chat thinking_surfaces value: %r", value)
        value = DEFAULT_THINKING_SURFACES
    config["thinking_surfaces"] = value
    return config


def _valid_thinking_surfaces(value: object) -> bool:
    return isinstance(value, str) and value in THINKING_SURFACES_VALUES


def _deep_merge(base: object, updates: dict[str, Any]) -> dict[str, Any]:
    if not isinstance(base, dict):
        base = {}
    merged = copy.deepcopy(base)
    for key, value in updates.items():
        if isinstance(value, dict) and isinstance(merged.get(key), dict):
            merged[key] = _deep_merge(merged[key], value)
            continue
        merged[key] = copy.deepcopy(value)
    return merged
