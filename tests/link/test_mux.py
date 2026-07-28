# SPDX-License-Identifier: AGPL-3.0-only
# Copyright (c) 2026 sol pbc

from __future__ import annotations

import asyncio
import contextlib
import threading
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import pytest
from flask import Response, request

from solstone.convey import root as root_module
from solstone.convey.secure_listener import mux as mux_module
from solstone.convey.secure_listener import wsgi as wsgi_module
from solstone.convey.secure_listener.framing import (
    FLAG_CLOSE,
    FLAG_DATA,
    FLAG_OPEN,
    FLAG_PING,
    FLAG_PONG,
    FLAG_RESET,
    FLAG_WINDOW,
    INITIAL_WINDOW,
    MAX_CONCURRENT_STREAMS,
    MAX_SEND_CREDIT,
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
    build_reset,
    build_window,
    parse_reset_reason,
    parse_window_credit,
)
from solstone.convey.secure_listener.mux import (
    RESET_CTX_APP_CANCELLATION,
    RESET_CTX_BAD_WINDOW_FRAME,
    RESET_CTX_BODY_DISCARD_CANCELLATION,
    RESET_CTX_DUPLICATE_OPEN,
    RESET_CTX_HANDLER_EXCEPTION,
    RESET_CTX_INVALID_FLAGS,
    RESET_CTX_MALFORMED_FRAME,
    RESET_CTX_MISPLACED_CONTROL,
    RESET_CTX_NO_IDENTITY,
    RESET_CTX_OVER_CREDIT_DATA,
    RESET_CTX_OVER_CREDIT_OPEN,
    RESET_CTX_PARITY_VIOLATION,
    RESET_CTX_SEND_CREDIT_STARVATION,
    RESET_CTX_STREAM_CAP_OVERFLOW,
    RESET_CTX_UNKNOWN_STREAM,
    RESET_CTX_WINDOW_OVERFLOW,
    Multiplexer,
    ResetDiagnostic,
    StreamWriter,
    TunnelFatalError,
)
from solstone.convey.secure_listener.wsgi import dispatch_stream
from solstone.think.link.auth import AuthorizedClients
from solstone.think.link.client import _http_head_bytes, _parse_http_response
from solstone.think.link.paths import authorized_clients_path
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


def _authorize_fingerprint(
    monkeypatch: pytest.MonkeyPatch,
    fingerprint: str,
) -> None:
    authorized = AuthorizedClients(authorized_clients_path())
    authorized.add(fingerprint, "pytest phone", "inst-1")
    monkeypatch.setattr(root_module, "get_authorized_clients", lambda: authorized)


class _ObservedEvent(asyncio.Event):
    def __init__(self, waiter_entered: asyncio.Event) -> None:
        super().__init__()
        self._waiter_entered = waiter_entered

    async def wait(self) -> bool:
        self._waiter_entered.set()
        return await super().wait()


@dataclass
class _ParkedWriter:
    mux: Multiplexer
    stream_id: int
    state: Any
    writer: Any
    task: asyncio.Task[None]
    release_handler: asyncio.Event
    handler_done: asyncio.Event


@dataclass
class _OpenReader:
    mux: Multiplexer
    stream_id: int
    state: Any
    reader: asyncio.StreamReader
    read_task: asyncio.Task[bytes]
    release_handler: asyncio.Event
    handler_done: asyncio.Event


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


def _reset_reason_name(reason: int) -> str:
    if reason == RESET_PROTOCOL_ERROR:
        return "protocol_error"
    if reason == RESET_FLOW_CONTROL_ERROR:
        return "flow_control_error"
    if reason == RESET_CANCEL:
        return "cancel"
    if reason == RESET_INTERNAL_ERROR:
        return "internal_error"
    if reason == RESET_STREAM_LIMIT_EXCEEDED:
        return "stream_limit_exceeded"
    return "unspecified"


def _assert_single_diag(
    diags: list[ResetDiagnostic],
    stream_id: int,
    reason: int,
    context: str,
) -> None:
    assert diags == [
        ResetDiagnostic(
            stream_id=stream_id,
            reason_code=reason,
            reason_name=_reset_reason_name(reason),
            context=context,
        )
    ]


async def _new_parked_writer(stream_id: int = 1) -> _ParkedWriter:
    opened = asyncio.Event()
    release_handler = asyncio.Event()
    handler_done = asyncio.Event()
    captured: dict[str, Any] = {}

    async def send(_: bytes) -> None:
        return

    async def handler(reader: asyncio.StreamReader, writer: Any) -> None:
        captured["reader"] = reader
        captured["writer"] = writer
        opened.set()
        try:
            await release_handler.wait()
        finally:
            handler_done.set()

    mux = Multiplexer(send, handler, is_listener=True)
    await mux.feed(build_open(stream_id).encode())
    await asyncio.wait_for(opened.wait(), timeout=1.0)
    state = mux._streams[stream_id]
    waiter_entered = asyncio.Event()
    state.credit_event = _ObservedEvent(waiter_entered)
    state.send_credit = 0
    task = asyncio.create_task(captured["writer"].write(b"x"))
    await asyncio.wait_for(waiter_entered.wait(), timeout=1.0)
    return _ParkedWriter(
        mux=mux,
        stream_id=stream_id,
        state=state,
        writer=captured["writer"],
        task=task,
        release_handler=release_handler,
        handler_done=handler_done,
    )


async def _new_parked_read_task(stream_id: int = 1) -> _OpenReader:
    opened = asyncio.Event()
    release_handler = asyncio.Event()
    handler_done = asyncio.Event()
    captured: dict[str, Any] = {}

    async def send(_: bytes) -> None:
        return

    async def handler(reader: asyncio.StreamReader, writer: Any) -> None:
        captured["reader"] = reader
        captured["writer"] = writer
        opened.set()
        try:
            await release_handler.wait()
        finally:
            handler_done.set()

    mux = Multiplexer(send, handler, is_listener=True)
    await mux.feed(build_open(stream_id).encode())
    await asyncio.wait_for(opened.wait(), timeout=1.0)
    state = mux._streams[stream_id]
    read_task = asyncio.create_task(captured["reader"].read(1))
    return _OpenReader(
        mux=mux,
        stream_id=stream_id,
        state=state,
        reader=captured["reader"],
        read_task=read_task,
        release_handler=release_handler,
        handler_done=handler_done,
    )


async def _apply_teardown_case(case: str, parked: _ParkedWriter) -> None:
    stream_id = parked.stream_id
    if case == "mux.close":
        await parked.mux.close()
    elif case == "peer RESET":
        await parked.mux.feed(build_reset(stream_id, RESET_CANCEL).encode())
    elif case == "local StreamWriter.reset":
        await parked.writer.reset(RESET_CANCEL, RESET_CTX_APP_CANCELLATION)
    elif case == "over-credit DATA":
        parked.state.recv_credit = 0
        await parked.mux.feed(build_data(stream_id, b"x").encode())
    elif case == "malformed WINDOW":
        await parked.mux.feed(Frame(stream_id, FLAG_WINDOW, b"x").encode())
    elif case == "WINDOW overflow":
        parked.state.send_credit = MAX_SEND_CREDIT
        await parked.mux.feed(build_window(stream_id, 1).encode())
    elif case == "drain-then-CLOSE":
        parked.writer.begin_drain(RESET_CTX_APP_CANCELLATION)
        await parked.mux.feed(build_close(stream_id).encode())
    elif case == "cancelled handler":
        assert parked.state.task is not None
        parked.state.task.cancel()
        with contextlib.suppress(asyncio.CancelledError):
            await parked.state.task
    else:  # pragma: no cover - table typo guard
        raise AssertionError(f"unknown teardown case {case}")


@pytest.mark.parametrize(
    "case",
    [
        "mux.close",
        "peer RESET",
        "local StreamWriter.reset",
        "over-credit DATA",
        "malformed WINDOW",
        "WINDOW overflow",
        "drain-then-CLOSE",
        "cancelled handler",
    ],
)
@pytest.mark.asyncio
async def test_teardown_paths_wake_zero_credit_writer_with_connection_error(
    case: str,
) -> None:
    parked = await _new_parked_writer()
    try:
        await _apply_teardown_case(case, parked)

        with pytest.raises(
            ConnectionError,
            match=f"stream {parked.stream_id} writer is closed",
        ):
            await asyncio.wait_for(parked.task, timeout=1.0)
        assert parked.state.writer_closed is True
        assert parked.stream_id not in parked.mux._streams
    finally:
        parked.release_handler.set()
        if parked.state.task is not None and not parked.state.task.done():
            with contextlib.suppress(asyncio.CancelledError):
                await asyncio.wait_for(parked.state.task, timeout=1.0)
        await parked.mux.close()


@pytest.mark.parametrize(
    "case",
    [
        "over-credit DATA",
        "malformed WINDOW",
        "WINDOW overflow",
        "_reject_stream",
    ],
)
@pytest.mark.asyncio
async def test_terminate_paths_wake_parked_reader_with_eof(case: str) -> None:
    opened = await _new_parked_read_task()
    try:
        if case == "over-credit DATA":
            opened.state.recv_credit = 0
            await opened.mux.feed(build_data(opened.stream_id, b"x").encode())
        elif case == "malformed WINDOW":
            await opened.mux.feed(Frame(opened.stream_id, FLAG_WINDOW, b"x").encode())
        elif case == "WINDOW overflow":
            opened.state.send_credit = MAX_SEND_CREDIT
            await opened.mux.feed(build_window(opened.stream_id, 1).encode())
        elif case == "_reject_stream":
            await opened.mux.feed(
                Frame(opened.stream_id, FLAG_DATA | FLAG_WINDOW, b"x").encode()
            )
        else:  # pragma: no cover - table typo guard
            raise AssertionError(f"unknown terminate case {case}")

        assert await asyncio.wait_for(opened.read_task, timeout=1.0) == b""
        assert opened.state.reader_closed is True
        assert opened.stream_id not in opened.mux._streams
    finally:
        opened.release_handler.set()
        if opened.state.task is not None and not opened.state.task.done():
            with contextlib.suppress(asyncio.CancelledError):
                await asyncio.wait_for(opened.state.task, timeout=1.0)
        await opened.mux.close()


@pytest.mark.asyncio
async def test_negative_control_close_stream_wake_is_required_to_release_waiter() -> (
    None
):
    parked = await _new_parked_writer()
    try:
        parked.mux._close_stream(parked.state)

        with pytest.raises(
            ConnectionError,
            match=f"stream {parked.stream_id} writer is closed",
        ):
            await asyncio.wait_for(parked.task, timeout=1.0)
    finally:
        parked.release_handler.set()
        if parked.state.task is not None and not parked.state.task.done():
            with contextlib.suppress(asyncio.CancelledError):
                await asyncio.wait_for(parked.state.task, timeout=1.0)
        await parked.mux.close()


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
async def test_unknown_stream_bare_close_is_ignored() -> None:
    sent: list[bytes] = []
    diags: list[ResetDiagnostic] = []

    async def send(data: bytes) -> None:
        sent.append(data)

    async def handler(*_: object) -> None:
        return

    mux = Multiplexer(send, handler, is_listener=True, on_reset=diags.append)
    await mux.feed(build_close(99).encode())

    assert _decode_frames(sent) == []
    assert diags == []
    await mux.close()


@pytest.mark.asyncio
async def test_unknown_stream_bare_reset_is_ignored() -> None:
    sent: list[bytes] = []
    diags: list[ResetDiagnostic] = []

    async def send(data: bytes) -> None:
        sent.append(data)

    async def handler(*_: object) -> None:
        return

    mux = Multiplexer(send, handler, is_listener=True, on_reset=diags.append)
    await mux.feed(build_reset(99, RESET_CANCEL).encode())

    assert _decode_frames(sent) == []
    assert diags == []
    await mux.close()


@pytest.mark.asyncio
async def test_unknown_stream_data_still_gets_reset_with_diagnostic() -> None:
    sent: list[bytes] = []
    diags: list[ResetDiagnostic] = []

    async def send(data: bytes) -> None:
        sent.append(data)

    async def handler(*_: object) -> None:
        return

    mux = Multiplexer(send, handler, is_listener=True, on_reset=diags.append)
    await mux.feed(build_data(99, b"x").encode())

    frames = _decode_frames(sent)
    _assert_single_reset(frames, 99, RESET_PROTOCOL_ERROR)
    assert diags == [
        ResetDiagnostic(
            stream_id=99,
            reason_code=RESET_PROTOCOL_ERROR,
            reason_name="protocol_error",
            context=RESET_CTX_UNKNOWN_STREAM,
        )
    ]
    await mux.close()


@pytest.mark.asyncio
async def test_unknown_stream_window_gets_reset() -> None:
    sent: list[bytes] = []
    diags: list[ResetDiagnostic] = []

    async def send(data: bytes) -> None:
        sent.append(data)

    async def handler(*_: object) -> None:
        return

    mux = Multiplexer(send, handler, is_listener=True, on_reset=diags.append)
    await mux.feed(build_window(99, 1).encode())

    frames = _decode_frames(sent)
    _assert_single_reset(frames, 99, RESET_PROTOCOL_ERROR)
    assert diags == [
        ResetDiagnostic(
            stream_id=99,
            reason_code=RESET_PROTOCOL_ERROR,
            reason_name="protocol_error",
            context=RESET_CTX_UNKNOWN_STREAM,
        )
    ]
    await mux.close()


@pytest.mark.asyncio
async def test_listener_window_credit_exact_cap_is_accepted() -> None:
    sent: list[bytes] = []

    async def send(data: bytes) -> None:
        sent.append(data)

    async def handler(*_: object) -> None:
        await asyncio.Event().wait()

    mux = Multiplexer(send, handler, is_listener=True)
    try:
        await mux.feed(build_open(1).encode())
        state = mux._streams[1]

        await mux.feed(build_window(1, MAX_SEND_CREDIT - state.send_credit).encode())

        assert state.send_credit == MAX_SEND_CREDIT
        assert _reset_reasons(_decode_frames(sent), 1) == []
        assert 1 in mux._streams
    finally:
        await mux.close()


@pytest.mark.asyncio
async def test_listener_window_credit_overflow_resets_and_terminates() -> None:
    sent: list[bytes] = []
    diags: list[ResetDiagnostic] = []

    async def send(data: bytes) -> None:
        sent.append(data)

    async def handler(*_: object) -> None:
        await asyncio.Event().wait()

    mux = Multiplexer(send, handler, is_listener=True, on_reset=diags.append)
    try:
        await mux.feed(build_open(1).encode())
        state = mux._streams[1]

        await mux.feed(
            build_window(1, MAX_SEND_CREDIT - state.send_credit + 1).encode()
        )

        frames = _decode_frames(sent)
        _assert_single_reset(frames, 1, RESET_FLOW_CONTROL_ERROR)
        _assert_single_diag(
            diags,
            1,
            RESET_FLOW_CONTROL_ERROR,
            RESET_CTX_WINDOW_OVERFLOW,
        )
        assert 1 not in mux._streams
    finally:
        await mux.close()


@pytest.mark.asyncio
async def test_listener_invalid_flags_on_unknown_stream_reset_without_state() -> None:
    sent: list[bytes] = []
    diags: list[ResetDiagnostic] = []

    async def send(data: bytes) -> None:
        sent.append(data)

    async def handler(*_: object) -> None:
        return

    mux = Multiplexer(send, handler, is_listener=True, on_reset=diags.append)
    try:
        await mux.feed(Frame(99, FLAG_DATA | FLAG_WINDOW, b"x").encode())

        frames = _decode_frames(sent)
        _assert_single_reset(frames, 99, RESET_PROTOCOL_ERROR)
        _assert_single_diag(
            diags,
            99,
            RESET_PROTOCOL_ERROR,
            RESET_CTX_INVALID_FLAGS,
        )
        assert 99 not in mux._streams
    finally:
        await mux.close()


@pytest.mark.asyncio
async def test_listener_invalid_open_flags_reject_before_opening() -> None:
    sent: list[bytes] = []
    diags: list[ResetDiagnostic] = []
    handler_invoked = False

    async def send(data: bytes) -> None:
        sent.append(data)

    async def handler(*_: object) -> None:
        nonlocal handler_invoked
        handler_invoked = True

    mux = Multiplexer(send, handler, is_listener=True, on_reset=diags.append)
    try:
        await mux.feed(Frame(1, FLAG_OPEN | FLAG_WINDOW, b"").encode())

        frames = _decode_frames(sent)
        _assert_single_reset(frames, 1, RESET_PROTOCOL_ERROR)
        _assert_single_diag(
            diags,
            1,
            RESET_PROTOCOL_ERROR,
            RESET_CTX_INVALID_FLAGS,
        )
        assert handler_invoked is False
        assert 1 not in mux._streams
    finally:
        await mux.close()


@pytest.mark.asyncio
async def test_listener_invalid_flags_on_known_stream_terminate_without_payload() -> (
    None
):
    sent: list[bytes] = []
    diags: list[ResetDiagnostic] = []

    async def send(data: bytes) -> None:
        sent.append(data)

    async def handler(*_: object) -> None:
        await asyncio.Event().wait()

    mux = Multiplexer(send, handler, is_listener=True, on_reset=diags.append)
    try:
        await mux.feed(build_open(1).encode())
        state = mux._streams[1]

        await mux.feed(Frame(1, FLAG_DATA | FLAG_WINDOW, b"x").encode())

        frames = _decode_frames(sent)
        _assert_single_reset(frames, 1, RESET_PROTOCOL_ERROR)
        assert bytes(state.reader._buffer) == b""
        assert diags[-1] == ResetDiagnostic(
            stream_id=1,
            reason_code=RESET_PROTOCOL_ERROR,
            reason_name="protocol_error",
            context=RESET_CTX_INVALID_FLAGS,
        )
        assert 1 not in mux._streams
    finally:
        await mux.close()


@pytest.mark.asyncio
async def test_listener_misplaced_pong_on_known_stream_resets_and_terminates() -> None:
    sent: list[bytes] = []
    diags: list[ResetDiagnostic] = []

    async def send(data: bytes) -> None:
        sent.append(data)

    async def handler(*_: object) -> None:
        await asyncio.Event().wait()

    mux = Multiplexer(send, handler, is_listener=True, on_reset=diags.append)
    try:
        await mux.feed(build_open(1).encode())

        await mux.feed(Frame(1, FLAG_PONG, b"\x00" * 8).encode())

        frames = _decode_frames(sent)
        _assert_single_reset(frames, 1, RESET_PROTOCOL_ERROR)
        assert diags[-1] == ResetDiagnostic(
            stream_id=1,
            reason_code=RESET_PROTOCOL_ERROR,
            reason_name="protocol_error",
            context=RESET_CTX_MISPLACED_CONTROL,
        )
        assert 1 not in mux._streams
        assert mux._closed is False
    finally:
        await mux.close()


@pytest.mark.asyncio
async def test_listener_sibling_stream_survives_window_overflow() -> None:
    sent: list[bytes] = []
    diags: list[ResetDiagnostic] = []

    async def send(data: bytes) -> None:
        sent.append(data)

    async def handler(*_: object) -> None:
        await asyncio.Event().wait()

    mux = Multiplexer(send, handler, is_listener=True, on_reset=diags.append)
    try:
        await mux.feed(build_open(1).encode() + build_open(3).encode())

        await mux.feed(build_window(1, MAX_SEND_CREDIT).encode())

        for _ in range(20):
            await asyncio.sleep(0)
            if 1 not in mux._streams:
                break
        frames = _decode_frames(sent)
        _assert_single_reset(frames, 1, RESET_FLOW_CONTROL_ERROR)
        _assert_single_diag(
            diags,
            1,
            RESET_FLOW_CONTROL_ERROR,
            RESET_CTX_WINDOW_OVERFLOW,
        )
        assert 1 not in mux._streams
        assert 3 in mux._streams
        assert mux._closed is False
        assert not any(diag.stream_id == 3 for diag in diags)
        assert _reset_reasons(frames, 3) == []
    finally:
        await mux.close()


@pytest.mark.asyncio
async def test_listener_sibling_stream_survives_invalid_flags() -> None:
    sent: list[bytes] = []
    diags: list[ResetDiagnostic] = []

    async def send(data: bytes) -> None:
        sent.append(data)

    async def handler(*_: object) -> None:
        await asyncio.Event().wait()

    mux = Multiplexer(send, handler, is_listener=True, on_reset=diags.append)
    try:
        await mux.feed(build_open(1).encode() + build_open(3).encode())

        await mux.feed(
            Frame(stream_id=1, flags=FLAG_DATA | FLAG_WINDOW, payload=b"").encode()
        )

        for _ in range(20):
            await asyncio.sleep(0)
            if 1 not in mux._streams:
                break
        frames = _decode_frames(sent)
        _assert_single_reset(frames, 1, RESET_PROTOCOL_ERROR)
        _assert_single_diag(
            diags,
            1,
            RESET_PROTOCOL_ERROR,
            RESET_CTX_INVALID_FLAGS,
        )
        assert 1 not in mux._streams
        assert 3 in mux._streams
        assert mux._closed is False
        assert not any(diag.stream_id == 3 for diag in diags)
        assert _reset_reasons(frames, 3) == []
    finally:
        await mux.close()


@pytest.mark.asyncio
async def test_listener_sibling_stream_survives_misplaced_control() -> None:
    sent: list[bytes] = []
    diags: list[ResetDiagnostic] = []

    async def send(data: bytes) -> None:
        sent.append(data)

    async def handler(*_: object) -> None:
        await asyncio.Event().wait()

    mux = Multiplexer(send, handler, is_listener=True, on_reset=diags.append)
    try:
        await mux.feed(build_open(1).encode() + build_open(3).encode())

        await mux.feed(
            Frame(stream_id=1, flags=FLAG_PING, payload=b"\x00" * 8).encode()
        )

        for _ in range(20):
            await asyncio.sleep(0)
            if 1 not in mux._streams:
                break
        frames = _decode_frames(sent)
        _assert_single_reset(frames, 1, RESET_PROTOCOL_ERROR)
        _assert_single_diag(
            diags,
            1,
            RESET_PROTOCOL_ERROR,
            RESET_CTX_MISPLACED_CONTROL,
        )
        assert 1 not in mux._streams
        assert 3 in mux._streams
        assert mux._closed is False
        assert not any(diag.stream_id == 3 for diag in diags)
        assert _reset_reasons(frames, 3) == []
    finally:
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


@pytest.mark.asyncio
async def test_listener_local_stream_ids_do_not_recycle_after_stream_close() -> None:
    sent: list[bytes] = []

    async def send(data: bytes) -> None:
        sent.append(data)

    async def handler(*_: object) -> None:
        await asyncio.Event().wait()

    mux = Multiplexer(send, handler, is_listener=True)
    try:
        _reader1, writer1 = await mux.open_stream()
        assert writer1.stream_id == 2
        await writer1.reset(RESET_CANCEL, RESET_CTX_APP_CANCELLATION)

        _reader2, writer2 = await mux.open_stream()

        assert writer2.stream_id == 4
        assert [
            frame.stream_id for frame in _decode_frames(sent) if frame.flags & FLAG_OPEN
        ] == [
            2,
            4,
        ]
    finally:
        await mux.close()


def test_listener_local_stream_id_exhaustion_still_raises() -> None:
    async def send(_: bytes) -> None:
        return

    async def handler(*_: object) -> None:
        return

    mux = Multiplexer(send, handler, is_listener=True)
    mux._next_local_id = 0x1_0000_0000

    with pytest.raises(RuntimeError, match="stream_id space exhausted"):
        mux._next_local_stream_id()


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


@pytest.mark.parametrize(
    "frame",
    [
        Frame(0, FLAG_PING | FLAG_PONG, b"\x00" * 8),
        Frame(0, FLAG_PING | FLAG_DATA, b"\x00" * 8),
    ],
)
@pytest.mark.asyncio
async def test_stream_zero_malformed_control_raises_tunnel_fatal_once(
    frame: Frame,
) -> None:
    sent: list[bytes] = []
    diags: list[ResetDiagnostic] = []

    async def send(data: bytes) -> None:
        sent.append(data)

    async def handler(*_: object) -> None:
        pytest.fail("handler should not be invoked for control frames")

    mux = Multiplexer(send, handler, is_listener=True, on_reset=diags.append)
    with pytest.raises(TunnelFatalError, match=RESET_CTX_MALFORMED_FRAME):
        await mux.feed(frame.encode())

    assert sent == []
    _assert_single_diag(
        diags,
        0,
        RESET_PROTOCOL_ERROR,
        RESET_CTX_MALFORMED_FRAME,
    )
    assert mux._closed is True


@pytest.mark.asyncio
async def test_decoder_corrupt_frame_fatal_tears_down_streams_without_diag_storm() -> (
    None
):
    sent: list[bytes] = []
    diags: list[ResetDiagnostic] = []

    async def send(data: bytes) -> None:
        sent.append(data)

    async def handler(*_: object) -> None:
        await asyncio.Event().wait()

    mux = Multiplexer(send, handler, is_listener=True, on_reset=diags.append)
    await mux.feed(build_open(1).encode() + build_open(3).encode())
    tasks = [state.task for state in mux._streams.values()]
    assert all(task is not None for task in tasks)
    corrupt = bytearray(build_data(1, b"x").encode())
    corrupt[4] |= 0x80

    with pytest.raises(TunnelFatalError, match=RESET_CTX_MALFORMED_FRAME):
        await mux.feed(bytes(corrupt))

    for _ in range(20):
        await asyncio.sleep(0)
        if all(task.done() for task in tasks if task is not None):
            break

    assert mux._closed is True
    assert mux._streams == {}
    assert all(task.done() for task in tasks if task is not None)
    _assert_single_diag(
        diags,
        0,
        RESET_PROTOCOL_ERROR,
        RESET_CTX_MALFORMED_FRAME,
    )


@pytest.mark.asyncio
async def test_second_feed_after_tunnel_fatal_is_inert() -> None:
    sent: list[bytes] = []
    diags: list[ResetDiagnostic] = []

    async def send(data: bytes) -> None:
        sent.append(data)

    async def handler(*_: object) -> None:
        return

    mux = Multiplexer(send, handler, is_listener=True, on_reset=diags.append)
    # accept.py awaits reader_task at line 379 and logs this raised fatal in the
    # generic Exception handler at line 187; direct mux callers should see it.
    with pytest.raises(TunnelFatalError, match=RESET_CTX_MALFORMED_FRAME):
        await mux.feed(Frame(0, FLAG_PING, b"short").encode())
    sent_count = len(sent)
    diag_count = len(diags)

    await mux.feed(build_ping(b"12345678").encode())

    assert len(sent) == sent_count
    assert len(diags) == diag_count
    assert mux._closed is True


@pytest.mark.asyncio
async def test_ping_on_nonzero_stream_is_protocol_error() -> None:
    sent: list[bytes] = []
    diags: list[ResetDiagnostic] = []

    async def send(data: bytes) -> None:
        sent.append(data)

    async def handler(*_: object) -> None:
        return

    mux = Multiplexer(send, handler, is_listener=True, on_reset=diags.append)
    # PING on stream 5 (illegal — control frames are streamID==0 only).
    illegal = Frame(stream_id=5, flags=FLAG_PING, payload=b"\x00" * 8).encode()
    await mux.feed(illegal)

    frames = _decode_frames(sent)
    _assert_single_reset(frames, 5, RESET_PROTOCOL_ERROR)
    _assert_single_diag(
        diags,
        5,
        RESET_PROTOCOL_ERROR,
        RESET_CTX_MISPLACED_CONTROL,
    )
    assert not any(f.flags & FLAG_PONG for f in frames)
    assert 5 not in mux._streams
    assert mux._closed is False
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
async def test_wsgi_pool_survives_zero_credit_streams(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    worker_count = 4
    parked_stream_ids = tuple(1 + offset * 2 for offset in range(worker_count))
    healthy_stream_id = 99
    parked_body = b"x" * (INITIAL_WINDOW + 1)
    healthy_body = b"ok-after-starvation"
    fingerprint = "sha256:" + ("a" * 64)
    monkeypatch.setattr(mux_module, "STREAM_CREDIT_STALL_TIMEOUT_SECONDS", 0.05)
    monkeypatch.setattr(wsgi_module, "WSGI_SEND_BRIDGE_POLL_SECONDS", 0.01)
    app, _journal = make_convey_app(tmp_path, monkeypatch, link={"posture": "spl"})
    _authorize_fingerprint(monkeypatch, fingerprint)

    @app.get("/_mux_test/park")
    def mux_test_park() -> Response:
        return Response(
            parked_body,
            content_type="application/octet-stream",
        )

    @app.get("/_mux_test/healthy")
    def mux_test_healthy() -> Response:
        return Response(
            healthy_body,
            content_type="application/octet-stream",
        )

    sent_payload_size: dict[int, int] = {}
    zero_credit_events = {stream_id: asyncio.Event() for stream_id in parked_stream_ids}
    healthy_payload = bytearray()
    healthy_closed = asyncio.Event()
    loop = asyncio.get_running_loop()
    identity = pl_identity(fingerprint)
    executor = ThreadPoolExecutor(max_workers=worker_count)

    async def send(data: bytes) -> None:
        for frame in _decode_frames([data]):
            if frame.flags & FLAG_DATA:
                sent_payload_size[frame.stream_id] = sent_payload_size.get(
                    frame.stream_id, 0
                ) + len(frame.payload)
                if frame.stream_id == healthy_stream_id:
                    healthy_payload.extend(frame.payload)
                parked = zero_credit_events.get(frame.stream_id)
                if (
                    parked is not None
                    and sent_payload_size[frame.stream_id] >= INITIAL_WINDOW
                ):
                    parked.set()
            if frame.flags & FLAG_CLOSE:
                if frame.stream_id == healthy_stream_id:
                    healthy_closed.set()

    async def handler(reader: asyncio.StreamReader, writer: Any) -> None:
        await dispatch_stream(app, identity, reader, writer, loop, executor)

    async def open_get(stream_id: int, path: str) -> None:
        head = _http_head_bytes("GET", path, headers=None, content_length=0)
        await mux.feed(
            build_open(stream_id, head).encode() + build_close(stream_id).encode()
        )

    async def read_healthy_response() -> tuple[int, bytes]:
        await open_get(healthy_stream_id, "/_mux_test/healthy")
        await healthy_closed.wait()
        status, _headers, body = _parse_http_response(bytes(healthy_payload))
        return status, body

    mux = Multiplexer(send, handler, is_listener=True)
    try:
        for stream_id in parked_stream_ids:
            await open_get(stream_id, "/_mux_test/park")

        await asyncio.wait_for(
            asyncio.gather(*(event.wait() for event in zero_credit_events.values())),
            timeout=2.0,
        )

        status, body = await asyncio.wait_for(read_healthy_response(), timeout=2.0)

        assert status == 200
        assert body == healthy_body
    finally:
        await mux.close()
        await asyncio.to_thread(executor.shutdown, wait=True, cancel_futures=True)


@pytest.mark.parametrize(
    "case",
    [
        "mux.close",
        "peer RESET",
        "local StreamWriter.reset",
        "over-credit DATA",
        "malformed WINDOW",
        "WINDOW overflow",
        "drain-then-CLOSE",
        "cancelled handler",
    ],
)
@pytest.mark.asyncio
async def test_teardown_paths_reclaim_wsgi_workers_for_sentinel_jobs(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    case: str,
) -> None:
    worker_count = 2
    stream_ids = tuple(1 + offset * 2 for offset in range(worker_count))
    monkeypatch.setattr(wsgi_module, "WSGI_SEND_BRIDGE_POLL_SECONDS", 0.01)
    app, _journal = make_convey_app(tmp_path, monkeypatch, link={"posture": "spl"})
    fingerprint = "sha256:" + ("b" * 64)
    _authorize_fingerprint(monkeypatch, fingerprint)

    @app.get("/_mux_test/park")
    def mux_test_park() -> Response:
        return Response(
            b"x" * (INITIAL_WINDOW + 1),
            content_type="application/octet-stream",
        )

    sent_payload_size: dict[int, int] = {}
    zero_credit_events = {stream_id: asyncio.Event() for stream_id in stream_ids}
    writers: dict[int, Any] = {}
    loop = asyncio.get_running_loop()
    identity = pl_identity(fingerprint)
    executor = ThreadPoolExecutor(max_workers=worker_count)

    async def send(data: bytes) -> None:
        for frame in _decode_frames([data]):
            if frame.flags & FLAG_DATA:
                sent_payload_size[frame.stream_id] = sent_payload_size.get(
                    frame.stream_id, 0
                ) + len(frame.payload)
                if sent_payload_size[frame.stream_id] >= INITIAL_WINDOW:
                    parked = zero_credit_events.get(frame.stream_id)
                    if parked is not None:
                        parked.set()

    async def handler(reader: asyncio.StreamReader, writer: Any) -> None:
        writers[writer.stream_id] = writer
        await dispatch_stream(app, identity, reader, writer, loop, executor)

    async def open_get(stream_id: int) -> None:
        head = _http_head_bytes(
            "GET",
            "/_mux_test/park",
            headers=None,
            content_length=0,
        )
        await mux.feed(build_open(stream_id, head).encode())

    mux = Multiplexer(send, handler, is_listener=True)
    try:
        for stream_id in stream_ids:
            await open_get(stream_id)

        await asyncio.wait_for(
            asyncio.gather(*(event.wait() for event in zero_credit_events.values())),
            timeout=2.0,
        )
        states = {stream_id: mux._streams[stream_id] for stream_id in stream_ids}

        if case == "mux.close":
            await mux.close()
        elif case == "peer RESET":
            for stream_id in stream_ids:
                await mux.feed(build_reset(stream_id, RESET_CANCEL).encode())
        elif case == "local StreamWriter.reset":
            for stream_id in stream_ids:
                await writers[stream_id].reset(
                    RESET_CANCEL,
                    RESET_CTX_APP_CANCELLATION,
                )
        elif case == "over-credit DATA":
            for stream_id, state in states.items():
                state.recv_credit = 0
                await mux.feed(build_data(stream_id, b"x").encode())
        elif case == "malformed WINDOW":
            for stream_id in stream_ids:
                await mux.feed(Frame(stream_id, FLAG_WINDOW, b"x").encode())
        elif case == "WINDOW overflow":
            for stream_id, state in states.items():
                state.send_credit = MAX_SEND_CREDIT
                await mux.feed(build_window(stream_id, 1).encode())
        elif case == "drain-then-CLOSE":
            for stream_id in stream_ids:
                writers[stream_id].begin_drain(RESET_CTX_APP_CANCELLATION)
                await mux.feed(build_close(stream_id).encode())
        elif case == "cancelled handler":
            for state in states.values():
                assert state.task is not None
                state.task.cancel()
        else:  # pragma: no cover - table typo guard
            raise AssertionError(f"unknown teardown case {case}")

        for state in states.values():
            if state.task is not None:
                with contextlib.suppress(asyncio.CancelledError):
                    await asyncio.wait_for(state.task, timeout=1.0)

        sentinel_futures = [
            executor.submit(lambda value=value: value) for value in range(worker_count)
        ]
        assert [future.result(timeout=1.0) for future in sentinel_futures] == list(
            range(worker_count)
        )
    finally:
        await mux.close()
        await asyncio.to_thread(executor.shutdown, wait=True, cancel_futures=True)


@pytest.mark.asyncio
async def test_write_rechecks_closed_before_clearing_teardown_wake() -> None:
    emit_entered = asyncio.Event()
    release_emit = asyncio.Event()

    async def send(_: bytes) -> None:
        emit_entered.set()
        await release_emit.wait()

    async def handler(*_: object) -> None:
        await asyncio.Event().wait()

    mux = Multiplexer(send, handler, is_listener=True)
    try:
        await mux.feed(build_open(1).encode())
        state = mux._streams[1]
        state.send_credit = 1
        writer = StreamWriter(mux, state)
        write_task = asyncio.create_task(writer.write(b"xy"))

        await asyncio.wait_for(emit_entered.wait(), timeout=1.0)
        mux._close_stream(state)
        release_emit.set()

        with pytest.raises(ConnectionError, match="stream 1 writer is closed"):
            await asyncio.wait_for(write_task, timeout=1.0)
    finally:
        await mux.close()


@pytest.mark.asyncio
async def test_credit_stall_deadline_is_per_stall_and_window_resets_clock(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(mux_module, "STREAM_CREDIT_STALL_TIMEOUT_SECONDS", 0.5)
    sent_bytes: list[bytes] = []
    data_events = [asyncio.Event(), asyncio.Event(), asyncio.Event()]

    async def send(data: bytes) -> None:
        for frame in _decode_frames([data]):
            if frame.flags & FLAG_DATA:
                sent_bytes.append(frame.payload)
                data_events[len(sent_bytes) - 1].set()

    async def handler(*_: object) -> None:
        await asyncio.Event().wait()

    mux = Multiplexer(send, handler, is_listener=True)
    try:
        await mux.feed(build_open(1).encode())
        state = mux._streams[1]
        state.send_credit = 1
        writer = StreamWriter(mux, state)
        write_task = asyncio.create_task(writer.write(b"abc"))

        await asyncio.wait_for(data_events[0].wait(), timeout=1.0)
        await mux.feed(build_window(1, 1).encode())
        await asyncio.wait_for(data_events[1].wait(), timeout=1.0)
        await mux.feed(build_window(1, 1).encode())
        await asyncio.wait_for(data_events[2].wait(), timeout=1.0)
        await asyncio.wait_for(write_task, timeout=1.0)

        assert b"".join(sent_bytes) == b"abc"
    finally:
        await mux.close()


@pytest.mark.asyncio
async def test_send_credit_starvation_diagnostic_survives_closed_mux_without_storm(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(mux_module, "STREAM_CREDIT_STALL_TIMEOUT_SECONDS", 0.05)
    diags: list[ResetDiagnostic] = []
    opened = asyncio.Event()
    captured: dict[str, Any] = {}
    state: Any | None = None

    async def send(_: bytes) -> None:
        return

    async def handler(reader: asyncio.StreamReader, writer: Any) -> None:
        captured["reader"] = reader
        captured["writer"] = writer
        opened.set()
        await asyncio.Event().wait()

    mux = Multiplexer(send, handler, is_listener=True, on_reset=diags.append)
    try:
        await mux.feed(build_open(1).encode())
        await asyncio.wait_for(opened.wait(), timeout=1.0)
        state = mux._streams[1]
        waiter_entered = asyncio.Event()
        state.credit_event = _ObservedEvent(waiter_entered)
        state.send_credit = 0
        write_task = asyncio.create_task(captured["writer"].write(b"x"))
        await asyncio.wait_for(waiter_entered.wait(), timeout=1.0)

        mux._closed = True

        with pytest.raises(ConnectionError, match="stream 1 writer is closed"):
            await asyncio.wait_for(write_task, timeout=1.0)
        assert diags == [
            ResetDiagnostic(
                stream_id=1,
                reason_code=RESET_CANCEL,
                reason_name="cancel",
                context=RESET_CTX_SEND_CREDIT_STARVATION,
            )
        ]
    finally:
        if state is not None and state.task is not None and not state.task.done():
            state.task.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await state.task
        if open_state := mux._streams.get(1):
            mux._close_stream(open_state)
        await mux.close()


@pytest.mark.asyncio
async def test_receive_window_credit_returns_after_head_and_body_consumption(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    app, _journal = make_convey_app(tmp_path, monkeypatch, link={"posture": "spl"})
    fingerprint = "sha256:" + ("c" * 64)
    _authorize_fingerprint(monkeypatch, fingerprint)
    read_gate = threading.Event()

    @app.post("/_mux_test/read-body")
    def mux_test_read_body() -> Response:
        read_gate.wait(timeout=1.0)
        data = request.environ["wsgi.input"].read(INITIAL_WINDOW // 2)
        return Response(data, content_type="application/octet-stream")

    sent: list[bytes] = []
    window_event = asyncio.Event()
    loop = asyncio.get_running_loop()
    identity = pl_identity(fingerprint)

    async def send(data: bytes) -> None:
        sent.append(data)
        if any(frame.flags & FLAG_WINDOW for frame in _decode_frames([data])):
            window_event.set()

    executor = ThreadPoolExecutor(max_workers=1)

    async def handler(reader: asyncio.StreamReader, writer: Any) -> None:
        await dispatch_stream(app, identity, reader, writer, loop, executor)

    mux = Multiplexer(send, handler, is_listener=True)
    try:
        body = b"z" * (INITIAL_WINDOW // 2)
        head = _http_head_bytes(
            "POST",
            "/_mux_test/read-body",
            headers={"content-type": "application/octet-stream"},
            content_length=len(body),
        )
        await mux.feed(build_open(1, head).encode())
        await mux.feed(build_data(1, body).encode())

        assert not any(
            frame.stream_id == 1 and frame.flags & FLAG_WINDOW
            for frame in _decode_frames(sent)
        )

        read_gate.set()
        await asyncio.wait_for(window_event.wait(), timeout=1.0)

        grants = [
            parse_window_credit(frame)
            for frame in _decode_frames(sent)
            if frame.stream_id == 1 and frame.flags & FLAG_WINDOW
        ]
        assert grants == [len(head) + len(body)]
    finally:
        await mux.close()
        assert mux._window_tasks == set()
        await asyncio.to_thread(executor.shutdown, wait=True, cancel_futures=True)


@pytest.mark.asyncio
async def test_wsgi_input_read_timeout_releases_pool_worker(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(wsgi_module, "WSGI_INPUT_READ_TIMEOUT_SECONDS", 0.05)
    app, _journal = make_convey_app(tmp_path, monkeypatch, link={"posture": "spl"})
    fingerprint = "sha256:" + ("d" * 64)
    _authorize_fingerprint(monkeypatch, fingerprint)

    @app.post("/_mux_test/read-timeout")
    def mux_test_read_timeout() -> Response:
        request.environ["wsgi.input"].read(1)
        return Response(b"unexpected")

    sent: list[bytes] = []
    dispatch_done = asyncio.Event()
    result: dict[str, int] = {}
    loop = asyncio.get_running_loop()
    identity = pl_identity(fingerprint)
    executor = ThreadPoolExecutor(max_workers=1)

    async def send(data: bytes) -> None:
        sent.append(data)

    async def handler(reader: asyncio.StreamReader, writer: Any) -> None:
        dispatch = await dispatch_stream(app, identity, reader, writer, loop, executor)
        result["status"] = dispatch.status
        dispatch_done.set()

    mux = Multiplexer(send, handler, is_listener=True)
    try:
        head = _http_head_bytes(
            "POST",
            "/_mux_test/read-timeout",
            headers={"content-type": "application/octet-stream"},
            content_length=1,
        )
        await mux.feed(build_open(1, head).encode())

        await asyncio.wait_for(dispatch_done.wait(), timeout=1.0)
        assert result["status"] == 499
        assert (
            executor.submit(lambda: "worker-free").result(timeout=1.0) == "worker-free"
        )
    finally:
        await mux.close()
        await asyncio.to_thread(executor.shutdown, wait=True, cancel_futures=True)


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

    misplaced_mux = Multiplexer(
        send,
        waiting_handler,
        is_listener=True,
        on_reset=diags.append,
    )
    try:
        await misplaced_mux.feed(
            Frame(stream_id=5, flags=FLAG_PING, payload=b"\x00" * 8).encode()
        )
    finally:
        await misplaced_mux.close()

    malformed_mux = Multiplexer(
        send,
        waiting_handler,
        is_listener=True,
        on_reset=diags.append,
    )
    try:
        with pytest.raises(TunnelFatalError):
            await malformed_mux.feed(
                Frame(
                    stream_id=0, flags=FLAG_PING | FLAG_PONG, payload=b"\x00" * 8
                ).encode()
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
        RESET_CTX_MISPLACED_CONTROL,
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
