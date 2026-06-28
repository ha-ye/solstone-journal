# SPDX-License-Identifier: AGPL-3.0-only
# Copyright (c) 2026 sol pbc

"""Unit tests for paired-link dial orchestration."""

from __future__ import annotations

import asyncio
import queue
import threading
import time
from concurrent.futures import CancelledError

import pytest

from solstone.think.link import dialer
from solstone.think.link.client import (
    BodySource,
    Client,
    ClientIdentity,
    EnrolledDevice,
    StreamResetError,
    TunnelSession,
    _http_head_bytes,
)
from solstone.think.link.dialer import (
    TunnelClient,
    TunnelLifecycleError,
    TunnelRequestError,
    TunnelResponseHead,
)
from solstone.think.link.tls import TlsError


def test_link_client_public_imports() -> None:
    assert Client is not None
    assert ClientIdentity is not None
    assert EnrolledDevice is not None
    assert TunnelSession is not None
    assert TlsError is not None
    assert StreamResetError is not None
    assert BodySource is not None
    assert _http_head_bytes(
        "GET",
        "/",
        headers={},
        content_length=0,
    ).startswith(b"GET / HTTP/1.1\r\n")


def _identity(*, endpoints: tuple[dict[str, object], ...]) -> ClientIdentity:
    return ClientIdentity(
        private_key_pem="private",
        client_cert_pem="cert",
        ca_chain_pem="chain",
        fingerprint="sha256:" + ("a" * 64),
        home_instance_id="instance",
        home_label="home",
        home_attestation="attestation",
        local_endpoints=endpoints,
    )


def _wait_until(predicate, *, timeout: float = 2.0) -> None:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if predicate():
            return
        time.sleep(0.01)
    assert predicate()


class _SlowBodyStream:
    def __init__(self, chunks: tuple[bytes, ...]) -> None:
        self._chunks = chunks

    async def read(self):
        for chunk in self._chunks:
            await asyncio.sleep(0.03)
            yield chunk


class _ManagedSession:
    def __init__(self) -> None:
        self.loop = asyncio.get_running_loop()
        self.closed_future: asyncio.Future[None] = self.loop.create_future()
        self.requests: list[tuple[str, str, dict[str, str], bytes | BodySource]] = []
        self.stream_requests: list[
            tuple[str, str, dict[str, str], bytes | BodySource]
        ] = []
        self.close_calls = 0
        self.failure_reason_value: str | None = None

    @property
    def is_alive(self) -> bool:
        return not self.closed_future.done()

    @property
    def closed(self) -> asyncio.Future[None]:
        return self.closed_future

    def failure_reason(self) -> str | None:
        return self.failure_reason_value

    def close_remote(self, reason: str = "session_closed") -> None:
        self.failure_reason_value = reason
        self.loop.call_soon_threadsafe(self._finish_closed)

    def _finish_closed(self) -> None:
        if not self.closed_future.done():
            self.closed_future.set_result(None)

    async def request(self, method, path, *, headers, body):
        self.requests.append((method, path, headers, body))
        return 200, {"x-session": "fresh"}, b"ok"

    async def stream_request(self, method, path, *, headers, body):
        self.stream_requests.append((method, path, headers, body))
        return 200, {"x-stream": "fresh"}, b"", _SlowBodyStream(())

    async def close(self) -> None:
        self.close_calls += 1
        self._finish_closed()


def _managed_client(**kwargs) -> TunnelClient:
    return TunnelClient(
        _identity(endpoints=()),
        None,
        request_session_wait=kwargs.pop("request_session_wait", 0.25),
        reconnect_initial_backoff=kwargs.pop("reconnect_initial_backoff", 0.01),
        reconnect_max_backoff=kwargs.pop("reconnect_max_backoff", 0.02),
        **kwargs,
    )


@pytest.mark.asyncio
async def test_lan_direct_race_picks_first_and_cancels_loser(monkeypatch) -> None:
    identity = _identity(
        endpoints=(
            {"ip": "10.0.0.1", "port": 7657},
            {"ip": "10.0.0.2", "port": 7657},
        )
    )
    cancelled: list[str] = []
    winner = object()

    async def dial_direct(_client, endpoint, _identity, _deadline=None):
        if endpoint["ip"] == "10.0.0.2":
            await asyncio.sleep(0)
            return winner
        try:
            await asyncio.sleep(60)
        except asyncio.CancelledError:
            cancelled.append(str(endpoint["ip"]))
            raise

    monkeypatch.setattr(dialer, "_dial_direct_endpoint", dial_direct)

    assert await dialer.open_tunnel(identity, None) is winner
    assert cancelled == ["10.0.0.1"]


@pytest.mark.asyncio
async def test_all_fail_error_names_every_attempt(monkeypatch) -> None:
    identity = _identity(endpoints=({"ip": "10.0.0.1", "port": 7657},))

    async def dial_direct(_client, _endpoint, _identity, _deadline=None):
        raise TlsError("lan failed")

    async def dial_relay(_client, _relay_url, _identity, _deadline=None):
        raise OSError("relay failed")

    monkeypatch.setattr(dialer, "_dial_direct_endpoint", dial_direct)
    monkeypatch.setattr(dialer, "_dial_relay", dial_relay)

    with pytest.raises(TlsError) as exc_info:
        await dialer.open_tunnel(identity, "https://relay.test")

    message = str(exc_info.value)
    assert "lan-direct 10.0.0.1:7657" in message
    assert "lan failed" in message
    assert "spl-relay" in message
    assert "relay failed" in message


def test_cached_session_drops_on_stream_reset(monkeypatch) -> None:
    class ResetSession(_ManagedSession):
        async def request(self, *_args, **_kwargs):
            raise StreamResetError("reset")

    sessions: list[ResetSession] = []

    async def open_tunnel(_identity, _relay_url):
        session = ResetSession()
        sessions.append(session)
        return session

    monkeypatch.setattr(dialer, "open_tunnel", open_tunnel)
    client = _managed_client()
    try:
        with pytest.raises(TunnelRequestError) as exc_info:
            client.request("GET", "/")
    finally:
        client.close()

    assert exc_info.value.reason == "StreamResetError"
    assert sessions[0].close_calls == 1
    assert client._session is None


def test_proxy_stream_request_queues_head_body_and_sentinel(monkeypatch) -> None:
    class FakeStream:
        async def read(self):
            yield b"chunk-a"
            yield b"chunk-b"

    client = TunnelClient(_identity(endpoints=()), None)
    calls = []

    async def fake_stream_request_async(method, path, *, headers, body):
        calls.append((method, path, headers, body))
        return 418, {"x-test": "yes"}, b"initial", FakeStream()

    monkeypatch.setattr(client, "_stream_request_async", fake_stream_request_async)
    chunks: queue.Queue[TunnelResponseHead | bytes | Exception | None] = queue.Queue()
    try:
        future = client.proxy_stream_request(
            "POST",
            "/hello",
            headers={"Host": "example"},
            body=b"payload",
            chunks=chunks,
        )
        future.result(timeout=2)
    finally:
        client.close()

    assert calls == [("POST", "/hello", {"Host": "example"}, b"payload")]
    assert chunks.get_nowait() == TunnelResponseHead(418, {"x-test": "yes"})
    assert chunks.get_nowait() == b"initial"
    assert chunks.get_nowait() == b"chunk-a"
    assert chunks.get_nowait() == b"chunk-b"
    assert chunks.get_nowait() is None


def test_proxy_stream_request_queues_tunnel_error_and_sentinel(monkeypatch) -> None:
    client = TunnelClient(_identity(endpoints=()), None)

    async def fake_stream_request_async(_method, _path, *, headers, body):
        _ = (headers, body)
        raise ConnectionError("down")

    monkeypatch.setattr(client, "_stream_request_async", fake_stream_request_async)
    chunks: queue.Queue[TunnelResponseHead | bytes | Exception | None] = queue.Queue()
    try:
        future = client.proxy_stream_request("GET", "/", chunks=chunks)
        future.result(timeout=2)
    finally:
        client.close()

    error = chunks.get_nowait()
    assert isinstance(error, TunnelRequestError)
    assert error.reason == "ConnectionError"
    assert chunks.get_nowait() is None


class _AliveSession:
    def __init__(self) -> None:
        self.closed = False
        self.requests: list[tuple[str, str, dict[str, str], bytes]] = []
        self.stream_requests: list[tuple[str, str, dict[str, str], bytes]] = []

    @property
    def is_alive(self) -> bool:
        return True

    async def request(self, method, path, *, headers, body):
        self.requests.append((method, path, headers, body))
        return 200, {"x-test": "yes"}, b"ok"

    async def stream_request(self, method, path, *, headers, body):
        self.stream_requests.append((method, path, headers, body))
        return 200, {"x-stream": "yes"}, b"initial", _SlowBodyStream(())

    async def close(self) -> None:
        self.closed = True


class _DeadSession:
    def __init__(self) -> None:
        self.closed = False

    @property
    def is_alive(self) -> bool:
        return False

    async def close(self) -> None:
        self.closed = True


class _HangingSession:
    def __init__(self) -> None:
        self.closed = False

    @property
    def is_alive(self) -> bool:
        return True

    async def request(self, *_args, **_kwargs):
        await asyncio.sleep(3600)

    async def stream_request(self, *_args, **_kwargs):
        await asyncio.sleep(3600)

    async def close(self) -> None:
        self.closed = True


def test_connection_manager_redials_after_remote_close(monkeypatch) -> None:
    sessions: list[_ManagedSession] = []

    async def fake_open_tunnel(_identity, _relay_url):
        session = _ManagedSession()
        sessions.append(session)
        return session

    monkeypatch.setattr(dialer, "open_tunnel", fake_open_tunnel)
    client = _managed_client()
    try:
        client.start()
        _wait_until(
            lambda: len(sessions) == 1 and client.status()["health"] == "healthy"
        )

        sessions[0].close_remote("session_closed")
        _wait_until(
            lambda: len(sessions) >= 2 and client.status()["health"] == "healthy"
        )

        status = client.status()
        assert status["state"] == "connected"
        assert status["reconnect_count"] == 1
        assert status["last_failure"] is not None
        assert status["last_failure"]["reason"] == "session_closed"
        assert client.request("GET", "/after-redial") == (
            200,
            {"x-session": "fresh"},
            b"ok",
        )
    finally:
        client.close()

    assert sessions[-1].requests == [("GET", "/after-redial", {}, b"")]


def test_connection_manager_records_liveness_failure(monkeypatch) -> None:
    sessions: list[_ManagedSession] = []

    async def fake_open_tunnel(_identity, _relay_url):
        session = _ManagedSession()
        sessions.append(session)
        return session

    monkeypatch.setattr(dialer, "open_tunnel", fake_open_tunnel)
    client = _managed_client()
    try:
        client.start()
        _wait_until(
            lambda: len(sessions) == 1 and client.status()["health"] == "healthy"
        )

        sessions[0].close_remote("liveness_failed")
        _wait_until(
            lambda: (
                len(sessions) >= 2
                and client.status()["last_failure"] is not None
                and client.status()["last_failure"]["reason"] == "liveness_failed"
            )
        )

        status = client.status()
        assert status["health"] == "healthy"
        assert status["state"] == "connected"
        assert status["reconnect_count"] == 1
    finally:
        client.close()


def test_request_during_reconnect_fails_with_lifecycle_error(monkeypatch) -> None:
    started = threading.Event()

    async def fake_open_tunnel(_identity, _relay_url):
        started.set()
        await asyncio.sleep(3600)
        raise AssertionError("unreachable")

    monkeypatch.setattr(dialer, "open_tunnel", fake_open_tunnel)
    client = _managed_client(request_session_wait=0.02)
    try:
        client.start()
        assert started.wait(timeout=1)
        with pytest.raises(TunnelLifecycleError) as exc_info:
            client.request("GET", "/during-reconnect")
    finally:
        client.close()

    assert exc_info.value.state == "connecting"
    assert exc_info.value.retryable is True
    assert "no live tunnel session" in exc_info.value.detail


def test_dead_manager_status_and_requests_fail_closed(monkeypatch) -> None:
    async def fake_open_tunnel(_identity, _relay_url):
        await asyncio.sleep(3600)
        raise AssertionError("unreachable")

    monkeypatch.setattr(dialer, "open_tunnel", fake_open_tunnel)
    client = _managed_client(request_session_wait=0.02)
    try:
        client.start()

        async def kill_manager() -> None:
            assert client._manager_task is not None
            client._manager_task.cancel()

        client._run(kill_manager())
        _wait_until(lambda: client.status()["state"] == "dead_manager")

        status = client.status()
        assert status["health"] == "unhealthy"
        assert status["manager_alive"] is False
        with pytest.raises(TunnelLifecycleError) as exc_info:
            client.request("GET", "/dead")
        assert exc_info.value.state == "dead_manager"
    finally:
        client.close()


def test_proxy_stream_request_accepts_body_source(monkeypatch) -> None:
    class FakeStream:
        async def read(self):
            if False:
                yield b""

    client = TunnelClient(_identity(endpoints=()), None)
    source = BodySource(6, (b"ab", b"cd", b"ef"))
    calls = []

    async def fake_stream_request_async(method, path, *, headers, body):
        calls.append((method, path, headers, body))
        return 200, {}, b"", FakeStream()

    monkeypatch.setattr(client, "_stream_request_async", fake_stream_request_async)
    chunks: queue.Queue[TunnelResponseHead | bytes | Exception | None] = queue.Queue()
    try:
        future = client.proxy_stream_request(
            "POST",
            "/upload",
            body=source,
            chunks=chunks,
        )
        future.result(timeout=2)
    finally:
        client.close()

    assert calls == [("POST", "/upload", {}, source)]
    assert chunks.get_nowait() == TunnelResponseHead(200, {})
    assert chunks.get_nowait() is None


def test_proxy_stream_request_cancel_resets_remote_stream(monkeypatch) -> None:
    entered_read = threading.Event()
    cancel_called = threading.Event()

    class CancellableStream:
        async def read(self):
            entered_read.set()
            await asyncio.sleep(3600)
            if False:
                yield b""

        async def cancel(self) -> None:
            cancel_called.set()

    client = TunnelClient(_identity(endpoints=()), None)

    async def fake_stream_request_async(_method, _path, *, headers, body):
        _ = (headers, body)
        return 200, {}, b"", CancellableStream()

    monkeypatch.setattr(client, "_stream_request_async", fake_stream_request_async)
    chunks: queue.Queue[TunnelResponseHead | bytes | Exception | None] = queue.Queue()
    try:
        future = client.proxy_stream_request("GET", "/events", chunks=chunks)
        assert chunks.get(timeout=1) == TunnelResponseHead(200, {})
        assert entered_read.wait(timeout=1)
        future.cancel()
        with pytest.raises(CancelledError):
            future.result(timeout=1)
        assert cancel_called.wait(timeout=1)
    finally:
        client.close()


def test_connection_manager_opens_session_for_request(monkeypatch) -> None:
    sessions: list[_ManagedSession] = []
    calls = 0

    async def fake_open_tunnel(_identity, _relay_url):
        nonlocal calls
        calls += 1
        session = _ManagedSession()
        sessions.append(session)
        return session

    monkeypatch.setattr(dialer, "open_tunnel", fake_open_tunnel)
    client = _managed_client()
    try:
        assert client.request("POST", "/api", headers={"x": "1"}, body=b"body") == (
            200,
            {"x-session": "fresh"},
            b"ok",
        )
    finally:
        client.close()

    assert calls == 1
    assert sessions[0].requests == [("POST", "/api", {"x": "1"}, b"body")]


def test_connection_manager_opens_session_for_stream_request(monkeypatch) -> None:
    sessions: list[_ManagedSession] = []
    calls = 0

    async def fake_open_tunnel(_identity, _relay_url):
        nonlocal calls
        calls += 1
        session = _ManagedSession()
        sessions.append(session)
        return session

    monkeypatch.setattr(dialer, "open_tunnel", fake_open_tunnel)
    client = _managed_client()
    try:
        status, headers, initial, _stream = client.stream_request("GET", "/stream")
    finally:
        client.close()

    assert calls == 1
    assert (status, headers, initial) == (200, {"x-stream": "fresh"}, b"")
    assert sessions[0].stream_requests == [("GET", "/stream", {}, b"")]


def test_connection_manager_reuses_live_session_without_redial(monkeypatch) -> None:
    sessions: list[_ManagedSession] = []
    calls = 0

    async def fake_open_tunnel(_identity, _relay_url):
        nonlocal calls
        calls += 1
        session = _ManagedSession()
        sessions.append(session)
        return session

    monkeypatch.setattr(dialer, "open_tunnel", fake_open_tunnel)
    client = _managed_client()
    try:
        assert client.request("GET", "/cached-a") == (
            200,
            {"x-session": "fresh"},
            b"ok",
        )
        assert client.request("GET", "/cached-b") == (
            200,
            {"x-session": "fresh"},
            b"ok",
        )
    finally:
        client.close()

    assert calls == 1
    assert sessions[0].requests == [
        ("GET", "/cached-a", {}, b""),
        ("GET", "/cached-b", {}, b""),
    ]


def test_failed_redial_queues_lifecycle_error(monkeypatch) -> None:
    async def fake_open_tunnel(_identity, _relay_url):
        raise OSError("dial failed")

    monkeypatch.setattr(dialer, "open_tunnel", fake_open_tunnel)
    client = _managed_client(request_session_wait=0.02)
    chunks: queue.Queue[TunnelResponseHead | bytes | Exception | None] = queue.Queue()
    try:
        future = client.proxy_stream_request("GET", "/", chunks=chunks)
        future.result(timeout=1)
    finally:
        client.close()

    error = chunks.get_nowait()
    assert isinstance(error, TunnelLifecycleError)
    assert error.reason == "lifecycle"
    assert error.state in {"connecting", "degraded"}
    assert chunks.get_nowait() is None
    assert client._session is None


def test_proxy_stream_request_times_out_during_head_and_clears_session() -> None:
    class HangingManagedSession(_ManagedSession):
        async def stream_request(self, *_args, **_kwargs):
            await asyncio.sleep(3600)

    sessions: list[HangingManagedSession] = []

    async def fake_open_tunnel(_identity, _relay_url):
        session = HangingManagedSession()
        sessions.append(session)
        return session

    monkeypatch = pytest.MonkeyPatch()
    monkeypatch.setattr(dialer, "open_tunnel", fake_open_tunnel)
    client = _managed_client(establish_timeout=0.05)
    chunks: queue.Queue[TunnelResponseHead | bytes | Exception | None] = queue.Queue()
    try:
        future = client.proxy_stream_request("GET", "/hang", chunks=chunks)
        future.result(timeout=1)
    finally:
        client.close()
        monkeypatch.undo()

    error = chunks.get_nowait()
    assert isinstance(error, TunnelRequestError)
    assert error.reason == "TimeoutError"
    assert chunks.get_nowait() is None
    assert sessions[0].close_calls == 1
    assert client._session is None


def test_bare_stream_request_times_out_during_head(monkeypatch) -> None:
    class HangingManagedSession(_ManagedSession):
        async def stream_request(self, *_args, **_kwargs):
            await asyncio.sleep(3600)

    async def fake_open_tunnel(_identity, _relay_url):
        return HangingManagedSession()

    monkeypatch.setattr(dialer, "open_tunnel", fake_open_tunnel)
    client = _managed_client(establish_timeout=0.05)
    try:
        with pytest.raises(TimeoutError):
            client.stream_request("GET", "/hang")
    finally:
        client.close()


def test_request_times_out_during_head_and_clears_session(monkeypatch) -> None:
    class HangingManagedSession(_ManagedSession):
        async def request(self, *_args, **_kwargs):
            await asyncio.sleep(3600)

    sessions: list[HangingManagedSession] = []

    async def fake_open_tunnel(_identity, _relay_url):
        session = HangingManagedSession()
        sessions.append(session)
        return session

    monkeypatch.setattr(dialer, "open_tunnel", fake_open_tunnel)
    client = _managed_client(establish_timeout=0.05)
    try:
        with pytest.raises(TunnelRequestError) as exc_info:
            client.request("GET", "/hang")
    finally:
        client.close()

    assert exc_info.value.reason == "TimeoutError"
    assert sessions[0].close_calls == 1
    assert client._session is None


def test_establish_timeout_is_not_armed_during_body_streaming() -> None:
    class SlowBodySession(_ManagedSession):
        async def stream_request(self, method, path, *, headers, body):
            self.stream_requests.append((method, path, headers, body))
            return 200, {}, b"initial", _SlowBodyStream((b"late-a", b"late-b"))

    sessions: list[SlowBodySession] = []

    async def fake_open_tunnel(_identity, _relay_url):
        session = SlowBodySession()
        sessions.append(session)
        return session

    monkeypatch = pytest.MonkeyPatch()
    monkeypatch.setattr(dialer, "open_tunnel", fake_open_tunnel)
    client = _managed_client(establish_timeout=0.01)
    chunks: queue.Queue[bytes | Exception | None] = queue.Queue()
    try:
        future = client.stream_request("GET", "/slow", chunks=chunks)
        future.result(timeout=1)
    finally:
        client.close()
        monkeypatch.undo()

    assert chunks.get_nowait() == b"initial"
    assert chunks.get_nowait() == b"late-a"
    assert chunks.get_nowait() == b"late-b"
    assert chunks.get_nowait() is None
    assert sessions[0].stream_requests == [("GET", "/slow", {}, b"")]


@pytest.mark.asyncio
async def test_dead_session_redial_is_single_flight(monkeypatch) -> None:
    sessions: list[_ManagedSession] = []
    calls = 0

    async def fake_open_tunnel(_identity, _relay_url):
        nonlocal calls
        calls += 1
        await asyncio.sleep(0)
        session = _ManagedSession()
        sessions.append(session)
        return session

    monkeypatch.setattr(dialer, "open_tunnel", fake_open_tunnel)
    client = _managed_client()

    first, second = await asyncio.gather(
        client._get_session_async(),
        client._get_session_async(),
    )

    assert calls == 1
    assert first is sessions[0]
    assert second is sessions[0]
