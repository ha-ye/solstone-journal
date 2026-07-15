# SPDX-License-Identifier: AGPL-3.0-only
# Copyright (c) 2026 sol pbc

from __future__ import annotations

import asyncio

import pytest
from websockets.exceptions import ConnectionClosed

from solstone.think.spl.ws_buffer import (
    BufferedWsReader,
    WsBufferClosed,
    WsProgressTimeout,
    WsReadTimeout,
)


class FakeWs:
    def __init__(self, frames: list[bytes | str]) -> None:
        self.frames = list(frames)

    async def recv(self) -> bytes | str:
        if not self.frames:
            raise ConnectionClosed(None, None)
        return self.frames.pop(0)


class BlockingWs:
    def __init__(self) -> None:
        self.event = asyncio.Event()

    async def recv(self) -> bytes:
        await self.event.wait()
        return b""


class FakeClock:
    def __init__(self) -> None:
        self.now = 0.0

    def __call__(self) -> float:
        return self.now

    def advance(self, seconds: float) -> None:
        self.now += seconds


class AdvancingWs:
    def __init__(
        self, frames: list[bytes], clock: FakeClock, *, advance_s: float
    ) -> None:
        self.frames = list(frames)
        self.clock = clock
        self.advance_s = advance_s

    async def recv(self) -> bytes:
        if not self.frames:
            raise ConnectionClosed(None, None)
        self.clock.advance(self.advance_s)
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


@pytest.mark.asyncio
async def test_read_exactly_bounded_returns_single_frame_payload() -> None:
    reader = BufferedWsReader(FakeWs([b"abcdef"]))

    assert await reader.read_exactly_bounded(6, deadline_s=10.0) == b"abcdef"


@pytest.mark.asyncio
async def test_read_exactly_bounded_stall_times_out() -> None:
    reader = BufferedWsReader(BlockingWs())

    with pytest.raises(WsReadTimeout):
        await reader.read_exactly_bounded(1, deadline_s=0.01)


@pytest.mark.asyncio
async def test_read_exactly_progress_drip_feed_below_window_minimum() -> None:
    clock = FakeClock()
    reader = BufferedWsReader(AdvancingWs([b"a", b"b", b"c"], clock, advance_s=1.1))

    with pytest.raises(WsProgressTimeout):
        await reader.read_exactly_progress(
            3,
            deadline_s=10.0,
            window_s=1.0,
            min_bytes_per_window=2,
            time_source=clock,
        )


@pytest.mark.asyncio
async def test_read_exactly_progress_absolute_deadline_is_not_reset() -> None:
    clock = FakeClock()
    reader = BufferedWsReader(AdvancingWs([b"aa", b"bb", b"cc"], clock, advance_s=1.1))

    with pytest.raises(WsReadTimeout):
        await reader.read_exactly_progress(
            6,
            deadline_s=2.0,
            window_s=1.0,
            min_bytes_per_window=2,
            time_source=clock,
        )


def test_bounded_timeout_exceptions_are_timeout_errors() -> None:
    assert issubclass(WsReadTimeout, TimeoutError)
    assert issubclass(WsProgressTimeout, TimeoutError)
