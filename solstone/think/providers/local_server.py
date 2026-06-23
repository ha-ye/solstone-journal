# SPDX-License-Identifier: AGPL-3.0-only
# Copyright (c) 2026 sol pbc

"""Connect-only client for the supervisor-owned local llama-server."""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from typing import Any

from solstone.think.models import LOCAL_MODEL
from solstone.think.providers.local import LocalProviderError
from solstone.think.utils import get_journal, read_service_port

STATE_IDLE = "idle"
STATE_STARTING = "starting"
STATE_LOADING = "loading"
STATE_READY = "ready"
STATE_FAILED = "failed"
STATE_STOPPED = "stopped"

_HOST = "127.0.0.1"
_SERVICE_NAME = "local"

# Minimum/floor context window. This is the OpenHands agent
# `max_input_tokens` floor, the floor-tier llama-server `-c`, and the sizing
# function's lower clamp. Capable GPU server launch `-c` comes from
# select_server_tier().
LOCAL_MIN_CONTEXT_TOKENS = 16384


@dataclass(frozen=True)
class ServerTier:
    name: str
    context_tokens: int
    parallel_slots: int
    prompt_cache_mib: int


# Tunable estimates — keep all tier values in these two instances; do not
# scatter literals elsewhere. The threshold is the only other tunable.
_CAPABLE_TIER_MIN_VRAM_MIB = 16000
_CAPABLE_TIER = ServerTier(
    name="capable", context_tokens=32768, parallel_slots=2, prompt_cache_mib=2048
)
_FLOOR_TIER = ServerTier(
    name="floor",
    context_tokens=LOCAL_MIN_CONTEXT_TOKENS,
    parallel_slots=1,
    prompt_cache_mib=0,
)

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


def select_server_tier(vram_mib: int) -> ServerTier:
    if vram_mib >= _CAPABLE_TIER_MIN_VRAM_MIB:
        return _CAPABLE_TIER
    return _FLOOR_TIER


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


def fetch_props(port: int, timeout_s: float = 1.0) -> dict[str, Any] | None:
    """Read llama-server GET /props.

    Returns parsed JSON dict or None on any failure (never raises).
    """
    import httpx

    try:
        response = httpx.get(f"{_base_url(port)}/props", timeout=timeout_s)
        if response.status_code != 200:
            return None
        body = response.json()
    except Exception:
        return None
    return body if isinstance(body, dict) else None


def _extract_n_ctx(props: dict[str, Any]) -> int | None:
    """Effective context window from a /props body.

    Prefer top-level 'n_ctx'; fall back to
    props['default_generation_settings']['n_ctx']. Coerce to int; return None
    if absent or non-numeric.
    """
    if "n_ctx" in props:
        value = props["n_ctx"]
    else:
        settings = props.get("default_generation_settings")
        if not isinstance(settings, dict) or "n_ctx" not in settings:
            return None
        value = settings["n_ctx"]

    try:
        return int(value)
    except (TypeError, ValueError):
        return None


def read_server_context_window(port: int) -> int | None:
    props = fetch_props(port)
    if props is None:
        return None
    return _extract_n_ctx(props)


def write_local_context_window(tokens: int) -> None:
    health_dir = Path(get_journal()) / "health"
    health_dir.mkdir(parents=True, exist_ok=True)
    (health_dir / "local.ctx").write_text(str(tokens))


def read_local_context_window() -> int | None:
    context_file = Path(get_journal()) / "health" / "local.ctx"
    try:
        return int(context_file.read_text().strip())
    except (FileNotFoundError, ValueError):
        return None


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
    "LOCAL_MIN_CONTEXT_TOKENS",
    "LOCAL_MODEL_NOT_READY_COPY",
    "LocalServerInfo",
    "ServerTier",
    "STATE_IDLE",
    "STATE_STARTING",
    "STATE_LOADING",
    "STATE_READY",
    "STATE_FAILED",
    "STATE_STOPPED",
    "connect",
    "fetch_props",
    "is_healthy",
    "probe_state",
    "read_local_context_window",
    "read_server_context_window",
    "select_server_tier",
    "write_local_context_window",
]
