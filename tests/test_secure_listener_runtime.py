# SPDX-License-Identifier: AGPL-3.0-only
# Copyright (c) 2026 sol pbc

import asyncio
import contextlib
import json
import logging
import socket
import threading
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import MagicMock

import pytest

from solstone.convey.secure_listener import accept as accept_module
from solstone.convey.secure_listener import runtime as rt
from solstone.convey.secure_listener.accept import (
    CERTLESS_PAIR_FAILURE_CAP,
    CERTLESS_TUNNEL_CAP,
    CertlessConnection,
    SecureListener,
    certless_admission_mode,
)
from solstone.convey.secure_listener.admission import (
    DEFAULT_SECURE_LISTENER_CAPACITY,
    DEFAULT_SECURE_LISTENER_QUEUE_TIMEOUT_SECONDS,
    DEFAULT_SECURE_LISTENER_STREAMING_CAPACITY,
    SecureListenerAdmission,
    SecureListenerAdmissionConfig,
    SecureListenerAdmissionRejected,
    resolve_admission_config,
)
from solstone.convey.secure_listener.framing import (
    RESET_CANCEL,
    RESET_PROTOCOL_ERROR,
    build_ping,
)
from solstone.convey.secure_listener.mux import (
    RESET_CTX_SEND_CREDIT_STARVATION,
    RESET_CTX_UNKNOWN_STREAM,
    ResetDiagnostic,
)
from solstone.think.link import client as link_client
from solstone.think.link.ca import load_or_generate_ca
from solstone.think.link.nonces import NONCE_TTL_SECONDS, NonceStore
from solstone.think.link.paths import ca_dir, nonces_path, state_path
from tests.link.certless_helpers import write_config
from tests.link.secure_listener_harness import SecureListenerHarness


def test_reuse_port_allows_coexisting_bind():
    admission = SecureListenerAdmission(
        SecureListenerAdmissionConfig(
            capacity=1,
            streaming_capacity=1,
            refuse_when_full=False,
        )
    )
    listener = SecureListener(
        app=MagicMock(),
        strict_tls_ctx=MagicMock(),
        relaxed_tls_ctx=MagicMock(),
        authorized=set(),
        admission=admission,
        callosum_emit=lambda *a, **kw: None,
        host="127.0.0.1",
        port=0,
    )
    loop = asyncio.new_event_loop()
    s2 = None
    try:
        loop.run_until_complete(listener.start())
        assert listener.sockets
        port = listener.sockets[0].getsockname()[1]
        assert port != 0

        s2 = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        s2.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        s2.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEPORT, 1)
        s2.bind(("127.0.0.1", port))
    finally:
        if listener.sockets:
            loop.run_until_complete(listener.stop())
        if s2 is not None:
            s2.close()
        loop.close()
        admission.shutdown(wait=True, cancel_futures=True)


def test_stop_all_after_loop_closed_does_not_raise():
    from solstone.convey.secure_listener import runtime as rt

    previous_runtime = rt._runtime
    s = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    admission = SecureListenerAdmission(
        SecureListenerAdmissionConfig(
            capacity=1,
            streaming_capacity=1,
            refuse_when_full=False,
        )
    )
    try:
        s.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        s.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEPORT, 1)
        s.bind(("127.0.0.1", 0))
        s.listen(1)

        loop = asyncio.new_event_loop()
        loop.close()
        thread = threading.Thread(target=lambda: None)
        thread.start()
        thread.join()
        app = SimpleNamespace(secure_listener_started=True)
        listener = SimpleNamespace(sockets=(s,))
        state = rt.RuntimeState(
            loop=loop,
            thread=thread,
            apps=[app],
            admission=admission,
            listener=listener,
            sockets=(s,),
        )
        rt._runtime = state

        rt.stop_all_secure_listener()

        assert s.fileno() == -1
    finally:
        rt._runtime = previous_runtime
        s.close()
        admission.shutdown(wait=True, cancel_futures=True)


def test_secure_listener_admission_config_defaults_to_current_capacity(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    caplog: pytest.LogCaptureFixture,
) -> None:
    _runtime_journal(
        tmp_path,
        monkeypatch,
        {"setup": {"completed_at": 1700000000000}},
    )

    with caplog.at_level(
        logging.INFO,
        logger="convey.secure_listener.admission",
    ):
        config = resolve_admission_config()

    assert config.capacity == DEFAULT_SECURE_LISTENER_CAPACITY == 16
    assert config.streaming_capacity == DEFAULT_SECURE_LISTENER_STREAMING_CAPACITY == 8
    assert config.refuse_when_full is False
    assert config.queue_timeout_seconds == DEFAULT_SECURE_LISTENER_QUEUE_TIMEOUT_SECONDS
    assert config.queue_limit == 32
    assert not caplog.records


def test_secure_listener_admission_config_reads_link_namespace(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _runtime_journal(
        tmp_path,
        monkeypatch,
        {
            "setup": {"completed_at": 1700000000000},
            "link": {
                "secure_listener_capacity": 24,
                "secure_listener_streaming_capacity": 6,
                "secure_listener_refuse_when_full": True,
                "secure_listener_queue_timeout_seconds": 30.5,
            },
        },
    )

    config = resolve_admission_config()

    assert config.capacity == 24
    assert config.streaming_capacity == 6
    assert config.refuse_when_full is True
    assert config.queue_timeout_seconds == 30.5
    assert config.queue_limit == 48


@pytest.mark.parametrize(
    ("raw", "expected"),
    [
        (120, 120.0),
        (1.0, 1.0),
        (600, 600.0),
    ],
)
def test_secure_listener_admission_config_queue_timeout_accepts_numbers_silently(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    caplog: pytest.LogCaptureFixture,
    raw: object,
    expected: float,
) -> None:
    _runtime_journal(
        tmp_path,
        monkeypatch,
        {
            "setup": {"completed_at": 1700000000000},
            "link": {"secure_listener_queue_timeout_seconds": raw},
        },
    )

    with caplog.at_level(
        logging.INFO,
        logger="convey.secure_listener.admission",
    ):
        config = resolve_admission_config()

    assert config.queue_timeout_seconds == expected
    assert not caplog.records


def test_secure_listener_admission_config_queue_timeout_zero_disables_at_info(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    caplog: pytest.LogCaptureFixture,
) -> None:
    _runtime_journal(
        tmp_path,
        monkeypatch,
        {
            "setup": {"completed_at": 1700000000000},
            "link": {"secure_listener_queue_timeout_seconds": 0},
        },
    )

    with caplog.at_level(
        logging.INFO,
        logger="convey.secure_listener.admission",
    ):
        config = resolve_admission_config()

    assert config.queue_timeout_seconds == 0.0
    assert [record.levelno for record in caplog.records] == [logging.INFO]
    assert (
        "link.secure_listener_queue_timeout_seconds is 0; "
        "secure listener queue timeout disabled"
    ) in caplog.text


@pytest.mark.parametrize(
    "raw",
    [
        601,
        False,
        True,
        -1,
        0.5,
        "abc",
    ],
)
def test_secure_listener_admission_config_queue_timeout_warns_and_falls_back(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    caplog: pytest.LogCaptureFixture,
    raw: object,
) -> None:
    _runtime_journal(
        tmp_path,
        monkeypatch,
        {
            "setup": {"completed_at": 1700000000000},
            "link": {"secure_listener_queue_timeout_seconds": raw},
        },
    )

    with caplog.at_level(
        logging.WARNING,
        logger="convey.secure_listener.admission",
    ):
        config = resolve_admission_config()

    assert config.queue_timeout_seconds == DEFAULT_SECURE_LISTENER_QUEUE_TIMEOUT_SECONDS
    assert [record.levelno for record in caplog.records] == [logging.WARNING]
    assert (
        "Invalid link.secure_listener_queue_timeout_seconds in journal config: "
        f"{raw!r} \u2014 defaulting to 120.0"
    ) in caplog.text


def test_secure_listener_admission_config_warns_and_falls_back(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    caplog: pytest.LogCaptureFixture,
) -> None:
    _runtime_journal(
        tmp_path,
        monkeypatch,
        {
            "setup": {"completed_at": 1700000000000},
            "link": {
                "secure_listener_capacity": "wide",
                "secure_listener_streaming_capacity": "many",
                "secure_listener_refuse_when_full": "yes",
            },
        },
    )

    with caplog.at_level(
        logging.WARNING,
        logger="convey.secure_listener.admission",
    ):
        config = resolve_admission_config()

    assert config == SecureListenerAdmissionConfig()
    assert (
        "Invalid link.secure_listener_capacity in journal config: 'wide' "
        "\u2014 defaulting to 16"
    ) in caplog.text
    assert (
        "Invalid link.secure_listener_streaming_capacity in journal config: 'many' "
        "\u2014 defaulting to 8"
    ) in caplog.text
    assert (
        "Invalid link.secure_listener_refuse_when_full in journal config: 'yes' "
        "\u2014 defaulting to false"
    ) in caplog.text


@pytest.mark.asyncio
async def test_secure_listener_admission_refuses_when_enabled_queue_is_full() -> None:
    admission = SecureListenerAdmission(
        SecureListenerAdmissionConfig(
            capacity=1,
            streaming_capacity=0,
            refuse_when_full=True,
        )
    )
    try:
        with admission._lock:
            admission._active_total = admission.config.capacity
            for _ in range(admission.config.queue_limit):
                admission._waiters.append(SimpleNamespace(queued_at=0.0))

        with pytest.raises(SecureListenerAdmissionRejected):
            await admission.acquire()

        snapshot = admission.snapshot()
        assert snapshot["queued"]["total"] == admission.config.queue_limit
        assert snapshot["rejected"]["total"] == 1
    finally:
        admission.shutdown(wait=True, cancel_futures=True)


def test_start_secure_listener_setup_incomplete_does_not_establish_identity(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    previous_runtime = rt._runtime
    try:
        rt._runtime = None
        _runtime_journal(tmp_path, monkeypatch, {"setup": {}})
        app = _enabled_listener_app()

        rt.start_secure_listener(app)

        assert rt._runtime is None
        assert not getattr(app, "secure_listener_started", False)
        ca_path = ca_dir()
        assert not (ca_path / "cert.pem").exists()
        assert not (ca_path / "private.pem").exists()
        state_file = state_path()
        if state_file.exists():
            state = json.loads(state_file.read_text("utf-8"))
            assert not state.get("instance_id")
    finally:
        rt._runtime = previous_runtime


def test_start_secure_listener_setup_incomplete_no_thread_no_warning(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    caplog: pytest.LogCaptureFixture,
) -> None:
    previous_runtime = rt._runtime
    try:
        rt._runtime = None
        _runtime_journal(tmp_path, monkeypatch, {"setup": {}})
        app = _enabled_listener_app()

        with caplog.at_level(
            logging.WARNING,
            logger="convey.secure_listener.runtime",
        ):
            rt.start_secure_listener(app)

        assert rt._runtime is None
        assert not getattr(app, "secure_listener_started", False)
        assert not any(
            record.name == "convey.secure_listener.runtime" for record in caplog.records
        )
    finally:
        rt._runtime = previous_runtime


def test_start_secure_listener_committed_but_setup_incomplete_warns(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    caplog: pytest.LogCaptureFixture,
) -> None:
    previous_runtime = rt._runtime
    try:
        rt._runtime = None
        _runtime_journal(tmp_path, monkeypatch, {"setup": {}})
        load_or_generate_ca(ca_dir())
        app = _enabled_listener_app()

        with caplog.at_level(
            logging.WARNING,
            logger="convey.secure_listener.runtime",
        ):
            rt.start_secure_listener(app)

        assert any(
            "identity is committed but setup" in record.getMessage()
            for record in caplog.records
        )
        assert rt._runtime is None
        assert not getattr(app, "secure_listener_started", False)
    finally:
        rt._runtime = previous_runtime


@pytest.mark.parametrize(
    ("mode", "window_is_open", "expected"),
    [
        ("pl-direct", True, "pl-direct"),
        ("pl-via-spl", True, "pl-via-spl"),
        ("pl-direct", False, None),
        ("pl-via-spl", False, None),
    ],
)
def test_certless_admission_mode(
    mode: str,
    window_is_open: bool,
    expected: str | None,
) -> None:
    assert certless_admission_mode(mode, window_is_open) == expected


@pytest.mark.asyncio
async def test_priority_send_queue_drains_urgent_frames_first_and_preserves_join(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    queue: asyncio.PriorityQueue[tuple[int, int, bytes]] = asyncio.PriorityQueue()
    queue.put_nowait((1, 0, b"normal"))
    queue.put_nowait((0, 1, b"urgent"))
    written: list[bytes] = []

    monkeypatch.setattr(accept_module, "_encrypt", lambda _tls, plaintext: plaintext)

    async def write_ciphertext(data: bytes) -> None:
        written.append(data)

    await accept_module._drain_send_queue(object(), write_ciphertext, queue)
    await asyncio.wait_for(queue.join(), timeout=1.0)

    assert written == [b"urgent", b"normal"]


def test_tcp_keepalive_options_are_feature_detected() -> None:
    linux_socket = SimpleNamespace(
        SOL_SOCKET=1,
        SO_KEEPALIVE=2,
        IPPROTO_TCP=3,
        TCP_KEEPIDLE=4,
        TCP_KEEPINTVL=5,
        TCP_KEEPCNT=6,
    )
    mac_socket = SimpleNamespace(
        SOL_SOCKET=1,
        SO_KEEPALIVE=2,
        IPPROTO_TCP=3,
        TCP_KEEPALIVE=7,
    )
    minimal_socket = SimpleNamespace(SOL_SOCKET=1, SO_KEEPALIVE=2)

    assert accept_module._tcp_keepalive_options(linux_socket) == [
        (1, 2, 1),
        (3, 4, 30),
        (3, 5, 10),
        (3, 6, 3),
    ]
    assert accept_module._tcp_keepalive_options(mac_socket) == [
        (1, 2, 1),
        (3, 7, 30),
    ]
    assert accept_module._tcp_keepalive_options(minimal_socket) == [(1, 2, 1)]


def test_tcp_keepalive_failures_log_and_continue(
    monkeypatch: pytest.MonkeyPatch,
    caplog: pytest.LogCaptureFixture,
) -> None:
    sock = _FailingSocket()
    writer = _SocketWriter(sock)
    monkeypatch.setattr(
        accept_module,
        "_tcp_keepalive_options",
        lambda: [(1, 2, 3), (4, 5, 6)],
    )

    with caplog.at_level(logging.WARNING, logger="convey.secure_listener.accept"):
        accept_module._apply_tcp_keepalive(
            writer, logging.getLogger("convey.secure_listener.accept")
        )

    assert sock.calls == [(1, 2, 3), (4, 5, 6)]
    assert caplog.text.count("secure listener TCP keepalive option failed") == 2


@pytest.mark.asyncio
async def test_pump_connection_writer_failure_ends_connection(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    listener = _listener()
    tcp_reader = asyncio.StreamReader()
    tcp_reader.feed_data(b"tls-record")
    tcp_writer = _HangingDrainWriter()
    ping = build_ping(b"12345678").encode()
    tls = SimpleNamespace(handshake_done=False, peer_fingerprint=None)

    monkeypatch.setattr(accept_module, "new_server", lambda _ctx: tls)
    monkeypatch.setattr(accept_module, "window_open", lambda: False)
    monkeypatch.setattr(
        accept_module,
        "SECURE_LISTENER_TCP_DRAIN_TIMEOUT_SECONDS",
        0.0,
    )

    async def no_reader_drain(*_args: object) -> None:
        return

    def fake_drive_tls(
        _tls: object,
        *,
        inbound: bytes = b"",
        plaintext_out: bytes = b"",
    ) -> tuple[bytes, bytes]:
        if plaintext_out:
            return plaintext_out, b""
        if inbound:
            return b"", ping
        return b"", b""

    monkeypatch.setattr(accept_module, "_drain_send_queue", no_reader_drain)
    monkeypatch.setattr(accept_module, "drive_tls", fake_drive_tls)

    with pytest.raises(TimeoutError):
        await asyncio.wait_for(
            listener._pump_connection(
                tcp_reader,
                tcp_writer,
                "conn-writer-failure",
                "pl-direct",
            ),
            timeout=1.0,
        )

    assert tcp_writer.writes


def test_on_stream_reset_payload_keys_and_log_shape(
    caplog: pytest.LogCaptureFixture,
) -> None:
    emitted: list[tuple[str, dict[str, object]]] = []
    listener = _listener()
    listener._emit = lambda event, fields: emitted.append((event, dict(fields)))
    starvation_diag = ResetDiagnostic(
        stream_id=7,
        reason_code=RESET_CANCEL,
        reason_name="cancel",
        context=RESET_CTX_SEND_CREDIT_STARVATION,
        stall_age_ms=250.0,
    )
    non_starvation_diag = ResetDiagnostic(
        stream_id=9,
        reason_code=RESET_PROTOCOL_ERROR,
        reason_name="protocol_error",
        context=RESET_CTX_UNKNOWN_STREAM,
    )

    with caplog.at_level(logging.INFO, logger="convey.secure_listener.accept"):
        listener._on_stream_reset("conn-starved", starvation_diag)
        listener._on_stream_reset("conn-normal", non_starvation_diag)

    records = [
        record
        for record in caplog.records
        if record.name == "convey.secure_listener.accept"
    ]
    assert records[0].levelno == logging.WARNING
    assert (
        records[0].getMessage()
        == "secure stream starvation reset conn=conn-starved stream_id=7 "
        "reason=cancel context=send_credit_starvation stall_age_ms=250.000"
    )
    assert records[1].levelno == logging.INFO
    assert (
        records[1].getMessage() == "secure stream reset conn=conn-normal stream_id=9 "
        "reason=protocol_error context=unknown_stream"
    )

    starvation_payload = emitted[0][1]
    non_starvation_payload = emitted[1][1]
    assert emitted[0][0] == "stream_reset"
    assert emitted[1][0] == "stream_reset"
    assert set(starvation_payload) == {
        "stream_id",
        "reason_code",
        "reason_name",
        "context",
        "tunnel_id",
        "stall_age_ms",
    }
    assert starvation_payload == {
        "stream_id": 7,
        "reason_code": RESET_CANCEL,
        "reason_name": "cancel",
        "context": RESET_CTX_SEND_CREDIT_STARVATION,
        "tunnel_id": "conn-starved",
        "stall_age_ms": 250.0,
    }
    assert set(non_starvation_payload) == {
        "stream_id",
        "reason_code",
        "reason_name",
        "context",
        "tunnel_id",
    }
    assert non_starvation_payload == {
        "stream_id": 9,
        "reason_code": RESET_PROTOCOL_ERROR,
        "reason_name": "protocol_error",
        "context": RESET_CTX_UNKNOWN_STREAM,
        "tunnel_id": "conn-normal",
    }
    forbidden = {
        "sha256:",
        "127.0.0.1",
        "token",
        "BEGIN CERTIFICATE",
    }
    for _event, payload in emitted:
        values = [str(value) for value in payload.values()]
        for secret in forbidden:
            assert all(secret not in value for value in values)


def test_on_stream_reset_correlates_two_starved_streams_to_tunnels(
    caplog: pytest.LogCaptureFixture,
) -> None:
    emitted: list[tuple[str, dict[str, object]]] = []
    listener = _listener()
    listener._emit = lambda event, fields: emitted.append((event, dict(fields)))

    with caplog.at_level(logging.WARNING, logger="convey.secure_listener.accept"):
        listener._on_stream_reset(
            "conn-a",
            ResetDiagnostic(
                stream_id=1,
                reason_code=RESET_CANCEL,
                reason_name="cancel",
                context=RESET_CTX_SEND_CREDIT_STARVATION,
                stall_age_ms=50.0,
            ),
        )
        listener._on_stream_reset(
            "conn-b",
            ResetDiagnostic(
                stream_id=3,
                reason_code=RESET_CANCEL,
                reason_name="cancel",
                context=RESET_CTX_SEND_CREDIT_STARVATION,
                stall_age_ms=75.0,
            ),
        )

    warnings = [
        record.getMessage()
        for record in caplog.records
        if record.name == "convey.secure_listener.accept"
        and record.levelno == logging.WARNING
    ]
    assert warnings == [
        "secure stream starvation reset conn=conn-a stream_id=1 reason=cancel "
        "context=send_credit_starvation stall_age_ms=50.000",
        "secure stream starvation reset conn=conn-b stream_id=3 reason=cancel "
        "context=send_credit_starvation stall_age_ms=75.000",
    ]
    assert emitted == [
        (
            "stream_reset",
            {
                "stream_id": 1,
                "reason_code": RESET_CANCEL,
                "reason_name": "cancel",
                "context": RESET_CTX_SEND_CREDIT_STARVATION,
                "tunnel_id": "conn-a",
                "stall_age_ms": 50.0,
            },
        ),
        (
            "stream_reset",
            {
                "stream_id": 3,
                "reason_code": RESET_CANCEL,
                "reason_name": "cancel",
                "context": RESET_CTX_SEND_CREDIT_STARVATION,
                "tunnel_id": "conn-b",
                "stall_age_ms": 75.0,
            },
        ),
    ]


def test_on_stream_reset_logs_warning_before_raising_emit(
    caplog: pytest.LogCaptureFixture,
) -> None:
    listener = _listener()

    def emit(_event: str, _fields: dict[str, object]) -> None:
        raise RuntimeError("emit failed")

    listener._emit = emit

    with caplog.at_level(logging.WARNING, logger="convey.secure_listener.accept"):
        with pytest.raises(RuntimeError, match="emit failed"):
            listener._on_stream_reset(
                "conn-raising",
                ResetDiagnostic(
                    stream_id=1,
                    reason_code=RESET_CANCEL,
                    reason_name="cancel",
                    context=RESET_CTX_SEND_CREDIT_STARVATION,
                    stall_age_ms=50.0,
                ),
            )

    assert (
        "secure stream starvation reset conn=conn-raising stream_id=1 "
        "reason=cancel context=send_credit_starvation stall_age_ms=50.000"
    ) in caplog.text


@pytest.mark.asyncio
async def test_certless_reap_tears_down_on_passive_expiry(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    journal = _journal(tmp_path, monkeypatch, link={"posture": "spl"})
    NonceStore(nonces_path()).add("live", "phone", now=1000)
    listener = _listener()
    handle, writer, mux, task = _register_fake_certless(listener)

    await listener._reap_certless_if_window_closed(now=1000 + NONCE_TTL_SECONDS + 1)
    await asyncio.sleep(0)

    assert handle.connection_id not in listener._certless_connections
    assert writer.closed is True
    assert mux.closed is True
    assert task.cancelled()
    assert journal.exists()


@pytest.mark.asyncio
async def test_certless_reap_tears_down_idle_after_nonce_consume(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _journal(tmp_path, monkeypatch, link={"posture": "spl"})
    store = NonceStore(nonces_path())
    store.add("live", "phone", now=1000)
    store.consume("live", now=1001)
    listener = _listener()
    handle, writer, mux, _task = _register_fake_certless(listener)
    assert handle.pair_in_flight.active is False

    await listener._reap_certless_if_window_closed(now=1002)

    assert handle.connection_id not in listener._certless_connections
    assert writer.closed is True
    assert mux.closed is True


@pytest.mark.asyncio
async def test_certless_reap_skips_inflight_pair_response_this_tick_then_reaps_idle(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _journal(tmp_path, monkeypatch, link={"posture": "spl"})
    listener = _listener()
    handle, writer, mux, _task = _register_fake_certless(listener)
    handle.pair_in_flight.active = True

    await listener._reap_certless_if_window_closed(now=1002)

    assert handle.connection_id in listener._certless_connections
    assert writer.closed is False
    assert mux.closed is False

    handle.pair_in_flight.active = False
    await listener._reap_certless_if_window_closed(now=1002)

    assert handle.connection_id not in listener._certless_connections
    assert writer.closed is True
    assert mux.closed is True


@pytest.mark.asyncio
async def test_certless_not_reaped_while_nonce_live(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _journal(tmp_path, monkeypatch, link={"posture": "direct"})
    NonceStore(nonces_path()).add("live", "phone", now=1000)
    listener = _listener()
    handle, writer, mux, _task = _register_fake_certless(listener)

    try:
        await listener._reap_certless_if_window_closed(now=1001)

        assert handle.connection_id in listener._certless_connections
        assert writer.closed is False
        assert mux.closed is False
    finally:
        await listener._close_certless_connection(handle)


@pytest.mark.asyncio
@pytest.mark.parametrize("consume", ["older", "newer"])
async def test_certless_reap_two_live_nonces_consuming_one_keeps_idle_connection_until_correct_expiry(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    consume: str,
) -> None:
    _journal(tmp_path, monkeypatch, link={"posture": "spl"})
    older_issued = 1000
    newer_issued = 1010
    older_expires = older_issued + NONCE_TTL_SECONDS
    newer_expires = newer_issued + NONCE_TTL_SECONDS
    store = NonceStore(nonces_path())
    store.add("older", "phone-a", now=older_issued)
    store.add("newer", "phone-b", now=newer_issued)
    store.consume(consume, now=1020)
    listener = _listener()
    handle, writer, mux, _task = _register_fake_certless(listener)

    try:
        await listener._reap_certless_if_window_closed(now=older_expires - 1)

        assert handle.connection_id in listener._certless_connections
        assert writer.closed is False
        assert mux.closed is False

        await listener._reap_certless_if_window_closed(now=older_expires + 1)

        if consume == "newer":
            assert handle.connection_id not in listener._certless_connections
            assert writer.closed is True
            assert mux.closed is True
            return

        assert handle.connection_id in listener._certless_connections
        assert writer.closed is False
        assert mux.closed is False

        await listener._reap_certless_if_window_closed(now=newer_expires + 1)

        assert handle.connection_id not in listener._certless_connections
        assert writer.closed is True
        assert mux.closed is True
    finally:
        if handle.connection_id in listener._certless_connections:
            await listener._close_certless_connection(handle)


@pytest.mark.asyncio
async def test_certless_pair_failure_cap_tears_down_after_third_pair_failure(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    harness = await SecureListenerHarness.start(tmp_path, monkeypatch)
    close_complete = asyncio.Event()
    real_close = harness.listener._close_certless_connection
    session: link_client.TunnelSession | None = None
    tcp_writer: asyncio.StreamWriter | None = None

    async def observed_close(handle: CertlessConnection) -> None:
        await real_close(handle)
        close_complete.set()

    monkeypatch.setattr(harness.listener, "_close_certless_connection", observed_close)
    try:
        harness.seed_nonce("10000000000000000000000000000031", "failure-phone")
        tcp_reader, tcp_writer = await asyncio.open_connection(
            harness.host, harness.port
        )
        session = await link_client._open_pairing_session(
            link_client._TcpEncryptedTransport(tcp_reader, tcp_writer),
        )

        for _index in range(CERTLESS_PAIR_FAILURE_CAP):
            status, _headers, _body = await session.request(
                "POST",
                "/app/network/pair",
                headers={"content-type": "application/json"},
                body=b"{}",
            )
            assert status == 400

        await asyncio.wait_for(close_complete.wait(), timeout=5.0)

        assert harness.listener._certless_connections == {}
    finally:
        if session is not None:
            with contextlib.suppress(Exception):
                await session.close()
        elif tcp_writer is not None:
            tcp_writer.close()
            with contextlib.suppress(Exception):
                await tcp_writer.wait_closed()
        await harness.close()


@pytest.mark.asyncio
async def test_certless_concurrent_cap_refuses_fifth() -> None:
    listener = _listener()
    registered_tasks: list[asyncio.Task[None]] = []
    rejected_task = asyncio.create_task(_sleep_forever())
    try:
        for index in range(CERTLESS_TUNNEL_CAP):
            handle, _writer, _mux, task = _register_fake_certless(
                listener,
                connection_id=f"conn-{index}",
            )
            registered_tasks.append(task)
            assert handle is not None

        rejected = listener._register_certless_connection(
            "conn-rejected",
            _FakeWriter(),
            rejected_task,
            _FakeMux(),
        )

        assert rejected is None
        assert len(listener._certless_connections) == CERTLESS_TUNNEL_CAP
    finally:
        for handle in list(listener._certless_connections.values()):
            await listener._close_certless_connection(handle)
        rejected_task.cancel()
        for task in registered_tasks + [rejected_task]:
            with contextlib.suppress(asyncio.CancelledError):
                await task


def _listener() -> SecureListener:
    return SecureListener(
        app=MagicMock(),
        strict_tls_ctx=MagicMock(),
        relaxed_tls_ctx=MagicMock(),
        authorized=MagicMock(),
        admission=MagicMock(),
        callosum_emit=lambda *a, **kw: None,
        host="127.0.0.1",
        port=0,
    )


def _enabled_listener_app() -> SimpleNamespace:
    return SimpleNamespace(config={"SECURE_LISTENER_ENABLED": True})


def _runtime_journal(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    config: dict[str, object],
) -> Path:
    journal = tmp_path / "journal"
    journal.mkdir()
    config_dir = journal / "config"
    config_dir.mkdir()
    (config_dir / "journal.json").write_text(
        json.dumps(config, indent=2),
        encoding="utf-8",
    )
    monkeypatch.setenv("SOLSTONE_JOURNAL", str(journal))
    return journal


def _journal(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    *,
    link: dict[str, object],
) -> Path:
    journal = tmp_path / "journal"
    journal.mkdir()
    monkeypatch.setenv("SOLSTONE_JOURNAL", str(journal))
    write_config(journal, link=link)
    return journal


def _register_fake_certless(
    listener: SecureListener,
    *,
    connection_id: str = "conn",
) -> tuple[object, "_FakeWriter", "_FakeMux", asyncio.Task[None]]:
    writer = _FakeWriter()
    mux = _FakeMux()
    task = asyncio.create_task(_sleep_forever())
    handle = listener._register_certless_connection(connection_id, writer, task, mux)
    assert handle is not None
    return handle, writer, mux, task


async def _sleep_forever() -> None:
    await asyncio.Event().wait()


class _FakeWriter:
    def __init__(self) -> None:
        self.closed = False

    def close(self) -> None:
        self.closed = True


class _FakeMux:
    def __init__(self) -> None:
        self.closed = False

    async def close(self) -> None:
        self.closed = True


class _FailingSocket:
    def __init__(self) -> None:
        self.calls: list[tuple[int, int, int]] = []

    def setsockopt(self, level: int, optname: int, value: int) -> None:
        self.calls.append((level, optname, value))
        raise OSError("option unavailable")


class _SocketWriter:
    def __init__(self, sock: _FailingSocket) -> None:
        self._sock = sock

    def get_extra_info(self, name: str) -> object:
        if name == "socket":
            return self._sock
        return None


class _HangingDrainWriter:
    def __init__(self) -> None:
        self.writes: list[bytes] = []

    def write(self, data: bytes) -> None:
        self.writes.append(data)

    async def drain(self) -> None:
        await asyncio.Event().wait()
