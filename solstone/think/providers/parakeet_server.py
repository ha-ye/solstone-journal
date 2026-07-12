# SPDX-License-Identifier: AGPL-3.0-only
# Copyright (c) 2026 sol pbc

"""Connect-only client for the supervisor-owned Parakeet STT service."""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path

from solstone.think import parakeet_readiness
from solstone.think.providers.parakeet_install import ParakeetProviderError
from solstone.think.utils import get_journal, read_service_port

STATE_READY = "ready"
STATE_FAILED = "failed"

_HOST = "127.0.0.1"
_SERVICE_NAME = "parakeet-cpp"
_PLACEMENT_FILE = "parakeet-cpp.placement"
_VALID_PLACEMENTS = {"cpu", "gpu"}


class ParakeetServerNotReady(ParakeetProviderError):
    """The supervised Parakeet STT service is not ready for retryable work.

    ``reason_code`` (inherited) classifies this as a provider error; ``retry_reason``
    says *why* the server was unreachable so the deferral is machine-readable at the
    point it is surfaced.  See ``solstone/observe/transcribe/failure-and-telemetry.md``.
    """

    def __init__(self, message: str, *, retry_reason: str) -> None:
        super().__init__("parakeet_server_not_ready", message)
        self.retry_reason = retry_reason


@dataclass(frozen=True)
class ParakeetServerInfo:
    model_id: str
    port: int
    base_url: str
    state: str


def _base_url(port: int) -> str:
    return f"http://{_HOST}:{port}"


def _placement_path() -> Path:
    return Path(get_journal()) / "health" / _PLACEMENT_FILE


def write_parakeet_placement(device: str) -> None:
    """Persist the resolved parakeet.cpp serving placement for telemetry."""
    if device not in _VALID_PLACEMENTS:
        raise ValueError(f"invalid parakeet placement: {device!r}")
    path = _placement_path()
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(device)


def read_parakeet_placement() -> str | None:
    """Read the resolved parakeet.cpp serving placement, if valid."""
    try:
        device = _placement_path().read_text().strip()
    except FileNotFoundError:
        return None
    return device if device in _VALID_PLACEMENTS else None


def clear_parakeet_placement() -> None:
    """Remove any stale parakeet.cpp serving placement record."""
    _placement_path().unlink(missing_ok=True)


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
        raise ParakeetServerNotReady(
            "Parakeet server is not ready yet.", retry_reason="no_port"
        )
    state, error = _probe_health(port)
    if state != STATE_READY:
        detail = f": {error}" if error else ""
        raise ParakeetServerNotReady(
            f"Parakeet server is not ready yet{detail}",
            retry_reason="server_not_ready",
        )
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
    "clear_parakeet_placement",
    "connect",
    "probe_state",
    "read_parakeet_placement",
    "write_parakeet_placement",
]
