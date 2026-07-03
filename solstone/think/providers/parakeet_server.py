# SPDX-License-Identifier: AGPL-3.0-only
# Copyright (c) 2026 sol pbc

"""Connect-only client for the supervisor-owned Parakeet STT service."""

from __future__ import annotations

from dataclasses import dataclass

from solstone.think import parakeet_readiness
from solstone.think.providers.parakeet_install import ParakeetProviderError
from solstone.think.utils import read_service_port

STATE_READY = "ready"
STATE_FAILED = "failed"

_HOST = "127.0.0.1"
_SERVICE_NAME = "parakeet-cpp"


class ParakeetServerNotReady(ParakeetProviderError):
    """The supervised Parakeet STT service is not ready for retryable work."""

    def __init__(self, message: str) -> None:
        super().__init__("parakeet_server_not_ready", message)


@dataclass(frozen=True)
class ParakeetServerInfo:
    model_id: str
    port: int
    base_url: str
    state: str


def _base_url(port: int) -> str:
    return f"http://{_HOST}:{port}"


def _probe_health(port: int, timeout_s: float = 1.0) -> tuple[str, str | None]:
    import httpx

    try:
        response = httpx.get(f"{_base_url(port)}/health", timeout=timeout_s)
    except Exception as exc:
        return STATE_FAILED, str(exc)
    if response.status_code == 200:
        return STATE_READY, None
    return STATE_FAILED, f"HTTP {response.status_code}: {response.text[:200]}"


def probe_state() -> tuple[str, str | None]:
    port = read_service_port(_SERVICE_NAME)
    if port is None:
        return STATE_FAILED, "no port"
    return _probe_health(port)


def connect() -> ParakeetServerInfo:
    port = read_service_port(_SERVICE_NAME)
    if port is None:
        raise ParakeetServerNotReady("Parakeet server is not ready yet.")
    state, error = _probe_health(port)
    if state != STATE_READY:
        detail = f": {error}" if error else ""
        raise ParakeetServerNotReady(f"Parakeet server is not ready yet{detail}")
    return ParakeetServerInfo(
        model_id=parakeet_readiness.PARAKEET_CPP_MODEL_FILENAME,
        port=port,
        base_url=_base_url(port),
        state=STATE_READY,
    )


__all__ = [
    "STATE_FAILED",
    "STATE_READY",
    "ParakeetServerInfo",
    "ParakeetServerNotReady",
    "connect",
    "probe_state",
]
