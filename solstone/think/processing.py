# SPDX-License-Identifier: AGPL-3.0-only
# Copyright (c) 2026 sol pbc

"""Deferred-processing settings and drain-gate read helpers."""

from __future__ import annotations

import copy
import logging
import re
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from typing import Any

from solstone.think.utils import day_dirs, get_config

logger = logging.getLogger(__name__)

AWAITING_ANALYSIS_TEMPLATE = "{count} segments captured, awaiting analysis"
DRAIN_STATE_REALTIME = "realtime"
DRAIN_STATE_WINDOW_OPEN = "window_open"
DRAIN_STATE_WAITING = "waiting_for_window"
DRAIN_STATE_NO_CONDITION = "no_active_condition"

_MISSING = object()
_MODES = frozenset({"realtime", "deferred"})
_HHMM_RE = re.compile(r"^([01]\d|2[0-3]):([0-5]\d)$")


@dataclass(frozen=True)
class TimeWindowSettings:
    enabled: bool
    start: str
    end: str


@dataclass(frozen=True)
class DisplayPowersaveSettings:
    enabled: bool


@dataclass(frozen=True)
class GateSettings:
    time_window: TimeWindowSettings
    display_powersave: DisplayPowersaveSettings


@dataclass(frozen=True)
class ProcessingSettings:
    mode: str
    gate: GateSettings

    def to_dict(self) -> dict[str, Any]:
        """Return the on-disk ``processing`` shape."""
        return {
            "mode": self.mode,
            "gate": {
                "time_window": {
                    "enabled": self.gate.time_window.enabled,
                    "start": self.gate.time_window.start,
                    "end": self.gate.time_window.end,
                },
                "display_powersave": {
                    "enabled": self.gate.display_powersave.enabled,
                },
            },
        }


@dataclass(frozen=True)
class ConditionState:
    enabled: bool
    available: bool
    open: bool


@dataclass(frozen=True)
class GateState:
    open: bool
    conditions: dict[str, ConditionState]


DEFAULT_PROCESSING = ProcessingSettings(
    mode="realtime",
    gate=GateSettings(
        time_window=TimeWindowSettings(
            enabled=True,
            start="02:00",
            end="06:00",
        ),
        display_powersave=DisplayPowersaveSettings(enabled=False),
    ),
)


def parse_processing_settings(raw: object, *, strict: bool) -> ProcessingSettings:
    """Parse processing settings with field defaults or strict validation."""
    if raw is None:
        if strict:
            _reject("processing", raw, strict)
        raw = {}
    elif not isinstance(raw, dict):
        _reject("processing", raw, strict)
        raw = {}

    raw_gate = raw.get("gate", _MISSING)
    if raw_gate is _MISSING:
        raw_gate = {}
    elif not isinstance(raw_gate, dict):
        _reject("gate", raw_gate, strict)
        raw_gate = {}

    raw_time_window = raw_gate.get("time_window", _MISSING)
    if raw_time_window is _MISSING:
        raw_time_window = {}
    elif not isinstance(raw_time_window, dict):
        _reject("gate.time_window", raw_time_window, strict)
        raw_time_window = {}

    raw_display_powersave = raw_gate.get("display_powersave", _MISSING)
    if raw_display_powersave is _MISSING:
        raw_display_powersave = {}
    elif not isinstance(raw_display_powersave, dict):
        _reject("gate.display_powersave", raw_display_powersave, strict)
        raw_display_powersave = {}

    if strict:
        _validate_known_keys("processing", raw, {"mode", "gate"})
        _validate_known_keys("gate", raw_gate, {"time_window", "display_powersave"})
        _validate_known_keys(
            "gate.time_window",
            raw_time_window,
            {"enabled", "start", "end"},
        )
        _validate_known_keys(
            "gate.display_powersave",
            raw_display_powersave,
            {"enabled"},
        )

    return ProcessingSettings(
        mode=_mode(raw.get("mode", _MISSING), strict),
        gate=GateSettings(
            time_window=TimeWindowSettings(
                enabled=_bool(
                    "gate.time_window.enabled",
                    raw_time_window.get("enabled", _MISSING),
                    DEFAULT_PROCESSING.gate.time_window.enabled,
                    strict,
                ),
                start=_hhmm(
                    "start",
                    raw_time_window.get("start", _MISSING),
                    DEFAULT_PROCESSING.gate.time_window.start,
                    strict,
                ),
                end=_hhmm(
                    "end",
                    raw_time_window.get("end", _MISSING),
                    DEFAULT_PROCESSING.gate.time_window.end,
                    strict,
                ),
            ),
            display_powersave=DisplayPowersaveSettings(
                enabled=_bool(
                    "gate.display_powersave.enabled",
                    raw_display_powersave.get("enabled", _MISSING),
                    DEFAULT_PROCESSING.gate.display_powersave.enabled,
                    strict,
                )
            ),
        ),
    )


def load_processing_settings() -> ProcessingSettings:
    """Load processing settings from journal config with default-at-read fallback."""
    return parse_processing_settings(get_config().get("processing"), strict=False)


def validate_processing_update(
    existing: dict[str, Any],
    updates: dict[str, Any],
) -> ProcessingSettings:
    """Deep-merge a partial processing update and strictly validate it."""
    return parse_processing_settings(_deep_merge(existing, updates), strict=True)


def evaluate_time_window(window: TimeWindowSettings, now: datetime) -> ConditionState:
    """Evaluate the local minute-resolution time-window condition."""
    now_min = now.hour * 60 + now.minute
    start_min = _hhmm_minutes(window.start)
    end_min = _hhmm_minutes(window.end)

    if start_min < end_min:
        is_open = start_min <= now_min < end_min
    elif start_min > end_min:
        is_open = now_min >= start_min or now_min < end_min
    else:
        is_open = False

    return ConditionState(enabled=window.enabled, available=True, open=is_open)


def evaluate_display_powersave(ps: DisplayPowersaveSettings) -> ConditionState:
    """Evaluate the display-powersave forward seam, currently unavailable."""
    return ConditionState(enabled=ps.enabled, available=False, open=False)


def evaluate_drain_gate(settings: ProcessingSettings, now: datetime) -> GateState:
    """Evaluate all drain gate conditions with OR composition."""
    conditions = {
        "time_window": evaluate_time_window(settings.gate.time_window, now),
        "display_powersave": evaluate_display_powersave(
            settings.gate.display_powersave
        ),
    }
    return GateState(
        open=any(
            condition.enabled and condition.available and condition.open
            for condition in conditions.values()
        ),
        conditions=conditions,
    )


def format_awaiting_analysis(count: int) -> str:
    """Return fixed awaiting-analysis copy for a backlog count."""
    return AWAITING_ANALYSIS_TEMPLATE.format(count=count)


def read_last_drained_at() -> int | None:
    """Return the latest completed daily drain marker mtime in epoch ms."""
    latest: float | None = None
    for path in day_dirs().values():
        marker = Path(path) / "health" / "daily.updated"
        try:
            mtime = marker.stat().st_mtime
        except FileNotFoundError:
            continue
        if latest is None or mtime > latest:
            latest = mtime
    if latest is None:
        return None
    return int(latest * 1000)


def derive_drain_state(
    settings: ProcessingSettings,
    gate_state: GateState,
) -> str:
    """Return the stable drain-state token for current settings and gate state."""
    if settings.mode != "deferred":
        return DRAIN_STATE_REALTIME
    if gate_state.open:
        return DRAIN_STATE_WINDOW_OPEN
    if any(
        condition.enabled and condition.available
        for condition in gate_state.conditions.values()
    ):
        return DRAIN_STATE_WAITING
    return DRAIN_STATE_NO_CONDITION


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


def _warn_invalid(key: str, raw_value: object) -> None:
    logger.warning(
        "processing settings invalid key=%s value=%r; falling back to default",
        key,
        raw_value,
    )


def _reject(key: str, raw_value: object, strict: bool) -> None:
    if raw_value is _MISSING:
        return
    if strict:
        raise ValueError(f"{key} has invalid value: {raw_value!r}")
    _warn_invalid(key, raw_value)


def _validate_known_keys(
    key: str,
    raw_value: dict[str, Any],
    allowed: set[str],
) -> None:
    for candidate in raw_value:
        if candidate not in allowed:
            raise ValueError(f"{key}.{candidate} is not a recognized setting")


def _bool(key: str, raw_value: object, default: bool, strict: bool) -> bool:
    if raw_value is _MISSING:
        return default
    if isinstance(raw_value, bool):
        return raw_value
    _reject(key, raw_value, strict)
    return default


def _mode(raw_value: object, strict: bool) -> str:
    if raw_value is _MISSING:
        return DEFAULT_PROCESSING.mode
    if isinstance(raw_value, str) and raw_value in _MODES:
        return raw_value
    if strict:
        raise ValueError("processing mode must be realtime or deferred")
    _warn_invalid("mode", raw_value)
    return DEFAULT_PROCESSING.mode


def _hhmm(field: str, raw_value: object, default: str, strict: bool) -> str:
    if raw_value is _MISSING:
        return default
    if isinstance(raw_value, str) and _HHMM_RE.fullmatch(raw_value):
        return raw_value
    if strict:
        raise ValueError(f"{field} must be HH:MM 24-hour time, got {raw_value!r}")
    _warn_invalid(field, raw_value)
    return default


def _hhmm_minutes(value: str) -> int:
    hour, minute = value.split(":", 1)
    return int(hour) * 60 + int(minute)


__all__ = [
    "AWAITING_ANALYSIS_TEMPLATE",
    "DRAIN_STATE_NO_CONDITION",
    "DRAIN_STATE_REALTIME",
    "DRAIN_STATE_WAITING",
    "DRAIN_STATE_WINDOW_OPEN",
    "DEFAULT_PROCESSING",
    "ConditionState",
    "DisplayPowersaveSettings",
    "GateSettings",
    "GateState",
    "ProcessingSettings",
    "TimeWindowSettings",
    "derive_drain_state",
    "evaluate_display_powersave",
    "evaluate_drain_gate",
    "evaluate_time_window",
    "format_awaiting_analysis",
    "load_processing_settings",
    "parse_processing_settings",
    "read_last_drained_at",
    "validate_processing_update",
]
