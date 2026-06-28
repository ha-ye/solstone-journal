# SPDX-License-Identifier: AGPL-3.0-only
# Copyright (c) 2026 sol pbc

from __future__ import annotations

import asyncio
import contextlib
import queue
import threading
import time
from collections.abc import Awaitable
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
    ) -> None:
        self._identity = identity
        self._relay_url = relay_url.rstrip("/") if relay_url else None
        self._establish_timeout = establish_timeout
        self._loop: asyncio.AbstractEventLoop | None = None
        self._loop_thread: threading.Thread | None = None
        self._session: TunnelSession | None = None
        self._session_lock: asyncio.Lock | None = None
        self._closed = False

    def _ensure_loop(self) -> asyncio.AbstractEventLoop:
        if self._closed:
            raise TunnelRequestError("closed", "tunnel client is closed")
        if self._loop is not None and self._loop.is_running():
            return self._loop

        loop = asyncio.new_event_loop()
        ready = threading.Event()

        def run_loop() -> None:
            asyncio.set_event_loop(loop)
            self._session_lock = asyncio.Lock()
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

    async def _get_session_async(self) -> TunnelSession:
        if self._session_lock is None:
            self._session_lock = asyncio.Lock()
        async with self._session_lock:
            cached = self._session
            if cached is not None and cached.is_alive:
                return cached
            self._session = None
            if cached is not None:
                await cached.close()
            self._session = await open_tunnel(self._identity, self._relay_url)
            return self._session

    def _get_session(self) -> TunnelSession:
        return self._run(self._get_session_async())

    async def _close_session_async(self) -> None:
        session = self._session
        self._session = None
        if session is not None:
            await session.close()

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
        session = await self._get_session_async()
        async with asyncio.timeout(self._establish_timeout):
            return await session.request(method, path, headers=headers, body=body)

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
        except (ConnectionError, OSError, StreamResetError, TlsError) as exc:
            await self._close_session_async()
            await _put_queue_item(
                chunks, TunnelRequestError(type(exc).__name__, str(exc))
            )
        except Exception as exc:
            await _put_queue_item(chunks, exc)
        finally:
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
        except (ConnectionError, OSError, StreamResetError, TlsError) as exc:
            await self._close_session_async()
            await _put_queue_item(
                chunks, TunnelRequestError(type(exc).__name__, str(exc))
            )
        except Exception as exc:
            await _put_queue_item(chunks, exc)
        finally:
            if not cancelled:
                await _put_queue_item(chunks, None)

    def close(self) -> None:
        if self._closed:
            return
        if self._loop is not None and self._loop.is_running():
            try:
                self._run(self._close_session_async())
            except Exception:
                pass
            self._loop.call_soon_threadsafe(self._loop.stop)
            if self._loop_thread is not None and self._loop_thread.is_alive():
                self._loop_thread.join(timeout=5.0)
        self._loop = None
        self._loop_thread = None
        self._closed = True

    def __enter__(self) -> Self:
        return self

    def __exit__(self, *_exc: object) -> None:
        self.close()
