# SPDX-License-Identifier: AGPL-3.0-only
# Copyright (c) 2026 sol pbc

"""Shared memory-headroom admission gate for local GPU work."""

from __future__ import annotations

import logging
import platform
import threading
import time
from collections.abc import Callable
from dataclasses import dataclass
from typing import Any

from solstone.observe.transcribe.resource import stt_local_floor_bytes
from solstone.think.providers import local_cuda
from solstone.think.providers.memory import (
    gb_label,
    read_available_bytes,
    read_total_bytes,
)
from solstone.think.utils import get_config

LOG = logging.getLogger(__name__)

_BYTES_PER_MIB = 1024**2
_BYTES_PER_GIB = 1024**3
_DEFAULT_STT_FLOOR_BYTES = 2 * _BYTES_PER_GIB
_DEFAULT_FLOOR_MARGIN_BYTES = 1 * _BYTES_PER_GIB
_DEFAULT_FLOOR_CAP_BYTES = 12 * _BYTES_PER_GIB
_POLL_INTERVAL_SECONDS = 1.0

# Serializes one-time config/probe resolution. This lock is deliberately
# separate from the throttle-state lock: it may cover a slow nvidia-smi probe,
# is never acquired together with _lock, and is never held across poll sleeps.
_resolution_lock = threading.Lock()
_lock = threading.Lock()
_resolved_floor_bytes: int | None = None
_unified_memory: bool | None = None
_warned_unreliable_memory = False
_throttle_count = 0
_throttle_floor_bytes: int | None = None
_throttle_available_bytes: int | None = None
_episode_started_at: float | None = None


@dataclass(frozen=True)
class ThrottleState:
    throttled: bool
    count: int
    floor_mib: int | None
    available_mib: int | None


def _bytes_to_mib(value: int | None) -> int | None:
    if value is None:
        return None
    return value // _BYTES_PER_MIB


def _detect_unified_memory_uncached() -> bool:
    if platform.system().lower() == "darwin" and platform.machine().lower() == "arm64":
        return True
    return local_cuda.detect_nvidia_unified_memory()


def detect_unified_memory() -> bool:
    global _unified_memory

    cached = _unified_memory
    if cached is not None:
        return cached

    with _resolution_lock:
        if _unified_memory is None:
            _unified_memory = _detect_unified_memory_uncached()
        return _unified_memory


def _configured_floor_bytes(config: dict[str, Any]) -> tuple[bool, int | None]:
    memory_config = config.get("memory")
    if not isinstance(memory_config, dict) or "floor_mib" not in memory_config:
        return False, None

    raw = memory_config.get("floor_mib")
    if not isinstance(raw, int) or isinstance(raw, bool) or raw < 0:
        LOG.warning(
            "Invalid memory.floor_mib in journal config: %r - defaulting to auto",
            raw,
        )
        return False, None
    return True, raw * _BYTES_PER_MIB


def _default_floor_bytes() -> int:
    # This gate is launch admission; resolve_stt_backend_choice is the in-job backend
    # choice. A gate floor below the STT local floor would admit transcribe jobs
    # that then silently downgrade off the local backend, so unified machines use
    # a default floor at or above the platform STT floor plus margin.
    lower = (
        stt_local_floor_bytes() or _DEFAULT_STT_FLOOR_BYTES
    ) + _DEFAULT_FLOOR_MARGIN_BYTES
    total = read_total_bytes()
    if total is None:
        return lower
    candidate = int(0.06 * total)
    return min(max(candidate, lower), _DEFAULT_FLOOR_CAP_BYTES)


def resolve_memory_floor_bytes() -> int:
    global _resolved_floor_bytes
    global _unified_memory

    cached = _resolved_floor_bytes
    if cached is not None:
        return cached

    with _resolution_lock:
        if _resolved_floor_bytes is not None:
            return _resolved_floor_bytes

        explicit, configured_floor = _configured_floor_bytes(get_config())
        if explicit:
            floor = configured_floor
            assert floor is not None
        else:
            if _unified_memory is None:
                _unified_memory = _detect_unified_memory_uncached()
            floor = _default_floor_bytes() if _unified_memory else 0

        _resolved_floor_bytes = floor
        return _resolved_floor_bytes


def _begin_throttle_locked(available: int, floor: int) -> bool:
    global _episode_started_at
    global _throttle_available_bytes
    global _throttle_count
    global _throttle_floor_bytes

    first_waiter = _throttle_count == 0
    _throttle_count += 1
    _throttle_floor_bytes = floor
    _throttle_available_bytes = available
    if first_waiter:
        _episode_started_at = time.monotonic()
    return first_waiter


def _finish_throttle_locked(available: int | None = None) -> tuple[bool, float]:
    global _episode_started_at
    global _throttle_available_bytes
    global _throttle_count
    global _throttle_floor_bytes

    if available is not None:
        _throttle_available_bytes = available
    _throttle_count = max(0, _throttle_count - 1)
    if _throttle_count > 0:
        return False, 0.0

    started_at = _episode_started_at
    _episode_started_at = None
    _throttle_floor_bytes = None
    _throttle_available_bytes = None
    if started_at is None:
        return True, 0.0
    return True, time.monotonic() - started_at


def _update_throttle_available_locked(available: int) -> None:
    global _throttle_available_bytes

    _throttle_available_bytes = available


def _warn_unreliable_memory_once() -> bool:
    global _warned_unreliable_memory

    with _lock:
        if _warned_unreliable_memory:
            return False
        _warned_unreliable_memory = True
        return True


def _call_throttle_start(
    callback: Callable[..., None] | None,
    *,
    stage: str,
    available: int,
    floor: int,
) -> None:
    if callback is None:
        return
    callback(
        stage=stage,
        available_bytes=available,
        floor_bytes=floor,
        available_mib=_bytes_to_mib(available),
        floor_mib=_bytes_to_mib(floor),
    )


def _call_throttle_end(
    callback: Callable[..., None] | None,
    *,
    stage: str,
    waited_seconds: float,
) -> None:
    if callback is None:
        return
    callback(stage=stage, waited_seconds=waited_seconds)


def _finish_wait(
    *,
    stage: str,
    started_at: float,
    available: int | None,
    on_throttle_end: Callable[..., None] | None,
) -> float:
    with _lock:
        episode_finished, episode_waited = _finish_throttle_locked(available)
    waited_seconds = time.monotonic() - started_at
    _call_throttle_end(
        on_throttle_end,
        stage=stage,
        waited_seconds=waited_seconds,
    )
    if episode_finished:
        LOG.info(
            "processing throttle ended after %.1fs",
            episode_waited,
        )
    return waited_seconds


def wait_for_memory_headroom(
    stage: str,
    *,
    should_stop: Callable[[], bool] | None = None,
    on_throttle_start: Callable[..., None] | None = None,
    on_throttle_end: Callable[..., None] | None = None,
) -> float:
    floor = resolve_memory_floor_bytes()
    if floor <= 0:
        return 0.0

    started_at = time.monotonic()
    waiting = False

    while True:
        if should_stop is not None and should_stop():
            if waiting:
                return _finish_wait(
                    stage=stage,
                    started_at=started_at,
                    available=None,
                    on_throttle_end=on_throttle_end,
                )
            return 0.0

        start_callback: tuple[int, int] | None = None
        first_waiter = False
        with _lock:
            available = read_available_bytes()
            if available is None:
                pass
            elif available >= floor:
                pass
            else:
                if not waiting:
                    waiting = True
                    first_waiter = _begin_throttle_locked(available, floor)
                    start_callback = (available, floor)
                else:
                    _update_throttle_available_locked(available)

        if available is None:
            if _warn_unreliable_memory_once():
                LOG.warning(
                    "Memory reading unreliable; admitting %s without headroom gate",
                    stage,
                )
            if waiting:
                return _finish_wait(
                    stage=stage,
                    started_at=started_at,
                    available=None,
                    on_throttle_end=on_throttle_end,
                )
            return 0.0

        if available >= floor:
            if waiting:
                return _finish_wait(
                    stage=stage,
                    started_at=started_at,
                    available=available,
                    on_throttle_end=on_throttle_end,
                )
            return 0.0

        if start_callback is not None:
            start_available, start_floor = start_callback
            _call_throttle_start(
                on_throttle_start,
                stage=stage,
                available=start_available,
                floor=start_floor,
            )
            if first_waiter:
                LOG.info(
                    "processing throttled: low memory (%s GiB available < %s GiB floor)",
                    gb_label(start_available),
                    gb_label(start_floor),
                )

        if should_stop is not None and should_stop():
            return _finish_wait(
                stage=stage,
                started_at=started_at,
                available=available,
                on_throttle_end=on_throttle_end,
            )
        time.sleep(_POLL_INTERVAL_SECONDS)
        if should_stop is not None and should_stop():
            return _finish_wait(
                stage=stage,
                started_at=started_at,
                available=available,
                on_throttle_end=on_throttle_end,
            )


def throttle_state() -> ThrottleState:
    with _lock:
        if _throttle_count <= 0:
            return ThrottleState(
                throttled=False,
                count=0,
                floor_mib=None,
                available_mib=None,
            )
        return ThrottleState(
            throttled=True,
            count=_throttle_count,
            floor_mib=_bytes_to_mib(_throttle_floor_bytes),
            available_mib=_bytes_to_mib(_throttle_available_bytes),
        )


def reset_admission_state() -> None:
    global _episode_started_at
    global _resolved_floor_bytes
    global _throttle_available_bytes
    global _throttle_count
    global _throttle_floor_bytes
    global _unified_memory
    global _warned_unreliable_memory

    with _resolution_lock:
        _resolved_floor_bytes = None
        _unified_memory = None

    with _lock:
        _warned_unreliable_memory = False
        _throttle_count = 0
        _throttle_floor_bytes = None
        _throttle_available_bytes = None
        _episode_started_at = None


__all__ = [
    "ThrottleState",
    "detect_unified_memory",
    "reset_admission_state",
    "resolve_memory_floor_bytes",
    "throttle_state",
    "wait_for_memory_headroom",
]
