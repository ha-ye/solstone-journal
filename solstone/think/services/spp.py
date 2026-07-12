# SPDX-License-Identifier: AGPL-3.0-only
# Copyright (c) 2026 sol pbc

"""Confidential processing service journal-config storage."""

from __future__ import annotations

import hashlib
import logging
import threading
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Any
from urllib.parse import urlsplit

from solstone.think.journal_config import (
    get_journal_config_path,
    hold_config_lock,
    read_journal_config,
    write_journal_config,
)
from solstone.think.providers.local_endpoint import normalize_local_endpoint_url
from solstone.think.services.spp_attest.cadence import AttestationSession

log = logging.getLogger(__name__)

_HANDOFF_FIELDS = (
    "endpoint_url",
    "served_model_id",
    "credential",
    "account_id",
    "created_at",
)
_SECRET_HANDOFF_FIELDS = frozenset({"credential"})
_REDACTED = "***redacted***"
CREDENTIAL_FINGERPRINT_FIELD = "credential_fingerprint_sha256"


class JournalNotInitializedError(RuntimeError):
    """Raised when the journal config file has not been initialized."""


@dataclass(frozen=True)
class DisableOutcome:
    was_enabled: bool
    credential_preserved: bool


@dataclass(frozen=True, slots=True)
class AttestationState:
    session: AttestationSession | None = None
    failure: str | None = None


_ATTESTATION_LOCK = threading.Lock()
_ATTESTATION_STATE = AttestationState()


def record_attestation_verified(session: AttestationSession) -> None:
    """Record a verified in-process attestation session."""

    global _ATTESTATION_STATE
    with _ATTESTATION_LOCK:
        _ATTESTATION_STATE = AttestationState(session=session, failure=None)


def record_attestation_failed(detail: str) -> None:
    """Record an in-process attestation failure."""

    global _ATTESTATION_STATE
    with _ATTESTATION_LOCK:
        _ATTESTATION_STATE = AttestationState(session=None, failure=detail)


def clear_attestation_state() -> None:
    """Clear process-local attestation state."""

    global _ATTESTATION_STATE
    with _ATTESTATION_LOCK:
        _ATTESTATION_STATE = AttestationState()


def get_attestation_state() -> AttestationState:
    """Return process-local attestation state."""

    with _ATTESTATION_LOCK:
        return _ATTESTATION_STATE


def _redact_handoff(payload: dict[str, Any]) -> dict[str, Any]:
    return {
        key: (_REDACTED if key in _SECRET_HANDOFF_FIELDS else value)
        for key, value in payload.items()
    }


def _require_journal_config() -> None:
    if not get_journal_config_path().exists():
        raise JournalNotInitializedError(
            "journal config file is not present; run 'journal setup' first"
        )


def _validate_handoff_payload(payload: dict[str, Any]) -> dict[str, str]:
    validated: dict[str, str] = {}
    for field in _HANDOFF_FIELDS:
        if field not in payload:
            raise ValueError(f"malformed handoff payload: missing field '{field}'")
        value = payload[field]
        if not isinstance(value, str) or not value:
            raise ValueError(
                f"malformed handoff payload: field '{field}' must be a non-empty string"
            )
        validated[field] = value

    endpoint_url = normalize_local_endpoint_url(validated["endpoint_url"])
    parsed = urlsplit(endpoint_url)
    if parsed.scheme not in {"http", "https"} or not parsed.netloc:
        raise ValueError("malformed handoff payload: endpoint_url must be http(s)")
    validated["endpoint_url"] = endpoint_url
    return validated


def _fingerprint_key(key: str) -> str:
    return hashlib.sha256(key.encode("utf-8")).hexdigest()


def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


def _providers_block(config: dict[str, Any]) -> dict[str, Any]:
    providers = config.get("providers")
    if not isinstance(providers, dict):
        providers = {}
        config["providers"] = providers
    return providers


def _local_block(providers: dict[str, Any]) -> dict[str, Any]:
    local = providers.get("local")
    if not isinstance(local, dict):
        local = {}
        providers["local"] = local
    return local


def _type_block(providers: dict[str, Any], agent_type: str) -> dict[str, Any]:
    block = providers.get(agent_type)
    if not isinstance(block, dict):
        block = {}
        providers[agent_type] = block
    return block


def provision_confidential_handoff(payload: dict[str, Any]) -> None:
    """Persist a portal-provisioned confidential handoff into journal config."""

    if log.isEnabledFor(logging.DEBUG):
        log.debug("received confidential handoff payload: %r", _redact_handoff(payload))
    values = _validate_handoff_payload(payload)
    _require_journal_config()

    with hold_config_lock():
        _require_journal_config()
        config = read_journal_config()
        providers = _providers_block(config)
        local = _local_block(providers)
        prior_local = dict(local) if local else None
        generate = _type_block(providers, "generate")
        cogitate = _type_block(providers, "cogitate")
        prior_generate_provider = generate.get("provider")
        prior_cogitate_provider = cogitate.get("provider")

        local["endpoint_url"] = values["endpoint_url"]
        local["served_model_id"] = values["served_model_id"]
        local["credential"] = values["credential"]
        generate["provider"] = "local"
        cogitate["provider"] = "local"
        config.setdefault("services", {})["confidential"] = {
            "enabled_at": _now_iso(),
            "account_id": values["account_id"],
            "endpoint_url": values["endpoint_url"],
            "served_model_id": values["served_model_id"],
            "credential_created_at": values["created_at"],
            CREDENTIAL_FINGERPRINT_FIELD: _fingerprint_key(values["credential"]),
            "prior_generate_provider": (
                prior_generate_provider
                if isinstance(prior_generate_provider, str)
                else None
            ),
            "prior_cogitate_provider": (
                prior_cogitate_provider
                if isinstance(prior_cogitate_provider, str)
                else None
            ),
            "prior_local_endpoint": prior_local,
        }
        write_journal_config(config)
        log.debug("provisioned confidential service")


def disable_confidential() -> DisableOutcome:
    """Disable confidential provisioning while preserving unrelated endpoints."""

    _require_journal_config()

    with hold_config_lock():
        _require_journal_config()
        config = read_journal_config()
        services = config.setdefault("services", {})
        block = services.get("confidential")
        if not isinstance(block, dict):
            return DisableOutcome(was_enabled=False, credential_preserved=False)

        providers = _providers_block(config)
        generate = _type_block(providers, "generate")
        cogitate = _type_block(providers, "cogitate")
        for type_block, field in (
            (generate, "prior_generate_provider"),
            (cogitate, "prior_cogitate_provider"),
        ):
            prior_provider = block.get(field)
            if isinstance(prior_provider, str) and prior_provider:
                type_block["provider"] = prior_provider
            else:
                type_block.pop("provider", None)

        local = _local_block(providers)
        current_credential = local.get("credential")
        stored_fingerprint = block.get(CREDENTIAL_FINGERPRINT_FIELD)
        credential_preserved = True
        if (
            isinstance(current_credential, str)
            and isinstance(stored_fingerprint, str)
            and _fingerprint_key(current_credential) == stored_fingerprint
        ):
            prior_local = block.get("prior_local_endpoint")
            providers["local"] = (
                dict(prior_local) if isinstance(prior_local, dict) else {}
            )
            credential_preserved = False

        services.pop("confidential", None)
        write_journal_config(config)
        log.debug("disabled confidential service")
        return DisableOutcome(
            was_enabled=True,
            credential_preserved=credential_preserved,
        )


def confidential_provenance() -> dict[str, Any] | None:
    """Return the confidential provenance block from journal config, if present."""

    provenance = read_journal_config().get("services", {}).get("confidential")
    return provenance if isinstance(provenance, dict) else None


def is_confidential_enabled() -> bool:
    """Return whether confidential processing is provisioned with a credential."""

    config = read_journal_config()
    block = config.get("services", {}).get("confidential")
    local = config.get("providers", {}).get("local", {})
    credential = local.get("credential") if isinstance(local, dict) else None
    return isinstance(block, dict) and bool(credential)
