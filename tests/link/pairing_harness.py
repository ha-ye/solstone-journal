# SPDX-License-Identifier: AGPL-3.0-only
# Copyright (c) 2026 sol pbc

from __future__ import annotations

import asyncio
import contextlib
import queue
import threading
import time
from collections.abc import Awaitable, Callable, Iterator
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import pytest
from OpenSSL import SSL

from solstone.apps.link.routes import _build_pair_link
from solstone.convey.secure_listener.identity import ConveyIdentity
from solstone.convey.secure_listener.mux import Multiplexer, StreamWriter
from solstone.convey.secure_listener.tls import (
    build_relaxed_server_context,
    drive_tls,
    issue_server_cert,
    new_server,
)
from solstone.convey.secure_listener.wsgi import dispatch_stream
from solstone.think.link.auth import AuthorizedClients
from solstone.think.link.ca import LoadedCa, load_or_generate_ca
from solstone.think.link.nonces import NonceStore
from solstone.think.link.paths import authorized_clients_path, ca_dir, nonces_path
from tests.link.certless_helpers import make_convey_app

PairingStreamHandler = Callable[
    [asyncio.StreamReader, StreamWriter],
    Awaitable[None],
]


@dataclass
class PairingHarness:
    app: Any
    journal: Path
    ca: LoadedCa
    relaxed_ctx: SSL.Context
    handle_stream: PairingStreamHandler | None = None
    host: str = "127.0.0.1"
    port: int = 0
    _executor: ThreadPoolExecutor = field(
        default_factory=lambda: ThreadPoolExecutor(max_workers=2),
    )
    _ready: queue.Queue[BaseException | None] = field(default_factory=queue.Queue)
    _loop: asyncio.AbstractEventLoop | None = None
    _thread: threading.Thread | None = None
    _server: asyncio.AbstractServer | None = None
    _connection_tasks: set[asyncio.Task[None]] = field(default_factory=set)
    _writers: set[asyncio.StreamWriter] = field(default_factory=set)
    _closing: bool = False

    def start(self) -> None:
        self._thread = threading.Thread(
            target=self._run_loop,
            name="pairing-harness-loop",
            daemon=True,
        )
        self._thread.start()
        ready = self._ready.get(timeout=5)
        if ready is not None:
            raise ready

    def seed_nonce(self, nonce: str, label: str, *, role: str = "") -> None:
        NonceStore(nonces_path()).add(nonce, label, role=role)

    def pair_link(self, nonce: str, *, ca_fp: str | None = None) -> str:
        # Default to the harness's real CA fingerprint so the joiner's pin check
        # passes; tests exercising a mismatch pass an explicit (wrong) ca_fp.
        return _build_pair_link(
            self.host,
            self.port,
            nonce,
            ca_fp or self.ca.fingerprint_sha256(),
        )

    def pair_url(self, nonce: str) -> str:
        return f"https://{self.host}:{self.port}/app/link/pair?token={nonce}"

    def wait_until_idle(self, timeout: float = 1.0) -> bool:
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            loop = self._loop
            if loop is None:
                return True
            future = asyncio.run_coroutine_threadsafe(self._idle(), loop)
            if future.result(timeout=timeout):
                return True
            time.sleep(0.01)
        return False

    def close(self) -> None:
        if self._closing:
            return
        self._closing = True
        loop = self._loop
        if loop is not None:
            future = asyncio.run_coroutine_threadsafe(self._close_async(), loop)
            future.result(timeout=5)
            loop.call_soon_threadsafe(loop.stop)
        if self._thread is not None:
            self._thread.join(timeout=5)
        self._executor.shutdown(wait=True, cancel_futures=True)

    def _run_loop(self) -> None:
        loop = asyncio.new_event_loop()
        self._loop = loop
        asyncio.set_event_loop(loop)
        try:
            loop.run_until_complete(self._start_server())
            self._ready.put(None)
            loop.run_forever()
        except BaseException as exc:
            self._ready.put(exc)
        finally:
            with contextlib.suppress(Exception):
                loop.run_until_complete(loop.shutdown_asyncgens())
            loop.close()

    async def _start_server(self) -> None:
        self._server = await asyncio.start_server(
            self._handle_connection,
            self.host,
            0,
        )
        sock = self._server.sockets[0]
        self.host, self.port = sock.getsockname()[:2]

    async def _handle_connection(
        self,
        reader: asyncio.StreamReader,
        writer: asyncio.StreamWriter,
    ) -> None:
        task = asyncio.current_task()
        if task is not None:
            self._connection_tasks.add(task)
        self._writers.add(writer)
        try:
            await self._pump_connection(reader, writer)
        finally:
            self._writers.discard(writer)
            if task is not None:
                self._connection_tasks.discard(task)

    async def _pump_connection(
        self,
        tcp_reader: asyncio.StreamReader,
        tcp_writer: asyncio.StreamWriter,
    ) -> None:
        tls = new_server(self.relaxed_ctx)
        send_queue: asyncio.Queue[bytes] = asyncio.Queue()
        loop = asyncio.get_running_loop()
        identity = ConveyIdentity(
            mode="pl-via-spl",
            fingerprint=None,
            device_label=None,
            paired_at=None,
            session_id=f"pairing-harness-{id(tcp_writer)}",
        )

        async def write_ciphertext(data: bytes) -> None:
            if not data:
                return
            tcp_writer.write(data)
            await tcp_writer.drain()

        async def send_frame(frame: bytes) -> None:
            send_queue.put_nowait(frame)

        async def handle_stream(
            reader: asyncio.StreamReader,
            writer: StreamWriter,
        ) -> None:
            if self.handle_stream is not None:
                await self.handle_stream(reader, writer)
                return
            await dispatch_stream(
                self.app,
                identity,
                reader,
                writer,
                loop,
                self._executor,
            )

        mux = Multiplexer(send_frame, handle_stream, is_listener=True)

        async def reader_loop() -> None:
            while True:
                inbound = await tcp_reader.read(65536)
                if not inbound:
                    return
                outbound, plaintext = drive_tls(tls, inbound=inbound)
                await write_ciphertext(outbound)
                if plaintext:
                    await mux.feed(plaintext)
                await _drain_send_queue(tls, write_ciphertext, send_queue)

        async def writer_loop() -> None:
            while True:
                frame = await send_queue.get()
                await write_ciphertext(_encrypt(tls, frame))

        reader_task = asyncio.create_task(reader_loop())
        writer_task = asyncio.create_task(writer_loop())
        try:
            await reader_task
        finally:
            writer_task.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await writer_task
            await mux.close()
            tcp_writer.close()
            with contextlib.suppress(Exception):
                await tcp_writer.wait_closed()

    async def _idle(self) -> bool:
        return not self._connection_tasks

    async def _close_async(self) -> None:
        if self._server is not None:
            self._server.close()
            await self._server.wait_closed()
            self._server = None
        for writer in list(self._writers):
            writer.close()
            with contextlib.suppress(Exception):
                await writer.wait_closed()
        for task in list(self._connection_tasks):
            task.cancel()
        if self._connection_tasks:
            await asyncio.gather(*self._connection_tasks, return_exceptions=True)


@contextlib.contextmanager
def pairing_harness(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    *,
    handle_stream: PairingStreamHandler | None = None,
) -> Iterator[PairingHarness]:
    app, journal = make_convey_app(tmp_path, monkeypatch, link={"posture": "spl"})
    ca = load_or_generate_ca(ca_dir())
    server_cert, server_key = issue_server_cert(ca)
    relaxed_ctx = build_relaxed_server_context(
        ca,
        server_cert,
        server_key,
        AuthorizedClients(authorized_clients_path()),
    )
    harness = PairingHarness(
        app=app,
        journal=journal,
        ca=ca,
        relaxed_ctx=relaxed_ctx,
        handle_stream=handle_stream,
    )
    harness.start()
    try:
        yield harness
    finally:
        harness.close()


def _encrypt(tls: Any, plaintext: bytes) -> bytes:
    outbound, _ = drive_tls(tls, inbound=b"", plaintext_out=plaintext)
    return outbound


async def _drain_send_queue(
    tls: Any,
    write_ciphertext: Callable[[bytes], Awaitable[None]],
    send_queue: asyncio.Queue[bytes],
) -> None:
    drained: list[bytes] = []
    while not send_queue.empty():
        drained.append(send_queue.get_nowait())
    for frame in drained:
        await write_ciphertext(_encrypt(tls, frame))
