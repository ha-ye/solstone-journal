# SPDX-License-Identifier: AGPL-3.0-only
# Copyright (c) 2026 sol pbc

from __future__ import annotations

import pytest
from websockets.exceptions import ConnectionClosed

from solstone.think.link.ws_buffer import BufferedWsReader, WsBufferClosed


class FakeWs:
    def __init__(self, frames: list[bytes | str]) -> None:
        self.frames = list(frames)

    async def recv(self) -> bytes | str:
        if not self.frames:
            raise ConnectionClosed(None, None)
        return self.frames.pop(0)


@pytest.mark.asyncio
async def test_peek_does_not_consume() -> None:
    reader = BufferedWsReader(FakeWs([b"abcdef"]))

    assert await reader.peek(4) == b"abcd"
    assert await reader.read_exactly(4) == b"abcd"
    assert await reader.read_exactly(2) == b"ef"


@pytest.mark.asyncio
async def test_read_exactly_spans_frames_and_normalizes_text() -> None:
    reader = BufferedWsReader(FakeWs([b"ab", "cd", b"ef"]))

    assert await reader.read_exactly(5) == b"abcde"
    assert await reader.read_exactly(1) == b"f"


@pytest.mark.asyncio
async def test_drain_returns_surplus() -> None:
    reader = BufferedWsReader(FakeWs([b"abcdef"]))

    assert await reader.peek(4) == b"abcd"
    assert reader.drain_buffer() == b"abcdef"
    assert reader.drain_buffer() == b""


@pytest.mark.asyncio
async def test_short_close_raises_clear_error() -> None:
    reader = BufferedWsReader(FakeWs([b"ab"]))

    with pytest.raises(WsBufferClosed, match="before 4 bytes"):
        await reader.read_exactly(4)
