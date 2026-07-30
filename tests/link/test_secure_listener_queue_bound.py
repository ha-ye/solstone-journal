# SPDX-License-Identifier: AGPL-3.0-only
# Copyright (c) 2026 sol pbc

from __future__ import annotations

import asyncio
import contextlib
import logging
import re
import threading
from collections.abc import Callable
from pathlib import Path
from typing import Any

import pytest
from flask import Response

from solstone.convey import root as root_module
from solstone.convey.secure_listener import admission as admission_module
from solstone.convey.secure_listener import mux as mux_module
from solstone.convey.secure_listener import wsgi as wsgi_module
from solstone.convey.secure_listener.admission import (
    DEFAULT_SECURE_LISTENER_QUEUE_TIMEOUT_SECONDS,
    SECURE_LISTENER_QUEUE_WARN_SECONDS,
    SecureListenerAdmission,
    SecureListenerAdmissionConfig,
)
from solstone.convey.secure_listener.framing import (
    FLAG_DATA,
    FLAG_RESET,
    INITIAL_WINDOW,
    RESET_CANCEL,
    Frame,
    FrameDecoder,
    build_close,
    build_data,
    build_open,
    build_reset,
    parse_reset_reason,
)
from solstone.convey.secure_listener.mux import (
    RESET_CTX_BODY_DISCARD_CANCELLATION,
    RESET_CTX_HANDLER_EXCEPTION,
    Multiplexer,
)
from solstone.convey.secure_listener.wsgi import DispatchResult, dispatch_stream
from solstone.think.link.auth import AuthorizedClients
from solstone.think.link.client import _http_head_bytes, _parse_http_response
from solstone.think.link.dialer import _REQUEST_TIMEOUT_SECONDS
from solstone.think.link.paths import authorized_clients_path
from tests.link.certless_helpers import FakeStreamWriter, make_convey_app, pl_identity
from tests.link.secure_listener_harness import (
    SecureListenerHarness,
    pair_and_open_session,
)

STATE_TIMEOUT_S = 3.0
RESPONSE_TIMEOUT_S = 3.0
POLL_INTERVAL_S = 0.005
BLOCK_GUARD_TIMEOUT_S = 10.0

FINGERPRINT = "sha256:" + ("7" * 64)
QUEUE_TIMEOUT_BODY = b'{"error":"secure listener queue timeout"}'
CAPACITY_REFUSAL_BODY = b'{"error":"secure listener capacity is full"}'
STREAMING_REFUSAL_BODY = b'{"error":"secure listener streaming capacity is full"}\n'
OK_BODY = b"queue-ok"


def _decode_frames(chunks: list[bytes]) -> list[Frame]:
    decoder = FrameDecoder()
    for chunk in chunks:
        decoder.feed(chunk)
    return decoder.drain()


def _authorize_fingerprint(
    monkeypatch: pytest.MonkeyPatch,
    fingerprint: str = FINGERPRINT,
) -> None:
    authorized = AuthorizedClients(authorized_clients_path())
    authorized.add(fingerprint, "pytest phone", "inst-1")
    monkeypatch.setattr(root_module, "get_authorized_clients", lambda: authorized)


def _admission(
    *,
    capacity: int = 1,
    streaming_capacity: int | None = None,
    queue_timeout_seconds: float = 0.05,
    refuse_when_full: bool = False,
) -> SecureListenerAdmission:
    return SecureListenerAdmission(
        SecureListenerAdmissionConfig(
            capacity=capacity,
            streaming_capacity=capacity
            if streaming_capacity is None
            else streaming_capacity,
            refuse_when_full=refuse_when_full,
            queue_timeout_seconds=queue_timeout_seconds,
        )
    )


async def _shutdown_admission(admission: SecureListenerAdmission) -> None:
    await asyncio.to_thread(admission.shutdown, wait=True, cancel_futures=True)


async def _wait_for_snapshot(
    admission: SecureListenerAdmission,
    predicate: Callable[[dict[str, Any]], bool],
) -> dict[str, Any]:
    loop = asyncio.get_running_loop()
    deadline = loop.time() + STATE_TIMEOUT_S
    while True:
        snapshot = admission.snapshot()
        if predicate(snapshot):
            return snapshot
        if loop.time() >= deadline:
            raise AssertionError(f"snapshot predicate not met: {snapshot!r}")
        await asyncio.sleep(POLL_INTERVAL_S)


async def _wait_for_mux_state(mux: Multiplexer, stream_id: int) -> Any:
    loop = asyncio.get_running_loop()
    deadline = loop.time() + STATE_TIMEOUT_S
    while True:
        state = mux._streams.get(stream_id)
        if state is not None:
            return state
        if loop.time() >= deadline:
            raise AssertionError(f"stream {stream_id} did not open")
        await asyncio.sleep(POLL_INTERVAL_S)


async def _wait_for_stream_payload(sent: list[bytes], stream_id: int) -> bytes:
    loop = asyncio.get_running_loop()
    deadline = loop.time() + RESPONSE_TIMEOUT_S
    while True:
        payload = b"".join(
            frame.payload
            for frame in _decode_frames(sent)
            if frame.stream_id == stream_id and frame.flags & FLAG_DATA
        )
        if payload:
            return payload
        if loop.time() >= deadline:
            raise AssertionError(f"stream {stream_id} did not emit DATA")
        await asyncio.sleep(POLL_INTERVAL_S)


async def _dispatch_raw_request(
    app: Any,
    admission: SecureListenerAdmission,
    method: str,
    path: str,
    *,
    body: bytes = b"",
    headers: dict[str, str] | None = None,
    writer: FakeStreamWriter | None = None,
) -> tuple[DispatchResult, int, dict[str, str], bytes, FakeStreamWriter]:
    reader = asyncio.StreamReader()
    reader.feed_data(
        _http_head_bytes(
            method,
            path,
            headers=headers,
            content_length=len(body),
        )
        + body
    )
    reader.feed_eof()
    stream_writer = writer or FakeStreamWriter()
    result = await dispatch_stream(
        app,
        pl_identity(FINGERPRINT),
        reader,
        stream_writer,
        asyncio.get_running_loop(),
        admission,
    )
    status, response_headers, response_body = _parse_http_response(
        bytes(stream_writer.data)
    )
    assert result.status == status
    return result, status, response_headers, response_body, stream_writer


def _register_basic_endpoints(
    app: Any,
    release_hold: threading.Event | None = None,
    *,
    invoked: dict[str, int] | None = None,
) -> None:
    @app.get("/_queue_bound/hold")
    def queue_bound_hold() -> Response:
        if release_hold is not None:
            release_hold.wait(timeout=BLOCK_GUARD_TIMEOUT_S)
        return Response(b"held", content_type="text/plain")

    @app.get("/_queue_bound/ok")
    def queue_bound_ok() -> Response:
        if invoked is not None:
            invoked["count"] = invoked.get("count", 0) + 1
        return Response(OK_BODY, content_type="text/plain")

    @app.post("/_queue_bound/upload")
    def queue_bound_upload() -> Response:
        if invoked is not None:
            invoked["count"] = invoked.get("count", 0) + 1
        return Response(b"uploaded", content_type="text/plain")


def _register_streaming_endpoint(app: Any) -> None:
    @app.get("/_queue_bound/events")
    def queue_bound_events() -> Response:
        return Response(iter((b"data: queue-bound\n\n",)), mimetype="text/event-stream")


async def _open_mux_request(
    mux: Multiplexer,
    stream_id: int,
    method: str,
    path: str,
    *,
    headers: dict[str, str] | None = None,
    content_length: int = 0,
) -> None:
    head = _http_head_bytes(
        method,
        path,
        headers=headers,
        content_length=content_length,
    )
    suffix = build_close(stream_id).encode() if content_length == 0 else b""
    await mux.feed(build_open(stream_id, head).encode() + suffix)


async def _open_mux_get(mux: Multiplexer, stream_id: int, path: str) -> None:
    await _open_mux_request(mux, stream_id, "GET", path)


def _assert_content_free(value: bytes | str) -> None:
    text = value.decode("utf-8", "replace") if isinstance(value, bytes) else value
    assert "sha256" not in text
    assert re.search(r"\b\d{1,3}(?:\.\d{1,3}){3}\b", text) is None


@pytest.mark.asyncio
async def test_queue_timeout_returns_503_retry_after_and_distinct_body(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    app, _journal = make_convey_app(tmp_path, monkeypatch, link={"posture": "spl"})
    _authorize_fingerprint(monkeypatch)
    release_hold = threading.Event()
    _register_basic_endpoints(app, release_hold)
    admission = _admission()
    hold_task = asyncio.create_task(
        _dispatch_raw_request(app, admission, "GET", "/_queue_bound/hold")
    )
    try:
        await _wait_for_snapshot(
            admission,
            lambda snapshot: snapshot["active"]["total"] == 1,
        )

        _result, status, headers, body, _writer = await asyncio.wait_for(
            _dispatch_raw_request(app, admission, "GET", "/_queue_bound/ok"),
            timeout=RESPONSE_TIMEOUT_S,
        )

        assert status == 503
        assert headers["retry-after"] == str(
            wsgi_module.SECURE_LISTENER_REFUSAL_RETRY_AFTER_SECONDS
        )
        assert body == QUEUE_TIMEOUT_BODY
        assert body != CAPACITY_REFUSAL_BODY
        assert body != STREAMING_REFUSAL_BODY
        _assert_content_free(body)
    finally:
        release_hold.set()
        with contextlib.suppress(Exception):
            await hold_task
        await _shutdown_admission(admission)


@pytest.mark.asyncio
async def test_queue_timeout_clears_queue_counts_and_keeps_streaming_separate(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    app, _journal = make_convey_app(tmp_path, monkeypatch, link={"posture": "spl"})
    _authorize_fingerprint(monkeypatch)
    release_hold = threading.Event()
    _register_basic_endpoints(app, release_hold)
    _register_streaming_endpoint(app)
    admission = _admission(streaming_capacity=1)
    held_streaming = admission.try_acquire_streaming()
    assert held_streaming is not None
    hold_task: (
        asyncio.Task[
            tuple[DispatchResult, int, dict[str, str], bytes, FakeStreamWriter]
        ]
        | None
    ) = None
    try:
        _result, streaming_status, _headers, _body, _writer = await asyncio.wait_for(
            _dispatch_raw_request(app, admission, "GET", "/_queue_bound/events"),
            timeout=RESPONSE_TIMEOUT_S,
        )
        assert streaming_status == 503

        hold_task = asyncio.create_task(
            _dispatch_raw_request(app, admission, "GET", "/_queue_bound/hold")
        )
        await _wait_for_snapshot(
            admission,
            lambda snapshot: snapshot["active"]["total"] == 1,
        )
        _result, status, _headers, _body, _writer = await asyncio.wait_for(
            _dispatch_raw_request(app, admission, "GET", "/_queue_bound/ok"),
            timeout=RESPONSE_TIMEOUT_S,
        )
        assert status == 503

        snapshot = admission.snapshot()
        assert snapshot["queued"]["total"] == 0
        assert snapshot["longest_wait_ms"] == 0
        assert snapshot["rejected"]["queue_timeout"] == 1
        assert snapshot["rejected"]["total"] == 1
        assert snapshot["rejected"]["streaming"] == 1

        release_hold.set()
        await asyncio.wait_for(hold_task, timeout=RESPONSE_TIMEOUT_S)
        snapshot = await _wait_for_snapshot(
            admission,
            lambda item: item["active"]["total"] == 0,
        )
        assert snapshot["active"]["total"] == 0

        _result, later_status, _headers, later_body, _writer = await asyncio.wait_for(
            _dispatch_raw_request(app, admission, "GET", "/_queue_bound/ok"),
            timeout=RESPONSE_TIMEOUT_S,
        )
        assert later_status == 200
        assert later_body == OK_BODY
    finally:
        held_streaming.release()
        release_hold.set()
        if hold_task is not None:
            with contextlib.suppress(Exception):
                await hold_task
        await _shutdown_admission(admission)


@pytest.mark.asyncio
async def test_cancelled_waiter_warns_without_timeout_count_and_quick_grant_is_quiet(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    caplog: pytest.LogCaptureFixture,
) -> None:
    app, _journal = make_convey_app(tmp_path, monkeypatch, link={"posture": "spl"})
    _authorize_fingerprint(monkeypatch)
    release_hold = threading.Event()
    _register_basic_endpoints(app, release_hold)
    admission = _admission(queue_timeout_seconds=10.0)
    sent: list[bytes] = []

    async def send(data: bytes, *, urgent: bool = False) -> None:
        sent.append(data)

    async def handler(reader: asyncio.StreamReader, writer: Any) -> None:
        await dispatch_stream(
            app,
            pl_identity(FINGERPRINT),
            reader,
            writer,
            asyncio.get_running_loop(),
            admission,
        )

    monkeypatch.setattr(admission_module, "SECURE_LISTENER_QUEUE_WARN_SECONDS", 0.0)
    mux = Multiplexer(send, handler, is_listener=True)
    try:
        await _open_mux_get(mux, 1, "/_queue_bound/hold")
        await _wait_for_snapshot(
            admission,
            lambda snapshot: snapshot["active"]["total"] == 1,
        )
        await _open_mux_get(mux, 3, "/_queue_bound/ok")
        await _wait_for_snapshot(
            admission,
            lambda snapshot: snapshot["queued"]["total"] == 1,
        )

        with caplog.at_level(
            logging.WARNING,
            logger="convey.secure_listener.admission",
        ):
            await mux.feed(build_reset(3, RESET_CANCEL).encode())
            await _wait_for_snapshot(
                admission,
                lambda snapshot: snapshot["queued"]["total"] == 0,
            )

        warnings = [
            record
            for record in caplog.records
            if record.levelno == logging.WARNING
            and "Secure listener admission waiter departed" in record.message
        ]
        assert len(warnings) == 1
        message = warnings[0].message
        assert "departure_reason=cancelled" in message
        assert "waiter_age_s=" in message
        assert "active_total=1" in message
        assert "queue_depth=0" in message
        assert "queue_timeout_seconds=10.000" in message
        _assert_content_free(message)
        assert admission.snapshot()["rejected"]["queue_timeout"] == 0
    finally:
        release_hold.set()
        await mux.close()
        await _shutdown_admission(admission)

    caplog.clear()
    monkeypatch.setattr(admission_module, "SECURE_LISTENER_QUEUE_WARN_SECONDS", 60.0)
    admission = _admission(queue_timeout_seconds=10.0)
    held = await admission.acquire()
    try:
        queued_task = asyncio.create_task(
            _dispatch_raw_request(app, admission, "GET", "/_queue_bound/ok")
        )
        await _wait_for_snapshot(
            admission,
            lambda snapshot: snapshot["queued"]["total"] == 1,
        )
        with caplog.at_level(
            logging.WARNING,
            logger="convey.secure_listener.admission",
        ):
            held.release()
            _result, status, _headers, _body, _writer = await asyncio.wait_for(
                queued_task,
                timeout=RESPONSE_TIMEOUT_S,
            )
        assert status == 200
        assert not [
            record
            for record in caplog.records
            if "Secure listener admission waiter departed" in record.message
        ]
    finally:
        if not held._released:
            held.release()
        await _shutdown_admission(admission)


@pytest.mark.asyncio
async def test_deadline_boundary_refuses_short_and_serves_before_generous_deadline(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    app, _journal = make_convey_app(tmp_path, monkeypatch, link={"posture": "spl"})
    _authorize_fingerprint(monkeypatch)
    release_short = threading.Event()
    _register_basic_endpoints(app, release_short)
    admission = _admission(queue_timeout_seconds=0.05)
    hold_task = asyncio.create_task(
        _dispatch_raw_request(app, admission, "GET", "/_queue_bound/hold")
    )
    try:
        await _wait_for_snapshot(
            admission,
            lambda snapshot: snapshot["active"]["total"] == 1,
        )
        _result, status, _headers, _body, _writer = await asyncio.wait_for(
            _dispatch_raw_request(app, admission, "GET", "/_queue_bound/ok"),
            timeout=RESPONSE_TIMEOUT_S,
        )
        assert status == 503
        assert admission.snapshot()["rejected"]["queue_timeout"] == 1
    finally:
        release_short.set()
        with contextlib.suppress(Exception):
            await hold_task
        await _shutdown_admission(admission)

    admission = _admission(queue_timeout_seconds=10.0)
    held = await admission.acquire()
    try:
        queued_task = asyncio.create_task(
            _dispatch_raw_request(app, admission, "GET", "/_queue_bound/ok")
        )
        await _wait_for_snapshot(
            admission,
            lambda snapshot: snapshot["queued"]["total"] == 1,
        )
        held.release()
        _result, status, _headers, body, _writer = await asyncio.wait_for(
            queued_task,
            timeout=RESPONSE_TIMEOUT_S,
        )
        assert status == 200
        assert body == OK_BODY
        assert admission.snapshot()["rejected"]["queue_timeout"] == 0
    finally:
        if not held._released:
            held.release()
        await _shutdown_admission(admission)


@pytest.mark.asyncio
async def test_timeout_reclaims_raced_permit_after_delivery_intercept(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    app, _journal = make_convey_app(tmp_path, monkeypatch, link={"posture": "spl"})
    _authorize_fingerprint(monkeypatch)
    invoked = {"count": 0}
    _register_basic_endpoints(app, invoked=invoked)
    admission = _admission(queue_timeout_seconds=0.05)
    first_permit = await admission.acquire()
    captured: dict[str, Any] = {}
    captured_event = asyncio.Event()
    real_deliver = admission._deliver_waiter

    def capture(waiter: Any, permit: Any) -> None:
        captured["waiter"] = waiter
        captured["permit"] = permit
        captured["active_total"] = admission.snapshot()["active"]["total"]
        captured["waiter_has_permit"] = waiter.permit is not None
        captured_event.set()

    admission._deliver_waiter = capture
    try:
        queued_task = asyncio.create_task(
            _dispatch_raw_request(app, admission, "GET", "/_queue_bound/ok")
        )
        await _wait_for_snapshot(
            admission,
            lambda snapshot: snapshot["queued"]["total"] == 1,
        )
        first_permit.release()
        await asyncio.wait_for(captured_event.wait(), timeout=STATE_TIMEOUT_S)

        assert captured["waiter_has_permit"] is True
        assert captured["active_total"] == admission.config.capacity

        _result, status, _headers, _body, _writer = await asyncio.wait_for(
            queued_task,
            timeout=RESPONSE_TIMEOUT_S,
        )
        assert status == 503
        assert invoked["count"] == 0

        real_deliver(captured["waiter"], captured["permit"])
        snapshot = await _wait_for_snapshot(
            admission,
            lambda item: item["active"]["total"] == 0,
        )
        assert snapshot["active"]["total"] == 0

        _result, later_status, _headers, later_body, _writer = await asyncio.wait_for(
            _dispatch_raw_request(app, admission, "GET", "/_queue_bound/ok"),
            timeout=RESPONSE_TIMEOUT_S,
        )
        assert later_status == 200
        assert later_body == OK_BODY
    finally:
        admission._deliver_waiter = real_deliver
        if not first_permit._released:
            first_permit.release()
        await _shutdown_admission(admission)


@pytest.mark.asyncio
async def test_raced_permit_release_guard_falsification_pins_active_total(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    app, _journal = make_convey_app(tmp_path, monkeypatch, link={"posture": "spl"})
    _authorize_fingerprint(monkeypatch)
    _register_basic_endpoints(app)
    admission = _admission(queue_timeout_seconds=0.05)
    first_permit = await admission.acquire()
    captured: dict[str, Any] = {}
    captured_event = asyncio.Event()
    real_deliver = admission._deliver_waiter

    def capture(waiter: Any, permit: Any) -> None:
        captured["waiter"] = waiter
        captured["permit"] = permit
        captured["active_total"] = admission.snapshot()["active"]["total"]
        captured["waiter_has_permit"] = waiter.permit is not None
        captured_event.set()

    admission._deliver_waiter = capture
    try:
        queued_task = asyncio.create_task(
            _dispatch_raw_request(app, admission, "GET", "/_queue_bound/ok")
        )
        await _wait_for_snapshot(
            admission,
            lambda snapshot: snapshot["queued"]["total"] == 1,
        )
        first_permit.release()
        await asyncio.wait_for(captured_event.wait(), timeout=STATE_TIMEOUT_S)

        assert captured["waiter_has_permit"] is True
        assert captured["active_total"] == admission.config.capacity
        captured["permit"].release = lambda: None

        _result, status, _headers, _body, _writer = await asyncio.wait_for(
            queued_task,
            timeout=RESPONSE_TIMEOUT_S,
        )
        assert status == 503
        real_deliver(captured["waiter"], captured["permit"])
        assert admission.snapshot()["active"]["total"] == admission.config.capacity
    finally:
        admission._deliver_waiter = real_deliver
        await _shutdown_admission(admission)


@pytest.mark.asyncio
async def test_concurrent_queue_timeouts_account_and_warn_individually(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    caplog: pytest.LogCaptureFixture,
) -> None:
    app, _journal = make_convey_app(tmp_path, monkeypatch, link={"posture": "spl"})
    _authorize_fingerprint(monkeypatch)
    _register_basic_endpoints(app)
    monkeypatch.setattr(admission_module, "SECURE_LISTENER_QUEUE_WARN_SECONDS", 0.0)
    admission = _admission(queue_timeout_seconds=0.05)
    held = await admission.acquire()
    count = 3
    try:
        with caplog.at_level(
            logging.WARNING,
            logger="convey.secure_listener.admission",
        ):
            tasks = [
                asyncio.create_task(
                    _dispatch_raw_request(app, admission, "GET", "/_queue_bound/ok")
                )
                for _index in range(count)
            ]
            await _wait_for_snapshot(
                admission,
                lambda snapshot: snapshot["queued"]["total"] == count,
            )
            results = await asyncio.wait_for(
                asyncio.gather(*tasks),
                timeout=RESPONSE_TIMEOUT_S,
            )

        assert [status for _result, status, *_rest in results] == [503] * count
        snapshot = admission.snapshot()
        assert snapshot["rejected"]["queue_timeout"] == count
        assert snapshot["queued"]["total"] == 0
        assert snapshot["longest_wait_ms"] == 0
        warnings = [
            record
            for record in caplog.records
            if record.levelno == logging.WARNING
            and "Secure listener admission waiter departed" in record.message
        ]
        assert len(warnings) == count
    finally:
        held.release()
        snapshot = await _wait_for_snapshot(
            admission,
            lambda item: item["active"]["total"] == 0,
        )
        assert snapshot["active"]["total"] == 0
        await _shutdown_admission(admission)


@pytest.mark.asyncio
async def test_zero_recv_credit_timeout_does_not_block_later_stream(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    app, _journal = make_convey_app(tmp_path, monkeypatch, link={"posture": "spl"})
    _authorize_fingerprint(monkeypatch)
    _register_basic_endpoints(app)
    sent: list[bytes] = []
    admission = _admission(queue_timeout_seconds=0.05)
    held = await admission.acquire()

    async def send(data: bytes, *, urgent: bool = False) -> None:
        sent.append(data)

    async def handler(reader: asyncio.StreamReader, writer: Any) -> None:
        await dispatch_stream(
            app,
            pl_identity(FINGERPRINT),
            reader,
            writer,
            asyncio.get_running_loop(),
            admission,
        )

    mux = Multiplexer(send, handler, is_listener=True)
    try:
        await _open_mux_request(
            mux,
            1,
            "POST",
            "/_queue_bound/upload",
            headers={"content-type": "application/octet-stream"},
            content_length=INITIAL_WINDOW + 100,
        )
        state = await _wait_for_mux_state(mux, 1)
        await mux.feed(build_data(1, b"x" * state.recv_credit).encode())
        await _wait_for_snapshot(
            admission,
            lambda snapshot: snapshot["rejected"]["queue_timeout"] == 1,
        )

        held.release()
        await _open_mux_get(mux, 3, "/_queue_bound/ok")
        payload = await _wait_for_stream_payload(sent, 3)

        assert b"HTTP/1.1 200" in payload
        assert OK_BODY in payload
    finally:
        if not held._released:
            held.release()
        await mux.close()
        await _shutdown_admission(admission)


@pytest.mark.asyncio
async def test_zero_queue_timeout_disables_refusal_but_keeps_slow_warning(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    caplog: pytest.LogCaptureFixture,
) -> None:
    app, _journal = make_convey_app(
        tmp_path,
        monkeypatch,
        link={"posture": "spl", "secure_listener_queue_timeout_seconds": 0},
    )
    _authorize_fingerprint(monkeypatch)
    release_hold = threading.Event()
    _register_basic_endpoints(app, release_hold)
    monkeypatch.setattr(admission_module, "SECURE_LISTENER_QUEUE_WARN_SECONDS", 0.0)
    admission = _admission(queue_timeout_seconds=0.0)
    hold_task = asyncio.create_task(
        _dispatch_raw_request(app, admission, "GET", "/_queue_bound/hold")
    )
    try:
        await _wait_for_snapshot(
            admission,
            lambda snapshot: snapshot["active"]["total"] == 1,
        )
        queued_task = asyncio.create_task(
            _dispatch_raw_request(app, admission, "GET", "/_queue_bound/ok")
        )
        await _wait_for_snapshot(
            admission,
            lambda snapshot: snapshot["queued"]["total"] == 1,
        )
        with caplog.at_level(
            logging.WARNING,
            logger="convey.secure_listener.admission",
        ):
            release_hold.set()
            _result, status, _headers, body, _writer = await asyncio.wait_for(
                queued_task,
                timeout=RESPONSE_TIMEOUT_S,
            )

        assert status == 200
        assert body == OK_BODY
        assert admission.snapshot()["rejected"]["queue_timeout"] == 0
        warnings = [
            record
            for record in caplog.records
            if "Secure listener admission waiter departed" in record.message
        ]
        assert len(warnings) == 1
        assert "departure_reason=granted" in warnings[0].message
    finally:
        release_hold.set()
        with contextlib.suppress(Exception):
            await hold_task
        await _shutdown_admission(admission)


@pytest.mark.asyncio
async def test_refuse_when_full_false_still_allows_deadline_timeout(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    app, _journal = make_convey_app(tmp_path, monkeypatch, link={"posture": "spl"})
    _authorize_fingerprint(monkeypatch)
    release_hold = threading.Event()
    _register_basic_endpoints(app, release_hold)
    admission = _admission(queue_timeout_seconds=0.05, refuse_when_full=False)
    assert admission.config.refuse_when_full is False
    hold_task = asyncio.create_task(
        _dispatch_raw_request(app, admission, "GET", "/_queue_bound/hold")
    )
    try:
        await _wait_for_snapshot(
            admission,
            lambda snapshot: snapshot["active"]["total"] == 1,
        )
        _result, status, _headers, body, _writer = await asyncio.wait_for(
            _dispatch_raw_request(app, admission, "GET", "/_queue_bound/ok"),
            timeout=RESPONSE_TIMEOUT_S,
        )

        assert status == 503
        assert body == QUEUE_TIMEOUT_BODY
        assert admission.snapshot()["rejected"]["queue_timeout"] == 1
    finally:
        release_hold.set()
        with contextlib.suppress(Exception):
            await hold_task
        await _shutdown_admission(admission)


@pytest.mark.asyncio
async def test_paired_mtls_client_parses_queue_timeout_and_cancel_clears_queue(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    forward_root = tmp_path / "forward"
    forward_root.mkdir()
    harness = await SecureListenerHarness.start(
        forward_root,
        monkeypatch,
        admission_config=SecureListenerAdmissionConfig(
            capacity=1,
            streaming_capacity=1,
            refuse_when_full=False,
            queue_timeout_seconds=0.05,
        ),
    )
    session = None
    held = None
    try:
        session = await pair_and_open_session(
            harness,
            nonce="10000000000000000000000000000041",
            label="queue-timeout-phone",
        )
        held = await harness.admission.acquire()
        status, headers, body = await asyncio.wait_for(
            session.request("GET", "/app/network/api/status"),
            timeout=RESPONSE_TIMEOUT_S,
        )

        assert status == 503
        assert headers["retry-after"] == str(
            wsgi_module.SECURE_LISTENER_REFUSAL_RETRY_AFTER_SECONDS
        )
        assert body == QUEUE_TIMEOUT_BODY
    finally:
        if held is not None:
            held.release()
        if session is not None:
            with contextlib.suppress(Exception):
                await session.close()
        await harness.close()

    inverse_root = tmp_path / "inverse"
    inverse_root.mkdir()
    harness = await SecureListenerHarness.start(
        inverse_root,
        monkeypatch,
        admission_config=SecureListenerAdmissionConfig(
            capacity=1,
            streaming_capacity=1,
            refuse_when_full=False,
            queue_timeout_seconds=10.0,
        ),
    )
    session = None
    held = None
    reset_payloads: list[dict[str, Any]] = []
    reset_cancel_seen = asyncio.Event()
    real_dispatch = mux_module.Multiplexer._dispatch

    async def observed_dispatch(self: Multiplexer, frame: Frame) -> None:
        if frame.flags & FLAG_RESET:
            with contextlib.suppress(ValueError):
                if parse_reset_reason(frame) == RESET_CANCEL:
                    reset_cancel_seen.set()
        await real_dispatch(self, frame)

    def capture_reset(event: str, fields: dict[str, Any]) -> None:
        if event == "stream_reset":
            reset_payloads.append(fields)

    monkeypatch.setattr(mux_module.Multiplexer, "_dispatch", observed_dispatch)
    harness.listener._emit = capture_reset
    try:
        session = await pair_and_open_session(
            harness,
            nonce="10000000000000000000000000000042",
            label="queue-cancel-phone",
        )
        held = await harness.admission.acquire()
        request_task = asyncio.create_task(
            session.request("GET", "/app/network/api/status")
        )
        await _wait_for_snapshot(
            harness.admission,
            lambda snapshot: snapshot["queued"]["total"] == 1,
        )
        request_task.cancel()
        with contextlib.suppress(asyncio.CancelledError):
            await request_task
        await asyncio.wait_for(reset_cancel_seen.wait(), timeout=STATE_TIMEOUT_S)
        snapshot = await _wait_for_snapshot(
            harness.admission,
            lambda item: item["queued"]["total"] == 0,
        )

        assert snapshot["queued"]["total"] == 0
        assert snapshot["rejected"]["queue_timeout"] == 0
        assert not any(
            payload.get("context") == RESET_CTX_HANDLER_EXCEPTION
            for payload in reset_payloads
        )
    finally:
        if held is not None:
            held.release()
        if session is not None:
            with contextlib.suppress(Exception):
                await session.close()
        await harness.close()


class _ClosedWriter(FakeStreamWriter):
    def __init__(self) -> None:
        super().__init__()
        self.begin_drain_called = False

    async def write(self, data: bytes) -> None:
        raise ConnectionError("stream closed")

    def begin_drain(self, context: str) -> None:
        self.begin_drain_called = True
        super().begin_drain(context)


@pytest.mark.asyncio
async def test_queue_timeout_dead_writer_returns_503_without_handler_exception(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    app, _journal = make_convey_app(tmp_path, monkeypatch, link={"posture": "spl"})
    _authorize_fingerprint(monkeypatch)
    _register_basic_endpoints(app)
    admission = _admission(queue_timeout_seconds=0.05)
    held = await admission.acquire()
    writer = _ClosedWriter()
    reader = asyncio.StreamReader()
    reader.feed_data(
        _http_head_bytes("GET", "/_queue_bound/ok", headers=None, content_length=0)
    )
    reader.feed_eof()
    try:
        result = await asyncio.wait_for(
            dispatch_stream(
                app,
                pl_identity(FINGERPRINT),
                reader,
                writer,
                asyncio.get_running_loop(),
                admission,
            ),
            timeout=RESPONSE_TIMEOUT_S,
        )

        assert result.status == 503
        assert writer.begin_drain_called is True
        assert writer.drain_context == RESET_CTX_BODY_DISCARD_CANCELLATION
    finally:
        held.release()
        await _shutdown_admission(admission)


def test_secure_listener_queue_bounds_stay_below_link_request_timeout() -> None:
    assert (
        SECURE_LISTENER_QUEUE_WARN_SECONDS
        < DEFAULT_SECURE_LISTENER_QUEUE_TIMEOUT_SECONDS
        < _REQUEST_TIMEOUT_SECONDS
        == 180
    )
