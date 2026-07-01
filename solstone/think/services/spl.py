# SPDX-License-Identifier: AGPL-3.0-only
# Copyright (c) 2026 sol pbc

"""Sol private link service journal storage."""

from __future__ import annotations

import fcntl
import json
import logging
import os
import ssl
import urllib.error
from dataclasses import dataclass
from pathlib import Path

from solstone.think.journal_config import (
    get_journal_config_path,
    read_journal_config,
    write_journal_config,
)
from solstone.think.link.ca import load_or_generate_ca
from solstone.think.link.paths import (
    LinkState,
    ca_dir,
    load_service_token,
    relay_url,
    save_service_token,
)
from solstone.think.link.window import read_posture
from solstone.think.spl.relay_client import enroll_home
from solstone.think.utils import get_journal

log = logging.getLogger(__name__)


class JournalNotInitializedError(RuntimeError):
    """Raised when the journal config file has not been initialized."""


class RelayUnreachableError(RuntimeError):
    """Raised when the spl relay cannot be reached."""


class RelayRejectedError(RuntimeError):
    """Raised when the relay was reached but rejected the enroll with an HTTP error."""

    def __init__(self, *, status: int, reason: str | None) -> None:
        self.status = status
        self.reason = reason
        super().__init__(f"relay rejected enroll: status={status} reason={reason}")


class RelayResponseError(RuntimeError):
    """Raised when the spl relay response is malformed."""


@dataclass(frozen=True)
class SplDisableOutcome:
    was_enabled: bool


def _lock_path() -> Path:
    # This MUST be the same path scout uses - both writers serialize on the
    # journal.json config lock; keep in sync.
    return Path(get_journal()) / "config" / ".journal.json.lock"


def _require_journal_config() -> None:
    if not get_journal_config_path().exists():
        raise JournalNotInitializedError(
            "journal config file is not present; run 'journal setup' first"
        )


def is_spl_enabled() -> bool:
    return read_posture() == "spl" and load_service_token() is not None


def _write_posture(value: str) -> None:
    lock_path = _lock_path()
    lock_path.parent.mkdir(parents=True, exist_ok=True)
    with open(lock_path, "w", encoding="utf-8") as lock_file:
        os.chmod(lock_path, 0o600)
        fcntl.flock(lock_file, fcntl.LOCK_EX)
        try:
            _require_journal_config()
            config = read_journal_config()
            config.setdefault("link", {})["posture"] = value
            write_journal_config(config)
        finally:
            fcntl.flock(lock_file, fcntl.LOCK_UN)


def _relay_error_reason(exc: urllib.error.HTTPError) -> str | None:
    """Best-effort extract the relay's ``{"error": ...}`` reason. Never raises."""
    try:
        raw = exc.read()
    except Exception:  # noqa: BLE001 - body parse must never raise (degrade to None)
        return None
    if not raw:
        return None
    try:
        parsed = json.loads(raw)
    except (ValueError, TypeError):
        return None
    if not isinstance(parsed, dict):
        return None
    reason = parsed.get("error")
    return reason if isinstance(reason, str) and reason else None


def enable_spl() -> None:
    _require_journal_config()
    state = LinkState.load_or_create()
    ca = load_or_generate_ca(ca_dir())

    try:
        token = enroll_home(
            relay_url(),
            instance_id=state.instance_id,
            ca_pubkey=ca.pubkey_spki_pem,
            home_label=state.home_label,
        )
    except urllib.error.HTTPError as exc:
        reason = _relay_error_reason(exc)
        log.warning("spl relay rejected enroll: status=%s reason=%s", exc.code, reason)
        raise RelayRejectedError(status=exc.code, reason=reason) from exc
    except (urllib.error.URLError, ssl.SSLError, TimeoutError) as exc:
        raise RelayUnreachableError(str(exc)) from exc
    except RuntimeError as exc:
        raise RelayResponseError(str(exc)) from exc

    save_service_token(token)
    _write_posture("spl")
    log.debug("enabled sol private link")


def disable_spl() -> SplDisableOutcome:
    """Set SPL posture to direct without clearing local or relay-side state.

    Sets `link.posture="direct"` (the authoritative reach/status gate). The
    cert-less pairing window remains bounded by live nonce existence, not
    posture. The supervised
    `journal spl` daemon observes the posture change and closes its listen WS
    within its poll interval. It keeps the local service token and cert state
    for quick re-enable. Direct (LAN/VPN) reach and existing paired-device
    bundles are untouched - no re-pairing.
    """
    _require_journal_config()

    lock_path = _lock_path()
    lock_path.parent.mkdir(parents=True, exist_ok=True)
    with open(lock_path, "w", encoding="utf-8") as lock_file:
        os.chmod(lock_path, 0o600)
        fcntl.flock(lock_file, fcntl.LOCK_EX)
        try:
            _require_journal_config()
            config = read_journal_config()
            link_config = config.get("link")
            if not isinstance(link_config, dict) or link_config.get("posture") != "spl":
                return SplDisableOutcome(was_enabled=False)

            link_config["posture"] = "direct"
            write_journal_config(config)
            log.debug("disabled sol private link")
            return SplDisableOutcome(was_enabled=True)
        finally:
            fcntl.flock(lock_file, fcntl.LOCK_UN)
