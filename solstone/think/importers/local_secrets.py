# SPDX-License-Identifier: AGPL-3.0-only
# Copyright (c) 2026 sol pbc

"""Local machine secret storage for Solstone credentials.

This module owns local-only secret files under Application Support. It must not
write into journal content, import bundles, or any replicated journal path.
"""

from __future__ import annotations

import hashlib
import json
import os
import re
from collections.abc import Mapping
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from solstone.think.utils import get_journal

OURA_TOKEN_SCHEMA = "solstone.local_secret.oura_oauth.v1"
SECRET_SCHEMA = OURA_TOKEN_SCHEMA
SECRET_PROVIDER = "oura"
LOCAL_SECRET_SCHEMA = "solstone.local_secret.v1"
_APP_SUPPORT_RELATIVE = Path("Library", "Application Support", "Solstone", "secrets")
_OURA_RELATIVE = _APP_SUPPORT_RELATIVE / "oura"
_SAFE_INTEGRATION_RE = re.compile(r"^[a-z0-9][a-z0-9_.-]*$")

ENV_SECRET_INTEGRATIONS: Mapping[str, str] = {
    "GOOGLE_API_KEY": "google",
    "OPENAI_API_KEY": "openai",
    "ANTHROPIC_API_KEY": "anthropic",
    "REVAI_ACCESS_TOKEN": "revai",
    "PLAUD_ACCESS_TOKEN": "plaud",
}


@dataclass(frozen=True, slots=True)
class OuraTokens:
    access_token: str
    refresh_token: str
    expires_at: float
    token_type: str = "Bearer"


def load_oura_tokens() -> OuraTokens | None:
    """Load Oura OAuth tokens for the active journal, if present and valid."""
    path = _oura_token_path()
    if not path.exists():
        return None
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return None
    return _tokens_from_payload(payload)


def save_oura_tokens(tokens: OuraTokens) -> None:
    """Save Oura OAuth tokens in the local machine secret store."""
    path = _oura_token_path()
    _ensure_private_dir(path.parent)
    payload = {
        "schema": OURA_TOKEN_SCHEMA,
        "provider": SECRET_PROVIDER,
        "journal_fingerprint": _journal_fingerprint(),
        "access_token": tokens.access_token,
        "refresh_token": tokens.refresh_token,
        "expires_at": tokens.expires_at,
        "token_type": tokens.token_type,
    }
    _write_private_json(path, payload)


def delete_oura_tokens() -> None:
    """Delete the active journal's Oura OAuth token file, if present."""
    try:
        _oura_token_path().unlink()
    except FileNotFoundError:
        return


def _tokens_from_payload(payload: Any) -> OuraTokens | None:
    if not isinstance(payload, dict):
        return None
    if payload.get("schema") != OURA_TOKEN_SCHEMA or payload.get("provider") != "oura":
        return None
    access_token = payload.get("access_token")
    refresh_token = payload.get("refresh_token")
    expires_at = payload.get("expires_at")
    token_type = payload.get("token_type", "Bearer")
    if not isinstance(access_token, str) or not access_token:
        return None
    if not isinstance(refresh_token, str) or not refresh_token:
        return None
    if not isinstance(expires_at, int | float):
        return None
    if not isinstance(token_type, str) or not token_type:
        return None
    return OuraTokens(
        access_token=access_token,
        refresh_token=refresh_token,
        expires_at=float(expires_at),
        token_type=token_type,
    )


def _oura_token_path() -> Path:
    return _secret_root() / "oura" / f"{_journal_fingerprint()}.json"


def secret_path_for(
    integration: str,
    *,
    journal_path: str | Path | None = None,
) -> Path:
    """Return the local-only secret file for an integration and journal.

    The journal path is represented only by a short fingerprint, so files stay
    outside replicated journal content while remaining scoped to the journal
    they unlock.
    """

    return (
        _secret_root()
        / _safe_integration(integration)
        / f"{_journal_fingerprint(journal_path)}.json"
    )


def load_secret(
    integration: str,
    name: str,
    *,
    journal_path: str | Path | None = None,
) -> str | None:
    """Load one local-only secret value."""

    payload = _load_secret_payload(integration, journal_path=journal_path)
    secrets = payload.get("secrets")
    if not isinstance(secrets, dict):
        return None
    value = secrets.get(name)
    return value if isinstance(value, str) and value else None


def save_secret(
    integration: str,
    name: str,
    value: str,
    *,
    journal_path: str | Path | None = None,
) -> None:
    """Save one local-only secret value for the selected journal."""

    save_secrets(integration, {name: value}, journal_path=journal_path)


def save_secrets(
    integration: str,
    values: Mapping[str, str],
    *,
    journal_path: str | Path | None = None,
) -> None:
    """Merge local-only secret values into an integration store."""

    clean_values = {
        key: value for key, value in values.items() if isinstance(value, str) and value
    }
    if not clean_values:
        return

    payload = _load_secret_payload(integration, journal_path=journal_path)
    secrets = payload.setdefault("secrets", {})
    if not isinstance(secrets, dict):
        secrets = {}
        payload["secrets"] = secrets
    secrets.update(clean_values)
    payload["schema"] = LOCAL_SECRET_SCHEMA
    payload["integration"] = _safe_integration(integration)
    payload["journal_fingerprint"] = _journal_fingerprint(journal_path)

    path = secret_path_for(integration, journal_path=journal_path)
    _ensure_private_dir(path.parent)
    _write_private_json(path, payload)


def delete_secret(
    integration: str,
    name: str,
    *,
    journal_path: str | Path | None = None,
) -> None:
    """Delete one local-only secret value, preserving sibling values."""

    payload = _load_secret_payload(integration, journal_path=journal_path)
    secrets = payload.get("secrets")
    if not isinstance(secrets, dict) or name not in secrets:
        return
    secrets.pop(name, None)
    path = secret_path_for(integration, journal_path=journal_path)
    if secrets:
        _ensure_private_dir(path.parent)
        _write_private_json(path, payload)
        return
    try:
        path.unlink()
    except FileNotFoundError:
        return


def load_env_secret(
    env_var: str,
    *,
    journal_path: str | Path | None = None,
    include_process: bool = True,
) -> str | None:
    """Resolve a managed env-style secret from local storage, then the process."""

    integration = ENV_SECRET_INTEGRATIONS.get(env_var)
    value = (
        load_secret(integration, env_var, journal_path=journal_path)
        if integration
        else None
    )
    if value:
        return value
    if include_process:
        process_value = os.getenv(env_var)
        return process_value if process_value else None
    return None


def save_env_secret(
    env_var: str,
    value: str,
    *,
    journal_path: str | Path | None = None,
) -> None:
    """Save a managed env-style secret to the local boundary."""

    integration = ENV_SECRET_INTEGRATIONS.get(env_var)
    if integration is None:
        raise ValueError(f"unsupported local env secret: {env_var}")
    save_secret(integration, env_var, value, journal_path=journal_path)


def delete_env_secret(
    env_var: str,
    *,
    journal_path: str | Path | None = None,
) -> None:
    """Delete a managed env-style secret from the local boundary."""

    integration = ENV_SECRET_INTEGRATIONS.get(env_var)
    if integration is None:
        raise ValueError(f"unsupported local env secret: {env_var}")
    delete_secret(integration, env_var, journal_path=journal_path)


def load_env_secrets(
    env_vars: Mapping[str, str] | list[str] | tuple[str, ...] | set[str] | None = None,
    *,
    journal_path: str | Path | None = None,
    include_process: bool = False,
) -> dict[str, str]:
    """Return configured managed env-style secrets without exposing values in config."""

    keys = ENV_SECRET_INTEGRATIONS if env_vars is None else env_vars
    return {
        key: value
        for key in keys
        if (
            value := load_env_secret(
                key,
                journal_path=journal_path,
                include_process=include_process,
            )
        )
    }


def is_env_secret_configured(
    env_var: str,
    *,
    journal_path: str | Path | None = None,
    include_process: bool = True,
) -> bool:
    """Return whether an env-style secret is present locally or in process env."""

    return bool(
        load_env_secret(
            env_var,
            journal_path=journal_path,
            include_process=include_process,
        )
    )


def _load_secret_payload(
    integration: str,
    *,
    journal_path: str | Path | None = None,
) -> dict[str, Any]:
    path = secret_path_for(integration, journal_path=journal_path)
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except (FileNotFoundError, OSError, json.JSONDecodeError):
        return {
            "schema": LOCAL_SECRET_SCHEMA,
            "integration": _safe_integration(integration),
            "journal_fingerprint": _journal_fingerprint(journal_path),
            "secrets": {},
        }
    if not isinstance(payload, dict):
        return {
            "schema": LOCAL_SECRET_SCHEMA,
            "integration": _safe_integration(integration),
            "journal_fingerprint": _journal_fingerprint(journal_path),
            "secrets": {},
        }
    if payload.get("schema") != LOCAL_SECRET_SCHEMA:
        return {
            "schema": LOCAL_SECRET_SCHEMA,
            "integration": _safe_integration(integration),
            "journal_fingerprint": _journal_fingerprint(journal_path),
            "secrets": {},
        }
    if payload.get("integration") != _safe_integration(integration):
        return {
            "schema": LOCAL_SECRET_SCHEMA,
            "integration": _safe_integration(integration),
            "journal_fingerprint": _journal_fingerprint(journal_path),
            "secrets": {},
        }
    return payload


def _secret_root() -> Path:
    return Path.home() / _APP_SUPPORT_RELATIVE


def _journal_fingerprint(journal_path: str | Path | None = None) -> str:
    journal = Path(journal_path or get_journal()).resolve()
    return hashlib.sha256(str(journal).encode("utf-8")).hexdigest()[:16]


def _safe_integration(integration: str) -> str:
    if not _SAFE_INTEGRATION_RE.fullmatch(integration):
        raise ValueError(f"invalid local secret integration: {integration!r}")
    return integration


def _ensure_private_dir(path: Path) -> None:
    path.mkdir(parents=True, mode=0o700, exist_ok=True)
    current = path
    root = _secret_root()
    while True:
        try:
            current.chmod(0o700)
        except FileNotFoundError:
            pass
        if current == root or current.parent == current:
            break
        current = current.parent


def _write_private_json(path: Path, payload: dict[str, Any]) -> None:
    data = json.dumps(payload, indent=2, sort_keys=True).encode("utf-8") + b"\n"
    flags = os.O_WRONLY | os.O_CREAT | os.O_TRUNC
    fd = os.open(path, flags, 0o600)
    try:
        os.fchmod(fd, 0o600)
        os.write(fd, data)
        os.fsync(fd)
    finally:
        os.close(fd)
    path.chmod(0o600)
    _fsync_dir(path.parent)


def _fsync_dir(path: Path) -> None:
    try:
        fd = os.open(path, os.O_RDONLY | os.O_DIRECTORY)
    except OSError:
        return
    try:
        os.fsync(fd)
    finally:
        os.close(fd)


def load_oura_client_secret() -> str | None:
    """Machine-local Oura client secret for confidential-client apps.

    Server-side-flow Oura apps require the client secret at token exchange
    and refresh. It lives beside the token files, never in the journal or
    chat transcripts; absent file means a public (PKCE-only) client.
    """
    path = Path.home() / _OURA_RELATIVE / "client_secret"
    try:
        secret = path.read_text(encoding="utf-8").strip()
    except OSError:
        return None
    return secret or None
