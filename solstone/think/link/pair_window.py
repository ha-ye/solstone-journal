# SPDX-License-Identifier: AGPL-3.0-only
# Copyright (c) 2026 sol pbc

"""Relay-backed pairing window for non-browser link clients."""

from __future__ import annotations

import asyncio
import contextlib
import json
import logging
import threading
from collections.abc import Callable
from typing import Any

import websockets
from websockets.asyncio.client import ClientConnection
from websockets.exceptions import ConnectionClosed, InvalidStatus, InvalidURI

from solstone.think.link.ws_buffer import BufferedWsReader

log = logging.getLogger("link.pair_window")

_PAIR_KEY_HEADER = "Sec-Pair-Key"
_PAIR_WINDOW_TIMEOUT_SECONDS = 300.0
_PAIR_WINDOW_OPEN_TIMEOUT_SECONDS = 8.0
_LINK_DIRECT_HOST = "127.0.0.1"
_LINK_DIRECT_PORT = 7657
_BUF = 65536

RelayWsOpener = Callable[..., Any]


async def _bridge_pairing_tunnel(
    ws_endpoint: str,
    tunnel_id: str,
    *,
    service_token: str,
    rk_hex: str,
    opener: RelayWsOpener | None = None,
) -> None:
    """Bridge one relay pairing tunnel to the local secure listener.

    Pairing tunnels route by RK in the upgrade header, carry no instance query,
    and are limited to a TLS ClientHello for native link clients.
    """
    connect = opener or websockets.connect
    url = ws_endpoint + f"/tunnel/{tunnel_id}"
    headers = {
        "Authorization": f"Bearer {service_token}",
        _PAIR_KEY_HEADER: rk_hex,
    }
    tcp_writer: asyncio.StreamWriter | None = None
    try:
        async with connect(url, additional_headers=headers, max_size=None) as ws:
            reader = BufferedWsReader(ws)
            prefix = await reader.peek(4)
            if prefix[:1] == b"\x16":
                tcp_reader, tcp_writer = await asyncio.open_connection(
                    _LINK_DIRECT_HOST,
                    _LINK_DIRECT_PORT,
                )
                await _write_initial_tcp_bytes(tcp_writer, reader.drain_buffer())
                await _pipe_tunnel(ws, tcp_reader, tcp_writer, tunnel_id)
            else:
                log.info(
                    "pairing tunnel %s closed: unknown first bytes=%s",
                    tunnel_id,
                    prefix.hex(),
                )
    except ConnectionClosed as exc:
        log.info("pairing tunnel %s closed: code=%s", tunnel_id, exc.code)
    except (
        OSError,
        InvalidStatus,
        InvalidURI,
        asyncio.TimeoutError,
        TimeoutError,
    ) as exc:
        log.warning("pairing tunnel %s failed: type=%s", tunnel_id, type(exc).__name__)
    except Exception as exc:  # noqa: BLE001
        log.warning("pairing tunnel %s error: type=%s", tunnel_id, type(exc).__name__)
    finally:
        if tcp_writer is not None:
            tcp_writer.close()
            with contextlib.suppress(OSError, RuntimeError):
                await tcp_writer.wait_closed()


async def hold_pair_window(
    *,
    relay_endpoint: str,
    service_token: str,
    rk: bytes,
    timeout: float = _PAIR_WINDOW_TIMEOUT_SECONDS,
    opener: RelayWsOpener | None = None,
    stop: asyncio.Event | None = None,
    on_open: Callable[[bool], None] | None = None,
) -> None:
    """Open a temporary relay pair window and bridge its TLS tunnels."""
    connect = opener or websockets.connect
    rk_hex = rk.hex()
    ws_endpoint = _to_ws(relay_endpoint.rstrip("/"))
    headers = {
        "Authorization": f"Bearer {service_token}",
        _PAIR_KEY_HEADER: rk_hex,
    }
    tunnels: dict[str, asyncio.Task[None]] = {}
    log.info("opening pair-window")
    opened = False
    try:
        async with connect(
            ws_endpoint + "/session/pair-window",
            additional_headers=headers,
            max_size=None,
        ) as ws:
            log.info("pair-window open")
            opened = True
            if on_open is not None:
                on_open(True)

            async def _serve() -> None:
                async for message in ws:
                    control = _parse_control(message)
                    tunnel_id = control.get("tunnel_id") if control else None
                    if not (
                        control and control.get("type") == "incoming" and tunnel_id
                    ):
                        continue
                    tunnel_id = str(tunnel_id)
                    log.info("pair-window incoming tunnel_id=%s", tunnel_id)
                    task = asyncio.create_task(
                        _bridge_pairing_tunnel(
                            ws_endpoint,
                            tunnel_id,
                            service_token=service_token,
                            rk_hex=rk_hex,
                            opener=opener,
                        ),
                        name=f"pair-tunnel-{tunnel_id}",
                    )
                    tunnels[tunnel_id] = task
                    task.add_done_callback(
                        lambda _t, tid=tunnel_id: tunnels.pop(tid, None)
                    )

            serve_task = asyncio.create_task(_serve(), name="pair-window-serve")
            waiters: list[asyncio.Task[Any]] = [serve_task]
            if stop is not None:
                waiters.append(
                    asyncio.create_task(stop.wait(), name="pair-window-stop")
                )
            _done, pending = await asyncio.wait(
                waiters,
                timeout=timeout,
                return_when=asyncio.FIRST_COMPLETED,
            )
            for task in pending:
                task.cancel()
            for task in pending:
                with contextlib.suppress(asyncio.CancelledError):
                    await task
            if serve_task.done() and not serve_task.cancelled():
                with contextlib.suppress(ConnectionClosed):
                    serve_task.result()
    except ConnectionClosed as exc:
        log.info("pair-window closed: code=%s", exc.code)
    except Exception as exc:  # noqa: BLE001
        log.warning("pair-window error: type=%s", type(exc).__name__)
    finally:
        if not opened and on_open is not None:
            on_open(False)
        for task in list(tunnels.values()):
            task.cancel()
        if tunnels:
            await asyncio.gather(*tunnels.values(), return_exceptions=True)
        log.info("pair-window closed")


class PairWindowHandle:
    """Handle to the single active convey-hosted pair-window thread."""

    def __init__(
        self,
        *,
        rk_hex: str,
        loop: asyncio.AbstractEventLoop,
        stop: asyncio.Event,
    ) -> None:
        self.rk_hex = rk_hex
        self._loop = loop
        self._stop = stop
        self._thread: threading.Thread | None = None
        self.opened: bool = False
        self._ready = threading.Event()

    def _signal_open(self, opened: bool) -> None:
        self.opened = opened
        self._ready.set()

    def wait_open(self, timeout: float = _PAIR_WINDOW_OPEN_TIMEOUT_SECONDS) -> bool:
        return self._ready.wait(timeout) and self.opened

    def cancel(self) -> None:
        self._loop.call_soon_threadsafe(self._stop.set)

    def join(self, timeout: float | None = None) -> None:
        if self._thread is not None:
            self._thread.join(timeout)

    def is_alive(self) -> bool:
        return self._thread is not None and self._thread.is_alive()


_pair_window_lock = threading.Lock()
_current_pair_window: PairWindowHandle | None = None


def start_pair_window(
    *,
    rk: bytes,
    service_token: str,
    relay_endpoint: str,
    opener: RelayWsOpener | None = None,
    timeout: float = _PAIR_WINDOW_TIMEOUT_SECONDS,
) -> PairWindowHandle:
    """Open and hold one pair-window in a background thread."""
    global _current_pair_window
    loop = asyncio.new_event_loop()
    stop = asyncio.Event()
    handle = PairWindowHandle(rk_hex=rk.hex(), loop=loop, stop=stop)

    def _run() -> None:
        asyncio.set_event_loop(loop)
        try:
            loop.run_until_complete(
                hold_pair_window(
                    relay_endpoint=relay_endpoint,
                    service_token=service_token,
                    rk=rk,
                    timeout=timeout,
                    opener=opener,
                    stop=stop,
                    on_open=handle._signal_open,
                )
            )
        except Exception:  # noqa: BLE001
            log.warning("pair-window thread terminated abnormally")
        finally:
            with contextlib.suppress(Exception):
                loop.close()
            with _pair_window_lock:
                global _current_pair_window
                if _current_pair_window is handle:
                    _current_pair_window = None

    thread = threading.Thread(target=_run, name="link-pair-window", daemon=True)
    handle._thread = thread
    with _pair_window_lock:
        prior = _current_pair_window
        _current_pair_window = handle
    if prior is not None:
        prior.cancel()
    thread.start()
    return handle


def cancel_pair_window() -> None:
    """Cancel the current pair-window, if any."""
    global _current_pair_window
    with _pair_window_lock:
        handle = _current_pair_window
        _current_pair_window = None
    if handle is not None:
        handle.cancel()


async def _pipe_tunnel(
    ws: ClientConnection,
    tcp_reader: asyncio.StreamReader,
    tcp_writer: asyncio.StreamWriter,
    tunnel_id: str,
) -> None:
    async def ws_to_tcp() -> None:
        async for frame in ws:
            tcp_writer.write(frame if isinstance(frame, bytes) else frame.encode())
            await tcp_writer.drain()
        with contextlib.suppress(OSError, RuntimeError):
            tcp_writer.write_eof()

    async def tcp_to_ws() -> None:
        while data := await tcp_reader.read(_BUF):
            await ws.send(data)

    tasks = [
        asyncio.create_task(ws_to_tcp()),
        asyncio.create_task(tcp_to_ws()),
    ]
    done, pending = await asyncio.wait(tasks, return_when=asyncio.FIRST_COMPLETED)
    for task in pending:
        task.cancel()
    if pending:
        await asyncio.gather(*pending, return_exceptions=True)
    for task in done:
        task.result()


async def _write_initial_tcp_bytes(
    tcp_writer: asyncio.StreamWriter,
    initial: bytes,
) -> None:
    if initial:
        tcp_writer.write(initial)
        await tcp_writer.drain()


def _to_ws(endpoint: str) -> str:
    if endpoint.startswith("http://"):
        return "ws://" + endpoint[len("http://") :]
    if endpoint.startswith("https://"):
        return "wss://" + endpoint[len("https://") :]
    return endpoint


def _parse_control(message: str | bytes) -> dict[str, Any] | None:
    try:
        text = message.decode() if isinstance(message, bytes) else message
        parsed = json.loads(text)
    except (UnicodeDecodeError, json.JSONDecodeError):
        return None
    return parsed if isinstance(parsed, dict) else None
