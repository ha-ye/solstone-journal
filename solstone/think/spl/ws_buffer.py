# SPDX-License-Identifier: AGPL-3.0-only
# Copyright (c) 2026 sol pbc

"""Buffered byte reads over a WebSocket message stream."""

from __future__ import annotations

from typing import Any

from websockets.exceptions import ConnectionClosed


class WsBufferClosed(RuntimeError):
    """Raised when a WebSocket closes before a requested byte count arrives."""


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
