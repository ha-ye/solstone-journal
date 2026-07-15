# SPDX-License-Identifier: AGPL-3.0-only
# Copyright (c) 2026 sol pbc

"""Buffered byte reads over a WebSocket message stream."""

from __future__ import annotations

import asyncio
from collections.abc import Callable
from typing import Any

from websockets.exceptions import ConnectionClosed


class WsBufferClosed(RuntimeError):
    """Raised when a WebSocket closes before a requested byte count arrives."""


class WsReadTimeout(TimeoutError):
    """Raised when a bounded WebSocket read exceeds its absolute deadline."""


class WsProgressTimeout(TimeoutError):
    """Raised when a bounded WebSocket read does not make minimum progress."""


class BufferedWsReader:
    def __init__(self, ws: Any) -> None:
        self._ws = ws
        self._buffer = bytearray()

    async def peek(self, n: int) -> bytes:
        if n < 0:
            raise ValueError("peek length must be non-negative")
        await self._fill(n)
        return bytes(self._buffer[:n])

    async def read_exactly(self, n: int) -> bytes:
        if n < 0:
            raise ValueError("read length must be non-negative")
        await self._fill(n)
        out = bytes(self._buffer[:n])
        del self._buffer[:n]
        return out

    async def peek_bounded(
        self,
        n: int,
        *,
        deadline_s: float,
        time_source: Callable[[], float] | None = None,
    ) -> bytes:
        if n < 0:
            raise ValueError("peek length must be non-negative")
        await self._fill_bounded(n, deadline_s=deadline_s, time_source=time_source)
        return bytes(self._buffer[:n])

    async def read_exactly_bounded(
        self,
        n: int,
        *,
        deadline_s: float,
        time_source: Callable[[], float] | None = None,
    ) -> bytes:
        if n < 0:
            raise ValueError("read length must be non-negative")
        await self._fill_bounded(n, deadline_s=deadline_s, time_source=time_source)
        out = bytes(self._buffer[:n])
        del self._buffer[:n]
        return out

    async def read_exactly_progress(
        self,
        n: int,
        *,
        deadline_s: float,
        window_s: float,
        min_bytes_per_window: int,
        time_source: Callable[[], float] | None = None,
    ) -> bytes:
        if n < 0:
            raise ValueError("read length must be non-negative")
        await self._fill_bounded(
            n,
            deadline_s=deadline_s,
            window_s=window_s,
            min_bytes_per_window=min_bytes_per_window,
            time_source=time_source,
        )
        out = bytes(self._buffer[:n])
        del self._buffer[:n]
        return out

    def drain_buffer(self) -> bytes:
        out = bytes(self._buffer)
        self._buffer.clear()
        return out

    async def _fill(self, n: int) -> None:
        while len(self._buffer) < n:
            try:
                frame = await self._ws.recv()
            except ConnectionClosed as exc:
                raise WsBufferClosed(
                    f"websocket closed before {n} bytes were available"
                ) from exc
            if not isinstance(frame, bytes):
                frame = frame.encode()
            self._buffer.extend(frame)

    async def _fill_bounded(
        self,
        n: int,
        *,
        deadline_s: float,
        window_s: float | None = None,
        min_bytes_per_window: int | None = None,
        time_source: Callable[[], float] | None = None,
    ) -> None:
        if n < 0:
            raise ValueError("fill length must be non-negative")
        if (window_s is None) != (min_bytes_per_window is None):
            raise ValueError("progress window requires both window and byte minimum")
        if window_s is not None and window_s <= 0:
            raise ValueError("progress window must be positive")
        if min_bytes_per_window is not None and min_bytes_per_window < 0:
            raise ValueError("minimum progress bytes must be non-negative")

        clock = (
            time_source if time_source is not None else asyncio.get_running_loop().time
        )
        deadline = clock() + deadline_s
        window_start = clock()
        bytes_since_window_start = 0
        use_progress = window_s is not None and min_bytes_per_window is not None

        while len(self._buffer) < n:
            now = clock()
            if now >= deadline:
                raise WsReadTimeout(f"websocket read exceeded {deadline_s:.3f}s")

            if (
                use_progress
                and window_s is not None
                and min_bytes_per_window is not None
            ):
                if now - window_start >= window_s:
                    if bytes_since_window_start < min_bytes_per_window:
                        raise WsProgressTimeout(
                            "websocket read progress below minimum "
                            f"({bytes_since_window_start} < {min_bytes_per_window} bytes)"
                        )
                    window_start = now
                    bytes_since_window_start = 0
                timeout_at = min(deadline, window_start + window_s)
            else:
                timeout_at = deadline

            timeout = max(0.0, timeout_at - clock())
            try:
                frame = await asyncio.wait_for(self._ws.recv(), timeout=timeout)
            except asyncio.TimeoutError as exc:
                now = clock()
                if now >= deadline:
                    raise WsReadTimeout(
                        f"websocket read exceeded {deadline_s:.3f}s"
                    ) from exc
                if (
                    use_progress
                    and window_s is not None
                    and min_bytes_per_window is not None
                    and now - window_start >= window_s
                ):
                    if bytes_since_window_start < min_bytes_per_window:
                        raise WsProgressTimeout(
                            "websocket read progress below minimum "
                            f"({bytes_since_window_start} < {min_bytes_per_window} bytes)"
                        ) from exc
                    window_start = now
                    bytes_since_window_start = 0
                    continue
                raise WsReadTimeout(
                    f"websocket read exceeded {deadline_s:.3f}s"
                ) from exc
            except ConnectionClosed as exc:
                raise WsBufferClosed(
                    f"websocket closed before {n} bytes were available"
                ) from exc

            if not isinstance(frame, bytes):
                frame = frame.encode()
            self._buffer.extend(frame)
            if use_progress:
                bytes_since_window_start += len(frame)
