# SPDX-License-Identifier: AGPL-3.0-only
# Copyright (c) 2026 sol pbc

"""Backup destination model, backend credentials, and sanitized probes."""

from __future__ import annotations

import logging
from dataclasses import dataclass
from pathlib import Path

from solstone.think.backup.runner import run_restic

logger = logging.getLogger("solstone.backup.destination")


@dataclass(frozen=True)
class Destination:
    repository: str
    backend: str
    credentials: dict[str, str]


@dataclass(frozen=True)
class DestinationStatus:
    reachable: bool
    repo_exists: bool
    reason_code: str
    message: str


_STATUS_BY_RETURNCODE = {
    0: DestinationStatus(
        reachable=True,
        repo_exists=True,
        reason_code="repo_exists",
        message="backup repository is reachable",
    ),
    10: DestinationStatus(
        reachable=True,
        repo_exists=False,
        reason_code="repo_missing",
        message="backup destination is reachable and needs setup",
    ),
    12: DestinationStatus(
        reachable=True,
        repo_exists=True,
        reason_code="auth_failed",
        message="repository password was rejected",
    ),
    11: DestinationStatus(
        reachable=True,
        repo_exists=True,
        reason_code="locked",
        message="repository is locked; try again shortly",
    ),
    124: DestinationStatus(
        reachable=False,
        repo_exists=False,
        reason_code="timeout",
        message="could not reach the backup destination",
    ),
}
_UNREACHABLE_STATUS = DestinationStatus(
    reachable=False,
    repo_exists=False,
    reason_code="unreachable",
    message="could not reach the backup destination",
)


def _require_credential(credentials: dict[str, str], key: str) -> str:
    try:
        return credentials[key]
    except KeyError as exc:
        raise KeyError(f"missing backup credential: {key}") from exc


def assemble_backend_env(destination: Destination) -> dict[str, str]:
    credentials = destination.credentials
    if destination.backend == "s3":
        return {
            "AWS_ACCESS_KEY_ID": _require_credential(
                credentials,
                "access_key_id",
            ),
            "AWS_SECRET_ACCESS_KEY": _require_credential(
                credentials,
                "secret_access_key",
            ),
        }
    if destination.backend == "b2":
        return {
            "B2_ACCOUNT_ID": _require_credential(credentials, "account_id"),
            "B2_ACCOUNT_KEY": _require_credential(credentials, "account_key"),
        }
    raise ValueError(f"unsupported backup backend: {destination.backend!r}")


def validate_destination(
    destination: Destination,
    password: str,
    *,
    restic_path: Path,
    timeout: float | None = None,
) -> DestinationStatus:
    result = run_restic(
        ["cat", "config"],
        repository=destination.repository,
        password=password,
        restic_path=restic_path,
        backend_env=assemble_backend_env(destination),
        timeout=timeout,
    )
    status = _STATUS_BY_RETURNCODE.get(result.returncode, _UNREACHABLE_STATUS)
    logger.debug(
        "backup destination probe completed returncode=%s reason_code=%s",
        result.returncode,
        status.reason_code,
    )
    return status


__all__ = [
    "Destination",
    "DestinationStatus",
    "assemble_backend_env",
    "validate_destination",
]
