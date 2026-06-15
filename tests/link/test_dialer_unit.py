# SPDX-License-Identifier: AGPL-3.0-only
# Copyright (c) 2026 sol pbc

"""Unit tests for paired-link dial orchestration."""

from __future__ import annotations

import asyncio
import queue

import pytest

from solstone.think.link import dialer
from solstone.think.link.client import (
    Client,
    ClientIdentity,
    EnrolledDevice,
    StreamResetError,
    TunnelSession,
    _http_request_bytes,
)
from solstone.think.link.dialer import (
    TunnelClient,
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
    assert _http_request_bytes(
        "GET",
        "/",
        headers={},
        body=b"",
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
    class ResetSession:
        def __init__(self) -> None:
            self.closed = False

        async def request(self, *_args, **_kwargs):
            raise StreamResetError("reset")

        async def close(self) -> None:
            self.closed = True

    session = ResetSession()

    async def open_tunnel(_identity, _relay_url):
        return session

    monkeypatch.setattr(dialer, "open_tunnel", open_tunnel)
    client = TunnelClient(_identity(endpoints=()), None)
    try:
        with pytest.raises(TunnelRequestError) as exc_info:
            client.request("GET", "/")
    finally:
        client.close()

    assert exc_info.value.reason == "StreamResetError"
    assert session.closed is True
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


class _SlowBodyStream:
    def __init__(self, chunks: tuple[bytes, ...]) -> None:
        self._chunks = chunks

    async def read(self):
        for chunk in self._chunks:
            await asyncio.sleep(0.03)
            yield chunk


def test_dead_cached_session_redials_for_request(monkeypatch) -> None:
    dead = _DeadSession()
    fresh = _AliveSession()
    calls = 0

    async def fake_open_tunnel(_identity, _relay_url):
        nonlocal calls
        calls += 1
        return fresh

    monkeypatch.setattr(dialer, "open_tunnel", fake_open_tunnel)
    client = TunnelClient(_identity(endpoints=()), None)
    client._session = dead  # type: ignore[assignment]
    try:
        assert client.request("POST", "/api", headers={"x": "1"}, body=b"body") == (
            200,
            {"x-test": "yes"},
            b"ok",
        )
    finally:
        client.close()

    assert calls == 1
    assert dead.closed is True
    assert fresh.requests == [("POST", "/api", {"x": "1"}, b"body")]


def test_dead_cached_session_redials_for_stream_request(monkeypatch) -> None:
    dead = _DeadSession()
    fresh = _AliveSession()
    calls = 0

    async def fake_open_tunnel(_identity, _relay_url):
        nonlocal calls
        calls += 1
        return fresh

    monkeypatch.setattr(dialer, "open_tunnel", fake_open_tunnel)
    client = TunnelClient(_identity(endpoints=()), None)
    client._session = dead  # type: ignore[assignment]
    try:
        status, headers, initial, _stream = client.stream_request("GET", "/stream")
    finally:
        client.close()

    assert calls == 1
    assert dead.closed is True
    assert (status, headers, initial) == (200, {"x-stream": "yes"}, b"initial")
    assert fresh.stream_requests == [("GET", "/stream", {}, b"")]


def test_live_cached_session_reused_without_redial(monkeypatch) -> None:
    live = _AliveSession()
    calls = 0

    async def fake_open_tunnel(_identity, _relay_url):
        nonlocal calls
        calls += 1
        raise AssertionError("live cached session should be reused")

    monkeypatch.setattr(dialer, "open_tunnel", fake_open_tunnel)
    client = TunnelClient(_identity(endpoints=()), None)
    client._session = live  # type: ignore[assignment]
    try:
        assert client.request("GET", "/cached") == (200, {"x-test": "yes"}, b"ok")
    finally:
        client.close()

    assert calls == 0
    assert live.requests == [("GET", "/cached", {}, b"")]


def test_failed_redial_leaves_session_none_and_queues_proxy_error(monkeypatch) -> None:
    dead = _DeadSession()

    async def fake_open_tunnel(_identity, _relay_url):
        raise OSError("dial failed")

    monkeypatch.setattr(dialer, "open_tunnel", fake_open_tunnel)
    client = TunnelClient(_identity(endpoints=()), None)
    client._session = dead  # type: ignore[assignment]
    chunks: queue.Queue[TunnelResponseHead | bytes | Exception | None] = queue.Queue()
    try:
        future = client.proxy_stream_request("GET", "/", chunks=chunks)
        future.result(timeout=1)
    finally:
        client.close()

    error = chunks.get_nowait()
    assert isinstance(error, TunnelRequestError)
    assert error.reason == "OSError"
    assert chunks.get_nowait() is None
    assert dead.closed is True
    assert client._session is None


def test_proxy_stream_request_times_out_during_head_and_clears_session() -> None:
    session = _HangingSession()
    client = TunnelClient(
        _identity(endpoints=()),
        None,
        establish_timeout=0.05,
    )
    client._session = session  # type: ignore[assignment]
    chunks: queue.Queue[TunnelResponseHead | bytes | Exception | None] = queue.Queue()
    try:
        future = client.proxy_stream_request("GET", "/hang", chunks=chunks)
        future.result(timeout=1)
    finally:
        client.close()

    error = chunks.get_nowait()
    assert isinstance(error, TunnelRequestError)
    assert error.reason == "TimeoutError"
    assert chunks.get_nowait() is None
    assert session.closed is True
    assert client._session is None


def test_bare_stream_request_times_out_during_head() -> None:
    session = _HangingSession()
    client = TunnelClient(
        _identity(endpoints=()),
        None,
        establish_timeout=0.05,
    )
    client._session = session  # type: ignore[assignment]
    try:
        with pytest.raises(TimeoutError):
            client.stream_request("GET", "/hang")
    finally:
        client.close()


def test_request_times_out_during_head_and_clears_session() -> None:
    session = _HangingSession()
    client = TunnelClient(
        _identity(endpoints=()),
        None,
        establish_timeout=0.05,
    )
    client._session = session  # type: ignore[assignment]
    try:
        with pytest.raises(TunnelRequestError) as exc_info:
            client.request("GET", "/hang")
    finally:
        client.close()

    assert exc_info.value.reason == "TimeoutError"
    assert session.closed is True
    assert client._session is None


def test_establish_timeout_is_not_armed_during_body_streaming() -> None:
    class SlowBodySession(_AliveSession):
        async def stream_request(self, method, path, *, headers, body):
            self.stream_requests.append((method, path, headers, body))
            return 200, {}, b"initial", _SlowBodyStream((b"late-a", b"late-b"))

    session = SlowBodySession()
    client = TunnelClient(
        _identity(endpoints=()),
        None,
        establish_timeout=0.01,
    )
    client._session = session  # type: ignore[assignment]
    chunks: queue.Queue[bytes | Exception | None] = queue.Queue()
    try:
        future = client.stream_request("GET", "/slow", chunks=chunks)
        future.result(timeout=1)
    finally:
        client.close()

    assert chunks.get_nowait() == b"initial"
    assert chunks.get_nowait() == b"late-a"
    assert chunks.get_nowait() == b"late-b"
    assert chunks.get_nowait() is None
    assert session.stream_requests == [("GET", "/slow", {}, b"")]


@pytest.mark.asyncio
async def test_dead_session_redial_is_single_flight(monkeypatch) -> None:
    dead = _DeadSession()
    fresh = _AliveSession()
    calls = 0

    async def fake_open_tunnel(_identity, _relay_url):
        nonlocal calls
        calls += 1
        await asyncio.sleep(0)
        return fresh

    monkeypatch.setattr(dialer, "open_tunnel", fake_open_tunnel)
    client = TunnelClient(_identity(endpoints=()), None)
    client._session = dead  # type: ignore[assignment]

    first, second = await asyncio.gather(
        client._get_session_async(),
        client._get_session_async(),
    )

    assert calls == 1
    assert first is fresh
    assert second is fresh
    assert dead.closed is True
