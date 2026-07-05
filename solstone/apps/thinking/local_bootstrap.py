# SPDX-License-Identifier: AGPL-3.0-only
# Copyright (c) 2026 sol pbc

"""Local provider first-run bootstrap helpers for Thinking."""

from __future__ import annotations

import logging
import sys
import threading

from solstone.apps.thinking.install_copy import (
    INSTALL_FAILED_NO_PROGRESS,
    LOCAL_MEMORY_WARNING_LOW_TEMPLATE,
    LOCAL_MEMORY_WARNING_UNKNOWN,
    LOCAL_MLX_MEMORY_WARNING_UNKNOWN,
)
from solstone.think.callosum import callosum_send
from solstone.think.models import LOCAL_MODEL, QWEN_35_9B
from solstone.think.providers import local_install, mlx_install
from solstone.think.providers.fit_report import FitReport
from solstone.think.providers.install_state import (
    IN_FLIGHT_STATES,
    InstallStatus,
    is_stalled,
    make_idle_status,
    read_install_status,
    transition_state,
    write_install_status,
)
from solstone.think.providers.local import (
    LOCAL_MODEL_SPECS,
    LocalModelSpec,
    LocalProviderError,
    normalize_model_id,
)
from solstone.think.providers.local_endpoint import resolve_local_endpoint
from solstone.think.providers.memory import (
    MLX_AVAILABLE_FLOOR_BYTES,
    assess_memory,
    gb,
    gb_label,
    read_total_bytes,
)

logger = logging.getLogger(__name__)

_INSTALL_THREADS: dict[str, threading.Thread] = {}
_INSTALL_PROGRESS: dict[str, tuple[int | None, int | None]] = {}
_INSTALL_LOCK = threading.Lock()
_MLX_MODEL_LABEL = f"qwen 3.5 9B VLM — {gb_label(MLX_AVAILABLE_FLOOR_BYTES)} GB"


class LocalBootstrapUnavailableError(RuntimeError):
    """Raised when the host cannot run the local provider."""


class LocalBootstrapStartError(RuntimeError):
    """Raised when the bootstrap worker could not be started."""


def _is_mlx_backend() -> bool:
    return sys.platform == "darwin"


def _resolve_model_id(model: str | None) -> str:
    if _is_mlx_backend():
        return QWEN_35_9B
    return normalize_model_id(model)


def accepted_request_model(model: str | None) -> str | None:
    """Return the canonical local model id for this backend, if recognized."""
    candidate = model or LOCAL_MODEL
    if _is_mlx_backend():
        return QWEN_35_9B if candidate in {LOCAL_MODEL, QWEN_35_9B} else None
    return candidate if candidate in LOCAL_MODEL_SPECS else None


def local_model_ids() -> list[str]:
    """Selectable canonical model ids for this backend."""
    if _is_mlx_backend():
        return [QWEN_35_9B]
    return list(LOCAL_MODEL_SPECS)


def list_local_models() -> list[dict[str, object]]:
    """Return backend-aware local model descriptors for Settings."""
    if _is_mlx_backend():
        spec = mlx_install.resolve_model_spec()
        return [
            {
                "name": spec.name,
                "label": _MLX_MODEL_LABEL,
                "min_ram_gb": MLX_AVAILABLE_FLOOR_BYTES // 1024**3,
                "size_bytes": spec.size_bytes,
            }
        ]
    return [
        {
            "name": name,
            "label": "qwen 3.5 4B VLM — 8 GB",
            "min_ram_gb": spec.min_ram_bytes // 1024**3,
            "size_bytes": spec.size_bytes,
        }
        for name, spec in LOCAL_MODEL_SPECS.items()
    ]


def check_binary_present() -> bool:
    """Return whether the pinned llama-server binary is installed."""
    try:
        return bool(local_install.inspect_readiness(LOCAL_MODEL)["binary_installed"])
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


def _download_bytes_for_local_spec(spec: LocalModelSpec) -> int:
    return int(spec.size_bytes + (spec.mmproj_size_bytes or 0))


def get_availability_payload(model: str) -> dict[str, bool | float | int | str | None]:
    """Return the local provider availability payload used by Settings."""
    model_id = _resolve_model_id(model)
    if _is_mlx_backend():
        spec = mlx_install.resolve_model_spec(model_id)
        readiness = mlx_install.inspect_readiness(model_id)
        memory_verdict = assess_memory(
            MLX_AVAILABLE_FLOOR_BYTES, block_below_floor=True
        )
        total_memory_bytes = read_total_bytes()
        min_ram_gb = MLX_AVAILABLE_FLOOR_BYTES // 1024**3
        memory_blocked = memory_verdict.severity == "blocked"
        available = bool(
            readiness["platform_supported"]
            and readiness["package_available"]
            and not memory_blocked
            and readiness["model_installed"]
        )
        warning = (
            LOCAL_MLX_MEMORY_WARNING_UNKNOWN
            if memory_verdict.severity == "warning"
            else ""
        )
        if not readiness["platform_supported"]:
            reason = "requires Apple Silicon macOS"
        elif memory_blocked:
            assert memory_verdict.available_bytes is not None
            reason = (
                "insufficient RAM "
                f"(need {gb_label(memory_verdict.required_bytes)} GB available, "
                f"have {gb_label(memory_verdict.available_bytes)} GB available)"
            )
        elif not readiness["package_available"]:
            reason = "mlx-vlm runtime is not installed"
        elif not readiness["model_installed"]:
            reason = "local model files are not installed"
        else:
            reason = ""
        return {
            "model": readiness["model_id"],
            "platform_supported": readiness["platform_supported"],
            "total_memory_gb": gb(total_memory_bytes),
            "available_memory_gb": gb(memory_verdict.available_bytes),
            "min_ram_gb": min_ram_gb,
            "binary_present": readiness["package_available"],
            "model_present": readiness["model_installed"],
            "available": available,
            "reason": reason,
            "warning": warning,
            "download_bytes": spec.size_bytes,
        }

    spec = LOCAL_MODEL_SPECS[model_id]
    binary_present = check_binary_present()
    model_present = check_model_present(model_id)
    platform_supported, reason = _platform_supported()
    total_memory_gb = gb(read_total_bytes())
    memory_verdict = assess_memory(spec.min_ram_bytes, block_below_floor=False)
    warning = ""
    if memory_verdict.severity == "warning":
        if memory_verdict.available_bytes is None:
            warning = LOCAL_MEMORY_WARNING_UNKNOWN
        else:
            warning = LOCAL_MEMORY_WARNING_LOW_TEMPLATE.format(
                ram_gb=spec.min_ram_bytes // 1024**3
            )

    if not platform_supported:
        available = False
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
        "available_memory_gb": gb(memory_verdict.available_bytes),
        "min_ram_gb": spec.min_ram_bytes // 1024**3,
        "binary_present": binary_present,
        "model_present": model_present,
        "available": available,
        "reason": reason,
        "warning": warning,
        "download_bytes": _download_bytes_for_local_spec(spec),
    }


def _read_status() -> InstallStatus:
    return read_install_status(scope="bundled", name=local_install.LOCAL_PROVIDER_NAME)


def _write_status(status: InstallStatus) -> InstallStatus:
    write_install_status(status, scope="bundled")
    return status


def _has_live_thread(model: str) -> bool:
    with _INSTALL_LOCK:
        thread = _INSTALL_THREADS.get(model)
    return thread is not None and thread.is_alive()


def _set_progress(model: str, received: int | None, total: int | None) -> None:
    received = None if received is None else max(0, int(received))
    total = None if total is None else max(0, int(total))
    with _INSTALL_LOCK:
        _INSTALL_PROGRESS[model] = (received, total)


def _clear_progress(model: str) -> None:
    with _INSTALL_LOCK:
        _INSTALL_PROGRESS.pop(model, None)


def _payload_for_status(
    model: str, status: InstallStatus
) -> dict[str, int | str | None]:
    if status["install_state"] in IN_FLIGHT_STATES:
        with _INSTALL_LOCK:
            received, total = _INSTALL_PROGRESS.get(
                model,
                (
                    status["progress_bytes_received"],
                    status["progress_bytes_total"],
                ),
            )
    else:
        received, total = None, None

    return {
        **status,
        "progress_bytes_received": received,
        "progress_bytes_total": total,
    }


def _normalize_stalled_status(model: str, status: InstallStatus) -> InstallStatus:
    # Local downloads refresh progress per chunk, so stale status fails only without a live worker.
    if is_stalled(status) and not _has_live_thread(model):
        status = transition_state(
            status,
            new_state="failed",
            error=INSTALL_FAILED_NO_PROGRESS,
        )
        _write_status(status)
        _clear_progress(model)
    return status


def get_state(model: str) -> dict[str, int | str | None]:
    """Return the serialized bootstrap state, applying stall detection."""
    model_id = _resolve_model_id(model)
    status = _normalize_stalled_status(model_id, _read_status())
    return _payload_for_status(model_id, status)


def start_bootstrap(model: str) -> tuple[dict[str, str], int]:
    """Start the local provider bootstrap worker if needed."""
    if not resolve_local_endpoint().is_bundled:
        logger.info("local bootstrap refused: BYO local endpoint is active")
        raise LocalBootstrapUnavailableError("BYO local endpoint is active")

    model_id = _resolve_model_id(model)
    get_state(model_id)
    status = _read_status()
    if status["install_state"] == "installed":
        return {"install_state": "installed"}, 200

    availability = get_availability_payload(model_id)
    installed = bool(availability["binary_present"] and availability["model_present"])
    with _INSTALL_LOCK:
        status = _read_status()
        if status["install_state"] == "installed":
            return {"install_state": "installed"}, 200

        if status["install_state"] == "idle" and installed:
            _write_status(
                transition_state(
                    make_idle_status(local_install.LOCAL_PROVIDER_NAME),
                    new_state="installed",
                )
            )
            _INSTALL_PROGRESS.pop(model_id, None)
            return {"install_state": "installed"}, 200

        if status["install_state"] in IN_FLIGHT_STATES:
            return {"install_state": status["install_state"]}, 200

        # Only genuinely-missing artifacts reach here: build the host-fit report
        # and gate the download. Already-installed/in-flight paths returned above
        # without ever constructing a fit report.
        report = _fit_report_for_model(model_id)
        blocked_reason = _blocked_reason(report)
        if blocked_reason:
            raise LocalBootstrapUnavailableError(blocked_reason)

        disk_reason = _disk_blocked_reason(report)
        if disk_reason:
            raise LocalBootstrapUnavailableError(disk_reason)

        try:
            worker = (
                _mlx_bootstrap_worker if _is_mlx_backend() else _run_bootstrap_worker
            )
            thread = threading.Thread(
                target=worker,
                args=(model_id,),
                name=f"local-provider-bootstrap-{model_id}",
                daemon=True,
            )
        except Exception as exc:
            _write_status(transition_state(status, new_state="failed", error=str(exc)))
            _INSTALL_PROGRESS.pop(model_id, None)
            raise LocalBootstrapStartError(str(exc)) from exc

        _write_status(transition_state(status, new_state="downloading"))
        _INSTALL_THREADS[model_id] = thread

    try:
        thread.start()
    except Exception as exc:
        with _INSTALL_LOCK:
            if _INSTALL_THREADS.get(model_id) is thread:
                _INSTALL_THREADS.pop(model_id, None)
        _write_status(
            transition_state(_read_status(), new_state="failed", error=str(exc))
        )
        _clear_progress(model_id)
        raise LocalBootstrapStartError(str(exc)) from exc
    return {"install_state": "downloading"}, 202


def _fit_report_for_model(model_id: str) -> FitReport:
    from solstone.think.providers import fit_report

    if _is_mlx_backend():
        return fit_report.build_mlx_fit_report(model_id)
    return fit_report.build_local_fit_report(model_id)


def _blocked_reason(report: FitReport) -> str:
    for check in report.checks:
        if check.name == "disk":
            continue
        if check.severity == "blocked":
            return check.detail
    return ""


def _disk_blocked_reason(report: FitReport) -> str:
    for check in report.checks:
        if check.name == "disk" and check.severity == "blocked":
            return check.detail
    return ""


def _mlx_bootstrap_worker(model: str) -> None:
    current_thread = threading.current_thread()
    try:
        mlx_install.install_local_mlx(model)
    except Exception:
        logger.exception("local MLX provider bootstrap failed")
        _clear_progress(model)
    finally:
        with _INSTALL_LOCK:
            if _INSTALL_THREADS.get(model) is current_thread:
                _INSTALL_THREADS.pop(model, None)


def _request_local_server_start() -> None:
    """Best-effort: ask the supervisor to start the local server. Never raises."""
    try:
        if not callosum_send("supervisor", "start_local"):
            logger.warning("could not request local server start: callosum send failed")
    except Exception:
        logger.exception("could not request local server start")


def _run_bootstrap_worker(model: str) -> None:
    current_thread = threading.current_thread()
    try:
        local_install.install_llama_server()
        _write_status(transition_state(_read_status(), new_state="downloading"))
        local_install.install_model(model)
    except Exception as exc:
        logger.exception("local provider bootstrap failed")
        _write_status(
            transition_state(_read_status(), new_state="failed", error=str(exc))
        )
        _clear_progress(model)
    else:
        logger.info("local provider bootstrap complete; requesting local server start")
        _request_local_server_start()
    finally:
        with _INSTALL_LOCK:
            if _INSTALL_THREADS.get(model) is current_thread:
                _INSTALL_THREADS.pop(model, None)
