# SPDX-License-Identifier: AGPL-3.0-only
# Copyright (c) 2026 sol pbc

"""Reusable HTTP client for local Convey API calls.

The ``session=`` constructor argument is the future tunnel seam: callers may
inject a duck-typed session such as ``PlHttpSession`` later, while this module
only selects the local/plain requests transport today.
"""

from __future__ import annotations

import contextlib
import functools
import json
import logging
from collections.abc import Callable
from typing import Any, NoReturn, TypeVar
from urllib.parse import urlencode

import requests
import typer

from solstone.think.pairing.config import get_host_url_override
from solstone.think.service import DEFAULT_SERVICE_PORT
from solstone.think.utils import read_service_port, require_solstone

logger = logging.getLogger(__name__)

MALFORMED_RESPONSE_MESSAGE = "I couldn't read the journal response."
SERVER_ERROR_MESSAGE = "The journal returned an unreadable error."
UNREACHABLE_MESSAGE = "I couldn't reach the journal over HTTP."

_F = TypeVar("_F", bound=Callable[..., Any])


class ConveyClientError(Exception):
    def __init__(
        self,
        error: str,
        *,
        reason_code: str | None = None,
        detail: str | None = None,
        status: int | None = None,
    ) -> None:
        self.error = error
        self.reason_code = reason_code
        self.detail = detail
        self.status = status
        super().__init__(error)


class ConveyUnreachableError(ConveyClientError):
    """Raised when the convey HTTP transport itself fails (service down/unreachable)."""


def resolve_base_url() -> str:
    override = get_host_url_override()
    if override is not None:
        return override
    port = read_service_port("convey") or DEFAULT_SERVICE_PORT
    return f"http://localhost:{port}"


class ConveyClient:
    def __init__(
        self,
        *,
        session: Any = None,
        base_url: str | None = None,
        require_service: bool = True,
    ) -> None:
        self._base_url = base_url or resolve_base_url()
        self._session = session or requests.Session()
        self._require_service = require_service

    def _handle_unreachable(self, exc: Exception) -> NoReturn:
        if self._require_service:
            require_solstone()
        raise ConveyUnreachableError(UNREACHABLE_MESSAGE, detail=str(exc)) from exc

    def request(
        self,
        method: str,
        path: str,
        *,
        params: Any = None,
        json: Any = None,
    ) -> Any:
        method = method.upper()
        if method not in {"DELETE", "GET", "POST", "PUT"}:
            raise ValueError(f"unsupported convey method: {method}")
        if not path.startswith("/"):
            raise ValueError("convey path must start with '/'")

        url = self._base_url.rstrip("/") + path
        if params:
            separator = "&" if "?" in url else "?"
            url += separator + urlencode(params, doseq=True)

        try:
            if method == "GET":
                response = self._session.get(url)
            elif method == "DELETE":
                response = self._session.delete(url)
            elif method == "PUT":
                response = self._session.put(url, json=json)
            else:
                response = self._session.post(url, json=json)
        except requests.exceptions.RequestException as exc:
            self._handle_unreachable(exc)

        return self._decode(response)

    def upload(self, path: str, *, files: dict[str, Any], data: Any = None) -> Any:
        if not path.startswith("/"):
            raise ValueError("convey path must start with '/'")
        url = self._base_url.rstrip("/") + path

        with contextlib.ExitStack() as stack:
            opened = {}
            for field, (filename, file_path, content_type) in files.items():
                handle = stack.enter_context(open(file_path, "rb"))
                opened[field] = (filename, handle, content_type)
            try:
                response = self._session.post(url, files=opened, data=data)
            except requests.exceptions.RequestException as exc:
                self._handle_unreachable(exc)
        return self._decode(response)

    def _decode(self, response: Any) -> Any:
        status = response.status_code
        text = response.text
        stripped = text.strip()
        parsed: Any = None
        parsed_ok = False
        if stripped:
            try:
                parsed = json.loads(stripped)
                parsed_ok = True
            except (json.JSONDecodeError, ValueError):
                parsed = None

        if 200 <= status < 300:
            if parsed_ok:
                return parsed
            raise ConveyClientError(MALFORMED_RESPONSE_MESSAGE, status=status)

        if isinstance(parsed, dict) and ("error" in parsed or "reason_code" in parsed):
            error = parsed.get("error") or parsed.get("reason_code")
            raise ConveyClientError(
                str(error),
                reason_code=parsed.get("reason_code"),
                detail=parsed.get("detail"),
                status=status,
            )

        raise ConveyClientError(SERVER_ERROR_MESSAGE, status=status)


_client: ConveyClient | None = None


def get_client() -> ConveyClient:
    global _client
    if _client is None:
        _client = ConveyClient()
    return _client


def paginate_collection(
    client: ConveyClient,
    path: str,
    *,
    params: dict | None = None,
    page_size: int = 100,
    top: int | None = None,
) -> list:
    collected: list = []
    offset = 0
    base_params = dict(params or {})

    while True:
        page_params = {**base_params, "limit": page_size, "offset": offset}
        body = client.request("GET", path, params=page_params)
        if not isinstance(body, dict):
            raise ConveyClientError(MALFORMED_RESPONSE_MESSAGE)

        items = body.get("items")
        total = body.get("total")
        if not isinstance(items, list) or not isinstance(total, int):
            raise ConveyClientError(MALFORMED_RESPONSE_MESSAGE)

        collected.extend(items)
        offset += len(items)

        if top is not None and len(collected) >= top:
            break
        if len(collected) >= total:
            break
        if not items:
            raise ConveyClientError(MALFORMED_RESPONSE_MESSAGE)

    if top is not None:
        return collected[:top]
    return collected


def convey_cli(fn: _F) -> _F:
    @functools.wraps(fn)
    def wrapper(*args: Any, **kwargs: Any) -> Any:
        try:
            return fn(*args, **kwargs)
        except ConveyClientError as err:
            typer.echo(err.error, err=True)
            raise typer.Exit(1) from err

    return wrapper  # type: ignore[return-value]
