# SPDX-License-Identifier: AGPL-3.0-only
# Copyright (c) 2026 sol pbc

"""In-process admission control for secure-listener WSGI work."""

from __future__ import annotations

import asyncio
import logging
import threading
import time
from collections import deque
from collections.abc import Callable
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass
from typing import Any, Final, Literal

from solstone.think.utils import get_config

log = logging.getLogger("convey.secure_listener.admission")

DEFAULT_SECURE_LISTENER_CAPACITY: Final[int] = 16
DEFAULT_SECURE_LISTENER_STREAMING_CAPACITY: Final[int] = 8
DEFAULT_SECURE_LISTENER_QUEUE_TIMEOUT_SECONDS: Final[float] = 120.0
SECURE_LISTENER_QUEUE_WARN_SECONDS: Final[float] = 60.0
MAX_SECURE_LISTENER_CAPACITY: Final[int] = 128
MAX_SECURE_LISTENER_QUEUE_TIMEOUT_SECONDS: Final[float] = 600.0

_PermitKind = Literal["total", "streaming", "streaming_over_budget"]
_DepartureReason = Literal["granted", "cancelled", "timed_out"]


class SecureListenerAdmissionRejected(Exception):
    """The listener refused admission before executor submission."""


class SecureListenerQueueTimeout(SecureListenerAdmissionRejected):
    """A queued listener request exceeded the admission wait deadline."""

    reason_code = "secure_listener_queue_timeout"


@dataclass(frozen=True)
class SecureListenerAdmissionConfig:
    capacity: int = DEFAULT_SECURE_LISTENER_CAPACITY
    streaming_capacity: int = DEFAULT_SECURE_LISTENER_STREAMING_CAPACITY
    refuse_when_full: bool = False
    queue_timeout_seconds: float = DEFAULT_SECURE_LISTENER_QUEUE_TIMEOUT_SECONDS

    @property
    def queue_limit(self) -> int:
        return self.capacity * 2


class SecureListenerPermit:
    """One in-process serving-capacity permit."""

    def __init__(
        self,
        admission: SecureListenerAdmission,
        kind: _PermitKind,
        *,
        queue_wait_ms: float = 0.0,
    ) -> None:
        self._admission = admission
        self.kind = kind
        self.queue_wait_ms = queue_wait_ms
        self._released = False
        self._lock = threading.Lock()

    def release(self) -> None:
        with self._lock:
            if self._released:
                return
            self._released = True
        self._admission._release(self.kind)

    def __enter__(self) -> SecureListenerPermit:
        return self

    def __exit__(self, exc_type: Any, exc: Any, tb: Any) -> None:
        self.release()


@dataclass
class _QueuedWaiter:
    loop: asyncio.AbstractEventLoop
    future: asyncio.Future[SecureListenerPermit]
    queued_at: float
    permit: SecureListenerPermit | None = None


class _PermitHandoff:
    def __init__(self, permit: SecureListenerPermit) -> None:
        self._permit = permit
        self._started = False
        self._released = False
        self._lock = threading.Lock()

    def mark_started(self) -> bool:
        with self._lock:
            if self._released:
                return False
            self._started = True
            return True

    def release_from_worker(self) -> None:
        permit = self._release(started_required=True)
        if permit is not None:
            permit.release()

    def release_from_caller_if_not_started(self) -> bool:
        permit = self._release(started_required=False)
        if permit is None:
            return False
        permit.release()
        return True

    def _release(self, *, started_required: bool) -> SecureListenerPermit | None:
        with self._lock:
            if self._released:
                return None
            if started_required and not self._started:
                return None
            if not started_required and self._started:
                return None
            self._released = True
            return self._permit


def resolve_admission_config() -> SecureListenerAdmissionConfig:
    cfg = get_config()
    link_cfg = cfg.get("link") if isinstance(cfg, dict) else None
    if not isinstance(link_cfg, dict):
        return SecureListenerAdmissionConfig()

    capacity = _resolve_int(
        link_cfg,
        "secure_listener_capacity",
        default=DEFAULT_SECURE_LISTENER_CAPACITY,
        valid_min=1,
        valid_max=MAX_SECURE_LISTENER_CAPACITY,
        warning_default="16",
    )
    streaming_default = min(DEFAULT_SECURE_LISTENER_STREAMING_CAPACITY, capacity)
    streaming_capacity = _resolve_int(
        link_cfg,
        "secure_listener_streaming_capacity",
        default=streaming_default,
        valid_min=0,
        valid_max=capacity,
        warning_default=str(streaming_default),
    )
    refuse_when_full = _resolve_bool(
        link_cfg,
        "secure_listener_refuse_when_full",
        default=False,
        warning_default="false",
    )
    queue_timeout_seconds = _resolve_float(
        link_cfg,
        "secure_listener_queue_timeout_seconds",
        default=DEFAULT_SECURE_LISTENER_QUEUE_TIMEOUT_SECONDS,
        valid_min=1.0,
        valid_max=MAX_SECURE_LISTENER_QUEUE_TIMEOUT_SECONDS,
        warning_default="120.0",
    )
    return SecureListenerAdmissionConfig(
        capacity=capacity,
        streaming_capacity=streaming_capacity,
        refuse_when_full=refuse_when_full,
        queue_timeout_seconds=queue_timeout_seconds,
    )


def _resolve_int(
    link_cfg: dict[str, Any],
    key: str,
    *,
    default: int,
    valid_min: int,
    valid_max: int,
    warning_default: str,
) -> int:
    raw = link_cfg.get(key, default)
    if (
        not isinstance(raw, int)
        or isinstance(raw, bool)
        or raw < valid_min
        or raw > valid_max
    ):
        log.warning(
            "Invalid link.%s in journal config: %r \u2014 defaulting to %s",
            key,
            raw,
            warning_default,
        )
        return default
    return raw


def _resolve_bool(
    link_cfg: dict[str, Any],
    key: str,
    *,
    default: bool,
    warning_default: str,
) -> bool:
    raw = link_cfg.get(key, default)
    if not isinstance(raw, bool):
        log.warning(
            "Invalid link.%s in journal config: %r \u2014 defaulting to %s",
            key,
            raw,
            warning_default,
        )
        return default
    return raw


def _resolve_float(
    link_cfg: dict[str, Any],
    key: str,
    *,
    default: float,
    valid_min: float,
    valid_max: float,
    warning_default: str,
) -> float:
    raw = link_cfg.get(key, default)
    if isinstance(raw, bool):
        log.warning(
            "Invalid link.%s in journal config: %r \u2014 defaulting to %s",
            key,
            raw,
            warning_default,
        )
        return default
    if raw == 0:
        log.info("link.%s is 0; secure listener queue timeout disabled", key)
        return 0.0
    if not isinstance(raw, (int, float)):
        log.warning(
            "Invalid link.%s in journal config: %r \u2014 defaulting to %s",
            key,
            raw,
            warning_default,
        )
        return default
    value = float(raw)
    if not (valid_min <= value <= valid_max):
        log.warning(
            "Invalid link.%s in journal config: %r \u2014 defaulting to %s",
            key,
            raw,
            warning_default,
        )
        return default
    return value


class SecureListenerAdmission:
    """Admission, queueing, and content-free telemetry for listener work."""

    def __init__(
        self,
        config: SecureListenerAdmissionConfig | None = None,
        *,
        thread_name_prefix: str = "secure-listener-wsgi",
    ) -> None:
        self.config = config or SecureListenerAdmissionConfig()
        self._executor = ThreadPoolExecutor(
            max_workers=self.config.capacity,
            thread_name_prefix=thread_name_prefix,
        )
        self._lock = threading.Lock()
        self._streaming_condition = threading.Condition(self._lock)
        self._waiters: deque[_QueuedWaiter] = deque()
        self._active_total = 0
        self._active_streaming = 0
        self._active_streaming_over_budget = 0
        self._rejected_total = 0
        self._rejected_streaming = 0
        self._rejected_queue_timeout = 0
        self._admitted_streaming_over_budget = 0

    async def acquire(self) -> SecureListenerPermit:
        started = time.monotonic()
        loop = asyncio.get_running_loop()
        with self._lock:
            if self._active_total < self.config.capacity and not self._waiters:
                self._active_total += 1
                return SecureListenerPermit(self, "total")
            if (
                self.config.refuse_when_full
                and len(self._waiters) >= self.config.queue_limit
            ):
                self._rejected_total += 1
                raise SecureListenerAdmissionRejected
            waiter = _QueuedWaiter(
                loop=loop,
                future=loop.create_future(),
                queued_at=started,
            )
            self._waiters.append(waiter)
            queue_timeout_seconds = self.config.queue_timeout_seconds

        departure_reason: _DepartureReason = "cancelled"
        try:
            if queue_timeout_seconds > 0.0:
                permit = await asyncio.wait_for(
                    waiter.future,
                    timeout=queue_timeout_seconds,
                )
            else:
                permit = await waiter.future
        except TimeoutError as exc:
            departure_reason = "timed_out"
            self._reclaim_abandoned_waiter(waiter)
            self._record_queue_timeout_rejection()
            raise SecureListenerQueueTimeout from exc
        except BaseException:
            departure_reason = "cancelled"
            self._reclaim_abandoned_waiter(waiter)
            raise
        else:
            departure_reason = "granted"
            return permit
        finally:
            self._warn_if_slow_waiter_departure(
                waiter,
                departure_reason,
                queue_timeout_seconds,
            )

    async def submit(
        self,
        loop: asyncio.AbstractEventLoop,
        func: Callable[..., Any],
        *args: Any,
    ) -> Any:
        permit = await self.acquire()
        handoff = _PermitHandoff(permit)
        try:
            future = loop.run_in_executor(
                self._executor,
                self._run_with_permit,
                handoff,
                func,
                args,
            )
        except BaseException:
            handoff.release_from_caller_if_not_started()
            raise
        try:
            return await future
        except asyncio.CancelledError:
            future.cancel()
            handoff.release_from_caller_if_not_started()
            raise

    def try_acquire_streaming(self) -> SecureListenerPermit | None:
        if self.config.streaming_capacity <= 0:
            return None
        with self._lock:
            if self._active_streaming >= self.config.streaming_capacity:
                return None
            self._active_streaming += 1
            return SecureListenerPermit(self, "streaming")

    def acquire_streaming(
        self,
        timeout_s: float,
        *,
        cancel_event: threading.Event,
    ) -> SecureListenerPermit | None:
        if self.config.streaming_capacity <= 0:
            return None
        deadline = time.monotonic() + max(0.0, timeout_s)
        with self._streaming_condition:
            while self._active_streaming >= self.config.streaming_capacity:
                if cancel_event.is_set():
                    return None
                remaining = deadline - time.monotonic()
                if remaining <= 0:
                    return None
                self._streaming_condition.wait(remaining)
            self._active_streaming += 1
            return SecureListenerPermit(self, "streaming")

    def reject_streaming(self) -> None:
        with self._lock:
            self._rejected_streaming += 1

    def admit_streaming_over_budget(self) -> SecureListenerPermit:
        with self._lock:
            self._active_streaming_over_budget += 1
            self._admitted_streaming_over_budget += 1
            return SecureListenerPermit(self, "streaming_over_budget")

    def snapshot(self) -> dict[str, Any]:
        now = time.monotonic()
        with self._lock:
            longest_wait_ms = 0.0
            if self._waiters:
                longest_wait_ms = max(
                    (now - waiter.queued_at) * 1000.0 for waiter in self._waiters
                )
            return {
                "timestamp_ms": int(time.time() * 1000),
                "active": {
                    "total": self._active_total,
                    "streaming": self._active_streaming,
                    "streaming_over_budget": self._active_streaming_over_budget,
                },
                "limit": {
                    "total": self.config.capacity,
                    "streaming": self.config.streaming_capacity,
                    "queue": self.config.queue_limit,
                },
                "queued": {"total": len(self._waiters)},
                "rejected": {
                    "total": self._rejected_total,
                    "streaming": self._rejected_streaming,
                    "queue_timeout": self._rejected_queue_timeout,
                },
                "admitted_over_budget": {
                    "streaming": self._admitted_streaming_over_budget,
                },
                "longest_wait_ms": longest_wait_ms,
                "refusal_enabled": self.config.refuse_when_full,
            }

    def shutdown(self, *, wait: bool = True, cancel_futures: bool = True) -> None:
        self._executor.shutdown(wait=wait, cancel_futures=cancel_futures)

    def _run_with_permit(
        self,
        handoff: _PermitHandoff,
        func: Callable[..., Any],
        args: tuple[Any, ...],
    ) -> Any:
        if not handoff.mark_started():
            raise SecureListenerAdmissionRejected
        try:
            return func(*args)
        finally:
            handoff.release_from_worker()

    def _release(self, kind: _PermitKind) -> None:
        with self._streaming_condition:
            if kind == "total":
                self._active_total = max(0, self._active_total - 1)
                self._wake_waiters_locked()
            elif kind == "streaming":
                self._active_streaming = max(0, self._active_streaming - 1)
                self._streaming_condition.notify()
            elif kind == "streaming_over_budget":
                self._active_streaming_over_budget = max(
                    0,
                    self._active_streaming_over_budget - 1,
                )

    def _cancel_waiter(self, waiter: _QueuedWaiter) -> bool:
        with self._lock:
            try:
                self._waiters.remove(waiter)
            except ValueError:
                return False
            return True

    def _reclaim_abandoned_waiter(self, waiter: _QueuedWaiter) -> None:
        if self._cancel_waiter(waiter):
            return
        if waiter.permit is not None:
            waiter.permit.release()

    def _record_queue_timeout_rejection(self) -> None:
        with self._lock:
            self._rejected_total += 1
            self._rejected_queue_timeout += 1

    def _warn_if_slow_waiter_departure(
        self,
        waiter: _QueuedWaiter,
        reason: _DepartureReason,
        queue_timeout_seconds: float,
    ) -> None:
        waiter_age_s = time.monotonic() - waiter.queued_at
        if waiter_age_s <= SECURE_LISTENER_QUEUE_WARN_SECONDS:
            return
        with self._lock:
            active_total = self._active_total
            queue_depth = len(self._waiters)
        log.warning(
            "Secure listener admission waiter departed departure_reason=%s "
            "waiter_age_s=%.3f active_total=%d queue_depth=%d "
            "queue_timeout_seconds=%.3f",
            reason,
            waiter_age_s,
            active_total,
            queue_depth,
            queue_timeout_seconds,
        )

    def _wake_waiters_locked(self) -> None:
        while self._waiters and self._active_total < self.config.capacity:
            waiter = self._waiters.popleft()
            if waiter.future.cancelled():
                continue
            self._active_total += 1
            permit = SecureListenerPermit(
                self,
                "total",
                queue_wait_ms=(time.monotonic() - waiter.queued_at) * 1000.0,
            )
            waiter.permit = permit
            waiter.loop.call_soon_threadsafe(self._deliver_waiter, waiter, permit)

    def _deliver_waiter(
        self,
        waiter: _QueuedWaiter,
        permit: SecureListenerPermit,
    ) -> None:
        if waiter.future.cancelled():
            permit.release()
            return
        waiter.future.set_result(permit)
