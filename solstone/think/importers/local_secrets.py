# SPDX-License-Identifier: AGPL-3.0-only
# Copyright (c) 2026 sol pbc

"""Local machine secret storage for importer OAuth tokens.

This module owns local-only secret files under Application Support. It must not
write into journal content, import bundles, or any replicated journal path.
"""

from __future__ import annotations

import hashlib
import json
import os
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from solstone.think.utils import get_journal

SECRET_SCHEMA = "solstone.local_secret.oura_oauth.v1"
SECRET_PROVIDER = "oura"
_APP_SUPPORT_RELATIVE = Path(
    "Library", "Application Support", "Solstone", "secrets", "oura"
)


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
    path.parent.mkdir(parents=True, mode=0o700, exist_ok=True)
    path.parent.chmod(0o700)
    payload = {
        "schema": SECRET_SCHEMA,
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
    if payload.get("schema") != SECRET_SCHEMA or payload.get("provider") != "oura":
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
    return Path.home() / _APP_SUPPORT_RELATIVE / f"{_journal_fingerprint()}.json"


def _journal_fingerprint() -> str:
    journal = Path(get_journal()).resolve()
    return hashlib.sha256(str(journal).encode("utf-8")).hexdigest()[:16]


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
