# SPDX-License-Identifier: AGPL-3.0-only
# Copyright (c) 2026 sol pbc

"""Hosted-tier binding storage + per-run broker credential fetch."""

from __future__ import annotations

import json
import logging
import socket
import ssl
import urllib.error
import urllib.request
from dataclasses import dataclass
from pathlib import Path

from solstone import __version__ as solstone_version
from solstone.think.backup.destination import Destination
from solstone.think.journal_io import write_json
from solstone.think.utils import get_journal

BROKER_TIMEOUT_SECONDS = 30

logger = logging.getLogger("solstone.backup.hosted")


@dataclass(frozen=True)
class HostedBinding:
    broker_endpoint: str
    account_id: str
    instance_id: str
    bucket: str
    prefix: str
    broker_token: str

    def __repr__(self) -> str:
        return (
            "HostedBinding("
            f"broker_endpoint={self.broker_endpoint!r}, "
            f"account_id={self.account_id!r}, "
            f"instance_id={self.instance_id!r}, "
            f"bucket={self.bucket!r}, "
            f"prefix={self.prefix!r}, "
            "broker_token=<redacted>"
            ")"
        )


@dataclass(frozen=True)
class HostedCredentials:
    access_key_id: str
    secret_access_key: str
    session_token: str
    endpoint: str
    expires_at: str

    def __repr__(self) -> str:
        return (
            "HostedCredentials("
            "access_key_id=<redacted>, "
            "secret_access_key=<redacted>, "
            "session_token=<redacted>, "
            f"endpoint={self.endpoint!r}"
            ")"
        )


class HostedCredsUnavailable(RuntimeError):
    def __init__(self, reason_code: str):
        super().__init__(reason_code)
        self.reason_code = reason_code


def hosted_dir() -> Path:
    path = Path(get_journal()) / "backup" / "hosted"
    path.mkdir(parents=True, exist_ok=True)
    return path


def hosted_binding_path() -> Path:
    return hosted_dir() / "binding.json"


def _hosted_binding_read_path() -> Path:
    return Path(get_journal()) / "backup" / "hosted" / "binding.json"


def _non_blank_string(payload: dict[str, object], key: str) -> str | None:
    value = payload.get(key)
    if not isinstance(value, str) or not value.strip():
        return None
    return value


def load_hosted_binding() -> HostedBinding | None:
    path = _hosted_binding_read_path()
    if not path.exists():
        return None
    try:
        raw = json.loads(path.read_text("utf-8"))
    except (json.JSONDecodeError, OSError):
        return None
    if not isinstance(raw, dict):
        return None

    broker_endpoint = _non_blank_string(raw, "broker_endpoint")
    account_id = _non_blank_string(raw, "account_id")
    instance_id = _non_blank_string(raw, "instance_id")
    bucket = _non_blank_string(raw, "bucket")
    prefix = _non_blank_string(raw, "prefix")
    broker_token = _non_blank_string(raw, "broker_token")
    if (
        broker_endpoint is None
        or account_id is None
        or instance_id is None
        or bucket is None
        or prefix is None
        or broker_token is None
    ):
        return None

    return HostedBinding(
        broker_endpoint=broker_endpoint,
        account_id=account_id,
        instance_id=instance_id,
        bucket=bucket,
        prefix=prefix,
        broker_token=broker_token,
    )


def save_hosted_binding(binding: HostedBinding) -> None:
    write_json(
        hosted_binding_path(),
        {
            "broker_endpoint": binding.broker_endpoint,
            "account_id": binding.account_id,
            "instance_id": binding.instance_id,
            "bucket": binding.bucket,
            "prefix": binding.prefix,
            "broker_token": binding.broker_token,
        },
        indent=2,
        mode=0o600,
    )


def delete_hosted_binding() -> None:
    hosted_binding_path().unlink(missing_ok=True)


def _needs_subscription(payload: object) -> bool:
    if not isinstance(payload, dict):
        return False
    if payload.get("needs_subscription") is True:
        return True
    for key in ("error", "reason", "code", "status"):
        if payload.get(key) == "needs_subscription":
            return True
    return False


def _parse_json_body(raw_body: bytes) -> dict[str, object] | None:
    try:
        payload = json.loads(raw_body.decode("utf-8"))
    except (json.JSONDecodeError, UnicodeDecodeError):
        return None
    return payload if isinstance(payload, dict) else None


def _read_http_error_payload(exc: urllib.error.HTTPError) -> dict[str, object] | None:
    try:
        raw_body = exc.read(8192)
    except OSError:
        return None
    return _parse_json_body(raw_body)


def fetch_hosted_credentials(
    binding: HostedBinding,
    *,
    scope: str,
) -> HostedCredentials:
    request = urllib.request.Request(
        f"{binding.broker_endpoint.rstrip('/')}/backup/credentials",
        headers={
            "Authorization": f"Bearer {binding.broker_token}",
            "Content-Type": "application/json",
            "User-Agent": f"solstone-backup/{solstone_version}",
            "Connection": "close",
        },
        data=json.dumps({"scope": scope}).encode("utf-8"),
        method="POST",
    )
    try:
        with urllib.request.urlopen(
            request,
            timeout=BROKER_TIMEOUT_SECONDS,
        ) as response:
            status = int(getattr(response, "status", response.getcode()))
            raw_body = response.read()
    except urllib.error.HTTPError as exc:
        if int(exc.code) == 402:
            raise HostedCredsUnavailable("hosted_entitlement_inactive") from None
        payload = _read_http_error_payload(exc)
        if _needs_subscription(payload):
            raise HostedCredsUnavailable("hosted_entitlement_inactive") from None
        raise HostedCredsUnavailable("broker_error") from None
    except ssl.SSLError as exc:
        raise HostedCredsUnavailable("broker_unreachable") from exc
    except urllib.error.URLError as exc:
        raise HostedCredsUnavailable("broker_unreachable") from exc
    except (socket.timeout, TimeoutError) as exc:
        raise HostedCredsUnavailable("broker_unreachable") from exc

    if status < 200 or status >= 300:
        raise HostedCredsUnavailable("broker_error")

    payload = _parse_json_body(raw_body)
    if payload is None:
        logger.warning("hosted credential broker response was invalid")
        raise HostedCredsUnavailable("broker_error")
    if _needs_subscription(payload):
        raise HostedCredsUnavailable("hosted_entitlement_inactive")

    access_key_id = _non_blank_string(payload, "access_key_id")
    secret_access_key = _non_blank_string(payload, "secret_access_key")
    session_token = _non_blank_string(payload, "session_token")
    endpoint = _non_blank_string(payload, "endpoint")
    expires_at = _non_blank_string(payload, "expires_at")
    if (
        access_key_id is None
        or secret_access_key is None
        or session_token is None
        or endpoint is None
        or expires_at is None
    ):
        logger.warning("hosted credential broker response was incomplete")
        raise HostedCredsUnavailable("broker_error")

    return HostedCredentials(
        access_key_id=access_key_id,
        secret_access_key=secret_access_key,
        session_token=session_token,
        endpoint=endpoint,
        expires_at=expires_at,
    )


def operated_repository(binding: HostedBinding, creds: HostedCredentials) -> str:
    return f"s3:{creds.endpoint.rstrip('/')}/{binding.bucket}/{binding.prefix}"


def operated_destination(
    binding: HostedBinding, creds: HostedCredentials
) -> Destination:
    return Destination(
        repository=operated_repository(binding, creds),
        backend="s3",
        credentials={
            "access_key_id": creds.access_key_id,
            "secret_access_key": creds.secret_access_key,
            "session_token": creds.session_token,
        },
    )


__all__ = [
    "BROKER_TIMEOUT_SECONDS",
    "HostedBinding",
    "HostedCredentials",
    "HostedCredsUnavailable",
    "delete_hosted_binding",
    "fetch_hosted_credentials",
    "hosted_binding_path",
    "hosted_dir",
    "load_hosted_binding",
    "operated_destination",
    "operated_repository",
    "save_hosted_binding",
]
