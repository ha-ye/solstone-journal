# SPDX-License-Identifier: AGPL-3.0-only
# Copyright (c) 2026 sol pbc

"""Operation-scoped credential sessions for hosted restic runs."""

from __future__ import annotations

from collections.abc import Iterator, Mapping
from contextlib import contextmanager
from dataclasses import dataclass
from pathlib import Path

from solstone.think.backup.destination import Destination, assemble_backend_env
from solstone.think.backup.hosted import (
    HostedBinding,
    HostedCredentials,
    operated_destination,
)


@dataclass(frozen=True)
class HostedResticSession:
    destination: Destination
    backend_env: Mapping[str, str]
    global_options: tuple[str, ...] = ()


@contextmanager
def hosted_restic_session(
    binding: HostedBinding,
    *,
    initial_credentials: HostedCredentials,
) -> Iterator[HostedResticSession]:
    destination = operated_destination(binding, initial_credentials)
    yield HostedResticSession(
        destination=destination,
        backend_env=assemble_backend_env(destination),
    )


@contextmanager
def hosted_append_only_restic_session(
    binding: HostedBinding,
    *,
    rclone_path: Path,
    initial_credentials: HostedCredentials,
) -> Iterator[HostedResticSession]:
    """Serve an operated repo through rclone's lock-aware append-only adapter."""
    backend_env = {
        "RCLONE_CONFIG_SPB_TYPE": "s3",
        "RCLONE_CONFIG_SPB_PROVIDER": "Cloudflare",
        "RCLONE_CONFIG_SPB_ENV_AUTH": "false",
        "RCLONE_CONFIG_SPB_ACCESS_KEY_ID": initial_credentials.access_key_id,
        "RCLONE_CONFIG_SPB_SECRET_ACCESS_KEY": initial_credentials.secret_access_key,
        "RCLONE_CONFIG_SPB_SESSION_TOKEN": initial_credentials.session_token,
        "RCLONE_CONFIG_SPB_ENDPOINT": initial_credentials.endpoint,
        "RCLONE_CONFIG_SPB_REGION": "auto",
        "RCLONE_CONFIG_SPB_NO_CHECK_BUCKET": "true",
    }
    yield HostedResticSession(
        destination=Destination(
            repository=f"rclone:spb:{binding.bucket}/{binding.prefix}",
            backend="rclone",
            credentials={},
        ),
        backend_env=backend_env,
        global_options=(
            "-o",
            f"rclone.program={rclone_path}",
            "-o",
            "rclone.args=serve restic --stdio --append-only --config /dev/null",
        ),
    )


__all__ = [
    "HostedResticSession",
    "hosted_append_only_restic_session",
    "hosted_restic_session",
]
