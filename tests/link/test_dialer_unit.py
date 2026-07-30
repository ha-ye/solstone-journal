# SPDX-License-Identifier: AGPL-3.0-only
# Copyright (c) 2026 sol pbc

"""Unit tests for paired-link dial orchestration."""

from __future__ import annotations

import asyncio
import contextlib
import gc
import queue
import threading
import time
from collections.abc import Iterator
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


class _SlowBodyStream:
    def __init__(self, chunks: tuple[bytes, ...]) -> None:
        self._chunks = chunks

    async def read(self):
        for chunk in self._chunks:
            await asyncio.sleep(0.03)
            yield chunk


class _RaceSession:
    def __init__(self, *, close_error: BaseException | None = None) -> None:
        self.close_calls = 0
        self.close_error = close_error

    @property
    def is_alive(self) -> bool:
        return True

    async def close(self) -> None:
        self.close_calls += 1
        if self.close_error is not None:
            raise self.close_error


class _OrderedDone(set[asyncio.Task[object]]):
    def __init__(self, ordered: tuple[asyncio.Task[object], ...]) -> None:
        super().__init__(ordered)
        self._ordered = ordered

    def __iter__(self) -> Iterator[asyncio.Task[object]]:
        return iter(self._ordered)


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


def _spy_lifecycle_failures(
    client: TunnelClient,
    *,
    after: int,
) -> tuple[list[tuple[str, str, float | None]], threading.Event]:
    records: list[tuple[str, str, float | None]] = []
    observed = threading.Event()
    original = client._record_lifecycle_failure

    async def record_spy(
        reason: str,
        detail: str,
        *,
        state: str,
        next_retry_in: float | None = None,
    ) -> None:
        records.append((reason, detail, next_retry_in))
        if len(records) >= after:
            observed.set()
        await original(
            reason,
            detail,
            state=state,
            next_retry_in=next_retry_in,
        )

    client._record_lifecycle_failure = record_spy
    return records, observed


async def _finish_session_after(session: _ManagedSession, delay: float) -> None:
    loop = asyncio.get_running_loop()
    done: asyncio.Future[None] = loop.create_future()

    def finish() -> None:
        session._finish_closed()
        if not done.done():
            done.set_result(None)

    loop.call_later(delay, finish)
    await done


def _remaining_deadline(deadline: float) -> float:
    return max(0.0, deadline - time.monotonic())


def test_connection_manager_passes_default_absolute_dial_deadline(monkeypatch) -> None:
    deadlines: list[float | None] = []
    called = threading.Event()

    async def fake_open_tunnel(_identity, _relay_url, *, deadline=None, **_kwargs):
        deadlines.append(deadline)
        called.set()
        return _ManagedSession()

    monkeypatch.setattr(dialer, "open_tunnel", fake_open_tunnel)
    client = _managed_client()
    try:
        before = time.monotonic()
        client.start()
        assert called.wait(timeout=1)
        after = time.monotonic()
    finally:
        client.close()

    assert deadlines[0] is not None
    assert before + dialer._DIAL_TIMEOUT_SECONDS <= deadlines[0]
    assert deadlines[0] <= after + dialer._DIAL_TIMEOUT_SECONDS


def test_connection_manager_dial_deadline_bounds_retries(monkeypatch) -> None:
    seen: list[float | None] = []
    calls = 0

    async def fake_open_tunnel(_identity, _relay_url, *, deadline=None, **_kwargs):
        nonlocal calls
        calls += 1
        seen.append(deadline)
        if deadline is None:
            raise AssertionError("dial received no deadline")
        await asyncio.wait_for(
            asyncio.sleep(3600),
            timeout=_remaining_deadline(deadline),
        )
        raise AssertionError("unreachable")

    monkeypatch.setattr(dialer, "open_tunnel", fake_open_tunnel)
    client = _managed_client(
        dial_timeout=0.01,
        reconnect_initial_backoff=0.005,
        reconnect_max_backoff=0.005,
        request_session_wait=0.01,
    )
    records, observed = _spy_lifecycle_failures(client, after=2)
    try:
        client.start()
        assert observed.wait(timeout=1)
    finally:
        client.close()

    assert calls > 1
    assert all(deadline is not None for deadline in seen)
    assert any(reason == "TimeoutError" for reason, _detail, _retry in records)


def test_connection_manager_adopts_session_inside_dial_bound(monkeypatch) -> None:
    sessions: list[_ManagedSession] = []

    async def fake_open_tunnel(_identity, _relay_url, *, deadline=None, **_kwargs):
        if deadline is None:
            raise AssertionError("dial received no deadline")
        await asyncio.wait_for(
            asyncio.sleep(0.01),
            timeout=_remaining_deadline(deadline),
        )
        session = _ManagedSession()
        sessions.append(session)
        return session

    monkeypatch.setattr(dialer, "open_tunnel", fake_open_tunnel)
    client = _managed_client(dial_timeout=0.2)
    try:
        assert client.request("GET", "/inside-bound") == (
            200,
            {"x-session": "fresh"},
            b"ok",
        )
    finally:
        client.close()

    assert len(sessions) == 1
    assert sessions[0].requests == [("GET", "/inside-bound", {}, b"")]


def test_connection_manager_passes_explicit_absolute_dial_deadline(monkeypatch) -> None:
    dial_timeout = 0.123
    deadlines: list[float | None] = []
    called = threading.Event()

    async def fake_open_tunnel(_identity, _relay_url, *, deadline=None, **_kwargs):
        deadlines.append(deadline)
        called.set()
        return _ManagedSession()

    monkeypatch.setattr(dialer, "open_tunnel", fake_open_tunnel)
    client = _managed_client(dial_timeout=dial_timeout)
    try:
        before = time.monotonic()
        client.start()
        assert called.wait(timeout=1)
        after = time.monotonic()
    finally:
        client.close()

    assert deadlines[0] is not None
    assert before + dial_timeout <= deadlines[0]
    assert deadlines[0] <= after + dial_timeout


@pytest.mark.asyncio
async def test_open_tunnel_reports_direct_attempt_timeout_label(monkeypatch) -> None:
    async def dial_direct(_host, _enrolled, *, port=7657):
        _ = port
        await asyncio.sleep(3600)

    monkeypatch.setattr(dialer.Client, "dial_direct", staticmethod(dial_direct))
    identity = _identity(endpoints=({"ip": "10.0.0.1", "port": 7657},))

    with pytest.raises(TlsError) as exc_info:
        await dialer.open_tunnel(
            identity,
            None,
            deadline=time.monotonic() + 0.01,
        )

    message = str(exc_info.value)
    assert "lan-direct 10.0.0.1:7657" in message
    assert "TimeoutError" in message


def test_short_lived_sessions_increase_then_plateau_backoff(monkeypatch) -> None:
    async def fake_open_tunnel(_identity, _relay_url, **_kwargs):
        session = _ManagedSession()
        session._finish_closed()
        return session

    monkeypatch.setattr(dialer, "open_tunnel", fake_open_tunnel)
    client = _managed_client(
        reconnect_initial_backoff=0.005,
        reconnect_max_backoff=0.04,
    )
    records, observed = _spy_lifecycle_failures(client, after=5)
    try:
        client.start()
        assert observed.wait(timeout=1)
    finally:
        client.close()

    assert [retry for _reason, _detail, retry in records[:5]] == [
        0.005,
        0.01,
        0.02,
        0.04,
        0.04,
    ]


def test_stable_session_resets_recorded_retry_backoff(monkeypatch) -> None:
    calls = 0
    held: list[_ManagedSession] = []
    held_ready = threading.Event()

    async def fake_open_tunnel(_identity, _relay_url, **_kwargs):
        nonlocal calls
        calls += 1
        session = _ManagedSession()
        if calls <= 2:
            session._finish_closed()
        else:
            held.append(session)
            held_ready.set()
        return session

    monkeypatch.setattr(dialer, "open_tunnel", fake_open_tunnel)
    client = _managed_client(
        reconnect_initial_backoff=0.005,
        reconnect_max_backoff=0.04,
        session_stable_after=0.02,
    )
    records, observed = _spy_lifecycle_failures(client, after=3)
    try:
        client.start()
        assert held_ready.wait(timeout=1)
        assert client.request("GET", "/stable") == (
            200,
            {"x-session": "fresh"},
            b"ok",
        )
        client._run(_finish_session_after(held[0], 0.03))
        assert observed.wait(timeout=1)
    finally:
        client.close()

    assert [retry for _reason, _detail, retry in records[:3]] == [
        0.005,
        0.01,
        0.005,
    ]


def test_short_session_below_stability_threshold_keeps_backing_off(monkeypatch) -> None:
    # Short-lived connect-then-die sessions can last long enough to pass small
    # thresholds; keep the production default at a minute so they do not reset backoff.
    assert dialer._SESSION_STABLE_AFTER_SECONDS >= 60

    calls = 0
    held: list[_ManagedSession] = []
    held_ready = threading.Event()

    async def fake_open_tunnel(_identity, _relay_url, **_kwargs):
        nonlocal calls
        calls += 1
        session = _ManagedSession()
        if calls == 1:
            session._finish_closed()
        else:
            held.append(session)
            held_ready.set()
        return session

    monkeypatch.setattr(dialer, "open_tunnel", fake_open_tunnel)
    client = _managed_client(
        reconnect_initial_backoff=0.005,
        reconnect_max_backoff=0.04,
        session_stable_after=0.06,
    )
    records, observed = _spy_lifecycle_failures(client, after=2)
    try:
        client.start()
        assert held_ready.wait(timeout=1)
        assert client.request("GET", "/short") == (
            200,
            {"x-session": "fresh"},
            b"ok",
        )
        # This scaled hold is on the short-lived side of the stability threshold.
        client._run(_finish_session_after(held[0], 0.035))
        assert observed.wait(timeout=1)
    finally:
        client.close()

    assert [retry for _reason, _detail, retry in records[:2]] == [0.005, 0.01]


@pytest.mark.asyncio
async def test_relay_enrollment_timeout_reports_relay_attempt(monkeypatch) -> None:
    identity = _identity(endpoints=())
    entered = threading.Event()
    release = threading.Event()
    dial_called = False

    def enroll_device(
        _relay_url: str,
        enrolled_identity: ClientIdentity,
    ) -> EnrolledDevice:
        entered.set()
        release.wait()
        return EnrolledDevice(device_token="token", identity=enrolled_identity)

    async def dial(_relay_url: str, _enrolled: EnrolledDevice) -> object:
        nonlocal dial_called
        dial_called = True
        raise AssertionError("dial should not run after enrollment timeout")

    monkeypatch.setattr(dialer.Client, "enroll_device", staticmethod(enroll_device))
    monkeypatch.setattr(dialer.Client, "dial", staticmethod(dial))

    try:
        with pytest.raises(TlsError) as exc_info:
            await dialer.open_tunnel(
                identity,
                "https://relay.test",
                deadline=time.monotonic() + 0.01,
            )
    finally:
        release.set()

    assert entered.wait(timeout=1)
    message = str(exc_info.value)
    assert "spl-relay" in message
    assert "TimeoutError" in message
    assert dial_called is False


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
async def test_open_tunnel_closes_same_batch_success_sibling(monkeypatch) -> None:
    identity = _identity(endpoints=({"ip": "10.0.0.1", "port": 7657},))
    direct_session = _RaceSession()
    relay_session = _RaceSession()

    async def dial_direct(
        _client: object,
        _endpoint: dict[str, object],
        _identity: ClientIdentity,
        _deadline: float | None = None,
    ) -> _RaceSession:
        return direct_session

    async def dial_relay(
        _client: object,
        _relay_url: str,
        _identity: ClientIdentity,
        _deadline: float | None = None,
    ) -> _RaceSession:
        return relay_session

    monkeypatch.setattr(dialer, "_dial_direct_endpoint", dial_direct)
    monkeypatch.setattr(dialer, "_dial_relay", dial_relay)

    returned = await dialer.open_tunnel(identity, "https://relay.test")

    assert returned in {direct_session, relay_session}
    other = direct_session if returned is relay_session else relay_session
    assert returned.close_calls == 0
    assert other.close_calls == 1


@pytest.mark.asyncio
async def test_open_tunnel_retrieves_same_batch_failure_before_return(
    monkeypatch,
) -> None:
    identity = _identity(endpoints=({"ip": "10.0.0.1", "port": 7657},))
    winner = _RaceSession()
    success_task: asyncio.Task[object] | None = None
    failure_task: asyncio.Task[object] | None = None
    real_wait = asyncio.wait

    async def dial_direct(
        _client: object,
        _endpoint: dict[str, object],
        _identity: ClientIdentity,
        _deadline: float | None = None,
    ) -> _RaceSession:
        nonlocal success_task
        success_task = asyncio.current_task()
        return winner

    async def dial_relay(
        _client: object,
        _relay_url: str,
        _identity: ClientIdentity,
        _deadline: float | None = None,
    ) -> _RaceSession:
        nonlocal failure_task
        failure_task = asyncio.current_task()
        raise RuntimeError("same-batch relay failed")

    async def wait_success_first(
        pending: set[asyncio.Task[object]],
        *,
        return_when: str,
    ) -> tuple[_OrderedDone, set[asyncio.Task[object]]]:
        assert return_when is asyncio.FIRST_COMPLETED
        await real_wait(pending, return_when=asyncio.ALL_COMPLETED)
        assert success_task is not None
        assert failure_task is not None
        return _OrderedDone((success_task, failure_task)), set()

    monkeypatch.setattr(dialer, "_dial_direct_endpoint", dial_direct)
    monkeypatch.setattr(dialer, "_dial_relay", dial_relay)
    monkeypatch.setattr(dialer.asyncio, "wait", wait_success_first)
    loop = asyncio.get_running_loop()
    previous_handler = loop.get_exception_handler()
    contexts: list[dict[str, object]] = []

    def capture_exception(
        _loop: asyncio.AbstractEventLoop,
        context: dict[str, object],
    ) -> None:
        contexts.append(context)

    try:
        loop.set_exception_handler(capture_exception)
        assert await dialer.open_tunnel(identity, "https://relay.test") is winner
        success_task = None
        failure_task = None
        for _index in range(3):
            gc.collect()
            await asyncio.sleep(0)
    finally:
        loop.set_exception_handler(previous_handler)

    assert [
        context
        for context in contexts
        if context.get("message") == "Task exception was never retrieved"
    ] == []


@pytest.mark.asyncio
async def test_open_tunnel_ignores_same_batch_sibling_close_error(monkeypatch) -> None:
    identity = _identity(endpoints=({"ip": "10.0.0.1", "port": 7657},))
    winner = _RaceSession()
    extra = _RaceSession(close_error=RuntimeError("close failed"))
    winner_task: asyncio.Task[object] | None = None
    extra_task: asyncio.Task[object] | None = None
    real_wait = asyncio.wait

    async def dial_direct(
        _client: object,
        _endpoint: dict[str, object],
        _identity: ClientIdentity,
        _deadline: float | None = None,
    ) -> _RaceSession:
        nonlocal winner_task
        winner_task = asyncio.current_task()
        return winner

    async def dial_relay(
        _client: object,
        _relay_url: str,
        _identity: ClientIdentity,
        _deadline: float | None = None,
    ) -> _RaceSession:
        nonlocal extra_task
        extra_task = asyncio.current_task()
        return extra

    async def wait_winner_first(
        pending: set[asyncio.Task[object]],
        *,
        return_when: str,
    ) -> tuple[_OrderedDone, set[asyncio.Task[object]]]:
        assert return_when is asyncio.FIRST_COMPLETED
        await real_wait(pending, return_when=asyncio.ALL_COMPLETED)
        assert winner_task is not None
        assert extra_task is not None
        return _OrderedDone((winner_task, extra_task)), set()

    monkeypatch.setattr(dialer, "_dial_direct_endpoint", dial_direct)
    monkeypatch.setattr(dialer, "_dial_relay", dial_relay)
    monkeypatch.setattr(dialer.asyncio, "wait", wait_winner_first)

    assert await dialer.open_tunnel(identity, "https://relay.test") is winner
    assert winner.close_calls == 0
    assert extra.close_calls == 1


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


@pytest.mark.asyncio
async def test_relay_enrollment_runs_off_event_loop_thread(monkeypatch) -> None:
    identity = _identity(endpoints=())
    loop_thread = threading.get_ident()
    enroll_thread: int | None = None
    enrolled = EnrolledDevice(device_token="token", identity=identity)
    session = object()

    def enroll_device(
        relay_url: str, enrolled_identity: ClientIdentity
    ) -> EnrolledDevice:
        nonlocal enroll_thread
        enroll_thread = threading.get_ident()
        assert relay_url == "https://relay.test"
        assert enrolled_identity is identity
        return enrolled

    async def dial(relay_url: str, dial_enrolled: EnrolledDevice) -> object:
        assert relay_url == "https://relay.test"
        assert dial_enrolled is enrolled
        return session

    monkeypatch.setattr(dialer.Client, "enroll_device", staticmethod(enroll_device))
    monkeypatch.setattr(dialer.Client, "dial", staticmethod(dial))

    assert await dialer.open_tunnel(identity, "https://relay.test/") is session
    assert enroll_thread is not None
    assert enroll_thread != loop_thread


def test_cached_session_drops_on_stream_reset(monkeypatch) -> None:
    class ResetSession(_ManagedSession):
        async def request(self, *_args, **_kwargs):
            raise StreamResetError("reset")

    sessions: list[ResetSession] = []

    async def open_tunnel(_identity, _relay_url, **_kwargs):
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

    async def fake_stream_request_async(method, path, *, headers, body, timeout=None):
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

    async def fake_stream_request_async(_method, _path, *, headers, body, timeout=None):
        _ = (headers, body, timeout)
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

    async def fake_open_tunnel(_identity, _relay_url, **_kwargs):
        session = _ManagedSession()
        sessions.append(session)
        return session

    async def wait_closed(session: _ManagedSession) -> None:
        await session.closed

    monkeypatch.setattr(dialer, "open_tunnel", fake_open_tunnel)
    client = _managed_client()
    try:
        client.start()
        assert client.request("GET", "/before-redial") == (
            200,
            {"x-session": "fresh"},
            b"ok",
        )
        assert len(sessions) == 1
        sessions[0].requests.clear()

        sessions[0].close_remote("session_closed")
        client._run(wait_closed(sessions[0]))
        assert client.request("GET", "/after-redial") == (
            200,
            {"x-session": "fresh"},
            b"ok",
        )
        assert len(sessions) == 2

        status = client.status()
        assert status["state"] == "connected"
        assert status["reconnect_count"] == 1
        assert status["last_failure"] is not None
        assert status["last_failure"]["reason"] == "session_closed"
    finally:
        client.close()

    assert sessions[-1].requests == [("GET", "/after-redial", {}, b"")]


def test_connection_manager_records_liveness_failure(monkeypatch) -> None:
    sessions: list[_ManagedSession] = []

    async def fake_open_tunnel(_identity, _relay_url, **_kwargs):
        session = _ManagedSession()
        sessions.append(session)
        return session

    async def wait_closed(session: _ManagedSession) -> None:
        await session.closed

    monkeypatch.setattr(dialer, "open_tunnel", fake_open_tunnel)
    client = _managed_client()
    try:
        client.start()
        assert client.request("GET", "/before-liveness-redial") == (
            200,
            {"x-session": "fresh"},
            b"ok",
        )
        assert len(sessions) == 1
        sessions[0].requests.clear()

        sessions[0].close_remote("liveness_failed")
        client._run(wait_closed(sessions[0]))
        assert client.request("GET", "/after-liveness-redial") == (
            200,
            {"x-session": "fresh"},
            b"ok",
        )
        assert len(sessions) == 2

        status = client.status()
        assert status["last_failure"] is not None
        assert status["last_failure"]["reason"] == "liveness_failed"
        assert status["health"] == "healthy"
        assert status["state"] == "connected"
        assert status["reconnect_count"] == 1
    finally:
        client.close()


def test_request_during_reconnect_fails_with_lifecycle_error(monkeypatch) -> None:
    """Request wait and stalled dial timeout are bounded independently."""
    started = threading.Event()

    async def fake_open_tunnel(_identity, _relay_url, *, deadline=None, **_kwargs):
        started.set()
        if deadline is None:
            raise AssertionError("dial received no deadline")
        await asyncio.wait_for(
            asyncio.sleep(3600),
            timeout=_remaining_deadline(deadline),
        )
        raise AssertionError("unreachable")

    monkeypatch.setattr(dialer, "open_tunnel", fake_open_tunnel)
    client = _managed_client(
        dial_timeout=1.0,
        request_session_wait=0.05,
        reconnect_initial_backoff=0.005,
    )
    records, observed = _spy_lifecycle_failures(client, after=1)
    try:
        client.start()
        assert started.wait(timeout=1)
        with pytest.raises(TunnelLifecycleError) as exc_info:
            client.request("GET", "/during-reconnect")
        assert observed.wait(timeout=2)
    finally:
        client.close()

    assert exc_info.value.state == "connecting"
    assert exc_info.value.retryable is True
    assert "no live tunnel session" in exc_info.value.detail
    assert records[0][0] == "TimeoutError"


def test_dead_manager_status_and_requests_fail_closed(monkeypatch) -> None:
    async def fake_open_tunnel(_identity, _relay_url, **_kwargs):
        await asyncio.sleep(3600)
        raise AssertionError("unreachable")

    monkeypatch.setattr(dialer, "open_tunnel", fake_open_tunnel)
    client = _managed_client(request_session_wait=0.02)
    try:
        client.start()

        async def kill_manager() -> None:
            assert client._manager_task is not None
            task = client._manager_task
            task.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await task

        client._run(kill_manager())

        status = client.status()
        assert status["health"] == "unhealthy"
        assert status["state"] == "dead_manager"
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

    async def fake_stream_request_async(method, path, *, headers, body, timeout=None):
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

    async def fake_stream_request_async(_method, _path, *, headers, body, timeout=None):
        _ = (headers, body, timeout)
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

    async def fake_open_tunnel(_identity, _relay_url, **_kwargs):
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

    async def fake_open_tunnel(_identity, _relay_url, **_kwargs):
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

    async def fake_open_tunnel(_identity, _relay_url, **_kwargs):
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
    async def fake_open_tunnel(_identity, _relay_url, **_kwargs):
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


def test_request_timeout_argument_can_loosen_constructor_bound(monkeypatch) -> None:
    class SlowRequestSession(_ManagedSession):
        async def request(self, method, path, *, headers, body):
            self.requests.append((method, path, headers, body))
            assert isinstance(body, bytes)
            assert len(body) >= 1024 * 1024
            await asyncio.sleep(0.1)
            return 200, {"x-timeout": "loosened"}, b"ok"

    async def fake_open_tunnel(_identity, _relay_url, **_kwargs):
        return SlowRequestSession()

    monkeypatch.setattr(dialer, "open_tunnel", fake_open_tunnel)
    body = b"x" * (1024 * 1024)

    client = _managed_client(request_timeout=0.02)
    try:
        assert client.request("POST", "/large", body=body, timeout=2.0) == (
            200,
            {"x-timeout": "loosened"},
            b"ok",
        )
    finally:
        client.close()

    client = _managed_client(request_timeout=0.02)
    try:
        with pytest.raises(TunnelRequestError) as exc_info:
            client.request("POST", "/large", body=body)
    finally:
        client.close()

    assert exc_info.value.reason == "TimeoutError"


def test_request_timeout_argument_can_tighten_large_constructor_bound(
    monkeypatch,
) -> None:
    class HangingManagedSession(_ManagedSession):
        async def request(self, *_args, **_kwargs):
            await asyncio.sleep(3600)

    sessions: list[HangingManagedSession] = []

    async def fake_open_tunnel(_identity, _relay_url, **_kwargs):
        session = HangingManagedSession()
        sessions.append(session)
        return session

    monkeypatch.setattr(dialer, "open_tunnel", fake_open_tunnel)
    client = _managed_client(request_timeout=10.0)
    started = time.monotonic()
    try:
        with pytest.raises(TunnelRequestError) as exc_info:
            client.request("GET", "/hang", timeout=0.05)
    finally:
        client.close()

    assert time.monotonic() - started < 1.0
    assert exc_info.value.reason == "TimeoutError"
    assert sessions[0].close_calls == 1


def test_request_timeout_uses_constructor_when_argument_omitted(monkeypatch) -> None:
    class HangingManagedSession(_ManagedSession):
        async def request(self, *_args, **_kwargs):
            await asyncio.sleep(3600)

    sessions: list[HangingManagedSession] = []

    async def fake_open_tunnel(_identity, _relay_url, **_kwargs):
        session = HangingManagedSession()
        sessions.append(session)
        return session

    monkeypatch.setattr(dialer, "open_tunnel", fake_open_tunnel)
    client = _managed_client(request_timeout=0.05)
    started = time.monotonic()
    try:
        with pytest.raises(TunnelRequestError) as exc_info:
            client.request("GET", "/hang")
    finally:
        client.close()

    assert time.monotonic() - started < 1.0
    assert exc_info.value.reason == "TimeoutError"
    assert sessions[0].close_calls == 1


def test_bare_stream_request_honors_per_request_timeout(monkeypatch) -> None:
    class HangingManagedSession(_ManagedSession):
        async def stream_request(self, *_args, **_kwargs):
            await asyncio.sleep(3600)

    async def fake_open_tunnel(_identity, _relay_url, **_kwargs):
        return HangingManagedSession()

    monkeypatch.setattr(dialer, "open_tunnel", fake_open_tunnel)
    client = _managed_client(request_timeout=10.0)
    started = time.monotonic()
    try:
        with pytest.raises(TimeoutError):
            client.stream_request("GET", "/hang", timeout=0.05)
    finally:
        client.close()

    assert time.monotonic() - started < 1.0


def test_proxy_stream_request_honors_per_request_timeout(monkeypatch) -> None:
    class HangingManagedSession(_ManagedSession):
        async def stream_request(self, *_args, **_kwargs):
            await asyncio.sleep(3600)

    sessions: list[HangingManagedSession] = []

    async def fake_open_tunnel(_identity, _relay_url, **_kwargs):
        session = HangingManagedSession()
        sessions.append(session)
        return session

    monkeypatch.setattr(dialer, "open_tunnel", fake_open_tunnel)
    client = _managed_client(request_timeout=10.0)
    chunks: queue.Queue[TunnelResponseHead | bytes | Exception | None] = queue.Queue()
    started = time.monotonic()
    try:
        future = client.proxy_stream_request(
            "GET",
            "/hang",
            chunks=chunks,
            timeout=0.05,
        )
        future.result(timeout=1)
    finally:
        client.close()

    assert time.monotonic() - started < 1.0
    error = chunks.get_nowait()
    assert isinstance(error, TunnelRequestError)
    assert error.reason == "TimeoutError"
    assert chunks.get_nowait() is None
    assert sessions[0].close_calls == 1


def test_stream_request_timeout_does_not_cover_response_tail(monkeypatch) -> None:
    class SlowTailStream:
        async def read(self):
            for chunk in (b"tail-a", b"tail-b", b"tail-c"):
                await asyncio.sleep(0.25)
                yield chunk

    class SlowTailSession(_ManagedSession):
        async def stream_request(self, method, path, *, headers, body):
            self.stream_requests.append((method, path, headers, body))
            return 200, {}, b"", SlowTailStream()

    async def fake_open_tunnel(_identity, _relay_url, **_kwargs):
        return SlowTailSession()

    monkeypatch.setattr(dialer, "open_tunnel", fake_open_tunnel)
    client = _managed_client(request_timeout=10.0)
    chunks: queue.Queue[bytes | Exception | None] = queue.Queue()
    try:
        future = client.stream_request(
            "GET",
            "/slow-tail",
            chunks=chunks,
            timeout=0.4,
        )
        future.result(timeout=3)
    finally:
        client.close()

    assert chunks.get_nowait() == b"tail-a"
    assert chunks.get_nowait() == b"tail-b"
    assert chunks.get_nowait() == b"tail-c"
    assert chunks.get_nowait() is None


def test_proxy_stream_request_times_out_during_head_and_clears_session() -> None:
    class HangingManagedSession(_ManagedSession):
        async def stream_request(self, *_args, **_kwargs):
            await asyncio.sleep(3600)

    sessions: list[HangingManagedSession] = []

    async def fake_open_tunnel(_identity, _relay_url, **_kwargs):
        session = HangingManagedSession()
        sessions.append(session)
        return session

    monkeypatch = pytest.MonkeyPatch()
    monkeypatch.setattr(dialer, "open_tunnel", fake_open_tunnel)
    client = _managed_client(request_timeout=0.05)
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

    async def fake_open_tunnel(_identity, _relay_url, **_kwargs):
        return HangingManagedSession()

    monkeypatch.setattr(dialer, "open_tunnel", fake_open_tunnel)
    client = _managed_client(request_timeout=0.05)
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

    async def fake_open_tunnel(_identity, _relay_url, **_kwargs):
        session = HangingManagedSession()
        sessions.append(session)
        return session

    monkeypatch.setattr(dialer, "open_tunnel", fake_open_tunnel)
    client = _managed_client(request_timeout=0.05)
    try:
        with pytest.raises(TunnelRequestError) as exc_info:
            client.request("GET", "/hang")
    finally:
        client.close()

    assert exc_info.value.reason == "TimeoutError"
    assert sessions[0].close_calls == 1
    assert client._session is None


def test_request_timeout_is_not_armed_during_body_streaming() -> None:
    class SlowBodySession(_ManagedSession):
        async def stream_request(self, method, path, *, headers, body):
            self.stream_requests.append((method, path, headers, body))
            return 200, {}, b"initial", _SlowBodyStream((b"late-a", b"late-b"))

    sessions: list[SlowBodySession] = []

    async def fake_open_tunnel(_identity, _relay_url, **_kwargs):
        session = SlowBodySession()
        sessions.append(session)
        return session

    monkeypatch = pytest.MonkeyPatch()
    monkeypatch.setattr(dialer, "open_tunnel", fake_open_tunnel)
    client = _managed_client(request_timeout=0.01)
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

    async def fake_open_tunnel(_identity, _relay_url, **_kwargs):
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
