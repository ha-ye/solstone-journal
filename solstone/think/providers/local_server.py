# SPDX-License-Identifier: AGPL-3.0-only
# Copyright (c) 2026 sol pbc

"""Connect-only client for the supervisor-owned local llama-server."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any

from solstone.think.models import LOCAL_MODEL
from solstone.think.providers.local import LocalProviderError
from solstone.think.utils import read_service_port

STATE_IDLE = "idle"
STATE_STARTING = "starting"
STATE_LOADING = "loading"
STATE_READY = "ready"
STATE_FAILED = "failed"
STATE_STOPPED = "stopped"

_HOST = "127.0.0.1"
_SERVICE_NAME = "local"

# COPY REVIEW: placeholder owner-facing copy; founder-gated before ship.
LOCAL_MODEL_NOT_READY_COPY = "Local model is not ready yet."


@dataclass(frozen=True)
class LocalServerInfo:
    model_id: str
    port: int
    base_url: str
    state: str
    binary_path: str | None = None
    model_path: str | None = None
    served_model_id: str = LOCAL_MODEL


def _base_url(port: int) -> str:
    return f"http://{_HOST}:{port}"


def _fetch_health(
    port: int, timeout_s: float = 1.0
) -> tuple[str, str | None, dict[str, Any] | None]:
    import httpx

    try:
        response = httpx.get(f"{_base_url(port)}/health", timeout=timeout_s)
    except Exception as exc:
        return STATE_FAILED, str(exc), None
    if response.status_code == 200:
        try:
            body = response.json()
        except Exception:
            body = None
        return STATE_READY, None, body if isinstance(body, dict) else None
    if response.status_code == 503 and "loading model" in response.text.lower():
        return STATE_LOADING, None, None
    return STATE_FAILED, f"HTTP {response.status_code}: {response.text[:200]}", None


def _probe_health(port: int, timeout_s: float = 1.0) -> tuple[str, str | None]:
    state, error, _ = _fetch_health(port, timeout_s)
    return state, error


def _resolve_served_model_id(health_body: dict[str, Any] | None) -> str | None:
    """Served/wire id from the /health body. None signals present-but-invalid."""
    if not isinstance(health_body, dict) or "loaded_model" not in health_body:
        return LOCAL_MODEL
    loaded = health_body["loaded_model"]
    if isinstance(loaded, str) and loaded.strip():
        return loaded
    return None


def is_healthy() -> bool:
    port = read_service_port(_SERVICE_NAME)
    if port is None:
        return False
    state, _ = _probe_health(port)
    return state == STATE_READY


def probe_state() -> tuple[str, str | None]:
    port = read_service_port(_SERVICE_NAME)
    if port is None:
        return STATE_FAILED, "no port"
    return _probe_health(port)


def connect() -> LocalServerInfo:
    port = read_service_port(_SERVICE_NAME)
    if port is None:
        raise LocalProviderError("local_model_not_ready", LOCAL_MODEL_NOT_READY_COPY)
    state, _, body = _fetch_health(port)
    if state != STATE_READY:
        raise LocalProviderError("local_model_not_ready", LOCAL_MODEL_NOT_READY_COPY)
    served_model_id = _resolve_served_model_id(body)
    if served_model_id is None:
        raise LocalProviderError("local_model_not_ready", LOCAL_MODEL_NOT_READY_COPY)
    return LocalServerInfo(
        model_id=LOCAL_MODEL,
        port=port,
        base_url=_base_url(port),
        state=STATE_READY,
        served_model_id=served_model_id,
    )


__all__ = [
    "LOCAL_MODEL_NOT_READY_COPY",
    "LocalServerInfo",
    "STATE_IDLE",
    "STATE_STARTING",
    "STATE_LOADING",
    "STATE_READY",
    "STATE_FAILED",
    "STATE_STOPPED",
    "connect",
    "is_healthy",
    "probe_state",
]
