# SPDX-License-Identifier: AGPL-3.0-only
# Copyright (c) 2026 sol pbc

"""Secure-listener saturation coverage.

These tests pin bounded refusal, an off-pool capacity diagnostic, and recovery
for a saturated listener that continued answering keepalives while serving no
stream.
"""

from __future__ import annotations

import asyncio
import json
import threading
from collections.abc import Awaitable, Callable, Iterator
from pathlib import Path
from typing import Any

import pytest
from flask import Response

from solstone.convey import root as root_module
from solstone.convey.secure_listener import wsgi as wsgi_module
from solstone.convey.secure_listener.admission import (
    SecureListenerAdmission,
    SecureListenerAdmissionConfig,
)
from solstone.convey.secure_listener.framing import (
    FLAG_CLOSE,
    FLAG_DATA,
    Frame,
    FrameDecoder,
    build_close,
    build_open,
)
from solstone.convey.secure_listener.mux import Multiplexer
from solstone.think.link.auth import AuthorizedClients
from solstone.think.link.client import _http_head_bytes, _parse_http_response
from solstone.think.link.paths import authorized_clients_path
from tests.link.certless_helpers import make_convey_app, pl_identity

STREAM_A = 1
STREAM_B = 3
STREAM_C = 5
STREAM_D = 7
STREAM_E = 9
STREAM_F = 11

STREAM_MARKER = b"data: saturation-marker\n\n"
STREAM_TERMINAL = b"data: saturation-terminal\n\n"
HOLD_BODY = b"held\n"

STATE_TIMEOUT_S = 3.0
RESPONSE_TIMEOUT_S = 3.0
NEGATIVE_TIMEOUT_S = 0.5
BLOCK_GUARD_TIMEOUT_S = 10.0
NEGATIVE_STREAMING_WAIT_TIMEOUT_S = 8.0
POLL_INTERVAL_S = 0.005


def _decode_frames(chunks: list[bytes]) -> list[Frame]:
    decoder = FrameDecoder()
    for chunk in chunks:
        decoder.feed(chunk)
    return decoder.drain()


def _authorize_fingerprint(
    monkeypatch: pytest.MonkeyPatch,
    fingerprint: str,
) -> None:
    authorized = AuthorizedClients(authorized_clients_path())
    authorized.add(fingerprint, "pytest phone", "inst-1")
    monkeypatch.setattr(root_module, "get_authorized_clients", lambda: authorized)


def _admission(
    capacity: int = 1,
    *,
    streaming_capacity: int | None = None,
    refuse_when_full: bool = False,
) -> SecureListenerAdmission:
    resolved_streaming_capacity = (
        capacity if streaming_capacity is None else streaming_capacity
    )
    return SecureListenerAdmission(
        SecureListenerAdmissionConfig(
            capacity=capacity,
            streaming_capacity=resolved_streaming_capacity,
            refuse_when_full=refuse_when_full,
        )
    )


async def _shutdown_admission(admission: SecureListenerAdmission) -> None:
    await asyncio.to_thread(admission.shutdown, wait=True, cancel_futures=True)


async def _wait_for_snapshot(
    admission: SecureListenerAdmission,
    predicate: Callable[[dict[str, Any]], bool],
    *,
    interval_s: float = POLL_INTERVAL_S,
) -> dict[str, Any]:
    while True:
        snapshot = admission.snapshot()
        if predicate(snapshot):
            return snapshot
        await asyncio.sleep(interval_s)


def _register_saturation_endpoints(
    app: Any,
    release_streams: threading.Event,
    release_hold: threading.Event,
) -> None:
    @app.get("/_saturation/stream")
    def saturation_stream() -> Response:
        def generate() -> Iterator[bytes]:
            yield STREAM_MARKER
            # Leak backstop only; finally sets this before teardown, so a longer
            # guard avoids self-releasing saturated streams under load.
            release_streams.wait(timeout=BLOCK_GUARD_TIMEOUT_S)
            yield STREAM_TERMINAL

        return Response(generate(), mimetype="text/event-stream")

    @app.get("/_saturation/hold")
    def saturation_hold() -> Response:
        # Leak backstop only; assertions synchronize through admission snapshots.
        release_hold.wait(timeout=BLOCK_GUARD_TIMEOUT_S)
        return Response(HOLD_BODY, content_type="application/octet-stream")


def _stream_collector(
    stream_ids: tuple[int, ...],
) -> tuple[
    dict[int, bytearray],
    dict[int, asyncio.Event],
    Callable[..., Awaitable[None]],
]:
    payloads = {stream_id: bytearray() for stream_id in stream_ids}
    closed = {stream_id: asyncio.Event() for stream_id in stream_ids}

    async def send(data: bytes, *, urgent: bool = False) -> None:
        for frame in _decode_frames([data]):
            if frame.flags & FLAG_DATA and frame.stream_id in payloads:
                payloads[frame.stream_id].extend(frame.payload)
            if frame.flags & FLAG_CLOSE and frame.stream_id in closed:
                closed[frame.stream_id].set()

    return payloads, closed, send


@pytest.mark.asyncio
async def test_saturated_listener_refuses_streaming_and_recovers(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    app, _journal = make_convey_app(tmp_path, monkeypatch, link={"posture": "spl"})
    fingerprint = "sha256:" + ("2" * 64)
    _authorize_fingerprint(monkeypatch, fingerprint)

    release_streams = threading.Event()
    release_hold = threading.Event()
    _register_saturation_endpoints(app, release_streams, release_hold)

    payloads, closed, send = _stream_collector(
        (STREAM_A, STREAM_B, STREAM_C, STREAM_D, STREAM_E, STREAM_F)
    )
    loop = asyncio.get_running_loop()
    identity = pl_identity(fingerprint)
    admission = _admission(capacity=3, streaming_capacity=2)

    async def handler(reader: asyncio.StreamReader, writer: Any) -> None:
        await wsgi_module.dispatch_stream(
            app, identity, reader, writer, loop, admission
        )

    async def open_get(stream_id: int, path: str) -> None:
        head = _http_head_bytes("GET", path, headers=None, content_length=0)
        await mux.feed(
            build_open(stream_id, head).encode() + build_close(stream_id).encode()
        )

    mux = Multiplexer(send, handler, is_listener=True)
    try:
        await open_get(STREAM_A, "/_saturation/stream")
        await open_get(STREAM_B, "/_saturation/stream")
        await asyncio.wait_for(
            _wait_for_snapshot(
                admission,
                lambda snapshot: snapshot["active"]["streaming"] == 2,
            ),
            timeout=STATE_TIMEOUT_S,
        )

        await open_get(STREAM_C, "/_saturation/hold")
        await asyncio.wait_for(
            _wait_for_snapshot(
                admission,
                lambda snapshot: snapshot["active"]["total"] == 3,
            ),
            timeout=STATE_TIMEOUT_S,
        )

        await open_get(STREAM_D, "/_saturation/stream")
        await asyncio.wait_for(
            _wait_for_snapshot(
                admission,
                lambda snapshot: snapshot["queued"]["total"] == 1,
            ),
            timeout=STATE_TIMEOUT_S,
        )

        await open_get(STREAM_E, wsgi_module.CAPACITY_SNAPSHOT_PATH)
        await asyncio.wait_for(closed[STREAM_E].wait(), timeout=RESPONSE_TIMEOUT_S)
        status, _headers, body = _parse_http_response(bytes(payloads[STREAM_E]))
        snapshot_body = json.loads(body.decode("utf-8"))
        assert status == 200
        assert snapshot_body["active"]["total"] == 3
        assert snapshot_body["limit"]["total"] == 3
        assert snapshot_body["queued"]["total"] == 1
        assert snapshot_body["longest_wait_ms"] > 0
        assert snapshot_body["active"]["streaming"] == 2
        assert snapshot_body["refusal_enabled"] is False

        release_hold.set()
        await asyncio.wait_for(closed[STREAM_D].wait(), timeout=RESPONSE_TIMEOUT_S)
        status, headers, body = _parse_http_response(bytes(payloads[STREAM_D]))
        assert status == 503
        assert headers["retry-after"] == str(
            wsgi_module.SECURE_LISTENER_REFUSAL_RETRY_AFTER_SECONDS
        )
        assert b"secure listener streaming capacity is full" in body
        assert STREAM_MARKER not in bytes(payloads[STREAM_D])

        release_streams.set()
        await asyncio.wait_for(closed[STREAM_A].wait(), timeout=RESPONSE_TIMEOUT_S)
        await asyncio.wait_for(closed[STREAM_B].wait(), timeout=RESPONSE_TIMEOUT_S)
        await asyncio.wait_for(
            _wait_for_snapshot(
                admission,
                lambda snapshot: snapshot["active"]["streaming"] == 0,
            ),
            timeout=STATE_TIMEOUT_S,
        )

        await open_get(STREAM_F, "/_saturation/stream")
        await asyncio.wait_for(closed[STREAM_F].wait(), timeout=RESPONSE_TIMEOUT_S)
        status, _headers, body = _parse_http_response(bytes(payloads[STREAM_F]))
        assert status == 200
        assert body == STREAM_MARKER + STREAM_TERMINAL
        await asyncio.wait_for(
            _wait_for_snapshot(
                admission,
                lambda snapshot: snapshot["active"]["streaming"] == 0,
            ),
            timeout=STATE_TIMEOUT_S,
        )
    finally:
        release_streams.set()
        release_hold.set()
        await mux.close()
        await _shutdown_admission(admission)


@pytest.mark.asyncio
async def test_negative_control_refusal_path_disabled_hangs_stream(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(wsgi_module, "_SAFE_STREAM_REFUSAL_METHODS", frozenset())
    monkeypatch.setattr(
        wsgi_module,
        "STREAMING_PERMIT_WAIT_TIMEOUT_SECONDS",
        NEGATIVE_STREAMING_WAIT_TIMEOUT_S,
    )
    app, _journal = make_convey_app(tmp_path, monkeypatch, link={"posture": "spl"})
    fingerprint = "sha256:" + ("3" * 64)
    _authorize_fingerprint(monkeypatch, fingerprint)

    release_streams = threading.Event()
    release_hold = threading.Event()
    _register_saturation_endpoints(app, release_streams, release_hold)

    payloads, closed, send = _stream_collector((STREAM_A, STREAM_B, STREAM_C, STREAM_D))
    loop = asyncio.get_running_loop()
    identity = pl_identity(fingerprint)
    admission = _admission(capacity=3, streaming_capacity=2)

    async def handler(reader: asyncio.StreamReader, writer: Any) -> None:
        await wsgi_module.dispatch_stream(
            app, identity, reader, writer, loop, admission
        )

    async def open_get(stream_id: int, path: str) -> None:
        head = _http_head_bytes("GET", path, headers=None, content_length=0)
        await mux.feed(
            build_open(stream_id, head).encode() + build_close(stream_id).encode()
        )

    mux = Multiplexer(send, handler, is_listener=True)
    try:
        await open_get(STREAM_A, "/_saturation/stream")
        await open_get(STREAM_B, "/_saturation/stream")
        await asyncio.wait_for(
            _wait_for_snapshot(
                admission,
                lambda snapshot: snapshot["active"]["streaming"] == 2,
            ),
            timeout=STATE_TIMEOUT_S,
        )

        await open_get(STREAM_C, "/_saturation/hold")
        await asyncio.wait_for(
            _wait_for_snapshot(
                admission,
                lambda snapshot: snapshot["active"]["total"] == 3,
            ),
            timeout=STATE_TIMEOUT_S,
        )

        await open_get(STREAM_D, "/_saturation/stream")
        await asyncio.wait_for(
            _wait_for_snapshot(
                admission,
                lambda snapshot: snapshot["queued"]["total"] == 1,
            ),
            timeout=STATE_TIMEOUT_S,
        )

        release_hold.set()
        await asyncio.wait_for(
            _wait_for_snapshot(
                admission,
                lambda snapshot: (
                    snapshot["queued"]["total"] == 0
                    and snapshot["active"]["total"] == 3
                ),
            ),
            timeout=STATE_TIMEOUT_S,
        )

        with pytest.raises(TimeoutError):
            await asyncio.wait_for(
                closed[STREAM_D].wait(),
                timeout=NEGATIVE_TIMEOUT_S,
            )

        snapshot = admission.snapshot()
        assert snapshot["queued"]["total"] == 0
        assert snapshot["active"]["total"] == 3
        assert snapshot["rejected"]["streaming"] == 0
        assert snapshot["admitted_over_budget"]["streaming"] == 0
    finally:
        release_streams.set()
        release_hold.set()
        await mux.close()
        await _shutdown_admission(admission)


@pytest.mark.asyncio
async def test_negative_control_snapshot_fast_path_disabled_queues_probe(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    snapshot_path = wsgi_module.CAPACITY_SNAPSHOT_PATH
    monkeypatch.setattr(
        wsgi_module,
        "CAPACITY_SNAPSHOT_PATH",
        "/__solstone/secure-listener/capacity-disabled",
    )
    app, _journal = make_convey_app(tmp_path, monkeypatch, link={"posture": "spl"})
    fingerprint = "sha256:" + ("4" * 64)
    _authorize_fingerprint(monkeypatch, fingerprint)

    release_streams = threading.Event()
    release_hold = threading.Event()
    _register_saturation_endpoints(app, release_streams, release_hold)

    payloads, closed, send = _stream_collector(
        (STREAM_A, STREAM_B, STREAM_C, STREAM_D, STREAM_E)
    )
    loop = asyncio.get_running_loop()
    identity = pl_identity(fingerprint)
    admission = _admission(capacity=3, streaming_capacity=2)

    async def handler(reader: asyncio.StreamReader, writer: Any) -> None:
        await wsgi_module.dispatch_stream(
            app, identity, reader, writer, loop, admission
        )

    async def open_get(stream_id: int, path: str) -> None:
        head = _http_head_bytes("GET", path, headers=None, content_length=0)
        await mux.feed(
            build_open(stream_id, head).encode() + build_close(stream_id).encode()
        )

    mux = Multiplexer(send, handler, is_listener=True)
    try:
        await open_get(STREAM_A, "/_saturation/stream")
        await open_get(STREAM_B, "/_saturation/stream")
        await asyncio.wait_for(
            _wait_for_snapshot(
                admission,
                lambda snapshot: snapshot["active"]["streaming"] == 2,
            ),
            timeout=STATE_TIMEOUT_S,
        )

        await open_get(STREAM_C, "/_saturation/hold")
        await asyncio.wait_for(
            _wait_for_snapshot(
                admission,
                lambda snapshot: snapshot["active"]["total"] == 3,
            ),
            timeout=STATE_TIMEOUT_S,
        )

        await open_get(STREAM_D, "/_saturation/stream")
        await asyncio.wait_for(
            _wait_for_snapshot(
                admission,
                lambda snapshot: snapshot["queued"]["total"] == 1,
            ),
            timeout=STATE_TIMEOUT_S,
        )

        await open_get(STREAM_E, snapshot_path)
        await asyncio.wait_for(
            _wait_for_snapshot(
                admission,
                lambda snapshot: snapshot["queued"]["total"] == 2,
            ),
            timeout=STATE_TIMEOUT_S,
        )

        with pytest.raises(TimeoutError):
            await asyncio.wait_for(
                closed[STREAM_E].wait(),
                timeout=NEGATIVE_TIMEOUT_S,
            )

        snapshot = admission.snapshot()
        assert snapshot["queued"]["total"] == 2
    finally:
        release_streams.set()
        release_hold.set()
        await mux.close()
        await _shutdown_admission(admission)
