# SPDX-License-Identifier: AGPL-3.0-only
# Copyright (c) 2026 sol pbc

from __future__ import annotations

import asyncio
import contextlib
from collections.abc import Callable

import pytest

from solstone.convey.secure_listener.framing import (
    FLAG_DATA,
    FLAG_OPEN,
    FLAG_PING,
    FLAG_PONG,
    FLAG_RESET,
    FLAG_WINDOW,
    INITIAL_WINDOW,
    MAX_PAYLOAD,
    RECOMMENDED_CHUNK,
    RESET_CANCEL,
    RESET_FLOW_CONTROL_ERROR,
    RESET_INTERNAL_ERROR,
    Frame,
    FrameDecoder,
    build_close,
    build_data,
    build_ping,
    build_pong,
    build_reset,
    parse_reset_reason,
    parse_window_credit,
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


def _frames_for_stream(chunks: list[bytes], stream_id: int) -> list[Frame]:
    return [frame for frame in _decode_frames(chunks) if frame.stream_id == stream_id]


def _open_frames(chunks: list[bytes], stream_id: int) -> list[Frame]:
    return [
        frame
        for frame in _frames_for_stream(chunks, stream_id)
        if frame.flags & FLAG_OPEN
    ]


def _window_frames(chunks: list[bytes], stream_id: int) -> list[Frame]:
    return [
        frame
        for frame in _frames_for_stream(chunks, stream_id)
        if frame.flags & FLAG_WINDOW
    ]


def _reset_frames(chunks: list[bytes], stream_id: int) -> list[Frame]:
    return [
        frame
        for frame in _frames_for_stream(chunks, stream_id)
        if frame.flags & FLAG_RESET
    ]


def _stream_payload_total(chunks: list[bytes], stream_id: int) -> int:
    return sum(
        len(frame.payload)
        for frame in _frames_for_stream(chunks, stream_id)
        if frame.flags & (FLAG_OPEN | FLAG_DATA)
    )


async def _finish_background_task(task: asyncio.Task) -> None:
    if not task.done():
        task.cancel()
    with contextlib.suppress(asyncio.CancelledError, ConnectionError):
        await task


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


def _probing_body_source(probe: list[int]) -> client.BodySource:
    chunk_count = (INITIAL_WINDOW // RECOMMENDED_CHUNK) * 4

    def chunks():
        for index in range(chunk_count):
            probe.append(index)
            yield bytes([97 + (index % 26)]) * RECOMMENDED_CHUNK

    return client.BodySource(RECOMMENDED_CHUNK * chunk_count, chunks())


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
async def test_tunnel_request_uses_head_only_open_for_body_over_max_payload(
    pass_through_tls: None,
) -> None:
    transport = FakeTransport()
    session = _session(transport)
    body = b"x" * (MAX_PAYLOAD + 1)
    task = asyncio.create_task(
        session.request("POST", "/upload", headers={}, body=body)
    )
    try:
        await _wait_for(lambda: len(_open_frames(transport.sent, 1)) == 1)

        if task.done():
            task.result()
        opens = _open_frames(transport.sent, 1)
        assert len(opens) == 1
        assert opens[0].payload.startswith(b"POST /upload HTTP/1.1\r\n")
        assert opens[0].payload.endswith(b"\r\n\r\n")
        assert body not in opens[0].payload
    finally:
        await session.close()
        await _finish_background_task(task)


@pytest.mark.asyncio
async def test_body_source_is_pulled_lazily_until_send_credit_starves(
    pass_through_tls: None,
) -> None:
    transport = FakeTransport()
    session = _session(transport)
    probe: list[int] = []
    source = _probing_body_source(probe)
    task = asyncio.create_task(
        session.request("POST", "/upload", headers={}, body=source)
    )
    try:
        await _wait_for(
            lambda: _stream_payload_total(transport.sent, 1) == INITIAL_WINDOW
        )

        pulled_bytes = len(probe) * RECOMMENDED_CHUNK
        assert pulled_bytes <= INITIAL_WINDOW
        assert pulled_bytes < source.length
        assert not task.done()
    finally:
        await session.close()
        await _finish_background_task(task)


@pytest.mark.asyncio
async def test_outbound_data_in_flight_is_bounded_by_initial_window(
    pass_through_tls: None,
) -> None:
    transport = FakeTransport()
    session = _session(transport)
    probe: list[int] = []
    source = _probing_body_source(probe)
    task = asyncio.create_task(
        session.request("POST", "/upload", headers={}, body=source)
    )
    try:
        await _wait_for(
            lambda: _stream_payload_total(transport.sent, 1) == INITIAL_WINDOW
        )

        assert _stream_payload_total(transport.sent, 1) == INITIAL_WINDOW
        assert len(probe) * RECOMMENDED_CHUNK < source.length
    finally:
        await session.close()
        await _finish_background_task(task)


@pytest.mark.asyncio
async def test_recv_window_is_granted_only_after_consumer_drains(
    pass_through_tls: None,
) -> None:
    transport = FakeTransport()
    session = _session(transport)
    try:
        stream = await session._mux.open_stream(b"GET / HTTP/1.1\r\n\r\n")
        threshold = INITIAL_WINDOW // 2
        transport.inbound.put_nowait(build_data(stream.id, b"x" * threshold).encode())
        await _wait_for(
            lambda: sum(len(chunk) for chunk in stream._state.buffered) >= threshold
        )

        assert _window_frames(transport.sent, stream.id) == []

        reader = stream.read()
        drained = 0
        while drained < threshold:
            drained += len(await asyncio.wait_for(reader.__anext__(), timeout=1.0))

        windows = _window_frames(transport.sent, stream.id)
        assert len(windows) == 1
        assert parse_window_credit(windows[0]) > 0
    finally:
        await session.close()


@pytest.mark.asyncio
async def test_non_reading_consumer_buffers_initial_window_then_resets_over_credit(
    pass_through_tls: None,
) -> None:
    transport = FakeTransport()
    session = _session(transport)
    try:
        stream = await session._mux.open_stream(b"GET / HTTP/1.1\r\n\r\n")
        transport.inbound.put_nowait(
            build_data(stream.id, b"x" * INITIAL_WINDOW).encode()
        )
        await _wait_for(
            lambda: (
                sum(len(chunk) for chunk in stream._state.buffered) == INITIAL_WINDOW
            )
        )
        assert _reset_frames(transport.sent, stream.id) == []

        transport.inbound.put_nowait(build_data(stream.id, b"x").encode())
        await _wait_for(lambda: len(_reset_frames(transport.sent, stream.id)) == 1)

        resets = _reset_frames(transport.sent, stream.id)
        assert parse_reset_reason(resets[0]) == RESET_FLOW_CONTROL_ERROR
    finally:
        await session.close()


@pytest.mark.asyncio
async def test_stream_cancel_emits_reset_cancel_and_forgets_state(
    pass_through_tls: None,
) -> None:
    transport = FakeTransport()
    session = _session(transport)
    try:
        stream = await session._mux.open_stream(b"GET / HTTP/1.1\r\n\r\n")

        await stream.cancel()

        resets = _reset_frames(transport.sent, stream.id)
        assert len(resets) == 1
        assert parse_reset_reason(resets[0]) == RESET_CANCEL
        assert stream.id not in session._mux._streams
    finally:
        await session.close()


@pytest.mark.asyncio
async def test_request_cancel_while_reading_response_resets_stream(
    pass_through_tls: None,
) -> None:
    transport = FakeTransport()
    session = _session(transport)
    task = asyncio.create_task(session.request("GET", "/slow"))
    try:
        await _wait_for(lambda: len(_open_frames(transport.sent, 1)) == 1)

        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task

        resets = _reset_frames(transport.sent, 1)
        assert len(resets) == 1
        assert parse_reset_reason(resets[0]) == RESET_CANCEL
        assert 1 not in session._mux._streams
    finally:
        await session.close()
        await _finish_background_task(task)


@pytest.mark.asyncio
async def test_mid_body_send_surfaces_remote_reset_or_session_close(
    pass_through_tls: None,
) -> None:
    transport = FakeTransport()
    session = _session(transport)
    probe: list[int] = []
    source = _probing_body_source(probe)
    task = asyncio.create_task(
        session.request("POST", "/upload", headers={}, body=source)
    )
    try:
        await _wait_for(
            lambda: _stream_payload_total(transport.sent, 1) == INITIAL_WINDOW
        )

        transport.inbound.put_nowait(build_reset(1, RESET_INTERNAL_ERROR).encode())

        with pytest.raises(client.StreamResetError):
            await asyncio.wait_for(task, timeout=1.0)
    finally:
        await session.close()
        await _finish_background_task(task)


@pytest.mark.asyncio
async def test_body_source_failure_resets_stream_and_preserves_sibling_stream(
    pass_through_tls: None,
) -> None:
    transport = FakeTransport()
    session = _session(transport)

    def failing_chunks():
        yield b"first"
        raise RuntimeError("body failed")

    source = client.BodySource(RECOMMENDED_CHUNK, failing_chunks())
    failing_task = asyncio.create_task(
        session.request("POST", "/broken", headers={}, body=source)
    )
    try:
        with pytest.raises(RuntimeError, match="body failed"):
            await asyncio.wait_for(failing_task, timeout=1.0)
        resets = _reset_frames(transport.sent, 1)
        assert len(resets) == 1
        assert 1 not in session._mux._streams

        sibling_task = asyncio.create_task(session.request("GET", "/ok"))
        await _wait_for(lambda: len(_open_frames(transport.sent, 3)) == 1)
        response = b"HTTP/1.1 200 OK\r\nContent-Length: 2\r\n\r\nok"
        transport.inbound.put_nowait(build_data(3, response).encode())
        transport.inbound.put_nowait(build_close(3).encode())

        assert await asyncio.wait_for(sibling_task, timeout=1.0) == (
            200,
            {"content-length": "2"},
            b"ok",
        )
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
