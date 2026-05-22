# SPDX-License-Identifier: AGPL-3.0-only
# Copyright (c) 2026 sol pbc

"""Local provider first-run bootstrap helpers for Settings."""

from __future__ import annotations

import logging
import threading
import time
from dataclasses import dataclass
from typing import Literal

import psutil

from solstone.think.models import LOCAL_FLASH
from solstone.think.providers import local_install
from solstone.think.providers.local import (
    LOCAL_MODEL_SPECS,
    LocalProviderError,
    normalize_model_id,
)

logger = logging.getLogger(__name__)

BootstrapStateName = Literal["idle", "downloading", "verifying", "installed", "failed"]
_STALL_SECONDS = 60.0


@dataclass
class LocalBootstrapState:
    state: BootstrapStateName = "idle"
    received_bytes: int = 0
    total_bytes: int = 0
    started_at: float | None = None
    last_progress_at: float | None = None
    message: str | None = None
    thread: threading.Thread | None = None


class LocalBootstrapUnavailableError(RuntimeError):
    """Raised when the host cannot run the local provider."""


class LocalBootstrapStartError(RuntimeError):
    """Raised when the bootstrap worker could not be started."""


_STATES: dict[str, LocalBootstrapState] = {
    name: LocalBootstrapState() for name in LOCAL_MODEL_SPECS
}
_STATES_LOCK = threading.Lock()


def check_binary_present() -> bool:
    """Return whether the pinned llama-server binary is installed."""
    try:
        return bool(local_install.inspect_readiness(LOCAL_FLASH)["binary_installed"])
    except Exception:
        return False


def check_model_present(model: str) -> bool:
    """Return whether the pinned GGUF model is installed."""
    try:
        model_id = normalize_model_id(model)
        return bool(local_install.inspect_readiness(model_id)["model_installed"])
    except Exception:
        return False


def _platform_supported() -> tuple[bool, str]:
    try:
        local_install.pin_for_current_platform()
    except LocalProviderError as exc:
        return False, str(exc)
    return True, ""


def get_availability_payload(model: str) -> dict[str, bool | float | int | str]:
    """Return the local provider availability payload used by Settings."""
    model_id = normalize_model_id(model)
    spec = LOCAL_MODEL_SPECS[model_id]
    binary_present = check_binary_present()
    model_present = check_model_present(model_id)
    platform_supported, reason = _platform_supported()
    total_memory_bytes = int(psutil.virtual_memory().total)
    total_memory_gb = round(total_memory_bytes / 1024**3, 1)
    ram_sufficient = total_memory_bytes >= spec.min_ram_bytes

    if not platform_supported:
        available = False
    elif not ram_sufficient:
        available = False
        reason = (
            f"insufficient RAM (need {spec.min_ram_bytes // 1024**3} GB, "
            f"have {int(total_memory_bytes / 1024**3)} GB)"
        )
    else:
        available = binary_present and model_present
        if not binary_present:
            reason = "local runtime is not installed"
        elif not model_present:
            reason = "local model files are not installed"
        else:
            reason = ""

    return {
        "model": model_id,
        "platform_supported": platform_supported,
        "total_memory_gb": total_memory_gb,
        "min_ram_gb": spec.min_ram_bytes // 1024**3,
        "binary_present": binary_present,
        "model_present": model_present,
        "available": available,
        "reason": reason,
    }


def _serialize_state_locked(model: str) -> dict[str, int | str | None]:
    state = _STATES[model]
    return {
        "state": state.state,
        "received_bytes": int(state.received_bytes),
        "total_bytes": int(state.total_bytes),
        "message": state.message,
    }


def _mark_installed_locked(model: str) -> None:
    state = _STATES[model]
    state.state = "installed"
    state.received_bytes = state.total_bytes
    state.message = None
    state.thread = None
    state.last_progress_at = time.monotonic()


def _mark_downloading_locked(model: str, thread: threading.Thread, now: float) -> None:
    state = _STATES[model]
    state.state = "downloading"
    state.received_bytes = 0
    state.total_bytes = LOCAL_MODEL_SPECS[model].size_bytes
    state.started_at = now
    state.last_progress_at = now
    state.message = None
    state.thread = thread


def _mark_verifying(model: str) -> None:
    with _STATES_LOCK:
        state = _STATES[model]
        if state.state != "downloading":
            return
        state.state = "verifying"
        state.received_bytes = 0
        state.total_bytes = LOCAL_MODEL_SPECS[model].size_bytes
        state.last_progress_at = time.monotonic()
        state.message = None


def _mark_failed_locked(model: str, message: str) -> None:
    state = _STATES[model]
    state.state = "failed"
    state.message = message
    state.last_progress_at = time.monotonic()


def _set_progress(
    model: str, received_bytes: int, total_bytes: int | None = None
) -> None:
    with _STATES_LOCK:
        state = _STATES[model]
        state.received_bytes = max(0, int(received_bytes))
        if total_bytes is not None:
            state.total_bytes = max(0, int(total_bytes))
        state.last_progress_at = time.monotonic()


def _observe_stall_locked(model: str, now: float) -> None:
    state = _STATES[model]
    if state.state not in ("downloading", "verifying"):
        return
    thread = state.thread
    if thread is not None and thread.is_alive():
        return
    last_progress = state.last_progress_at or state.started_at
    if last_progress is None or now - last_progress <= _STALL_SECONDS:
        return
    _mark_failed_locked(model, f"{state.state} stalled with no progress")


def get_state(model: str) -> dict[str, int | str | None]:
    """Return the serialized bootstrap state, applying stall detection."""
    model_id = normalize_model_id(model)
    with _STATES_LOCK:
        _observe_stall_locked(model_id, time.monotonic())
        return _serialize_state_locked(model_id)


def start_bootstrap(model: str) -> tuple[dict[str, str], int]:
    """Start the local provider bootstrap worker if needed."""
    model_id = normalize_model_id(model)
    availability = get_availability_payload(model_id)
    blocked_reason = _blocked_reason(availability)
    if blocked_reason:
        raise LocalBootstrapUnavailableError(blocked_reason)

    installed = bool(availability["binary_present"] and availability["model_present"])
    with _STATES_LOCK:
        state = _STATES[model_id]
        if (
            state.state in ("downloading", "verifying")
            and state.thread
            and state.thread.is_alive()
        ):
            return {"state": state.state}, 200
        retry_after_failed = state.state == "failed"
        if installed and not retry_after_failed:
            _mark_installed_locked(model_id)
            return {"state": "installed"}, 200

    with _STATES_LOCK:
        state = _STATES[model_id]
        if (
            state.state in ("downloading", "verifying")
            and state.thread
            and state.thread.is_alive()
        ):
            return {"state": state.state}, 200
        retry_after_failed = retry_after_failed or state.state == "failed"
        if (
            not retry_after_failed
            and check_binary_present()
            and check_model_present(model_id)
        ):
            _mark_installed_locked(model_id)
            return {"state": "installed"}, 200
        try:
            thread = threading.Thread(
                target=_run_bootstrap_worker,
                args=(model_id,),
                name=f"local-provider-bootstrap-{model_id}",
                daemon=True,
            )
        except Exception as exc:
            _mark_failed_locked(model_id, str(exc))
            raise LocalBootstrapStartError(str(exc)) from exc
        _mark_downloading_locked(model_id, thread, time.monotonic())

    try:
        thread.start()
    except Exception as exc:
        with _STATES_LOCK:
            _mark_failed_locked(model_id, str(exc))
        raise LocalBootstrapStartError(str(exc)) from exc
    return {"state": "downloading"}, 202


def _blocked_reason(availability: dict[str, bool | float | int | str]) -> str:
    if not availability["platform_supported"]:
        return str(availability["reason"])
    reason = str(availability["reason"])
    if reason.startswith("insufficient RAM"):
        return reason
    return ""


def _run_bootstrap_worker(model: str) -> None:
    spec = LOCAL_MODEL_SPECS[model]
    try:
        local_install.install_llama_server()
        _set_progress(model, min(1, spec.size_bytes), spec.size_bytes)
        local_install.install_model(model)
        _mark_verifying(model)
        _set_progress(model, spec.size_bytes, spec.size_bytes)
        with _STATES_LOCK:
            if _STATES[model].state == "verifying":
                _mark_installed_locked(model)
    except Exception as exc:
        logger.exception("local provider bootstrap failed")
        with _STATES_LOCK:
            _mark_failed_locked(model, str(exc))
