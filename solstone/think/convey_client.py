# SPDX-License-Identifier: AGPL-3.0-only
# Copyright (c) 2026 sol pbc

"""Reusable HTTP client for local Convey API calls.

The ``session=`` constructor argument is the future tunnel seam: callers may
inject a duck-typed session such as ``PlHttpSession`` later, while this module
only selects the local/plain requests transport today.
"""

from __future__ import annotations

import functools
import json
import logging
from collections.abc import Callable
from typing import Any, TypeVar
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


def resolve_base_url() -> str:
    override = get_host_url_override()
    if override is not None:
        return override
    port = read_service_port("convey") or DEFAULT_SERVICE_PORT
    return f"http://localhost:{port}"


class ConveyClient:
    def __init__(self, *, session: Any = None, base_url: str | None = None) -> None:
        self._base_url = base_url or resolve_base_url()
        self._session = session or requests.Session()

    def request(
        self,
        method: str,
        path: str,
        *,
        params: Any = None,
        json: Any = None,
    ) -> dict[str, Any]:
        method = method.upper()
        if method not in {"GET", "POST"}:
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
            else:
                response = self._session.post(url, json=json)
        except requests.exceptions.RequestException as exc:
            require_solstone()
            raise ConveyClientError(UNREACHABLE_MESSAGE, detail=str(exc)) from exc

        return self._decode(response)

    def _decode(self, response: Any) -> dict[str, Any]:
        status = response.status_code
        text = response.text
        stripped = text.strip()
        parsed: Any = None
        if stripped:
            try:
                parsed = json.loads(stripped)
            except (json.JSONDecodeError, ValueError):
                parsed = None

        if 200 <= status < 300:
            if isinstance(parsed, dict):
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


def convey_cli(fn: _F) -> _F:
    @functools.wraps(fn)
    def wrapper(*args: Any, **kwargs: Any) -> Any:
        try:
            return fn(*args, **kwargs)
        except ConveyClientError as err:
            typer.echo(err.error, err=True)
            raise typer.Exit(1) from err

    return wrapper  # type: ignore[return-value]
