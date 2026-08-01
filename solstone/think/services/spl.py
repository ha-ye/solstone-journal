# SPDX-License-Identifier: AGPL-3.0-only
# Copyright (c) 2026 sol pbc

"""Sol private link service journal storage."""

from __future__ import annotations

import json
import logging
import ssl
import urllib.error
import urllib.request
from dataclasses import dataclass
from typing import Any

from solstone.think.journal_config import (
    JournalConfigMutation,
    JournalConfigPostCommitError,
    get_journal_config_path,
    mutate_journal_config,
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


def _post_json_sync(url: str, body: dict[str, Any]) -> dict[str, Any]:
    if not url.startswith(("http://", "https://")):
        raise ValueError(f"unsupported url scheme: {url!r}")
    req = urllib.request.Request(  # noqa: S310
        url,
        data=json.dumps(body).encode(),
        headers={
            "content-type": "application/json",
            "user-agent": "solstone-link/0.1",
        },
        method="POST",
    )
    with urllib.request.urlopen(req, timeout=30) as resp:  # noqa: S310
        parsed = json.loads(resp.read())
    if not isinstance(parsed, dict):
        raise RuntimeError("relay returned invalid JSON response")
    return parsed


def enroll_home(
    relay_endpoint: str,
    *,
    instance_id: str,
    ca_pubkey: str,
    home_label: str,
) -> str:
    """POST the home service identity and return the relay service token."""
    body = {
        "instance_id": instance_id,
        "ca_pubkey": ca_pubkey,
        "home_label": home_label,
    }
    result = _post_json_sync(f"{relay_endpoint.rstrip('/')}/enroll/home", body)
    token = result.get("service_token") or result.get("account_token")
    if not isinstance(token, str) or not token:
        raise RuntimeError("relay returned no service_token")
    return token


@dataclass(frozen=True)
class SplDisableOutcome:
    was_enabled: bool


def _require_journal_config() -> None:
    if not get_journal_config_path().exists():
        raise JournalNotInitializedError(
            "journal config file is not present; run 'journal setup' first"
        )


def is_spl_enabled() -> bool:
    return read_posture() == "spl" and load_service_token() is not None


def _write_posture(value: str):
    _require_journal_config()

    def apply(config: dict[str, Any]) -> JournalConfigMutation[None]:
        link = config.setdefault("link", {})
        changed = link.get("posture") != value
        link["posture"] = value
        return JournalConfigMutation(changed=changed, value=None)

    return mutate_journal_config(apply)


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

    result = _write_posture("spl")
    try:
        save_service_token(token)
    except Exception as exc:
        raise JournalConfigPostCommitError(
            "sol private link posture was saved, but the service token was not",
            result=result,
            error=exc,
        ) from exc
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

    def apply(config: dict[str, Any]) -> JournalConfigMutation[SplDisableOutcome]:
        link_config = config.get("link")
        if not isinstance(link_config, dict) or link_config.get("posture") != "spl":
            return JournalConfigMutation(
                changed=False,
                value=SplDisableOutcome(was_enabled=False),
            )

        link_config["posture"] = "direct"
        return JournalConfigMutation(
            changed=True,
            value=SplDisableOutcome(was_enabled=True),
        )

    result = mutate_journal_config(apply)
    if result.changed:
        log.debug("disabled sol private link")
    return result.value
