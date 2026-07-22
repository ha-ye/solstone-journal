# SPDX-License-Identifier: AGPL-3.0-only
# Copyright (c) 2026 sol pbc

"""Inline WSGI dispatch for secure listener mux streams."""

from __future__ import annotations

import asyncio
import sys
import threading
import urllib.parse
from collections.abc import Iterable
from concurrent.futures import CancelledError, ThreadPoolExecutor
from dataclasses import dataclass
from typing import Any, Callable, Final

from werkzeug.exceptions import HTTPException

from solstone.think.link.window import window_open

from .identity import ConveyIdentity
from .mux import (
    RESET_CTX_APP_CANCELLATION,
    RESET_CTX_BODY_DISCARD_CANCELLATION,
    StreamWriter,
)

_HEAD_LIMIT = 64 * 1024
_BODY_METHODS = {"POST", "PUT", "PATCH"}
_NO_BODY_STATUSES = {204, 304}
_DEFAULT_PORTS: Final[dict[str, int]] = {"http": 80, "https": 443}
# The cert-less pairing tunnel admits EXACTLY these endpoints (canonical +
# the /app/link legacy alias). Never relax to a suffix/substring match — that
# would admit pair_start and widen the tunnel.
CERTLESS_PAIR_ENDPOINTS = frozenset({"app:network.pair", "app:link.pair"})


class HttpBadRequest(ValueError): ...


class _WsgiClientDisconnected(ConnectionError): ...


def _normalize_location_headers(
    headers: list[tuple[str, str]],
    host_header: str | None,
    request_scheme: str,
) -> list[tuple[str, str]]:
    # Peer-controlled Host/Location values preserve app response bytes here
    # instead of turning observer navigation into a 500 at the response boundary.
    request_authority = _parse_host_authority(host_header, request_scheme)
    if request_authority is None:
        return list(headers)

    normalized: list[tuple[str, str]] = []
    for name, value in headers:
        if name.lower() != "location":
            normalized.append((name, value))
            continue
        normalized.append((name, _normalize_location_value(value, request_authority)))
    return normalized


def _parse_host_authority(
    host_header: str | None,
    request_scheme: str,
) -> tuple[str, int] | None:
    default_port = _DEFAULT_PORTS.get(request_scheme)
    if default_port is None or not host_header:
        return None
    if "," in host_header or "@" in host_header:
        return None
    try:
        parsed = urllib.parse.urlsplit(f"//{host_header}")
    except ValueError:
        return None
    if parsed.path or parsed.query or parsed.fragment or not parsed.hostname:
        return None
    try:
        port = parsed.port
    except ValueError:
        return None
    return parsed.hostname, port if port is not None else default_port


def _normalize_location_value(
    value: str,
    request_authority: tuple[str, int],
) -> str:
    try:
        parsed = urllib.parse.urlsplit(value)
    except ValueError:
        return value
    if not parsed.scheme or parsed.scheme not in _DEFAULT_PORTS:
        return value
    if not parsed.hostname:
        return value
    try:
        port = parsed.port
    except ValueError:
        return value

    redirect_port = port if port is not None else _DEFAULT_PORTS[parsed.scheme]
    if (parsed.hostname, redirect_port) != request_authority:
        return value

    if parsed.path.startswith("//") or "\\" in parsed.path:
        # Browser URL parsing can treat this relative form as authority-bearing;
        # keep the absolute URI rather than changing origin during the rewrite.
        return value

    path = parsed.path or "/"
    if parsed.query:
        path += f"?{parsed.query}"
    if parsed.fragment:
        path += f"#{parsed.fragment}"
    return path


@dataclass(frozen=True)
class ParsedRequest:
    method: str
    target: str
    path: str
    query: str
    version: str
    headers: list[tuple[str, str]]
    content_length: int | None
    transfer_encoding: str | None


@dataclass(frozen=True)
class DispatchResult:
    endpoint: str | None
    status: int


async def parse_http_head(stream_reader: asyncio.StreamReader) -> ParsedRequest:
    try:
        raw = await stream_reader.readuntil(b"\r\n\r\n")
    except (asyncio.IncompleteReadError, asyncio.LimitOverrunError) as exc:
        raise HttpBadRequest("malformed HTTP request head") from exc
    if len(raw) > _HEAD_LIMIT:
        raise HttpBadRequest("HTTP request head too large")
    try:
        text = raw.decode("iso-8859-1")
    except UnicodeDecodeError as exc:
        raise HttpBadRequest("HTTP request head is not latin-1") from exc
    lines = text[:-4].split("\r\n")
    if not lines:
        raise HttpBadRequest("missing request line")
    parts = lines[0].split(" ")
    if len(parts) != 3:
        raise HttpBadRequest("bad request line")
    method, target, version = parts
    if not method or not target or not version.startswith("HTTP/"):
        raise HttpBadRequest("bad request line")

    headers: list[tuple[str, str]] = []
    content_length: int | None = None
    transfer_encoding: str | None = None
    for line in lines[1:]:
        if not line:
            continue
        if ":" not in line:
            raise HttpBadRequest("bad header line")
        name, value = line.split(":", 1)
        clean_name = name.strip()
        clean_value = value.strip()
        if not clean_name:
            raise HttpBadRequest("bad header line")
        headers.append((clean_name, clean_value))
        lower = clean_name.lower()
        if lower == "content-length":
            try:
                parsed_length = int(clean_value)
            except ValueError as exc:
                raise HttpBadRequest("bad content-length") from exc
            if parsed_length < 0:
                raise HttpBadRequest("bad content-length")
            content_length = parsed_length
        elif lower == "transfer-encoding":
            transfer_encoding = clean_value

    split = urllib.parse.urlsplit(target)
    path = split.path or "/"
    query = split.query
    return ParsedRequest(
        method=method.upper(),
        target=target,
        path=path,
        query=query,
        version=version,
        headers=headers,
        content_length=content_length,
        transfer_encoding=transfer_encoding,
    )


class MuxWSGIInput:
    def __init__(
        self,
        stream_reader: asyncio.StreamReader,
        loop: asyncio.AbstractEventLoop,
        content_length: int | None,
    ) -> None:
        self._stream_reader = stream_reader
        self._loop = loop
        self._remaining = content_length or 0

    @property
    def remaining(self) -> int:
        return self._remaining

    def read(self, size: int = -1) -> bytes:
        if self._remaining <= 0:
            return b""
        if size is None or size < 0 or size > self._remaining:
            size = self._remaining
        if size <= 0:
            return b""
        future = asyncio.run_coroutine_threadsafe(
            self._stream_reader.read(size),
            self._loop,
        )
        chunk = future.result()
        self._remaining -= len(chunk)
        return chunk

    def readline(self, size: int = -1) -> bytes:
        if self._remaining <= 0:
            return b""
        limit = (
            self._remaining if size is None or size < 0 else min(size, self._remaining)
        )
        out = bytearray()
        while len(out) < limit:
            chunk = self.read(1)
            if not chunk:
                break
            out.extend(chunk)
            if chunk == b"\n":
                break
        return bytes(out)

    def readlines(self, hint: int = -1) -> list[bytes]:
        lines: list[bytes] = []
        total = 0
        while self._remaining > 0:
            line = self.readline()
            if not line:
                break
            lines.append(line)
            total += len(line)
            if hint is not None and hint >= 0 and total >= hint:
                break
        return lines


async def dispatch_stream(
    app: Any,
    identity: ConveyIdentity,
    stream_reader: asyncio.StreamReader,
    stream_writer: StreamWriter,
    loop: asyncio.AbstractEventLoop,
    executor: ThreadPoolExecutor,
) -> DispatchResult:
    try:
        request = await parse_http_head(stream_reader)
    except HttpBadRequest as exc:
        await _finish_early(
            stream_writer,
            400,
            "Bad Request",
            str(exc),
            context=RESET_CTX_BODY_DISCARD_CANCELLATION,
        )
        return DispatchResult(endpoint=None, status=400)

    transfer_encoding = (request.transfer_encoding or "").lower()
    if transfer_encoding:
        await _finish_early(
            stream_writer,
            400,
            "Bad Request",
            "unsupported transfer-encoding",
            context=RESET_CTX_BODY_DISCARD_CANCELLATION,
        )
        return DispatchResult(endpoint=None, status=400)
    if request.method in _BODY_METHODS and request.content_length is None:
        await _finish_early(
            stream_writer,
            411,
            "Length Required",
            "content-length required",
            context=RESET_CTX_BODY_DISCARD_CANCELLATION,
        )
        return DispatchResult(endpoint=None, status=411)

    path_info = urllib.parse.unquote(request.path)
    endpoint: str | None = None
    if identity.fingerprint is None:
        # Request-level confinement: a cert-less request after the window closes is refused immediately (property 3), not served by a /pair that would 410. The 5s poll only reaps the idle socket.
        if not window_open():
            await _finish_early(
                stream_writer,
                403,
                "Forbidden",
                "pairing window closed",
                context=RESET_CTX_BODY_DISCARD_CANCELLATION,
            )
            return DispatchResult(endpoint=None, status=403)
        if request.path != path_info:
            await _finish_early(
                stream_writer,
                403,
                "Forbidden",
                "pairing tunnel may only use /app/network/pair",
                context=RESET_CTX_BODY_DISCARD_CANCELLATION,
            )
            return DispatchResult(endpoint=None, status=403)
        endpoint = _match_endpoint(app, path_info, request.method)
        if endpoint not in CERTLESS_PAIR_ENDPOINTS:
            await _finish_early(
                stream_writer,
                403,
                "Forbidden",
                "pairing tunnel may only use /app/network/pair",
                context=RESET_CTX_BODY_DISCARD_CANCELLATION,
            )
            return DispatchResult(endpoint=endpoint, status=403)

    disconnect_event = threading.Event()
    environ = build_environ(
        request,
        identity,
        stream_reader,
        loop,
        disconnect_event,
        path_info,
    )
    future = loop.run_in_executor(
        executor,
        _run_wsgi,
        app,
        environ,
        stream_writer,
        loop,
        disconnect_event,
    )
    try:
        status = await future
        wsgi_input = environ["wsgi.input"]
        if wsgi_input.remaining > 0:
            stream_writer.begin_drain(RESET_CTX_APP_CANCELLATION)
    except asyncio.CancelledError:
        disconnect_event.set()
        raise
    finally:
        disconnect_event.set()
    return DispatchResult(endpoint=endpoint, status=status)


def certless_target_allowed(app: Any, path_info: str, method: str) -> bool:
    return _match_endpoint(app, path_info, method) in CERTLESS_PAIR_ENDPOINTS


def _match_endpoint(app: Any, path_info: str, method: str) -> str | None:
    try:
        endpoint, _args = app.url_map.bind(
            "solstone.local",
            url_scheme="https",
        ).match(path_info, method=method)
    except HTTPException:
        return None
    return str(endpoint)


def build_environ(
    request: ParsedRequest,
    identity: ConveyIdentity,
    stream_reader: asyncio.StreamReader,
    loop: asyncio.AbstractEventLoop,
    disconnect_event: threading.Event,
    path_info: str,
) -> dict[str, Any]:
    environ: dict[str, Any] = {
        "REQUEST_METHOD": request.method,
        "SCRIPT_NAME": "",
        "PATH_INFO": path_info,
        "QUERY_STRING": request.query,
        "SERVER_NAME": "solstone.local",
        "SERVER_PORT": "7657",
        "SERVER_PROTOCOL": "HTTP/1.1",
        "wsgi.version": (1, 0),
        "wsgi.input": MuxWSGIInput(stream_reader, loop, request.content_length),
        "wsgi.errors": sys.stderr,
        "wsgi.url_scheme": "https",
        "wsgi.multithread": True,
        "wsgi.multiprocess": False,
        "wsgi.run_once": False,
        "REMOTE_ADDR": "",
        "REMOTE_PORT": "",
        "pl.identity": identity,
        "pl.disconnect_event": disconnect_event,
    }
    grouped: dict[str, list[str]] = {}
    for name, value in request.headers:
        lower = name.lower()
        if lower == "content-type":
            environ["CONTENT_TYPE"] = value
            continue
        if lower == "content-length":
            environ["CONTENT_LENGTH"] = value
            continue
        key = "HTTP_" + name.upper().replace("-", "_")
        grouped.setdefault(key, []).append(value)
    for key, values in grouped.items():
        environ[key] = ",".join(values)
    return environ


async def write_simple_response(
    writer: StreamWriter,
    status_code: int,
    reason: str,
    text: str,
) -> None:
    body = (text + "\n").encode("utf-8")
    head = (
        f"HTTP/1.1 {status_code} {reason}\r\n"
        "Content-Type: text/plain\r\n"
        f"Content-Length: {len(body)}\r\n"
        "Connection: close\r\n"
        "\r\n"
    ).encode("ascii")
    await writer.write(head + body)
    await writer.close()


async def _finish_early(
    stream_writer: StreamWriter,
    status_code: int,
    reason: str,
    text: str,
    *,
    context: str,
) -> None:
    await write_simple_response(stream_writer, status_code, reason, text)
    stream_writer.begin_drain(context)


def _run_wsgi(
    app: Any,
    environ: dict[str, Any],
    stream_writer: StreamWriter,
    loop: asyncio.AbstractEventLoop,
    disconnect_event: threading.Event,
) -> int:
    state: dict[str, Any] = {
        "status": None,
        "headers": None,
        "headers_sent": False,
        "chunked": False,
        "body_allowed": True,
    }

    def send(data: bytes) -> None:
        if disconnect_event.is_set():
            raise _WsgiClientDisconnected
        try:
            future = asyncio.run_coroutine_threadsafe(stream_writer.write(data), loop)
            future.result()
        except (
            BrokenPipeError,
            CancelledError,
            ConnectionError,
            ConnectionResetError,
            RuntimeError,
        ) as exc:
            disconnect_event.set()
            raise _WsgiClientDisconnected from exc

    def start_response(
        status: str,
        headers: list[tuple[str, str]],
        exc_info: object = None,
    ) -> Callable[[bytes], None]:
        if exc_info is not None and state["headers_sent"]:
            exc_type, exc, tb = exc_info  # type: ignore[misc]
            raise exc.with_traceback(tb)
        state["status"] = status
        state["headers"] = headers
        return write

    def ensure_head() -> None:
        if state["headers_sent"]:
            return
        status = state["status"] or "500 Internal Server Error"
        headers = list(state["headers"] or [])
        headers = _normalize_location_headers(
            headers,
            environ.get("HTTP_HOST"),
            environ["wsgi.url_scheme"],
        )
        status_code = _status_code(status)
        method = str(environ.get("REQUEST_METHOD", "GET")).upper()
        body_allowed = method != "HEAD" and status_code not in _NO_BODY_STATUSES
        state["body_allowed"] = body_allowed
        has_content_length = any(
            name.lower() == "content-length" for name, _ in headers
        )
        has_transfer_encoding = any(
            name.lower() == "transfer-encoding" for name, _ in headers
        )
        if body_allowed and not has_content_length and not has_transfer_encoding:
            headers.append(("Transfer-Encoding", "chunked"))
            state["chunked"] = True
        lines = [f"HTTP/1.1 {status}\r\n"]
        lines.extend(f"{name}: {value}\r\n" for name, value in headers)
        lines.append("\r\n")
        send("".join(lines).encode("iso-8859-1"))
        state["headers_sent"] = True

    def write(data: bytes) -> None:
        if isinstance(data, str):
            data = data.encode("utf-8")
        ensure_head()
        if not data or not state["body_allowed"]:
            return
        if state["chunked"]:
            send(f"{len(data):x}\r\n".encode("ascii") + data + b"\r\n")
        else:
            send(data)

    response_iter: Iterable[bytes] | None = None
    try:
        response_iter = app.wsgi_app(environ, start_response)
        for chunk in response_iter:
            if disconnect_event.is_set():
                break
            try:
                write(chunk)
            except _WsgiClientDisconnected:
                break
        if not disconnect_event.is_set():
            ensure_head()
            if state["chunked"] and state["body_allowed"]:
                send(b"0\r\n\r\n")
            future = asyncio.run_coroutine_threadsafe(stream_writer.close(), loop)
            future.result()
    finally:
        disconnect_event.set()
        if response_iter is not None and hasattr(response_iter, "close"):
            response_iter.close()  # type: ignore[attr-defined]
    return _status_code(str(state["status"] or "500 Internal Server Error"))


def _status_code(status: str) -> int:
    try:
        return int(status.split(" ", 1)[0])
    except (ValueError, IndexError):
        return 500
