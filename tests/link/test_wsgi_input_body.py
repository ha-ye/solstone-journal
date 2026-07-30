# SPDX-License-Identifier: AGPL-3.0-only
# Copyright (c) 2026 sol pbc

from __future__ import annotations

import asyncio
import hashlib
import json
import logging
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Callable

import pytest

from solstone.apps.observer import routes as observer_routes
from solstone.apps.observer.utils import load_history, load_observer, save_observer
from solstone.convey.secure_listener import wsgi as wsgi_module
from solstone.convey.secure_listener.framing import (
    FLAG_DATA,
    FLAG_RESET,
    build_close,
    build_data,
    build_open,
)
from solstone.convey.secure_listener.mux import Multiplexer, ResetDiagnostic
from solstone.think.link.client import _http_head_bytes, _parse_http_response
from tests.link.certless_helpers import make_convey_app, pl_identity
from tests.link.test_mux import (
    _admission,
    _authorize_fingerprint,
    _decode_frames,
    _shutdown_admission,
)

DAY = "20250103"
SEGMENT = "120000_300"
STREAM = "contract-valid-test"
BOUNDARY = "solstone-wsgi-input-boundary"
DEFAULT_FILENAME = "audio.flac"
DEFAULT_PAYLOAD = b"submitted-audio-bytes--0123456789--END"

PAYLOAD_MARKER = "wsgi-input-secret-payload"
FILENAME_MARKER = "wsgi-input-secret-file.flac"
FORM_FIELD_MARKER = "wsgi-input-secret-field"
CREDENTIAL_MARKER = "wsgi-input-secret-credential"
FORBIDDEN_MARKERS = {
    PAYLOAD_MARKER,
    FILENAME_MARKER,
    FORM_FIELD_MARKER,
    CREDENTIAL_MARKER,
}
PAYLOAD_SURFACE_FORBIDDEN_MARKERS = {
    PAYLOAD_MARKER,
    CREDENTIAL_MARKER,
}


@dataclass(frozen=True)
class ObserverMultipartFixture:
    day: str
    segment: str
    stream: str
    filename: str
    payload: bytes
    boundary: str
    body: bytes
    prefix: bytes
    tail: bytes
    content_type: str


@dataclass
class BodyReadProbe:
    entry_count: int = 0
    exit_count: int = 0
    cumulative_bytes_returned: int = 0

    def install(self, reader: asyncio.StreamReader) -> None:
        original_read = reader.read

        async def read(n: int = -1) -> bytes:
            self.entry_count += 1
            try:
                data = await original_read(n)
                self.cumulative_bytes_returned += len(data)
                return data
            finally:
                self.exit_count += 1

        reader.read = read

    def has_pending_read_after(self, byte_count: int) -> bool:
        return (
            self.cumulative_bytes_returned == byte_count
            and self.entry_count == self.exit_count + 1
        )


@dataclass
class MuxExchange:
    fixture: ObserverMultipartFixture
    journal: Path
    key: str
    head: bytes
    sent: list[bytes]
    dispatch: dict[str, Any]
    emitted: list[dict[str, Any]]
    diagnostics: list[ResetDiagnostic]
    report_recv_consumed_calls: list[int]
    worker_reclaimed: bool
    elapsed: float

    @property
    def response_payload(self) -> bytes:
        return b"".join(
            frame.payload
            for frame in _decode_frames(self.sent)
            if frame.stream_id == 1 and frame.flags & FLAG_DATA
        )

    @property
    def reset_frames(self) -> list[Any]:
        return [
            frame
            for frame in _decode_frames(self.sent)
            if frame.stream_id == 1 and frame.flags & FLAG_RESET
        ]


def build_observer_multipart_body(
    *,
    filename: str = DEFAULT_FILENAME,
    payload: bytes = DEFAULT_PAYLOAD,
    extra_field: str | None = None,
) -> ObserverMultipartFixture:
    def part(name: str, value: bytes) -> bytes:
        return (
            (
                f'--{BOUNDARY}\r\nContent-Disposition: form-data; name="{name}"\r\n\r\n'
            ).encode("ascii")
            + value
            + b"\r\n"
        )

    def file_part(
        name: str, part_filename: str, content: bytes, content_type: str
    ) -> bytes:
        return (
            (
                f"--{BOUNDARY}\r\n"
                f'Content-Disposition: form-data; name="{name}"; filename="{part_filename}"\r\n'
                f"Content-Type: {content_type}\r\n"
                "\r\n"
            ).encode("ascii")
            + content
            + b"\r\n"
        )

    audio = b'{"raw":"audio.flac"}\n{"start":"00:00:00","text":"hello"}\n'
    screen = b'{"raw":"screen.mp4","qualified_count":1}\n{"timestamp":1.0}\n'
    stream = (
        b'{"stream":"contract-valid-test","prev_day":null,'
        b'"prev_segment":null,"seq":1}\n'
    )
    chunks = [
        part("day", DAY.encode("ascii")),
        part("segment", SEGMENT.encode("ascii")),
    ]
    if extra_field is not None:
        chunks.append(part("notes", extra_field.encode("ascii")))
    chunks.extend(
        [
            file_part("files", "120000_300_audio.jsonl", audio, "application/jsonl"),
            file_part("files", "screen.jsonl", screen, "application/jsonl"),
            file_part("files", "stream.json", stream, "application/json"),
        ]
    )
    target_header = (
        f"--{BOUNDARY}\r\n"
        f'Content-Disposition: form-data; name="files"; filename="{filename}"\r\n'
        "Content-Type: audio/flac\r\n"
        "\r\n"
    ).encode("ascii")
    prefix = b"".join(chunks) + target_header
    tail = payload + b"\r\n" + f"--{BOUNDARY}--\r\n".encode("ascii")
    assert prefix.endswith(b"\r\n\r\n")
    assert tail.startswith(payload)
    return ObserverMultipartFixture(
        day=DAY,
        segment=SEGMENT,
        stream=STREAM,
        filename=filename,
        payload=payload,
        boundary=BOUNDARY,
        body=prefix + tail,
        prefix=prefix,
        tail=tail,
        content_type=f"multipart/form-data; boundary={BOUNDARY}",
    )


def build_marker_observer_multipart_body(
    *,
    payload: bytes = PAYLOAD_MARKER.encode("ascii"),
) -> ObserverMultipartFixture:
    return build_observer_multipart_body(
        filename=FILENAME_MARKER,
        payload=payload,
        extra_field=FORM_FIELD_MARKER,
    )


def _observer_key(name: str, *, include_marker: bool = False) -> str:
    marker = CREDENTIAL_MARKER if include_marker else "bodyinputcredential"
    digest = hashlib.sha256(name.encode("utf-8")).hexdigest()
    return f"{marker}-{digest}"


def _save_observer(key: str, stream: str, fingerprint: str) -> None:
    assert save_observer(
        {
            "key": key,
            "name": stream,
            "platform": "linux",
            "hostname": stream,
            "stream_type": "desktop",
            "label": None,
            "version": "test",
            "stream": stream,
            "device_binding": {"device": fingerprint, "kind": "cert"},
            "created_at": 1_700_000_000_000,
            "last_seen": None,
            "last_segment": None,
            "last_segment_received_at": None,
            "last_segment_day": None,
            "enabled": True,
            "stats": {"segments_received": 0, "bytes_received": 0},
        }
    )


async def _wait_for_probe(
    predicate: Callable[[], bool],
    *,
    timeout: float = 1.0,
) -> None:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if predicate():
            return
        await asyncio.sleep(0.005)
    raise AssertionError("body read probe did not reach expected state")


async def _run_mux_ingest(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    fixture: ObserverMultipartFixture,
    *,
    name: str,
    key: str | None = None,
    include_credential_marker: bool = False,
    feed: Callable[[Multiplexer, BodyReadProbe, asyncio.Event], Any],
) -> MuxExchange:
    tmp_path.mkdir(parents=True, exist_ok=True)
    app, journal = make_convey_app(tmp_path, monkeypatch, link={"posture": "spl"})
    fingerprint = "sha256:" + hashlib.sha256(name.encode("utf-8")).hexdigest()
    _authorize_fingerprint(monkeypatch, fingerprint)
    resolved_key = key or _observer_key(name, include_marker=include_credential_marker)
    _save_observer(resolved_key, fixture.stream, fingerprint)

    emitted: list[dict[str, Any]] = []
    monkeypatch.setattr(
        observer_routes,
        "emit",
        lambda tract, event, **fields: (
            emitted.append({"tract": tract, "event": event, **fields}) or True
        ),
    )

    loop = asyncio.get_running_loop()
    admission = _admission(capacity=1)
    identity = pl_identity(fingerprint)
    sent: list[bytes] = []
    diagnostics: list[ResetDiagnostic] = []
    dispatch_result: dict[str, Any] = {}
    report_calls: list[int] = []
    probe = BodyReadProbe()
    dispatch_done = asyncio.Event()
    head = _http_head_bytes(
        "POST",
        "/app/observer/ingest",
        headers={
            "authorization": f"Bearer {resolved_key}",
            "content-type": fixture.content_type,
        },
        content_length=len(fixture.body),
    )

    async def send(data: bytes, *, urgent: bool = False) -> None:
        sent.append(data)

    async def handler(reader: asyncio.StreamReader, writer: Any) -> None:
        probe.install(reader)
        original_report = writer.report_recv_consumed

        def report_recv_consumed(n: int) -> None:
            report_calls.append(n)
            original_report(n)

        writer.report_recv_consumed = report_recv_consumed
        dispatch = await wsgi_module.dispatch_stream(
            app,
            identity,
            reader,
            writer,
            loop,
            admission,
        )
        dispatch_result["endpoint"] = dispatch.endpoint
        dispatch_result["status"] = dispatch.status
        dispatch_done.set()

    mux = Multiplexer(send, handler, is_listener=True, on_reset=diagnostics.append)
    started = time.monotonic()
    try:
        await mux.feed(build_open(1, head).encode())
        await feed(mux, probe, dispatch_done)
        await asyncio.wait_for(dispatch_done.wait(), timeout=2.0)
        await asyncio.sleep(0)
        worker_reclaimed = (
            await admission.submit(asyncio.get_running_loop(), lambda: "worker-free")
        ) == "worker-free"
        return MuxExchange(
            fixture=fixture,
            journal=journal,
            key=resolved_key,
            head=head,
            sent=sent,
            dispatch=dispatch_result,
            emitted=emitted,
            diagnostics=diagnostics,
            report_recv_consumed_calls=report_calls,
            worker_reclaimed=worker_reclaimed,
            elapsed=time.monotonic() - started,
        )
    finally:
        await mux.close()
        await _shutdown_admission(admission)


def _parse_response(exchange: MuxExchange) -> tuple[int, dict[str, str], bytes]:
    return _parse_http_response(exchange.response_payload)


def _segment_dir(exchange: MuxExchange) -> Path:
    fixture = exchange.fixture
    return (
        exchange.journal / "chronicle" / fixture.day / fixture.stream / fixture.segment
    )


def _stored_bytes(exchange: MuxExchange) -> bytes:
    return (_segment_dir(exchange) / exchange.fixture.filename).read_bytes()


def _assert_stored_payload_exact(exchange: MuxExchange) -> None:
    stored = _stored_bytes(exchange)
    expected_sha = hashlib.sha256(exchange.fixture.payload).hexdigest()
    stored_sha = hashlib.sha256(stored).hexdigest()
    assert {
        "stored_len": len(stored),
        "expected_len": len(exchange.fixture.payload),
        "stored_sha": stored_sha,
        "expected_sha": expected_sha,
        "stored_prefix": stored[:2],
        "expected_prefix": exchange.fixture.payload[:2],
    } == {
        "stored_len": len(exchange.fixture.payload),
        "expected_len": len(exchange.fixture.payload),
        "stored_sha": expected_sha,
        "expected_sha": expected_sha,
        "stored_prefix": exchange.fixture.payload[:2],
        "expected_prefix": exchange.fixture.payload[:2],
    }
    assert stored == exchange.fixture.payload
    assert not stored.startswith(b"\r\n")


def assert_no_ingest_mutations(exchange: MuxExchange) -> None:
    fixture = exchange.fixture
    segment_dir = _segment_dir(exchange)
    assert not segment_dir.exists()
    assert load_history(exchange.key[:8], fixture.day) == []
    observer = load_observer(exchange.key)
    assert observer is not None
    assert observer.get("health", {}).get("ingest_rejection") is None
    assert not (exchange.journal / "streams" / f"{fixture.stream}.json").exists()
    assert exchange.emitted == []


def _assert_no_marker_leaks_in_listener_logs_or_reset_diagnostics(
    exchange: MuxExchange,
    caplog: pytest.LogCaptureFixture,
) -> None:
    surfaces = [record.getMessage() for record in caplog.records]
    for diag in exchange.diagnostics:
        assert set(diag.__dict__) == {
            "stream_id",
            "reason_code",
            "reason_name",
            "context",
            "stall_age_ms",
        }
        surfaces.extend(str(value) for value in diag.__dict__.values())
    for marker in FORBIDDEN_MARKERS:
        assert all(marker not in surface for surface in surfaces)


def _assert_no_body_or_credential_leak_in_peer_or_callosum_payloads(
    exchange: MuxExchange,
    *,
    response_body: bytes = b"",
) -> None:
    # why: successful observer ingest legitimately returns/emits stored
    # filenames and meta (routes.py:1229-1238), so these payload surfaces only
    # forbid file body bytes and bearer credentials.
    surfaces = [response_body.decode("utf-8", "replace")]
    surfaces.extend(json.dumps(payload, sort_keys=True) for payload in exchange.emitted)
    for marker in PAYLOAD_SURFACE_FORBIDDEN_MARKERS:
        assert all(marker not in surface for surface in surfaces)


@pytest.mark.asyncio
async def test_observer_multipart_boundary_split_does_not_prefix_file_bytes(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    caplog: pytest.LogCaptureFixture,
) -> None:
    fixture = build_marker_observer_multipart_body()

    async def feed(
        mux: Multiplexer,
        probe: BodyReadProbe,
        _dispatch_done: asyncio.Event,
    ) -> None:
        await mux.feed(build_data(1, fixture.prefix).encode())
        await _wait_for_probe(lambda: probe.has_pending_read_after(len(fixture.prefix)))
        await mux.feed(build_data(1, fixture.tail).encode() + build_close(1).encode())

    with caplog.at_level(logging.DEBUG):
        exchange = await _run_mux_ingest(
            tmp_path,
            monkeypatch,
            fixture,
            name="boundary-split",
            include_credential_marker=True,
            feed=feed,
        )

    status, _headers, response_body = _parse_response(exchange)
    assert status == 200
    assert json.loads(response_body)["status"] == "ok"
    assert exchange.emitted
    _assert_stored_payload_exact(exchange)
    _assert_no_marker_leaks_in_listener_logs_or_reset_diagnostics(exchange, caplog)
    _assert_no_body_or_credential_leak_in_peer_or_callosum_payloads(
        exchange,
        response_body=response_body,
    )


@pytest.mark.asyncio
async def test_wsgi_input_small_fragmentation_reaches_content_length_without_close(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    caplog: pytest.LogCaptureFixture,
) -> None:
    fixture = build_marker_observer_multipart_body()
    chunks = [
        fixture.body[index : index + 37] for index in range(0, len(fixture.body), 37)
    ]

    async def feed(
        mux: Multiplexer,
        probe: BodyReadProbe,
        _dispatch_done: asyncio.Event,
    ) -> None:
        sent = 0
        for chunk in chunks:
            await _wait_for_probe(lambda sent=sent: probe.has_pending_read_after(sent))
            await mux.feed(build_data(1, chunk).encode())
            sent += len(chunk)

    with caplog.at_level(logging.DEBUG):
        exchange = await _run_mux_ingest(
            tmp_path,
            monkeypatch,
            fixture,
            name="small-fragmentation",
            include_credential_marker=True,
            feed=feed,
        )

    status, _headers, response_body = _parse_response(exchange)
    assert status == 200
    assert exchange.emitted
    _assert_stored_payload_exact(exchange)
    assert exchange.report_recv_consumed_calls[0] == len(exchange.head)
    assert sum(exchange.report_recv_consumed_calls[1:]) == len(fixture.body)
    _assert_no_marker_leaks_in_listener_logs_or_reset_diagnostics(exchange, caplog)
    _assert_no_body_or_credential_leak_in_peer_or_callosum_payloads(
        exchange,
        response_body=response_body,
    )


@pytest.mark.asyncio
async def test_observer_multipart_premature_eof_remains_400_without_mutation(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    caplog: pytest.LogCaptureFixture,
) -> None:
    fixture = build_marker_observer_multipart_body()

    async def feed_full_tail(
        mux: Multiplexer,
        probe: BodyReadProbe,
        _dispatch_done: asyncio.Event,
    ) -> None:
        await mux.feed(build_data(1, fixture.prefix).encode())
        await _wait_for_probe(lambda: probe.has_pending_read_after(len(fixture.prefix)))
        await mux.feed(build_data(1, fixture.tail).encode() + build_close(1).encode())

    with caplog.at_level(logging.DEBUG):
        green_exchange = await _run_mux_ingest(
            tmp_path / "green",
            monkeypatch,
            fixture,
            name="premature-eof-green",
            include_credential_marker=True,
            feed=feed_full_tail,
        )
    green_status, _headers, green_response_body = _parse_response(green_exchange)
    assert green_status == 200
    assert green_exchange.emitted
    _assert_stored_payload_exact(green_exchange)
    _assert_no_marker_leaks_in_listener_logs_or_reset_diagnostics(
        green_exchange,
        caplog,
    )
    _assert_no_body_or_credential_leak_in_peer_or_callosum_payloads(
        green_exchange,
        response_body=green_response_body,
    )
    caplog.clear()

    async def feed_eof(
        mux: Multiplexer,
        probe: BodyReadProbe,
        _dispatch_done: asyncio.Event,
    ) -> None:
        await mux.feed(build_data(1, fixture.prefix).encode())
        await _wait_for_probe(lambda: probe.has_pending_read_after(len(fixture.prefix)))
        await mux.feed(build_close(1).encode())

    with caplog.at_level(logging.DEBUG):
        exchange = await _run_mux_ingest(
            tmp_path / "eof",
            monkeypatch,
            fixture,
            name="premature-eof",
            include_credential_marker=True,
            feed=feed_eof,
        )

    status, _headers, response_body = _parse_response(exchange)
    assert status == 400
    assert exchange.response_payload.startswith(b"HTTP/1.1 400")
    assert response_body
    assert exchange.dispatch["status"] == 400
    assert exchange.reset_frames == []
    assert exchange.diagnostics == []
    assert exchange.worker_reclaimed
    assert_no_ingest_mutations(exchange)
    _assert_no_marker_leaks_in_listener_logs_or_reset_diagnostics(exchange, caplog)
    _assert_no_body_or_credential_leak_in_peer_or_callosum_payloads(
        exchange,
        response_body=response_body,
    )


@pytest.mark.asyncio
async def test_wsgi_input_absolute_deadline_progress_does_not_renew_timeout(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    caplog: pytest.LogCaptureFixture,
) -> None:
    timeout_seconds = 0.2
    fragment_interval = 0.05
    fragment_count = 32
    elapsed_upper_bound = timeout_seconds * 2.5
    renewing_timeout_floor = fragment_count * fragment_interval + timeout_seconds
    assert renewing_timeout_floor >= elapsed_upper_bound * 3
    monkeypatch.setattr(wsgi_module, "WSGI_INPUT_READ_TIMEOUT_SECONDS", timeout_seconds)
    fixture = build_marker_observer_multipart_body(
        payload=(PAYLOAD_MARKER * 20).encode("ascii"),
    )
    progress_chunks = [
        fixture.tail[index : index + 1]
        for index in range(min(fragment_count, len(fixture.tail)))
    ]

    async def feed_timeout(
        mux: Multiplexer,
        probe: BodyReadProbe,
        dispatch_done: asyncio.Event,
    ) -> None:
        await mux.feed(build_data(1, fixture.prefix).encode())
        sent = len(fixture.prefix)
        await _wait_for_probe(lambda: probe.has_pending_read_after(sent))
        for chunk in progress_chunks:
            if dispatch_done.is_set():
                break
            await asyncio.sleep(fragment_interval)
            await mux.feed(build_data(1, chunk).encode())
            sent += len(chunk)
            if not dispatch_done.is_set():
                try:
                    await _wait_for_probe(
                        lambda sent=sent: probe.has_pending_read_after(sent),
                        timeout=0.2,
                    )
                except AssertionError:
                    if dispatch_done.is_set():
                        break
                    raise

    with caplog.at_level(logging.DEBUG):
        exchange = await _run_mux_ingest(
            tmp_path,
            monkeypatch,
            fixture,
            name="absolute-deadline-timeout",
            include_credential_marker=True,
            feed=feed_timeout,
        )

    assert exchange.dispatch["status"] == 499
    assert exchange.response_payload == b""
    assert exchange.elapsed < elapsed_upper_bound
    assert exchange.worker_reclaimed
    assert_no_ingest_mutations(exchange)
    _assert_no_marker_leaks_in_listener_logs_or_reset_diagnostics(exchange, caplog)
    _assert_no_body_or_credential_leak_in_peer_or_callosum_payloads(exchange)
