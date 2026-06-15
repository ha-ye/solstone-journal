# SPDX-License-Identifier: AGPL-3.0-only
# Copyright (c) 2026 sol pbc

from __future__ import annotations

import asyncio
from collections.abc import Callable

import pytest

from solstone.convey.secure_listener.framing import (
    FLAG_PING,
    FLAG_PONG,
    FLAG_RESET,
    Frame,
    FrameDecoder,
    build_close,
    build_data,
    build_ping,
    build_pong,
)
from solstone.think.link import client


class FakeTransport:
    def __init__(self, *, auto_pong: bool = False) -> None:
        self.sent: list[bytes] = []
        self.inbound: asyncio.Queue[bytes | None] = asyncio.Queue()
        self.closed = False
        self._auto_pong = auto_pong
        self._decoder = FrameDecoder()

    async def send(self, data: bytes) -> None:
        self.sent.append(data)
        if not self._auto_pong:
            return
        self._decoder.feed(data)
        for frame in self._decoder.drain():
            if frame.stream_id == 0 and frame.flags & FLAG_PING:
                self.inbound.put_nowait(build_pong(frame.payload).encode())

    async def recv(self) -> bytes | None:
        return await self.inbound.get()

    async def close(self) -> None:
        if self.closed:
            return
        self.closed = True
        self.inbound.put_nowait(None)


def _decode_frames(chunks: list[bytes]) -> list[Frame]:
    decoder = FrameDecoder()
    for chunk in chunks:
        decoder.feed(chunk)
    return decoder.drain()


def _ping_frames(chunks: list[bytes]) -> list[Frame]:
    return [
        frame
        for frame in _decode_frames(chunks)
        if frame.stream_id == 0 and frame.flags & FLAG_PING
    ]


async def _wait_for(
    predicate: Callable[[], bool],
    *,
    timeout: float = 1.0,
) -> None:
    loop = asyncio.get_running_loop()
    deadline = loop.time() + timeout
    while loop.time() < deadline:
        if predicate():
            return
        await asyncio.sleep(0.001)
    raise AssertionError("condition was not reached")


@pytest.fixture
def pass_through_tls(monkeypatch: pytest.MonkeyPatch) -> None:
    def drive_tls(
        _state: object,
        *,
        inbound: bytes = b"",
        plaintext_out: bytes = b"",
    ) -> tuple[bytes, bytes]:
        return plaintext_out, inbound

    monkeypatch.setattr(client, "_drive_tls_client", drive_tls)


def _session(
    transport: FakeTransport,
    *,
    keepalive_interval: float = 1.0,
    keepalive_timeout: float = 5.0,
) -> client.TunnelSession:
    return client.TunnelSession(
        transport=transport,
        tls=client._TlsClientState(conn=object()),
        keepalive_interval=keepalive_interval,
        keepalive_timeout=keepalive_timeout,
    )


@pytest.mark.asyncio
async def test_dialer_mux_ping_emits_matching_pong() -> None:
    sent: list[bytes] = []

    async def send(data: bytes) -> None:
        sent.append(data)

    nonce = b"12345678"
    mux = client._DialerMultiplexer(send)
    await mux.feed(build_ping(nonce).encode())

    frames = _decode_frames(sent)
    assert [f.payload for f in frames if f.stream_id == 0 and f.flags & FLAG_PONG] == [
        nonce
    ]
    assert not any(f.stream_id == 0 and f.flags & FLAG_RESET for f in frames)


@pytest.mark.asyncio
async def test_dialer_mux_pong_records_liveness_without_emit() -> None:
    sent: list[bytes] = []
    inbound_count = 0

    async def send(data: bytes) -> None:
        sent.append(data)

    def on_inbound() -> None:
        nonlocal inbound_count
        inbound_count += 1

    mux = client._DialerMultiplexer(send, on_inbound=on_inbound)
    await mux.feed(build_pong(b"abcdefgh").encode())

    assert inbound_count == 1
    assert sent == []


@pytest.mark.asyncio
async def test_dialer_mux_malformed_control_frame_closes_without_raising() -> None:
    sent: list[bytes] = []

    async def send(data: bytes) -> None:
        sent.append(data)

    mux = client._DialerMultiplexer(send)
    await mux.feed(Frame(0, FLAG_PING | FLAG_PONG, b"abcdefgh").encode())

    assert mux._closed is True
    assert sent == []


@pytest.mark.asyncio
async def test_tunnel_session_keepalive_emits_distinct_ping_nonces(
    pass_through_tls: None,
) -> None:
    transport = FakeTransport()
    session = _session(
        transport,
        keepalive_interval=0.01,
        keepalive_timeout=1.0,
    )
    try:
        await _wait_for(lambda: len(_ping_frames(transport.sent)) >= 2)
        pings = _ping_frames(transport.sent)[:2]
        assert all(len(frame.payload) == 8 for frame in pings)
        assert pings[0].payload != pings[1].payload
    finally:
        await session.close()


@pytest.mark.asyncio
async def test_tunnel_session_pongs_keep_session_alive_and_requests_work(
    pass_through_tls: None,
) -> None:
    transport = FakeTransport(auto_pong=True)
    session = _session(
        transport,
        keepalive_interval=0.01,
        keepalive_timeout=0.03,
    )
    try:
        await _wait_for(lambda: len(_ping_frames(transport.sent)) >= 4)
        assert session.is_alive is True

        request_task = asyncio.create_task(session.request("GET", "/"))
        await _wait_for(
            lambda: any(
                frame.stream_id == 1 for frame in _decode_frames(transport.sent)
            )
        )
        response = b"HTTP/1.1 200 OK\r\nContent-Length: 2\r\n\r\nok"
        transport.inbound.put_nowait(build_data(1, response).encode())
        transport.inbound.put_nowait(build_close(1).encode())

        assert await request_task == (200, {"content-length": "2"}, b"ok")
        assert session.is_alive is True
    finally:
        await session.close()


@pytest.mark.asyncio
async def test_tunnel_session_silent_peer_marks_dead_and_unblocks_stream_read(
    pass_through_tls: None,
) -> None:
    transport = FakeTransport()
    session = _session(
        transport,
        keepalive_interval=0.01,
        keepalive_timeout=0.02,
    )
    try:
        stream = await session._mux.open_stream(b"GET / HTTP/1.1\r\n\r\n")
        read_task = asyncio.create_task(stream.read().__anext__())

        await _wait_for(lambda: not session.is_alive)

        assert transport.closed is True
        with pytest.raises(client.StreamResetError):
            await asyncio.wait_for(read_task, timeout=1.0)
    finally:
        await session.close()


@pytest.mark.asyncio
async def test_tunnel_session_close_reaps_keepalive_task(
    pass_through_tls: None,
) -> None:
    transport = FakeTransport()
    session = _session(
        transport,
        keepalive_interval=0.1,
        keepalive_timeout=1.0,
    )
    task = session._keepalive_task

    await session.close()

    assert task is not None
    assert task.done() is True
    assert task.cancelled() is True
