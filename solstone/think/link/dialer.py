# SPDX-License-Identifier: AGPL-3.0-only
# Copyright (c) 2026 sol pbc

from __future__ import annotations

import asyncio
import contextlib
import inspect
import queue
import threading
import time
from collections.abc import Awaitable, Callable
from concurrent.futures import Future
from typing import Any, NamedTuple, Self

from solstone.think.link.bundle import endpoint_label
from solstone.think.link.client import (
    BodySource,
    Client,
    ClientIdentity,
    EnrolledDevice,
    StreamResetError,
    TunnelSession,
)
from solstone.think.link.tls import TlsError

_ESTABLISH_TIMEOUT_SECONDS = 30
_QUEUE_PUT_TIMEOUT_SECONDS = 0.1
_REQUEST_SESSION_WAIT_SECONDS = 5.0
_RECONNECT_INITIAL_BACKOFF_SECONDS = 1.0
_RECONNECT_MAX_BACKOFF_SECONDS = 30.0
_SESSION_POLL_SECONDS = 0.25

STATE_DISCONNECTED = "disconnected"
STATE_CONNECTING = "connecting"
STATE_CONNECTED = "connected"
STATE_DEGRADED = "degraded"
STATE_DEAD_MANAGER = "dead_manager"
STATE_CLOSED = "closed"


async def _put_queue_item(chunks: queue.Queue[Any], item: Any) -> None:
    """Put without blocking the tunnel event loop when a bounded queue is full."""

    try:
        chunks.put_nowait(item)
        return
    except queue.Full:
        pass

    while True:
        try:
            await asyncio.to_thread(
                chunks.put,
                item,
                True,
                _QUEUE_PUT_TIMEOUT_SECONDS,
            )
            return
        except queue.Full:
            await asyncio.sleep(0)


class TunnelResponseHead(NamedTuple):
    status: int
    headers: dict[str, str]


class TunnelRequestError(ConnectionError):
    def __init__(self, reason: str, detail: str) -> None:
        super().__init__(f"{reason}: {detail}" if detail else reason)
        self.reason = reason
        self.detail = detail


class TunnelLifecycleError(TunnelRequestError):
    def __init__(self, state: str, detail: str, *, retryable: bool = True) -> None:
        super().__init__("lifecycle", detail)
        self.state = state
        self.retryable = retryable

    def as_dict(self) -> dict[str, object]:
        return {
            "error": "link_lifecycle",
            "retryable": self.retryable,
            "state": self.state,
            "detail": self.detail,
        }


class TunnelLifecycleFailure(NamedTuple):
    reason: str
    detail: str
    at: float


async def _wait_session_closed(session: Any, *, is_closed: Callable[[], bool]) -> None:
    closed = getattr(session, "closed", None)
    if inspect.isawaitable(closed):
        await closed
        return
    while not is_closed() and getattr(session, "is_alive", False):
        await asyncio.sleep(_SESSION_POLL_SECONDS)


async def _dial_direct_endpoint(
    client: Client,
    endpoint: dict[str, object],
    identity: ClientIdentity,
    deadline: float | None = None,
) -> TunnelSession:
    host = str(endpoint.get("ip") or endpoint.get("host") or "").strip()
    if not host:
        raise TlsError("LAN endpoint missing ip")
    port_value = endpoint.get("port") or 7657
    try:
        port = int(port_value)
    except (TypeError, ValueError) as exc:
        raise TlsError(f"LAN endpoint has invalid port: {port_value!r}") from exc
    enrolled = EnrolledDevice(device_token="", identity=identity)
    return await _with_deadline(
        client.dial_direct(host, enrolled, port=port),
        deadline,
    )


async def _dial_relay(
    client: Client,
    relay_url: str,
    identity: ClientIdentity,
    deadline: float | None = None,
) -> TunnelSession:
    enrolled = client.enroll_device(relay_url, identity)
    return await _with_deadline(client.dial(relay_url, enrolled), deadline)


async def _with_deadline(coro: Awaitable[Any], deadline: float | None) -> Any:
    if deadline is None:
        return await coro
    timeout = max(0.0, deadline - time.monotonic())
    return await asyncio.wait_for(coro, timeout=timeout)


async def open_tunnel(
    identity: ClientIdentity,
    relay_url: str | None,
    *,
    deadline: float | None = None,
) -> TunnelSession:
    client = Client()
    attempts: list[tuple[str, Any]] = []
    for endpoint in identity.local_endpoints:
        label = endpoint_label(endpoint)
        attempts.append(
            (label, _dial_direct_endpoint(client, endpoint, identity, deadline))
        )
    if relay_url:
        attempts.append(
            (
                "spl-relay",
                _dial_relay(client, relay_url.rstrip("/"), identity, deadline),
            )
        )
    if not attempts:
        raise TlsError("no PL dial attempts configured")

    tasks = {asyncio.create_task(coro): label for label, coro in attempts}
    pending = set(tasks)
    failures: dict[str, BaseException] = {}

    while pending:
        done, pending = await asyncio.wait(
            pending,
            return_when=asyncio.FIRST_COMPLETED,
        )
        for task in done:
            label = tasks[task]
            try:
                session = task.result()
            except BaseException as exc:
                failures[label] = exc
                continue
            for loser in pending:
                loser.cancel()
            if pending:
                await asyncio.gather(*pending, return_exceptions=True)
            return session

    detail = "; ".join(
        f"{label}: {type(exc).__name__}: {exc}" for label, exc in failures.items()
    )
    raise TlsError(f"all PL dial attempts failed: {detail}")


class TunnelClient:
    def __init__(
        self,
        identity: ClientIdentity,
        relay_url: str | None,
        *,
        establish_timeout: float = _ESTABLISH_TIMEOUT_SECONDS,
        request_session_wait: float = _REQUEST_SESSION_WAIT_SECONDS,
        reconnect_initial_backoff: float = _RECONNECT_INITIAL_BACKOFF_SECONDS,
        reconnect_max_backoff: float = _RECONNECT_MAX_BACKOFF_SECONDS,
    ) -> None:
        self._identity = identity
        self._relay_url = relay_url.rstrip("/") if relay_url else None
        self._establish_timeout = establish_timeout
        self._request_session_wait = request_session_wait
        self._reconnect_initial_backoff = reconnect_initial_backoff
        self._reconnect_max_backoff = reconnect_max_backoff
        self._loop: asyncio.AbstractEventLoop | None = None
        self._loop_thread: threading.Thread | None = None
        self._session: TunnelSession | None = None
        self._state_condition: asyncio.Condition | None = None
        self._manager_task: asyncio.Task[None] | None = None
        self._state = STATE_DISCONNECTED
        self._connected_at: float | None = None
        self._last_connected_at: float | None = None
        self._last_failure: TunnelLifecycleFailure | None = None
        self._next_retry_at: float | None = None
        self._reconnect_count = 0
        self._active_requests = 0
        self._closed = False

    def start(self) -> None:
        self._run(self._ensure_manager_async())

    def _ensure_loop(self) -> asyncio.AbstractEventLoop:
        if self._closed:
            raise TunnelRequestError("closed", "tunnel client is closed")
        if self._loop is not None and self._loop.is_running():
            return self._loop

        loop = asyncio.new_event_loop()
        ready = threading.Event()

        def run_loop() -> None:
            asyncio.set_event_loop(loop)
            self._state_condition = asyncio.Condition()
            ready.set()
            loop.run_forever()

        thread = threading.Thread(
            target=run_loop,
            name=f"link-tunnel-{self._identity.home_instance_id}",
            daemon=True,
        )
        thread.start()
        ready.wait()
        self._loop = loop
        self._loop_thread = thread
        return loop

    def _run(self, coro: Awaitable[Any]) -> Any:
        loop = self._ensure_loop()
        future = asyncio.run_coroutine_threadsafe(coro, loop)
        return future.result()

    async def _ensure_manager_async(self) -> None:
        if self._state_condition is None:
            self._state_condition = asyncio.Condition()
        if self._manager_task is not None and not self._manager_task.done():
            return
        if self._manager_task is not None and self._manager_task.done():
            if not self._closed:
                self._state = STATE_DEAD_MANAGER
                if self._state_condition is not None:
                    async with self._state_condition:
                        self._state_condition.notify_all()
            return
        self._manager_task = asyncio.create_task(
            self._connection_manager_loop(),
            name=f"link-tunnel-manager-{self._identity.home_instance_id}",
        )

    async def _connection_manager_loop(self) -> None:
        backoff = self._reconnect_initial_backoff
        while not self._closed:
            await self._set_lifecycle_state(STATE_CONNECTING)
            try:
                session = await open_tunnel(self._identity, self._relay_url)
            except asyncio.CancelledError:
                raise
            except Exception as exc:
                await self._record_lifecycle_failure(
                    type(exc).__name__,
                    str(exc),
                    state=STATE_DEGRADED,
                    next_retry_in=backoff,
                )
                await asyncio.sleep(backoff)
                backoff = min(self._reconnect_max_backoff, backoff * 2)
                continue

            backoff = self._reconnect_initial_backoff
            await self._adopt_session(session)
            await _wait_session_closed(session, is_closed=lambda: self._closed)
            if self._closed:
                return
            if self._session is session:
                failure_reason = getattr(session, "failure_reason", None)
                if callable(failure_reason):
                    failure_reason = failure_reason()
                reason = str(failure_reason or "session_closed")
                detail = "PL session closed"
                await self._clear_session_with_failure(
                    session,
                    reason,
                    detail,
                    state=STATE_DEGRADED,
                    next_retry_in=backoff,
                )
            await asyncio.sleep(backoff)
            backoff = min(self._reconnect_max_backoff, backoff * 2)

    async def _set_lifecycle_state(self, state: str) -> None:
        self._state = state
        if state != STATE_CONNECTED:
            self._connected_at = None
        if state == STATE_CONNECTING:
            self._next_retry_at = None
        if self._state_condition is not None:
            async with self._state_condition:
                self._state_condition.notify_all()

    async def _adopt_session(self, session: TunnelSession) -> None:
        self._session = session
        now = time.time()
        self._state = STATE_CONNECTED
        self._connected_at = now
        self._last_connected_at = now
        self._next_retry_at = None
        if self._state_condition is not None:
            async with self._state_condition:
                self._state_condition.notify_all()

    async def _record_lifecycle_failure(
        self,
        reason: str,
        detail: str,
        *,
        state: str,
        next_retry_in: float | None = None,
    ) -> None:
        self._session = None
        self._state = state
        self._connected_at = None
        now = time.time()
        self._last_failure = TunnelLifecycleFailure(reason, detail, now)
        self._reconnect_count += 1
        self._next_retry_at = now + next_retry_in if next_retry_in is not None else None
        if self._state_condition is not None:
            async with self._state_condition:
                self._state_condition.notify_all()

    async def _clear_session_with_failure(
        self,
        session: TunnelSession | None,
        reason: str,
        detail: str,
        *,
        state: str = STATE_DEGRADED,
        next_retry_in: float | None = None,
    ) -> None:
        if session is not None and self._session is session:
            self._session = None
        await self._record_lifecycle_failure(
            reason,
            detail,
            state=state,
            next_retry_in=next_retry_in,
        )
        if session is not None:
            with contextlib.suppress(Exception):
                await session.close()

    async def _get_session_async(self) -> TunnelSession:
        await self._ensure_manager_async()
        deadline = time.monotonic() + self._request_session_wait
        while True:
            self._raise_if_manager_dead()
            cached = self._session
            if cached is not None and cached.is_alive:
                return cached
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                raise self._lifecycle_error("no live tunnel session available")
            condition = self._state_condition
            if condition is None:
                raise self._lifecycle_error("tunnel manager is not initialized")
            try:
                async with asyncio.timeout(remaining):
                    async with condition:
                        await condition.wait()
            except TimeoutError as exc:
                raise self._lifecycle_error("no live tunnel session available") from exc

    def _raise_if_manager_dead(self) -> None:
        task = self._manager_task
        if task is not None and task.done() and not self._closed:
            self._state = STATE_DEAD_MANAGER
            raise self._lifecycle_error("tunnel connection manager is not running")

    def _lifecycle_error(self, detail: str) -> TunnelLifecycleError:
        return TunnelLifecycleError(self._effective_state(), detail, retryable=True)

    def _effective_state(self) -> str:
        if self._closed:
            return STATE_CLOSED
        task = self._manager_task
        if task is not None and task.done():
            return STATE_DEAD_MANAGER
        return self._state

    def _get_session(self) -> TunnelSession:
        return self._run(self._get_session_async())

    async def _begin_active_request(self) -> None:
        self._active_requests += 1
        if self._state_condition is not None:
            async with self._state_condition:
                self._state_condition.notify_all()

    async def _end_active_request(self) -> None:
        self._active_requests = max(0, self._active_requests - 1)
        if self._state_condition is not None:
            async with self._state_condition:
                self._state_condition.notify_all()

    async def _close_session_async(self) -> None:
        session = self._session
        self._session = None
        if self._state != STATE_CLOSED:
            self._state = STATE_DISCONNECTED
            self._connected_at = None
        if session is not None:
            await session.close()
        if self._state_condition is not None:
            async with self._state_condition:
                self._state_condition.notify_all()

    def _close_session(self) -> None:
        if self._loop is None or not self._loop.is_running():
            self._session = None
            return
        self._run(self._close_session_async())

    def request(
        self,
        method: str,
        path: str,
        *,
        headers: dict[str, str] | None = None,
        body: bytes | BodySource = b"",
    ) -> tuple[int, dict[str, str], bytes]:
        try:
            return self._run(
                self._request_async(
                    method,
                    path,
                    headers=headers or {},
                    body=body,
                )
            )
        except TunnelLifecycleError:
            raise
        except (ConnectionError, OSError, StreamResetError, TlsError) as exc:
            self._close_session()
            raise TunnelRequestError(type(exc).__name__, str(exc)) from exc

    async def _request_async(
        self,
        method: str,
        path: str,
        *,
        headers: dict[str, str],
        body: bytes | BodySource,
    ) -> tuple[int, dict[str, str], bytes]:
        await self._begin_active_request()
        try:
            session = await self._get_session_async()
            async with asyncio.timeout(self._establish_timeout):
                return await session.request(method, path, headers=headers, body=body)
        finally:
            await self._end_active_request()

    def proxy_stream_request(
        self,
        method: str,
        path: str,
        *,
        headers: dict[str, str] | None = None,
        body: bytes | BodySource = b"",
        chunks: queue.Queue[TunnelResponseHead | bytes | Exception | None],
    ) -> Future[None]:
        """Stream a proxy response to a queue.

        Queue items are one TunnelResponseHead, then zero or more bytes chunks.
        An Exception may appear before the head for gateway failure or after it
        for mid-stream truncation. None terminates the stream.
        """
        loop = self._ensure_loop()
        return asyncio.run_coroutine_threadsafe(
            self._proxy_to_queue(
                method,
                path,
                headers=headers or {},
                body=body,
                chunks=chunks,
            ),
            loop,
        )

    def stream_request(
        self,
        method: str,
        path: str,
        *,
        headers: dict[str, str] | None = None,
        body: bytes | BodySource = b"",
        chunks: queue.Queue[bytes | Exception | None] | None = None,
    ) -> Future[None] | tuple[int, dict[str, str], bytes, Any]:
        if chunks is None:
            return self._run(
                self._stream_request_async(
                    method,
                    path,
                    headers=headers or {},
                    body=body,
                )
            )
        loop = self._ensure_loop()
        return asyncio.run_coroutine_threadsafe(
            self._stream_to_queue(
                method,
                path,
                headers=headers or {},
                body=body,
                chunks=chunks,
            ),
            loop,
        )

    async def _stream_request_async(
        self,
        method: str,
        path: str,
        *,
        headers: dict[str, str],
        body: bytes | BodySource,
    ) -> tuple[int, dict[str, str], bytes, Any]:
        session = await self._get_session_async()
        async with asyncio.timeout(self._establish_timeout):
            return await session.stream_request(
                method, path, headers=headers, body=body
            )

    async def _proxy_to_queue(
        self,
        method: str,
        path: str,
        *,
        headers: dict[str, str],
        body: bytes | BodySource,
        chunks: queue.Queue[TunnelResponseHead | bytes | Exception | None],
    ) -> None:
        stream: Any | None = None
        cancelled = False
        await self._begin_active_request()
        try:
            (
                status,
                resp_headers,
                initial_body,
                stream,
            ) = await self._stream_request_async(
                method,
                path,
                headers=headers,
                body=body,
            )
            await _put_queue_item(
                chunks, TunnelResponseHead(status, dict(resp_headers))
            )
            if initial_body:
                await _put_queue_item(chunks, initial_body)
            async for chunk in stream.read():
                await _put_queue_item(chunks, chunk)
        except asyncio.CancelledError:
            cancelled = True
            if stream is not None and hasattr(stream, "cancel"):
                with contextlib.suppress(Exception):
                    await stream.cancel()
            raise
        except TunnelLifecycleError as exc:
            await _put_queue_item(chunks, exc)
        except (ConnectionError, OSError, StreamResetError, TlsError) as exc:
            await self._close_session_async()
            await _put_queue_item(
                chunks, TunnelRequestError(type(exc).__name__, str(exc))
            )
        except Exception as exc:
            await _put_queue_item(chunks, exc)
        finally:
            await self._end_active_request()
            if not cancelled:
                await _put_queue_item(chunks, None)

    async def _stream_to_queue(
        self,
        method: str,
        path: str,
        *,
        headers: dict[str, str],
        body: bytes | BodySource,
        chunks: queue.Queue[bytes | Exception | None],
    ) -> None:
        stream: Any | None = None
        cancelled = False
        await self._begin_active_request()
        try:
            status, _headers, initial_body, stream = await self._stream_request_async(
                method,
                path,
                headers=headers,
                body=body,
            )
            if status == 200:
                if initial_body:
                    await _put_queue_item(chunks, initial_body)
                async for chunk in stream.read():
                    await _put_queue_item(chunks, chunk)
                return
            if status in {401, 403}:
                await _put_queue_item(
                    chunks,
                    PermissionError(f"stream request rejected ({status})"),
                )
                return
            await _put_queue_item(
                chunks, RuntimeError(f"stream request failed ({status})")
            )
        except asyncio.CancelledError:
            cancelled = True
            if stream is not None and hasattr(stream, "cancel"):
                with contextlib.suppress(Exception):
                    await stream.cancel()
            raise
        except TunnelLifecycleError as exc:
            await _put_queue_item(chunks, exc)
        except (ConnectionError, OSError, StreamResetError, TlsError) as exc:
            await self._close_session_async()
            await _put_queue_item(
                chunks, TunnelRequestError(type(exc).__name__, str(exc))
            )
        except Exception as exc:
            await _put_queue_item(chunks, exc)
        finally:
            await self._end_active_request()
            if not cancelled:
                await _put_queue_item(chunks, None)

    async def _status_async(self) -> dict[str, object]:
        return self._status_snapshot()

    def status(self) -> dict[str, object]:
        if self._loop is None or not self._loop.is_running():
            return self._status_snapshot()
        try:
            future = asyncio.run_coroutine_threadsafe(self._status_async(), self._loop)
            return future.result(timeout=1.0)
        except Exception as exc:
            failure = TunnelLifecycleFailure(type(exc).__name__, str(exc), time.time())
            return self._status_snapshot(
                override_state=STATE_DEAD_MANAGER,
                override_failure=failure,
            )

    def _status_snapshot(
        self,
        *,
        override_state: str | None = None,
        override_failure: TunnelLifecycleFailure | None = None,
    ) -> dict[str, object]:
        now = time.time()
        state = override_state or self._effective_state()
        session = self._session
        manager_alive = (
            self._manager_task is not None
            and not self._manager_task.done()
            and self._loop is not None
            and self._loop.is_running()
            and not self._closed
        )
        healthy = (
            state == STATE_CONNECTED
            and manager_alive
            and session is not None
            and session.is_alive
        )
        connected_age = None
        if healthy and self._connected_at is not None:
            connected_age = max(0.0, now - self._connected_at)
        failure = override_failure or self._last_failure
        return {
            "health": "healthy" if healthy else "unhealthy",
            "state": state,
            "manager_alive": manager_alive,
            "connected_age_seconds": connected_age,
            "last_connected_at": self._last_connected_at,
            "last_failure": (
                None
                if failure is None
                else {
                    "reason": failure.reason,
                    "detail": failure.detail,
                    "at": failure.at,
                }
            ),
            "next_retry_at": self._next_retry_at,
            "reconnect_count": self._reconnect_count,
            "active_requests": self._active_requests,
        }

    def close(self) -> None:
        if self._closed:
            return
        loop = self._loop
        self._closed = True
        if loop is not None and loop.is_running():
            try:
                future = asyncio.run_coroutine_threadsafe(self._shutdown_async(), loop)
                future.result(timeout=5.0)
            except Exception:
                pass
            loop.call_soon_threadsafe(loop.stop)
            if self._loop_thread is not None and self._loop_thread.is_alive():
                self._loop_thread.join(timeout=5.0)
        self._loop = None
        self._loop_thread = None

    async def _shutdown_async(self) -> None:
        self._state = STATE_CLOSED
        task = self._manager_task
        self._manager_task = None
        if task is not None and not task.done():
            task.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await task
        await self._close_session_async()

    def __enter__(self) -> Self:
        return self

    def __exit__(self, *_exc: object) -> None:
        self.close()
