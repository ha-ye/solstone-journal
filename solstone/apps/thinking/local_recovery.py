# SPDX-License-Identifier: AGPL-3.0-only
# Copyright (c) 2026 sol pbc

"""Owner-safe local runtime recovery projection and actions."""

from __future__ import annotations

from typing import Any

from solstone.think.providers.runtime_health import (
    inspect_retry_token,
    inspect_runtime_health,
    request_runtime_retry,
)

_TRANSIENT_PHASES = frozenset(
    {
        "observing",
        "artifact-not-ready",
        "host-blocked",
        "starting",
        "warming",
        "backoff",
        "retry-requested",
        "stop-deferred",
        "stopping",
    }
)


def runtime_view() -> dict[str, Any]:
    """Return the small owner-safe projection consumed by Thinking."""
    health_inspection = inspect_runtime_health("local")
    retry_inspection = inspect_retry_token("local")

    unavailable = _unavailable_view(health_inspection, retry_inspection)
    if unavailable is not None:
        return unavailable

    health = health_inspection["record"]
    retry = retry_inspection["record"]
    assert isinstance(health, dict)
    assert isinstance(retry, dict)

    current_health = health
    current_retry = retry
    fingerprint = current_health["desired_fingerprint_sha256"]
    retry_present = current_retry["token_id"] is not None
    retry_matches = (
        retry_present and current_retry["desired_fingerprint_sha256"] == fingerprint
    )
    phase = current_health["phase"]
    reason_code = current_health["reason_code"]
    poll = phase in _TRANSIENT_PHASES

    if retry_matches:
        phase = "retry-requested"
        reason_code = "retry-token-requested"
        poll = True
    return {
        "status": "ok",
        "phase": phase,
        "reason_code": reason_code,
        "health_revision": current_health["revision"],
        "desired_fingerprint_sha256": fingerprint,
        "retry_revision": current_retry["revision"],
        "retry_pending": retry_matches,
        "can_retry": (
            current_health["phase"] == "failed"
            and fingerprint is not None
            and not retry_matches
        ),
        "poll": poll,
        "updated_at": current_health["updated_at"],
    }


def request_retry(
    *,
    health_revision: int,
    retry_revision: int,
    desired_fingerprint_sha256: str,
) -> dict[str, Any]:
    """Request one guarded retry, then return the committed projection."""
    request_runtime_retry(
        "local",
        expected_health_revision=health_revision,
        expected_retry_revision=retry_revision,
        desired_fingerprint_sha256=desired_fingerprint_sha256,
        owner={
            "module": "solstone.apps.thinking.local_recovery",
            "source": "owner-recovery",
        },
    )
    return runtime_view()


def _unavailable_view(
    health_inspection: dict[str, Any],
    retry_inspection: dict[str, Any],
) -> dict[str, Any] | None:
    statuses = {
        str(health_inspection["status"]),
        str(retry_inspection["status"]),
    }
    if "unavailable" in statuses:
        return _failed_read_view("unavailable", "state-unavailable")
    if "corrupt" in statuses:
        return _failed_read_view("corrupt", "state-corrupt")
    return None


def _failed_read_view(status: str, phase: str) -> dict[str, Any]:
    return {
        "status": status,
        "phase": phase,
        "reason_code": (
            "record-unavailable" if status == "unavailable" else "record-malformed"
        ),
        "health_revision": None,
        "desired_fingerprint_sha256": None,
        "retry_revision": None,
        "retry_pending": False,
        "can_retry": False,
        "poll": False,
        "updated_at": None,
    }
