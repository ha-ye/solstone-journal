# SPDX-License-Identifier: AGPL-3.0-only
# Copyright (c) 2026 sol pbc

from __future__ import annotations

import asyncio
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from typing import Any

import pytest

from solstone.convey.secure_listener.framing import (
    FLAG_CLOSE,
    FLAG_DATA,
    FLAG_PING,
    FLAG_PONG,
    FLAG_RESET,
    FLAG_WINDOW,
    INITIAL_WINDOW,
    MAX_CONCURRENT_STREAMS,
    RESET_CANCEL,
    RESET_FLOW_CONTROL_ERROR,
    RESET_INTERNAL_ERROR,
    RESET_PROTOCOL_ERROR,
    RESET_STREAM_LIMIT_EXCEEDED,
    Frame,
    FrameDecoder,
    build_close,
    build_data,
    build_open,
    build_ping,
    build_pong,
    parse_reset_reason,
)
from solstone.convey.secure_listener.mux import (
    RESET_CTX_APP_CANCELLATION,
    RESET_CTX_BAD_WINDOW_FRAME,
    RESET_CTX_BODY_DISCARD_CANCELLATION,
    RESET_CTX_DUPLICATE_OPEN,
    RESET_CTX_HANDLER_EXCEPTION,
    RESET_CTX_MALFORMED_FRAME,
    RESET_CTX_NO_IDENTITY,
    RESET_CTX_OVER_CREDIT_DATA,
    RESET_CTX_OVER_CREDIT_OPEN,
    RESET_CTX_PARITY_VIOLATION,
    RESET_CTX_STREAM_CAP_OVERFLOW,
    RESET_CTX_UNKNOWN_STREAM,
    Multiplexer,
    ResetDiagnostic,
)
from solstone.convey.secure_listener.wsgi import dispatch_stream
from solstone.think.link.client import _http_head_bytes
from tests.link.certless_helpers import (
    certless_identity,
    make_convey_app,
    pl_identity,
)


def _decode_frames(chunks: list[bytes]) -> list[Frame]:
    decoder = FrameDecoder()
    for chunk in chunks:
        decoder.feed(chunk)
    return decoder.drain()


def _reset_reasons(frames: list[Frame], stream_id: int) -> list[int]:
    return [
        parse_reset_reason(frame)
        for frame in frames
        if frame.stream_id == stream_id and frame.flags & FLAG_RESET
    ]


async def _wait_for_draining_stream(mux: Multiplexer, stream_id: int) -> Any:
    for _ in range(100):
        await asyncio.sleep(0.005)
        state = mux._streams.get(stream_id)
        if state is not None and state.draining and state.task and state.task.done():
            return state
    raise AssertionError(f"stream {stream_id} did not enter completed drain state")


async def _wait_for_stream_data(sent: list[bytes], stream_id: int) -> bytes:
    for _ in range(100):
        await asyncio.sleep(0.005)
        payload = b"".join(
            frame.payload
            for frame in _decode_frames(sent)
            if frame.stream_id == stream_id and frame.flags & FLAG_DATA
        )
        if payload:
            return payload
    raise AssertionError(f"stream {stream_id} did not emit DATA")


def _assert_single_reset(
    frames: list[Frame],
    stream_id: int,
    reason: int,
) -> None:
    assert _reset_reasons(frames, stream_id) == [reason]


@pytest.mark.asyncio
async def test_open_with_initial_payload_hits_handler() -> None:
    handler_seen: dict[int, bytes] = {}

    async def handler(
        reader: asyncio.StreamReader, writer: object
    ) -> None:  # pragma: no cover - typed by mux
        data = await reader.readuntil(b"\n")
        handler_seen[1] = data
        await writer.write(b"ack\n")  # type: ignore[attr-defined]
        await writer.close()  # type: ignore[attr-defined]

    sent: list[bytes] = []

    async def send(data: bytes) -> None:
        sent.append(data)

    mux = Multiplexer(send, handler, is_listener=True)
    await mux.feed(build_open(1, b"hello\n").encode() + build_close(1).encode())

    for _ in range(20):
        await asyncio.sleep(0.005)
        if handler_seen.get(1):
            break

    assert handler_seen.get(1) == b"hello\n"

    frames = _decode_frames(sent)
    flags = [frame.flags for frame in frames]
    assert any(flag & FLAG_DATA for flag in flags)
    assert any(flag & FLAG_CLOSE for flag in flags)
    assert (
        b"".join(frame.payload for frame in frames if frame.flags & FLAG_DATA)
        == b"ack\n"
    )
    await mux.close()


@pytest.mark.asyncio
async def test_open_payload_over_window_is_rejected_without_opening() -> None:
    """An OPEN whose payload exceeds the receive window is reset before the
    stream is opened — no reader/task spawned, no data buffered, no credit
    granted (mirrors the DATA-path flow-control guard)."""
    sent: list[bytes] = []
    handler_invoked = False

    async def send(data: bytes) -> None:
        sent.append(data)

    async def handler(*_: object) -> None:  # pragma: no cover - must not run
        nonlocal handler_invoked
        handler_invoked = True
        await asyncio.Event().wait()

    mux = Multiplexer(send, handler, is_listener=True)
    try:
        await mux.feed(build_open(1, b"x" * (INITIAL_WINDOW + 1)).encode())
        for _ in range(10):
            await asyncio.sleep(0)
        assert 1 not in mux._streams  # stream never opened
        assert handler_invoked is False
        frames = _decode_frames(sent)
        _assert_single_reset(frames, 1, RESET_FLOW_CONTROL_ERROR)
        assert not any(frame.flags & FLAG_WINDOW for frame in frames)
    finally:
        await mux.close()


@pytest.mark.asyncio
async def test_open_payload_at_window_boundary_opens() -> None:
    """An OPEN payload of exactly INITIAL_WINDOW is within the window (the
    guard is strictly greater-than) and opens the stream normally."""

    async def send(_: bytes) -> None:
        return

    async def handler(*_: object) -> None:
        await asyncio.Event().wait()

    mux = Multiplexer(send, handler, is_listener=True)
    try:
        await mux.feed(build_open(1, b"x" * INITIAL_WINDOW).encode())
        for _ in range(10):
            await asyncio.sleep(0)
            if 1 in mux._streams:
                break
        assert 1 in mux._streams  # boundary payload opened the stream
    finally:
        await mux.close()


@pytest.mark.asyncio
async def test_wrong_parity_stream_id_gets_reset() -> None:
    sent: list[bytes] = []

    async def send(data: bytes) -> None:
        sent.append(data)

    async def handler(*_: object) -> None:
        pytest.fail("handler should not be reached for wrong-parity stream ids")

    mux = Multiplexer(send, handler, is_listener=True)
    await mux.feed(build_open(2).encode())

    frames = _decode_frames(sent)
    assert any(frame.stream_id == 2 and frame.flags & FLAG_RESET for frame in frames)
    await mux.close()


@pytest.mark.asyncio
async def test_unknown_stream_data_gets_reset() -> None:
    sent: list[bytes] = []

    async def send(data: bytes) -> None:
        sent.append(data)

    async def handler(*_: object) -> None:
        return

    mux = Multiplexer(send, handler, is_listener=True)
    await mux.feed(build_data(99, b"x").encode())

    frames = _decode_frames(sent)
    assert any(frame.stream_id == 99 and frame.flags & FLAG_RESET for frame in frames)
    await mux.close()


@pytest.mark.asyncio
async def test_concurrent_streams_do_not_interfere() -> None:
    responses: dict[int, bytes] = {}

    async def handler(
        reader: asyncio.StreamReader, writer: object
    ) -> None:  # pragma: no cover - typed by mux
        payload = await reader.readuntil(b"\n")
        await writer.write(payload)  # type: ignore[attr-defined]
        await writer.close()  # type: ignore[attr-defined]

    sent: list[bytes] = []

    async def send(data: bytes) -> None:
        sent.append(data)

    mux = Multiplexer(send, handler, is_listener=True)
    bulk = bytearray()
    for stream_id in (1, 3, 5, 7, 9):
        bulk.extend(build_open(stream_id, f"stream-{stream_id}\n".encode()).encode())
        bulk.extend(build_close(stream_id).encode())

    await mux.feed(bytes(bulk))

    for _ in range(50):
        await asyncio.sleep(0.005)
        frames = _decode_frames(sent)
        for frame in frames:
            if frame.flags & FLAG_DATA:
                responses.setdefault(frame.stream_id, b"")
                responses[frame.stream_id] += frame.payload
        if all(stream_id in responses for stream_id in (1, 3, 5, 7, 9)):
            break

    for stream_id in (1, 3, 5, 7, 9):
        assert responses.get(stream_id) == f"stream-{stream_id}\n".encode()
    await mux.close()


@pytest.mark.asyncio
async def test_validates_open_reopen_is_protocol_error() -> None:
    sent: list[bytes] = []

    async def send(data: bytes) -> None:
        sent.append(data)

    gate = asyncio.Event()

    async def handler(
        reader: asyncio.StreamReader, writer: object
    ) -> None:  # pragma: no cover - typed by mux
        await gate.wait()
        await writer.close()  # type: ignore[attr-defined]

    mux = Multiplexer(send, handler, is_listener=True)
    await mux.feed(build_open(1).encode())
    await asyncio.sleep(0.01)
    await mux.feed(build_open(1).encode())

    frames = _decode_frames(sent)
    assert any(frame.stream_id == 1 and frame.flags & FLAG_RESET for frame in frames)

    gate.set()
    await mux.close()


# ---- streamID==0 PING/PONG keepalive responder ------------------------------


@pytest.mark.asyncio
async def test_ping_emits_matching_pong() -> None:
    sent: list[bytes] = []

    async def send(data: bytes) -> None:
        sent.append(data)

    async def handler(*_: object) -> None:
        pytest.fail("handler should not be invoked for control frames")

    nonce = bytes(range(1, 9))
    mux = Multiplexer(send, handler, is_listener=True)
    await mux.feed(build_ping(nonce).encode())

    frames = _decode_frames(sent)
    pongs = [f for f in frames if f.stream_id == 0 and f.flags & FLAG_PONG]
    assert len(pongs) == 1
    assert pongs[0].payload == nonce
    await mux.close()


@pytest.mark.asyncio
async def test_repeated_pings_each_get_matching_pong() -> None:
    sent: list[bytes] = []

    async def send(data: bytes) -> None:
        sent.append(data)

    async def handler(*_: object) -> None:
        pytest.fail("handler should not be invoked for control frames")

    nonces = [bytes([i]) * 8 for i in range(1, 6)]
    mux = Multiplexer(send, handler, is_listener=True)
    for nonce in nonces:
        await mux.feed(build_ping(nonce).encode())

    pongs = [f for f in _decode_frames(sent) if f.flags & FLAG_PONG]
    assert [p.payload for p in pongs] == nonces
    await mux.close()


@pytest.mark.asyncio
async def test_unsolicited_pong_is_silently_dropped() -> None:
    sent: list[bytes] = []

    async def send(data: bytes) -> None:
        sent.append(data)

    async def handler(*_: object) -> None:
        pytest.fail("handler should not be invoked for control frames")

    mux = Multiplexer(send, handler, is_listener=True)
    await mux.feed(build_pong(b"\x00" * 8).encode())

    # No emit on stray PONG — neither RESET nor PONG nor any other frame.
    assert sent == []
    await mux.close()


@pytest.mark.asyncio
async def test_ping_on_nonzero_stream_is_protocol_error() -> None:
    sent: list[bytes] = []

    async def send(data: bytes) -> None:
        sent.append(data)

    async def handler(*_: object) -> None:
        return

    mux = Multiplexer(send, handler, is_listener=True)
    # PING on stream 5 (illegal — control frames are streamID==0 only).
    illegal = Frame(stream_id=5, flags=FLAG_PING, payload=b"\x00" * 8).encode()
    await mux.feed(illegal)

    # Behavior parity with other top-level protocol errors: a RESET stamps the
    # tunnel as broken; we don't have streams to reset here, so the side effect
    # is internal teardown. The wire effect is no PONG emission.
    frames = _decode_frames(sent)
    assert not any(f.flags & FLAG_PONG for f in frames)
    await mux.close()


@pytest.mark.asyncio
async def test_pings_interleave_with_open_streams() -> None:
    # Keepalive cadence is 500ms, which is faster than most app traffic; the
    # responder must not block on or be blocked by concurrent data streams.
    handler_seen: dict[int, bytes] = {}

    async def handler(
        reader: asyncio.StreamReader, writer: object
    ) -> None:  # pragma: no cover - typed by mux
        payload = await reader.readuntil(b"\n")
        handler_seen[1] = payload
        await writer.write(b"ok\n")  # type: ignore[attr-defined]
        await writer.close()  # type: ignore[attr-defined]

    sent: list[bytes] = []

    async def send(data: bytes) -> None:
        sent.append(data)

    mux = Multiplexer(send, handler, is_listener=True)
    nonce = b"\xab" * 8
    # PING arrives mid-stream between OPEN and CLOSE.
    await mux.feed(build_open(1, b"hello\n").encode())
    await mux.feed(build_ping(nonce).encode())
    await mux.feed(build_close(1).encode())

    for _ in range(20):
        await asyncio.sleep(0.005)
        if handler_seen.get(1):
            break

    assert handler_seen.get(1) == b"hello\n"
    frames = _decode_frames(sent)
    pongs = [f for f in frames if f.flags & FLAG_PONG]
    assert len(pongs) == 1 and pongs[0].payload == nonce
    data_payload = b"".join(f.payload for f in frames if f.flags & FLAG_DATA)
    assert data_payload == b"ok\n"
    await mux.close()


@pytest.mark.asyncio
async def test_early_bridge_response_drains_in_flight_body_without_unknown_stream_reset(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    app, _journal = make_convey_app(tmp_path, monkeypatch, link={"posture": "spl"})
    identity = certless_identity()
    sent: list[bytes] = []
    loop = asyncio.get_running_loop()

    async def send(data: bytes) -> None:
        sent.append(data)

    with ThreadPoolExecutor(max_workers=1) as executor:

        async def handler(
            reader: asyncio.StreamReader,
            writer: Any,
        ) -> None:
            stream_identity = certless_identity() if writer.stream_id == 3 else identity
            await dispatch_stream(app, stream_identity, reader, writer, loop, executor)

        mux = Multiplexer(send, handler, is_listener=True)
        try:
            head = _http_head_bytes(
                "POST",
                "/app/network/api/status",
                headers={"content-type": "application/octet-stream"},
                content_length=4096,
            )
            await mux.feed(build_open(1, head).encode())
            state = await _wait_for_draining_stream(mux, 1)

            await mux.feed(build_data(1, b"x" * 100).encode())
            await mux.feed(build_data(1, b"y" * 100, close=True).encode())

            frames = _decode_frames(sent)
            assert b"HTTP/1.1 403 Forbidden" in b"".join(
                frame.payload for frame in frames if frame.flags & FLAG_DATA
            )
            assert _reset_reasons(frames, 1) == []
            assert 1 not in mux._streams
            assert state.task is not None and state.task.done()
        finally:
            await mux.close()


@pytest.mark.asyncio
async def test_early_bridge_response_cancels_on_drain_budget_exhaustion(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    app, _journal = make_convey_app(tmp_path, monkeypatch, link={"posture": "spl"})
    identity = certless_identity()
    sent: list[bytes] = []
    diags: list[ResetDiagnostic] = []
    loop = asyncio.get_running_loop()

    async def send(data: bytes) -> None:
        sent.append(data)

    with ThreadPoolExecutor(max_workers=1) as executor:

        async def handler(
            reader: asyncio.StreamReader,
            writer: Any,
        ) -> None:
            stream_identity = certless_identity() if writer.stream_id == 3 else identity
            await dispatch_stream(app, stream_identity, reader, writer, loop, executor)

        mux = Multiplexer(send, handler, is_listener=True, on_reset=diags.append)
        try:
            head = _http_head_bytes(
                "POST",
                "/app/network/api/status",
                headers={"content-type": "application/octet-stream"},
                content_length=INITIAL_WINDOW + 100,
            )
            await mux.feed(build_open(1, head).encode())
            state = await _wait_for_draining_stream(mux, 1)
            budget = state.recv_credit
            before = len(sent)

            await mux.feed(build_data(1, b"x" * budget).encode())

            frames = _decode_frames(sent[before:])
            _assert_single_reset(frames, 1, RESET_CANCEL)
            assert not any(
                frame.stream_id == 1 and frame.flags & FLAG_WINDOW for frame in frames
            )
            assert 1 not in mux._streams
            assert state.task is not None and state.task.done()
            assert diags[-1] == ResetDiagnostic(
                stream_id=1,
                reason_code=RESET_CANCEL,
                reason_name="cancel",
                context=RESET_CTX_BODY_DISCARD_CANCELLATION,
            )
        finally:
            await mux.close()


@pytest.mark.asyncio
async def test_app_early_response_drains_then_cancels_with_app_context(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    app, _journal = make_convey_app(tmp_path, monkeypatch, link={"posture": "spl"})
    identity = pl_identity("sha256:deadbeef")
    sent: list[bytes] = []
    diags: list[ResetDiagnostic] = []
    loop = asyncio.get_running_loop()

    async def send(data: bytes) -> None:
        sent.append(data)

    with ThreadPoolExecutor(max_workers=1) as executor:

        async def handler(
            reader: asyncio.StreamReader,
            writer: Any,
        ) -> None:
            stream_identity = certless_identity() if writer.stream_id == 3 else identity
            await dispatch_stream(app, stream_identity, reader, writer, loop, executor)

        mux = Multiplexer(send, handler, is_listener=True, on_reset=diags.append)
        try:
            head = _http_head_bytes(
                "POST",
                "/app/observer/ingest",
                headers={"content-type": "multipart/form-data; boundary=x"},
                content_length=INITIAL_WINDOW + 100,
            )
            await mux.feed(build_open(1, head).encode())
            state = await _wait_for_draining_stream(mux, 1)
            assert b"HTTP/1.1 401" in b"".join(
                frame.payload
                for frame in _decode_frames(sent)
                if frame.stream_id == 1 and frame.flags & FLAG_DATA
            )

            await mux.feed(build_data(1, b"x" * state.recv_credit).encode())

            frames = _decode_frames(sent)
            _assert_single_reset(frames, 1, RESET_CANCEL)
            assert diags[-1].context == RESET_CTX_APP_CANCELLATION
            assert 1 not in mux._streams

            second_head = _http_head_bytes(
                "POST",
                "/app/network/pair",
                headers={},
                content_length=0,
            )
            await mux.feed(
                build_open(3, second_head).encode() + build_close(3).encode()
            )
            second_payload = await _wait_for_stream_data(sent, 3)
            assert b"HTTP/1.1 403" in second_payload
            assert _reset_reasons(_decode_frames(sent), 3) == []
            assert not mux._closed
        finally:
            await mux.close()


@pytest.mark.asyncio
async def test_handler_exception_is_stream_scoped() -> None:
    sent: list[bytes] = []
    diags: list[ResetDiagnostic] = []

    async def send(data: bytes) -> None:
        sent.append(data)

    async def handler(
        reader: asyncio.StreamReader,
        writer: Any,
    ) -> None:
        if writer.stream_id == 1:
            raise RuntimeError("boom-secret")
        payload = await reader.readuntil(b"\n")
        await writer.write(payload)
        await writer.close()

    mux = Multiplexer(send, handler, is_listener=True, on_reset=diags.append)
    try:
        await mux.feed(build_open(1, b"fail\n").encode())
        for _ in range(20):
            await asyncio.sleep(0)
            if _reset_reasons(_decode_frames(sent), 1):
                break
        _assert_single_reset(_decode_frames(sent), 1, RESET_INTERNAL_ERROR)
        assert diags[-1].context == RESET_CTX_HANDLER_EXCEPTION

        await mux.feed(build_open(3, b"ok\n").encode() + build_close(3).encode())
        payload = await _wait_for_stream_data(sent, 3)
        assert payload == b"ok\n"
        assert _reset_reasons(_decode_frames(sent), 3) == []
    finally:
        await mux.close()


@pytest.mark.asyncio
async def test_true_mux_violations_keep_reason_codes() -> None:
    async def run_single(
        action: Any,
        *,
        stream_id: int,
        reason: int,
        context: str,
    ) -> None:
        sent: list[bytes] = []
        diags: list[ResetDiagnostic] = []

        async def send(data: bytes) -> None:
            sent.append(data)

        async def handler(*_: object) -> None:
            await asyncio.Event().wait()

        mux = Multiplexer(send, handler, is_listener=True, on_reset=diags.append)
        try:
            await action(mux)
            frames = _decode_frames(sent)
            _assert_single_reset(frames, stream_id, reason)
            assert diags[-1].stream_id == stream_id
            assert diags[-1].reason_code == reason
            assert diags[-1].context == context
        finally:
            await mux.close()

    async def unknown(mux: Multiplexer) -> None:
        await mux.feed(build_data(99, b"x").encode())

    async def parity(mux: Multiplexer) -> None:
        await mux.feed(build_open(2).encode())

    async def duplicate(mux: Multiplexer) -> None:
        await mux.feed(build_open(1).encode())
        await asyncio.sleep(0)
        await mux.feed(build_open(1).encode())

    async def over_credit(mux: Multiplexer) -> None:
        await mux.feed(build_open(1).encode())
        await asyncio.sleep(0)
        await mux.feed(build_data(1, b"x" * (INITIAL_WINDOW + 1)).encode())

    async def over_credit_open(mux: Multiplexer) -> None:
        await mux.feed(build_open(1, b"x" * (INITIAL_WINDOW + 1)).encode())

    async def stream_cap(mux: Multiplexer) -> None:
        for offset in range(MAX_CONCURRENT_STREAMS):
            await mux.feed(build_open(1 + offset * 2).encode())
        await mux.feed(build_open(1 + MAX_CONCURRENT_STREAMS * 2).encode())

    async def bad_window(mux: Multiplexer) -> None:
        await mux.feed(build_open(1).encode())
        await asyncio.sleep(0)
        await mux.feed(Frame(stream_id=1, flags=FLAG_WINDOW, payload=b"x").encode())

    await run_single(
        unknown,
        stream_id=99,
        reason=RESET_PROTOCOL_ERROR,
        context=RESET_CTX_UNKNOWN_STREAM,
    )
    await run_single(
        parity,
        stream_id=2,
        reason=RESET_PROTOCOL_ERROR,
        context=RESET_CTX_PARITY_VIOLATION,
    )
    await run_single(
        duplicate,
        stream_id=1,
        reason=RESET_PROTOCOL_ERROR,
        context=RESET_CTX_DUPLICATE_OPEN,
    )
    await run_single(
        over_credit,
        stream_id=1,
        reason=RESET_FLOW_CONTROL_ERROR,
        context=RESET_CTX_OVER_CREDIT_DATA,
    )
    await run_single(
        over_credit_open,
        stream_id=1,
        reason=RESET_FLOW_CONTROL_ERROR,
        context=RESET_CTX_OVER_CREDIT_OPEN,
    )
    await run_single(
        stream_cap,
        stream_id=1 + MAX_CONCURRENT_STREAMS * 2,
        reason=RESET_STREAM_LIMIT_EXCEEDED,
        context=RESET_CTX_STREAM_CAP_OVERFLOW,
    )
    await run_single(
        bad_window,
        stream_id=1,
        reason=RESET_PROTOCOL_ERROR,
        context=RESET_CTX_BAD_WINDOW_FRAME,
    )


@pytest.mark.asyncio
async def test_reset_diagnostics_distinguish_contexts_and_are_privacy_clean(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    diags: list[ResetDiagnostic] = []

    async def send(_: bytes) -> None:
        return

    async def collect_with_handler(handler: Any, frame: bytes) -> None:
        mux = Multiplexer(send, handler, is_listener=True, on_reset=diags.append)
        try:
            await mux.feed(frame)
            for _ in range(20):
                await asyncio.sleep(0)
                if mux._streams == {}:
                    break
        finally:
            await mux.close()

    async def waiting_handler(*_: object) -> None:
        await asyncio.Event().wait()

    await collect_with_handler(waiting_handler, build_data(99, b"secret-body").encode())
    await collect_with_handler(waiting_handler, build_open(2).encode())

    duplicate_mux = Multiplexer(
        send,
        waiting_handler,
        is_listener=True,
        on_reset=diags.append,
    )
    try:
        await duplicate_mux.feed(build_open(1).encode())
        await asyncio.sleep(0)
        await duplicate_mux.feed(build_open(1).encode())
    finally:
        await duplicate_mux.close()

    over_credit_mux = Multiplexer(
        send,
        waiting_handler,
        is_listener=True,
        on_reset=diags.append,
    )
    try:
        await over_credit_mux.feed(build_open(1).encode())
        await asyncio.sleep(0)
        await over_credit_mux.feed(
            build_data(1, b"secret-body" * (INITIAL_WINDOW // 11 + 1)).encode()
        )
    finally:
        await over_credit_mux.close()

    async def no_identity_handler(
        _reader: asyncio.StreamReader,
        writer: Any,
    ) -> None:
        await writer.reset(RESET_INTERNAL_ERROR, RESET_CTX_NO_IDENTITY)

    await collect_with_handler(no_identity_handler, build_open(1).encode())

    async def raising_handler(*_: object) -> None:
        raise RuntimeError("boom-secret")

    await collect_with_handler(raising_handler, build_open(1).encode())

    malformed_mux = Multiplexer(
        send,
        waiting_handler,
        is_listener=True,
        on_reset=diags.append,
    )
    try:
        await malformed_mux.feed(
            Frame(stream_id=5, flags=FLAG_PING, payload=b"\x00" * 8).encode()
        )
    finally:
        await malformed_mux.close()

    app, _journal = make_convey_app(tmp_path, monkeypatch, link={"posture": "spl"})
    loop = asyncio.get_running_loop()
    with ThreadPoolExecutor(max_workers=1) as executor:

        async def bridge_handler(
            reader: asyncio.StreamReader,
            writer: Any,
        ) -> None:
            await dispatch_stream(
                app,
                certless_identity(),
                reader,
                writer,
                loop,
                executor,
            )

        bridge_mux = Multiplexer(
            send,
            bridge_handler,
            is_listener=True,
            on_reset=diags.append,
        )
        try:
            head = _http_head_bytes(
                "POST",
                "/secret/upload?token=abc",
                headers={"authorization": "Bearer secret-header"},
                content_length=INITIAL_WINDOW + 1,
            )
            await bridge_mux.feed(build_open(1, head).encode())
            state = await _wait_for_draining_stream(bridge_mux, 1)
            await bridge_mux.feed(build_data(1, b"x" * state.recv_credit).encode())
        finally:
            await bridge_mux.close()

        async def app_handler(
            reader: asyncio.StreamReader,
            writer: Any,
        ) -> None:
            await dispatch_stream(
                app,
                pl_identity("sha256:deadbeef"),
                reader,
                writer,
                loop,
                executor,
            )

        app_mux = Multiplexer(
            send, app_handler, is_listener=True, on_reset=diags.append
        )
        try:
            head = _http_head_bytes(
                "POST",
                "/app/observer/ingest?filename=secret.txt",
                headers={"x-solstone-observer": "secret-header"},
                content_length=INITIAL_WINDOW + 1,
            )
            await app_mux.feed(build_open(1, head).encode())
            state = await _wait_for_draining_stream(app_mux, 1)
            await app_mux.feed(build_data(1, b"x" * state.recv_credit).encode())
        finally:
            await app_mux.close()

    contexts = {diag.context for diag in diags}
    assert {
        RESET_CTX_UNKNOWN_STREAM,
        RESET_CTX_PARITY_VIOLATION,
        RESET_CTX_DUPLICATE_OPEN,
        RESET_CTX_OVER_CREDIT_DATA,
        RESET_CTX_NO_IDENTITY,
        RESET_CTX_HANDLER_EXCEPTION,
        RESET_CTX_MALFORMED_FRAME,
        RESET_CTX_BODY_DISCARD_CANCELLATION,
        RESET_CTX_APP_CANCELLATION,
    } <= contexts

    malformed = [diag for diag in diags if diag.context == RESET_CTX_MALFORMED_FRAME]
    assert malformed == [
        ResetDiagnostic(
            stream_id=0,
            reason_code=RESET_PROTOCOL_ERROR,
            reason_name="protocol_error",
            context=RESET_CTX_MALFORMED_FRAME,
        )
    ]

    forbidden = {
        "/secret/upload",
        "token=abc",
        "secret-header",
        "secret-body",
        "secret.txt",
        "boom-secret",
    }
    for diag in diags:
        assert set(diag.__dict__) == {
            "stream_id",
            "reason_code",
            "reason_name",
            "context",
        }
        values = [str(value) for value in diag.__dict__.values()]
        for secret in forbidden:
            assert all(secret not in value for value in values)
