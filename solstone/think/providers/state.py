# SPDX-License-Identifier: AGPL-3.0-only
# Copyright (c) 2026 sol pbc

"""Provider configuration status helpers for setup surfaces."""

from __future__ import annotations

import os
import sys


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


def _is_darwin() -> bool:
    return sys.platform == "darwin"


def local_status_dict() -> dict:
    """Build the local provider setup status dict."""
    from solstone.think.models import is_local_provider_needed
    from solstone.think.providers.local_endpoint import (
        probe_local_endpoint,
        resolve_local_endpoint,
    )

    endpoint = resolve_local_endpoint()
    selected = is_local_provider_needed()
    if not endpoint.is_bundled:
        reachable, _ = probe_local_endpoint(endpoint)
        return {
            "configured": True,
            "selected": selected,
            "generate_ready": reachable,
            "cogitate_ready": reachable,
            "issues": [] if reachable else ["local_endpoint_unreachable"],
        }

    if _is_darwin():
        from solstone.think.providers import local_server, mlx_install

        readiness = mlx_install.inspect_readiness()
        runtime_available = bool(readiness.host["package_available"])
        model_installed = bool(readiness.artifacts["model_installed"])
        configured = runtime_available and model_installed

        if not selected:
            return {
                "configured": configured,
                "selected": False,
                "generate_ready": False,
                "cogitate_ready": False,
                "issues": [],
            }

        issues: list[str] = []
        server_healthy = local_server.is_healthy()
        if not runtime_available:
            issues.append("runtime_missing")
        if not model_installed:
            issues.append("model_missing")
        if configured and not server_healthy:
            issues.append("server_unhealthy")

        ready = configured and server_healthy
        return {
            "configured": configured,
            "selected": True,
            "generate_ready": ready,
            "cogitate_ready": ready,
            "issues": issues,
        }

    from solstone.think.providers import local_install, local_server

    readiness = local_install.inspect_readiness()
    binary_installed = bool(readiness.artifacts["binary_installed"])
    model_installed = bool(readiness.artifacts["model_installed"])
    configured = binary_installed and model_installed

    if not selected:
        return {
            "configured": configured,
            "selected": False,
            "generate_ready": False,
            "cogitate_ready": False,
            "issues": [],
        }

    issues: list[str] = []
    server_healthy = local_server.is_healthy()
    if not readiness.host.get("gpu_available", True):
        issues.append("gpu_unavailable")
    if readiness.proof["binary"]["status"] == "missing-or-mismatched":
        issues.append("binary_missing")
    if readiness.proof["model"]["status"] == "missing-or-mismatched":
        issues.append("model_missing")
    if configured and not server_healthy:
        runnable, detail = local_install.probe_binary_runnable(
            readiness.artifacts["binary_path"]
        )
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
        "issues": issues,
    }


__all__ = ["cloud_key_configured", "local_status_dict"]
