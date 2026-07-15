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
    MAX_SEND_CREDIT,
    RECOMMENDED_CHUNK,
    RESET_CANCEL,
    RESET_FLOW_CONTROL_ERROR,
    RESET_INTERNAL_ERROR,
    RESET_PROTOCOL_ERROR,
    Frame,
    FrameDecoder,
    build_close,
    build_data,
    build_ping,
    build_pong,
    build_reset,
    build_window,
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
    dispatched: list[Frame] = []

    async def send(data: bytes) -> None:
        sent.append(data)

    mux = client._DialerMultiplexer(send)
    original_dispatch = mux._dispatch

    async def dispatch_spy(frame: Frame) -> None:
        dispatched.append(frame)
        await original_dispatch(frame)

    mux._dispatch = dispatch_spy
    await mux.feed(
        Frame(0, FLAG_PING | FLAG_PONG, b"abcdefgh").encode()
        + build_ping(b"12345678").encode()
    )

    assert mux._closed is True
    assert sent == []
    assert [frame.flags for frame in dispatched] == [FLAG_PING | FLAG_PONG]


@pytest.mark.asyncio
async def test_dialer_window_credit_exact_cap_is_accepted() -> None:
    sent: list[bytes] = []

    async def send(data: bytes) -> None:
        sent.append(data)

    mux = client._DialerMultiplexer(send)
    stream = await mux.open_stream()

    await mux.feed(build_window(stream.id, MAX_SEND_CREDIT - INITIAL_WINDOW).encode())

    assert stream._state.send_credit == MAX_SEND_CREDIT
    assert _reset_frames(sent, stream.id) == []


@pytest.mark.asyncio
async def test_dialer_window_credit_overflow_resets_and_forgets() -> None:
    sent: list[bytes] = []

    async def send(data: bytes) -> None:
        sent.append(data)

    mux = client._DialerMultiplexer(send)
    stream = await mux.open_stream()

    await mux.feed(
        build_window(stream.id, MAX_SEND_CREDIT - INITIAL_WINDOW + 1).encode()
    )

    resets = _reset_frames(sent, stream.id)
    assert len(resets) == 1
    assert parse_reset_reason(resets[0]) == RESET_FLOW_CONTROL_ERROR
    assert stream._state.reset_reason == RESET_FLOW_CONTROL_ERROR
    assert stream.id not in mux._streams


@pytest.mark.asyncio
async def test_dialer_window_credit_uses_remaining_credit_accounting() -> None:
    sent: list[bytes] = []

    async def send(data: bytes) -> None:
        sent.append(data)

    mux = client._DialerMultiplexer(send)
    stream = await mux.open_stream()
    await stream.write(b"x")
    assert stream._state.send_credit == INITIAL_WINDOW - 1

    await mux.feed(
        build_window(stream.id, MAX_SEND_CREDIT - INITIAL_WINDOW + 1).encode()
    )

    assert stream._state.send_credit == MAX_SEND_CREDIT
    assert _reset_frames(sent, stream.id) == []

    no_consume_sent: list[bytes] = []

    async def no_consume_send(data: bytes) -> None:
        no_consume_sent.append(data)

    no_consume_mux = client._DialerMultiplexer(no_consume_send)
    no_consume_stream = await no_consume_mux.open_stream()
    await no_consume_mux.feed(
        build_window(
            no_consume_stream.id,
            MAX_SEND_CREDIT - INITIAL_WINDOW + 1,
        ).encode()
    )
    no_consume_resets = _reset_frames(no_consume_sent, no_consume_stream.id)
    assert len(no_consume_resets) == 1
    assert parse_reset_reason(no_consume_resets[0]) == RESET_FLOW_CONTROL_ERROR

    accum_sent: list[bytes] = []

    async def accum_send(data: bytes) -> None:
        accum_sent.append(data)

    accum_mux = client._DialerMultiplexer(accum_send)
    accum_stream = await accum_mux.open_stream()
    await accum_mux.feed(build_window(accum_stream.id, 0x40000000).encode())
    assert accum_stream._state.send_credit == 1_074_790_400
    assert _reset_frames(accum_sent, accum_stream.id) == []
    await accum_mux.feed(build_window(accum_stream.id, 0x40000000).encode())
    accum_resets = _reset_frames(accum_sent, accum_stream.id)
    assert len(accum_resets) == 1
    assert parse_reset_reason(accum_resets[0]) == RESET_FLOW_CONTROL_ERROR

    single_sent: list[bytes] = []

    async def single_send(data: bytes) -> None:
        single_sent.append(data)

    single_mux = client._DialerMultiplexer(single_send)
    single_stream = await single_mux.open_stream()
    await single_mux.feed(build_window(single_stream.id, 0xFFFFFFFF).encode())
    single_resets = _reset_frames(single_sent, single_stream.id)
    assert len(single_resets) == 1
    assert parse_reset_reason(single_resets[0]) == RESET_FLOW_CONTROL_ERROR


@pytest.mark.asyncio
async def test_dialer_invalid_flags_on_known_stream_reset_and_forget() -> None:
    sent: list[bytes] = []

    async def send(data: bytes) -> None:
        sent.append(data)

    mux = client._DialerMultiplexer(send)
    stream = await mux.open_stream()

    await mux.feed(Frame(stream.id, FLAG_DATA | FLAG_WINDOW, b"x").encode())

    resets = _reset_frames(sent, stream.id)
    assert len(resets) == 1
    assert parse_reset_reason(resets[0]) == RESET_PROTOCOL_ERROR
    assert stream._state.buffered == []
    assert stream._state.reset_reason == RESET_PROTOCOL_ERROR
    assert stream.id not in mux._streams


@pytest.mark.asyncio
async def test_dialer_invalid_open_flags_close_existing_stream_before_open_policy() -> (
    None
):
    sent: list[bytes] = []

    async def send(data: bytes) -> None:
        sent.append(data)

    mux = client._DialerMultiplexer(send)
    stream = await mux.open_stream()

    await mux.feed(Frame(stream.id, FLAG_OPEN | FLAG_WINDOW, b"").encode())

    resets = _reset_frames(sent, stream.id)
    assert len(resets) == 1
    assert parse_reset_reason(resets[0]) == RESET_PROTOCOL_ERROR
    assert stream._state.reset_reason == RESET_PROTOCOL_ERROR
    assert stream.id not in mux._streams


@pytest.mark.asyncio
async def test_dialer_misplaced_control_on_unknown_stream_resets() -> None:
    sent: list[bytes] = []

    async def send(data: bytes) -> None:
        sent.append(data)

    mux = client._DialerMultiplexer(send)

    await mux.feed(Frame(99, FLAG_PING, b"\x00" * 8).encode())

    resets = _reset_frames(sent, 99)
    assert len(resets) == 1
    assert parse_reset_reason(resets[0]) == RESET_PROTOCOL_ERROR
    assert 99 not in mux._streams
    assert mux._closed is False


@pytest.mark.asyncio
async def test_dialer_misplaced_control_on_known_stream_beats_invalid_data() -> None:
    sent: list[bytes] = []

    async def send(data: bytes) -> None:
        sent.append(data)

    mux = client._DialerMultiplexer(send)
    stream = await mux.open_stream()

    await mux.feed(Frame(stream.id, FLAG_PING | FLAG_DATA, b"x").encode())

    resets = _reset_frames(sent, stream.id)
    assert len(resets) == 1
    assert parse_reset_reason(resets[0]) == RESET_PROTOCOL_ERROR
    assert stream._state.buffered == []
    assert stream._state.reset_reason == RESET_PROTOCOL_ERROR
    assert stream.id not in mux._streams


@pytest.mark.asyncio
async def test_dialer_unknown_stream_close_is_ignored() -> None:
    sent: list[bytes] = []

    async def send(data: bytes) -> None:
        sent.append(data)

    mux = client._DialerMultiplexer(send)

    await mux.feed(build_close(99).encode())

    assert sent == []
    assert mux._closed is False


@pytest.mark.asyncio
async def test_dialer_unknown_stream_reset_is_ignored() -> None:
    sent: list[bytes] = []

    async def send(data: bytes) -> None:
        sent.append(data)

    mux = client._DialerMultiplexer(send)

    await mux.feed(build_reset(99, RESET_CANCEL).encode())

    assert sent == []
    assert mux._closed is False


@pytest.mark.asyncio
async def test_dialer_unknown_stream_data_and_window_get_reset() -> None:
    sent: list[bytes] = []

    async def send(data: bytes) -> None:
        sent.append(data)

    mux = client._DialerMultiplexer(send)

    await mux.feed(build_data(99, b"x").encode())
    await mux.feed(build_window(101, 1).encode())

    data_resets = _reset_frames(sent, 99)
    window_resets = _reset_frames(sent, 101)
    assert len(data_resets) == 1
    assert len(window_resets) == 1
    assert parse_reset_reason(data_resets[0]) == RESET_PROTOCOL_ERROR
    assert parse_reset_reason(window_resets[0]) == RESET_PROTOCOL_ERROR
    assert 99 not in mux._streams
    assert 101 not in mux._streams


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
async def test_tunnel_session_mux_fatal_closes_session_without_keepalive_wait(
    pass_through_tls: None,
) -> None:
    transport = FakeTransport()
    session = _session(
        transport,
        keepalive_interval=60.0,
        keepalive_timeout=60.0,
    )
    try:
        transport.inbound.put_nowait(
            Frame(0, FLAG_PING | FLAG_PONG, b"abcdefgh").encode()
        )

        await _wait_for(
            lambda: session._closed.is_set() and session._reader_task.done(),
            timeout=1.0,
        )

        assert session._mux._closed is True
        assert transport.closed is True
        assert session._reader_task.exception() is None
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
