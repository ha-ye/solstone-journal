# SPDX-License-Identifier: AGPL-3.0-only
# Copyright (c) 2026 sol pbc

"""Listen WS client and raw relay tunnel pipe."""

from __future__ import annotations

import asyncio
import contextlib
import json
import logging
import random
import urllib.parse
import urllib.request
from collections.abc import Callable
from typing import Any

import websockets
from websockets.asyncio.client import ClientConnection
from websockets.exceptions import ConnectionClosed, InvalidStatus, InvalidURI

from solstone.think.spl.health import (
    LINK_HEALTH_EVENT,
    REASON_HOME_MISSING_MOBILE,
    REASON_LOCAL_PRIVATE_LISTENER_UNREACHABLE,
    REASON_RELAY_TUNNEL_REJECTED,
    REASON_RELAY_TUNNEL_UNREACHABLE,
    REASON_SERVICE_TOKEN_REJECTED,
)
from solstone.think.utils import now_ms

log = logging.getLogger("spl.relay_client")

_RECONNECT_MIN = 1.0
_RECONNECT_MAX = 60.0
_HEALTH_REFRESH_SECONDS = 30.0
_LINK_DIRECT_HOST = "127.0.0.1"
_LINK_DIRECT_PORT = 7657
_BUF = 65536

CallosumEmit = Callable[[str, dict[str, Any]], None]


def enroll_home(
    relay_endpoint: str,
    *,
    instance_id: str,
    ca_pubkey: str,
    home_label: str,
    totp_secret: str | None = None,
) -> str:
    """POST /enroll/home and return the service_token.

    Carries totp_secret when provided. The relay is external
    (github.com/solpbc/spl, out of this repo); STATED ASSUMPTION: /enroll/home
    upserts totp_secret for an existing (instance_id, ca_pubkey) enrollment and
    returns the same/refreshed service_token, so an already-enrolled home can
    re-POST to upload a later-generated secret.
    """
    body = {
        "instance_id": instance_id,
        "ca_pubkey": ca_pubkey,
        "home_label": home_label,
    }
    if totp_secret is not None:
        body["totp_secret"] = totp_secret
    result = _post_json_sync(f"{relay_endpoint.rstrip('/')}/enroll/home", body)
    # back-compat: relay still returns "account_token" until lode L2 renames it
    token = result.get("service_token") or result.get("account_token")
    if not isinstance(token, str) or not token:
        raise RuntimeError("relay returned no service_token")
    return token


class RelayClient:
    def __init__(
        self,
        *,
        instance_id: str,
        relay_endpoint: str,
        service_token: str,
        callosum_emit: CallosumEmit | None = None,
    ) -> None:
        self._instance_id = instance_id
        self._relay_endpoint = relay_endpoint.rstrip("/")
        self._relay_ws_endpoint = _to_ws(self._relay_endpoint)
        self._service_token = service_token
        self._emit = callosum_emit or (lambda _event, _fields: None)
        self._running = False
        self._tunnels: dict[str, asyncio.Task[None]] = {}
        self._listen_generation = 0
        self._state = "connecting"
        self._last_successful_tunnel_at: int | None = None
        self._last_tunnel_error: str | None = None
        self._last_tunnel_error_at: int | None = None
        self._last_tunnel_error_status: int | None = None
        self._listen_ws: ClientConnection | None = None

    async def run(self) -> None:
        self._running = True
        delay = _RECONNECT_MIN
        while self._running:
            try:
                await self._run_once()
                delay = _RECONNECT_MIN
            except ConnectionClosed as exc:
                log.warning("listen WS closed: code=%s", exc.code)
            except Exception as exc:  # noqa: BLE001
                log.warning("listen loop error: type=%s", type(exc).__name__)
            if not self._running:
                break
            self._set_state("disconnect", "reconnecting")
            jitter = delay * 0.25
            wait = delay + random.uniform(-jitter, jitter)  # noqa: S311
            log.info("reconnecting in %.1fs", wait)
            await asyncio.sleep(wait)
            delay = min(_RECONNECT_MAX, delay * 2.0)

    async def stop(self) -> None:
        self._running = False
        for task in self._tunnels.values():
            task.cancel()
        if self._tunnels:
            await asyncio.gather(*self._tunnels.values(), return_exceptions=True)
        self._tunnels.clear()

    async def _run_once(self) -> None:
        self._listen_generation += 1
        assert self._service_token is not None
        refresh_task: asyncio.Task[None] | None = None
        self._set_state("connecting", "connecting")
        listen_url = self._url_for("/session/listen", token=self._service_token)
        log.info("opening listen WS")
        try:
            async with websockets.connect(
                listen_url,
                additional_headers={"Authorization": f"Bearer {self._service_token}"},
                max_size=None,
            ) as ws:
                self._listen_ws = ws
                self._set_state("connected", "connected")
                refresh_task = asyncio.create_task(
                    self._refresh_health_loop(),
                    name="spl-relay-health-refresh",
                )
                log.info("listen WS open; waiting for incoming")
                async for message in ws:
                    control = _parse_control(message)
                    tunnel_id = control.get("tunnel_id") if control else None
                    if not (
                        control and control.get("type") == "incoming" and tunnel_id
                    ):
                        continue
                    tunnel_id = str(tunnel_id)
                    log.info("incoming tunnel_id=%s", tunnel_id)
                    self._emit("tunnel_pair", {"tunnel_id": tunnel_id})
                    task = asyncio.create_task(
                        self._handle_tunnel(tunnel_id),
                        name=f"link-tunnel-{tunnel_id}",
                    )
                    self._tunnels[tunnel_id] = task
                    task.add_done_callback(
                        lambda _t, tid=tunnel_id: self._tunnels.pop(tid, None)
                    )
        finally:
            if refresh_task is not None:
                refresh_task.cancel()
                with contextlib.suppress(asyncio.CancelledError):
                    await refresh_task
            self._listen_ws = None

    async def _handle_tunnel(self, tunnel_id: str) -> None:
        assert self._service_token is not None
        tcp_writer: asyncio.StreamWriter | None = None
        try:
            async with websockets.connect(
                self._url_for(f"/tunnel/{tunnel_id}", token=self._service_token),
                additional_headers={"Authorization": f"Bearer {self._service_token}"},
                max_size=None,
            ) as ws:
                self._record_tunnel_success()
                self._emit_health()
                try:
                    tcp_reader, tcp_writer = await asyncio.open_connection(
                        _LINK_DIRECT_HOST,
                        _LINK_DIRECT_PORT,
                    )
                except OSError:
                    self._record_tunnel_error(REASON_LOCAL_PRIVATE_LISTENER_UNREACHABLE)
                    self._emit_health()
                    log.warning(
                        "tunnel %s failed: reason=%s",
                        tunnel_id,
                        REASON_LOCAL_PRIVATE_LISTENER_UNREACHABLE,
                    )
                    return
                try:
                    await _pipe_tunnel(ws, tcp_reader, tcp_writer, tunnel_id)
                except ConnectionClosed as exc:
                    log.info("tunnel %s closed: code=%s", tunnel_id, exc.code)
                except OSError:
                    log.warning("tunnel %s pipe socket error", tunnel_id)
        except InvalidStatus as exc:
            status = exc.response.status_code
            if status == 404:
                self._record_tunnel_error(REASON_HOME_MISSING_MOBILE)
                self._emit_health()
                log.warning(
                    "tunnel %s failed: reason=%s", tunnel_id, REASON_HOME_MISSING_MOBILE
                )
                if self._listen_ws is not None:
                    with contextlib.suppress(Exception):
                        await self._listen_ws.close()
            elif status in (401, 403):
                self._record_tunnel_error(REASON_SERVICE_TOKEN_REJECTED)
                self._emit_health()
                log.warning(
                    "tunnel %s failed: reason=%s",
                    tunnel_id,
                    REASON_SERVICE_TOKEN_REJECTED,
                )
            else:
                self._record_tunnel_error(REASON_RELAY_TUNNEL_REJECTED, status=status)
                self._emit_health()
                log.warning(
                    "tunnel %s failed: reason=%s status=%s",
                    tunnel_id,
                    REASON_RELAY_TUNNEL_REJECTED,
                    status,
                )
        except (OSError, InvalidURI, asyncio.TimeoutError, TimeoutError):
            self._record_tunnel_error(REASON_RELAY_TUNNEL_UNREACHABLE)
            self._emit_health()
            log.warning(
                "tunnel %s failed: reason=%s",
                tunnel_id,
                REASON_RELAY_TUNNEL_UNREACHABLE,
            )
        except ConnectionClosed as exc:
            log.info("tunnel %s closed: code=%s", tunnel_id, exc.code)
        except Exception as exc:  # noqa: BLE001
            log.warning("tunnel %s error: type=%s", tunnel_id, type(exc).__name__)
        finally:
            if tcp_writer is not None:
                tcp_writer.close()
                with contextlib.suppress(OSError, RuntimeError):
                    await tcp_writer.wait_closed()
            self._emit("tunnel_close", {"tunnel_id": tunnel_id})
            self._emit_health()

    async def _refresh_health_loop(self) -> None:
        while True:
            await asyncio.sleep(_HEALTH_REFRESH_SECONDS)
            self._emit_health()

    def _emit_health(self) -> None:
        self._emit(
            LINK_HEALTH_EVENT,
            {
                "state": self._state,
                "listen_generation": self._listen_generation,
                "last_successful_relay_tunnel_at": self._last_successful_tunnel_at,
                "last_relay_tunnel_error": self._last_tunnel_error,
                "last_relay_tunnel_error_at": self._last_tunnel_error_at,
                "relay_tunnel_error_status": self._last_tunnel_error_status,
            },
        )

    def _set_state(self, coarse_event: str, state: str) -> None:
        self._state = state
        self._emit(coarse_event, {})
        self._emit_health()

    def _record_tunnel_success(self) -> None:
        self._last_successful_tunnel_at = now_ms()
        self._last_tunnel_error = None
        self._last_tunnel_error_at = None
        self._last_tunnel_error_status = None

    def _record_tunnel_error(self, reason: str, *, status: int | None = None) -> None:
        self._last_tunnel_error = reason
        self._last_tunnel_error_at = now_ms()
        self._last_tunnel_error_status = status

    def _url_for(self, path: str, *, token: str | None = None) -> str:
        query = {"instance": self._instance_id}
        if token:
            query["token"] = token
        return self._relay_ws_endpoint + path + "?" + urllib.parse.urlencode(query)


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


def _post_json_sync(url: str, body: dict[str, Any]) -> dict[str, Any]:
    if not url.startswith(("http://", "https://")):
        raise ValueError(f"unsupported url scheme: {url!r}")
    req = urllib.request.Request(  # noqa: S310
        url,
        data=json.dumps(body).encode(),
        headers={
            "content-type": "application/json",
            "user-agent": "solstone-link/0.1",
        },
        method="POST",
    )
    with urllib.request.urlopen(req, timeout=30) as resp:  # noqa: S310
        parsed = json.loads(resp.read())
    if not isinstance(parsed, dict):
        raise RuntimeError("relay returned invalid JSON response")
    return parsed


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
