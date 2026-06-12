# SPDX-License-Identifier: AGPL-3.0-only
# Copyright (c) 2026 sol pbc

"""Typed provider readiness and health-file state."""

from __future__ import annotations

import fcntl
import json
import logging
import os
from dataclasses import dataclass, replace
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Literal

from solstone.think.callosum import callosum_send
from solstone.think.providers.shared import RUNTIME_REASON_CODES
from solstone.think.utils import get_journal, now_ms

logger = logging.getLogger(__name__)

Status = Literal["ready", "blocked", "unhealthy", "unknown"]
Source = Literal[
    "config",
    "local_install",
    "local_server",
    "active_check",
    "runtime_failure",
]

READINESS_REASON_CODES = frozenset(
    {
        "provider_key_missing",
        "ram_insufficient",
        "local_model_missing",
        "local_model_installing",
        "local_model_loading",
        "gpu_unavailable",
        "local_server_unhealthy",
    }
)
REASON_CODES = READINESS_REASON_CODES | RUNTIME_REASON_CODES

_HEALTH_FILENAME = "talents.json"


@dataclass(frozen=True)
class ProviderState:
    provider: str
    interface: str
    status: Status
    model: str | None = None
    context: str | None = None
    reason_code: str | None = None
    message: str | None = None
    reset_at_ms: int | None = None
    checked_at: str | None = None
    source: Source = "config"

    def __post_init__(self) -> None:
        if self.status == "ready":
            if self.reason_code is not None:
                raise ValueError("ProviderState.ready must not carry a reason_code")
            return
        if self.reason_code is None:
            raise ValueError("ProviderState non-ready status requires reason_code")
        if self.reason_code not in REASON_CODES:
            raise ValueError(f"unknown provider reason_code: {self.reason_code}")


def _health_path() -> Path:
    return Path(get_journal()) / "health" / _HEALTH_FILENAME


def read_health_status() -> dict | None:
    """Load health status from journal/health/talents.json."""
    try:
        with open(_health_path(), encoding="utf-8") as handle:
            return json.load(handle)
    except (FileNotFoundError, json.JSONDecodeError, OSError):
        return None


def is_provider_healthy(provider: str, health_data: dict | None) -> bool:
    """Check if a provider is healthy based on health data."""
    if health_data is None:
        return True
    results = health_data.get("results", [])
    provider_results = [row for row in results if row.get("provider") == provider]
    if not provider_results:
        return True
    return any(row.get("ok") for row in provider_results)


def is_provider_model_interface_healthy(
    provider: str,
    model: str,
    interface: str,
    health_data: dict | None,
) -> bool:
    """Check health for a specific provider/model/interface row."""
    if health_data is None:
        return True
    for row in health_data.get("results", []):
        if (
            row.get("provider") == provider
            and row.get("model") == model
            and row.get("interface") == interface
            and row.get("ok") is False
        ):
            return False
    return True


def should_recheck_health(health_data: dict | None) -> bool:
    """Check if health data should be rechecked."""
    if health_data is None:
        return False
    failed_rows = [
        row for row in health_data.get("results", []) if row.get("ok") is False
    ]
    reset_values = [
        int(row["reset_at_ms"])
        for row in failed_rows
        if isinstance(row.get("reset_at_ms"), (int, float))
    ]
    missing_reset = len(reset_values) < len(failed_rows)
    if reset_values and not missing_reset:
        return now_ms() > min(reset_values)

    checked_at = health_data.get("checked_at")
    if not checked_at:
        return False
    try:
        checked_time = datetime.fromisoformat(checked_at)
        if checked_time.tzinfo is None:
            checked_time = checked_time.replace(tzinfo=timezone.utc)
        age = datetime.now(timezone.utc) - checked_time
        return age.total_seconds() > 3600
    except (ValueError, TypeError):
        return False


def _summarize_health_results(results: list[dict[str, Any]]) -> dict[str, int]:
    return {
        "total": len(results),
        "passed": sum(1 for row in results if row.get("status") == "ok"),
        "skipped": sum(1 for row in results if row.get("status") == "skip"),
        "failed": sum(1 for row in results if row.get("ok") is False),
    }


def record_quota_failure(
    provider: str,
    tier: str,
    model: str,
    interface: str,
    reset_at_ms: int,
) -> None:
    """Record a provider/model/interface quota failure in health status."""
    health_dir = _health_path().parent
    health_dir.mkdir(parents=True, exist_ok=True)
    health_path = _health_path()
    lock_path = health_dir / "talents.json.lock"
    tmp_path = health_dir / f".talents.json.{os.getpid()}.{now_ms()}.tmp"
    recorded_at = datetime.now(timezone.utc).isoformat()
    message = f"Quota exhausted; retry after {reset_at_ms}"

    with open(lock_path, "w", encoding="utf-8") as lock_file:
        fcntl.flock(lock_file, fcntl.LOCK_EX)
        try:
            try:
                with open(health_path, encoding="utf-8") as health_file:
                    payload = json.load(health_file)
            except (FileNotFoundError, json.JSONDecodeError, OSError):
                payload = {}

            results = payload.get("results", [])
            if not isinstance(results, list):
                results = []
            failure_row = {
                "provider": provider,
                "tier": tier,
                "model": model,
                "interface": interface,
                "ok": False,
                "status": "quota_exhausted",
                "reason_code": "provider_quota_exceeded",
                "message": message,
                "elapsed_s": 0.0,
                "reset_at_ms": reset_at_ms,
                "recorded_at": recorded_at,
            }

            for row in results:
                if (
                    row.get("provider") == provider
                    and row.get("model") == model
                    and row.get("interface") == interface
                ):
                    row.update(failure_row)
                    break
            else:
                results.append(failure_row)

            payload["results"] = results
            payload["summary"] = _summarize_health_results(results)
            payload.setdefault("checked_at", recorded_at)

            with open(tmp_path, "w", encoding="utf-8") as tmp_file:
                json.dump(payload, tmp_file, indent=2)
                tmp_file.write("\n")
                tmp_file.flush()
                os.fsync(tmp_file.fileno())
            os.replace(tmp_path, health_path)
        finally:
            try:
                tmp_path.unlink()
            except FileNotFoundError:
                pass


def write_active_check(
    results: list[dict],
    summary: dict,
    checked_at: str,
) -> None:
    """Write active provider check results to the canonical health file."""
    payload = {
        "results": results,
        "summary": summary,
        "checked_at": checked_at,
    }
    health_path = _health_path()
    health_path.parent.mkdir(parents=True, exist_ok=True)
    health_path.write_text(json.dumps(payload, indent=2), encoding="utf-8")


def request_recheck() -> None:
    """Request a health re-check through the supervisor."""
    ok = callosum_send(
        "supervisor",
        "request",
        cmd=["journal", "providers", "check", "--targeted"],
    )
    if not ok:
        logger.warning("request_health_recheck: callosum_send returned false")


def cloud_key_configured(env_key: str) -> bool:
    if not env_key:
        return False
    if os.getenv(env_key):
        return True
    try:
        from solstone.think.journal_config import read_journal_config

        return bool(read_journal_config().get("env", {}).get(env_key))
    except Exception:
        # Intended fail-closed-on-unreadable-config: report no cloud key.
        return False


def local_status_dict() -> dict:
    """Build the legacy local provider status dict."""
    from solstone.think.models import is_local_provider_needed
    from solstone.think.providers import local_install, local_server

    readiness = local_install.inspect_readiness()
    binary_installed = bool(readiness["binary_installed"])
    model_installed = bool(readiness["model_installed"])
    selected = is_local_provider_needed()
    configured = binary_installed and model_installed

    if not selected:
        return {
            "configured": configured,
            "selected": False,
            "generate_ready": False,
            "cogitate_ready": False,
            "cogitate_cli": "llama-server",
            "cogitate_cli_found": binary_installed,
            "issues": [],
        }

    issues: list[str] = []
    server_healthy = local_server.is_healthy()
    if not readiness.get("gpu_available", True):
        issues.append("gpu_unavailable")
    if not binary_installed:
        issues.append("binary_missing")
    if not model_installed:
        issues.append("model_missing")
    if configured and not server_healthy:
        runnable, detail = local_install.probe_binary_runnable(readiness["binary_path"])
        if runnable:
            issues.append("server_unhealthy")
        else:
            issues.append(f"failed to launch: {detail}")
            issues.append(f"run `{local_install.install_hint()}`")
    if "binary_missing" in issues or "model_missing" in issues:
        issues.append(f"run `{local_install.install_hint()}`")

    ready = configured and server_healthy
    return {
        "configured": configured,
        "selected": True,
        "generate_ready": ready,
        "cogitate_ready": ready,
        "cogitate_cli": "llama-server",
        "cogitate_cli_found": binary_installed,
        "issues": issues,
    }


def _ready_state(
    provider: str,
    interface: str,
    *,
    model: str | None = None,
    context: str | None = None,
    checked_at: str | None = None,
    source: Source = "active_check",
) -> ProviderState:
    return ProviderState(
        provider=provider,
        interface=interface,
        status="ready",
        model=model,
        context=context,
        checked_at=checked_at,
        source=source,
    )


def _state(
    provider: str,
    interface: str,
    status: Status,
    reason_code: str,
    *,
    model: str | None = None,
    context: str | None = None,
    message: str | None = None,
    reset_at_ms: int | None = None,
    checked_at: str | None = None,
    source: Source = "config",
) -> ProviderState:
    return ProviderState(
        provider=provider,
        interface=interface,
        status=status,
        model=model,
        context=context,
        reason_code=reason_code,
        message=message,
        reset_at_ms=reset_at_ms,
        checked_at=checked_at,
        source=source,
    )


def _reason_code(value: Any, fallback: str) -> str:
    if isinstance(value, str) and value in REASON_CODES:
        return value
    return fallback


def _matching_health_row(
    health_data: dict | None,
    provider: str,
    interface: str,
    model: str | None,
) -> dict[str, Any] | None:
    if health_data is None:
        return None
    rows = [
        row
        for row in health_data.get("results", [])
        if row.get("provider") == provider
        and row.get("interface") == interface
        and (model is None or row.get("model") == model)
    ]
    if not rows and model is None:
        rows = [
            row
            for row in health_data.get("results", [])
            if row.get("provider") == provider
        ]
    if not rows:
        return None
    for row in rows:
        if row.get("ok") is False:
            return row
    return rows[0]


def _local_readiness_for_provider(
    provider: str,
    interface: str,
    model: str | None,
) -> ProviderState:
    from solstone.think.models import LOCAL_MODEL
    from solstone.think.providers import local_install, local_server
    from solstone.think.providers.install_state import IN_FLIGHT_STATES

    selected_model = model or LOCAL_MODEL
    readiness = local_install.inspect_readiness(selected_model)
    model_id = str(readiness.get("model_id") or selected_model)

    if readiness["install_state"] in IN_FLIGHT_STATES:
        return _state(
            provider,
            interface,
            "blocked",
            "local_model_installing",
            model=model_id,
            message=str(readiness["install_state"]),
            source="local_install",
        )

    if not readiness.get("gpu_available", True):
        return _state(
            provider,
            interface,
            "blocked",
            "gpu_unavailable",
            model=model_id,
            source="local_install",
        )

    if not readiness["binary_installed"] or not readiness["model_installed"]:
        return _state(
            provider,
            interface,
            "blocked",
            "local_model_missing",
            model=model_id,
            message=str(readiness.get("install_error") or "") or None,
            source="local_install",
        )

    server_state, server_error = local_server.probe_state()
    if server_state == local_server.STATE_LOADING:
        return _state(
            provider,
            interface,
            "blocked",
            "local_model_loading",
            model=model_id,
            message=server_error,
            source="local_server",
        )
    if server_state == local_server.STATE_READY:
        return _ready_state(
            provider,
            interface,
            model=model_id,
            source="local_server",
        )
    return _state(
        provider,
        interface,
        "unhealthy",
        "local_server_unhealthy",
        model=model_id,
        message=server_error,
        source="local_server",
    )


def _cloud_readiness_for_provider(
    provider: str,
    interface: str,
    model: str | None,
) -> ProviderState:
    from solstone.think.providers import PROVIDER_METADATA

    env_key = PROVIDER_METADATA.get(provider, {}).get("env_key", "")
    if not cloud_key_configured(env_key):
        return _state(
            provider,
            interface,
            "blocked",
            "provider_key_missing",
            model=model,
            message=f"{env_key} not set" if env_key else None,
            source="config",
        )

    health_data = read_health_status()
    row = _matching_health_row(health_data, provider, interface, model)
    checked_at = (
        health_data.get("checked_at") if isinstance(health_data, dict) else None
    )
    if row is None:
        return _state(
            provider,
            interface,
            "unknown",
            "unknown",
            model=model,
            checked_at=checked_at,
            source="config",
        )

    if row.get("ok") is True:
        return _ready_state(
            provider,
            interface,
            model=str(row.get("model") or model) if row.get("model") or model else None,
            checked_at=checked_at,
            source="active_check",
        )

    reset_at_ms = row.get("reset_at_ms")
    parsed_reset = int(reset_at_ms) if isinstance(reset_at_ms, (int, float)) else None
    if row.get("status") == "quota_exhausted":
        status: Status = (
            "unhealthy"
            if parsed_reset is not None and parsed_reset > now_ms()
            else "unknown"
        )
        return _state(
            provider,
            interface,
            status,
            "provider_quota_exceeded",
            model=str(row.get("model") or model) if row.get("model") or model else None,
            message=str(row.get("message") or "") or None,
            reset_at_ms=parsed_reset,
            checked_at=checked_at,
            source="active_check",
        )

    return _state(
        provider,
        interface,
        "unhealthy",
        _reason_code(row.get("reason_code"), "provider_unavailable"),
        model=str(row.get("model") or model) if row.get("model") or model else None,
        message=str(row.get("message") or "") or None,
        checked_at=checked_at,
        source="active_check",
    )


def readiness_for_provider(
    provider: str,
    interface: str,
    model: str | None = None,
) -> ProviderState:
    """Return passive readiness for a provider/model/interface."""
    if provider == "local":
        return _local_readiness_for_provider(provider, interface, model)
    return _cloud_readiness_for_provider(provider, interface, model)


def readiness_for_context(context: str, interface: str) -> ProviderState:
    """Resolve a context then return passive readiness for its provider."""
    from solstone.think.models import resolve_provider

    provider, model = resolve_provider(context, interface)
    provider_state = readiness_for_provider(provider, interface, model)
    return replace(provider_state, context=context)


__all__ = [
    "ProviderState",
    "READINESS_REASON_CODES",
    "REASON_CODES",
    "RUNTIME_REASON_CODES",
    "Source",
    "Status",
    "cloud_key_configured",
    "is_provider_healthy",
    "is_provider_model_interface_healthy",
    "local_status_dict",
    "read_health_status",
    "readiness_for_context",
    "readiness_for_provider",
    "record_quota_failure",
    "request_recheck",
    "should_recheck_health",
    "write_active_check",
]
