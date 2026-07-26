# SPDX-License-Identifier: AGPL-3.0-only
# Copyright (c) 2026 sol pbc

"""Capability composition and read-only observed-state reconciliation."""

from __future__ import annotations

import hashlib
import json
import logging
from pathlib import Path
from typing import Any

from solstone.think.backup import state as backup_state
from solstone.think.backup.hosted import delete_hosted_binding, save_hosted_binding
from solstone.think.journal_config import (
    JournalConfigMutation,
    JournalConfigPostCommitError,
    mutate_journal_config,
)
from solstone.think.link.ca import load_or_generate_ca
from solstone.think.link.paths import LinkState, ca_dir
from solstone.think.providers.local_endpoint import (
    confidential_fingerprint_provenance_block,
)
from solstone.think.sandbox_profile import envelope, intent, manifest
from solstone.think.services import scout, spl, spp
from solstone.think.services.spb_handoff import _BINDING_FIELDS, _binding_from_payload
from solstone.think.services.spl_handoff import _classify_spl_payload

_SCOUT_FIELDS = tuple(scout._HANDOFF_FIELDS)
_SPL_FIELDS = ("service", "state", "approved_at")
_SPP_FIELDS = tuple(spp._HANDOFF_FIELDS)
logger = logging.getLogger(__name__)

_DISABLE_CARRIED_RESIDUALS: dict[str, frozenset[str]] = {
    manifest.CAPABILITY_SCOUT: frozenset({"scout_block_missing"}),
    manifest.CAPABILITY_SPB: frozenset({"spb_binding_missing"}),
    manifest.CAPABILITY_SPP: frozenset({"spp_block_missing"}),
}


class PayloadValidationError(ValueError):
    def __init__(self, message: str, *, error_code: str = "payload_invalid") -> None:
        super().__init__(message)
        self.message = message
        self.error_code = error_code


def _read_json_file(path: Path) -> Any:
    try:
        return json.loads(path.read_text("utf-8"))
    except (FileNotFoundError, json.JSONDecodeError, OSError):
        return None


def _read_config(journal_path: Path) -> dict[str, Any]:
    payload = _read_json_file(journal_path / "config" / "journal.json")
    return payload if isinstance(payload, dict) else {}


def _read_link_state(journal_path: Path) -> dict[str, Any] | None:
    payload = _read_json_file(journal_path / "link" / "state.json")
    return payload if isinstance(payload, dict) else None


def _read_service_token(journal_path: Path) -> str | None:
    payload = _read_json_file(journal_path / "link" / "tokens" / "account.json")
    if not isinstance(payload, dict):
        return None
    token = payload.get("service_token") or payload.get("account_token")
    return token if isinstance(token, str) and token else None


def _read_hosted_binding(journal_path: Path) -> dict[str, Any] | None:
    payload = _read_json_file(journal_path / "backup" / "hosted" / "binding.json")
    return payload if isinstance(payload, dict) else None


def _ca_present(journal_path: Path) -> bool:
    ca_root = journal_path / "link" / "ca"
    return (ca_root / "cert.pem").exists() and (ca_root / "private.pem").exists()


def _posture(config: dict[str, Any]) -> str:
    link = config.get("link")
    if isinstance(link, dict) and link.get("posture") == "spl":
        return "spl"
    return "direct"


def _backup_config(config: dict[str, Any]) -> dict[str, Any]:
    backup = config.get("backup")
    return backup if isinstance(backup, dict) else {}


def _intent_capability(
    intent_payload: dict[str, Any] | None,
    name: str,
) -> dict[str, Any] | None:
    if not isinstance(intent_payload, dict):
        return None
    caps = intent_payload.get("capabilities")
    if not isinstance(caps, list):
        return None
    for item in caps:
        if isinstance(item, dict) and item.get("name") == name:
            return item
    return None


def _cap_intent_state(intent_payload: dict[str, Any] | None, name: str) -> str | None:
    item = _intent_capability(intent_payload, name)
    state = item.get("intent_state") if isinstance(item, dict) else None
    return state if isinstance(state, str) else None


def _observed_at_apply(
    intent_payload: dict[str, Any] | None, name: str
) -> dict[str, Any]:
    if not isinstance(intent_payload, dict):
        return {}
    observed = intent_payload.get("observed_at_apply")
    if not isinstance(observed, dict):
        return {}
    block = observed.get(name)
    return block if isinstance(block, dict) else {}


def _observed_string(
    intent_payload: dict[str, Any] | None, name: str, field: str
) -> str | None:
    value = _observed_at_apply(intent_payload, name).get(field)
    return value if isinstance(value, str) and value else None


def _is_applied_intent(state: str | None) -> bool:
    return state in {intent.INTENT_APPLIED, intent.INTENT_APPLY_STARTED}


def _is_disabled_intent(state: str | None) -> bool:
    return state in {intent.INTENT_DISABLED, intent.INTENT_DISABLE_STARTED}


def _cap(name: str, state: str, *residuals: str) -> envelope.CapabilityEnvelope:
    return envelope.CapabilityEnvelope(name, state, tuple(residuals))


def _secret_sha256(value: str) -> str:
    # scout.KEY_FINGERPRINT_FIELD is written from this same UTF-8 SHA-256 input.
    return hashlib.sha256(value.encode("utf-8")).hexdigest()


def _validate_exact_fields(
    payload: dict[str, Any],
    fields: tuple[str, ...],
    *,
    capability: str,
) -> None:
    expected = set(fields)
    actual = set(payload)
    if actual != expected:
        raise PayloadValidationError(f"{capability} payload fields are unsupported")


def _non_blank_payload(
    payload: dict[str, Any], fields: tuple[str, ...], capability: str
) -> None:
    for field in fields:
        value = payload.get(field)
        if not isinstance(value, str) or not value:
            raise PayloadValidationError(
                f"{capability} payload field {field} is invalid"
            )


def _validate_scout_payload(payload: dict[str, Any]) -> None:
    _validate_exact_fields(payload, _SCOUT_FIELDS, capability=manifest.CAPABILITY_SCOUT)
    try:
        scout._validate_handoff_payload(payload)
    except ValueError:
        raise PayloadValidationError("scout payload is invalid") from None


def _validate_spl_payload(payload: dict[str, Any]) -> None:
    _validate_exact_fields(payload, _SPL_FIELDS, capability=manifest.CAPABILITY_SPL)
    try:
        state = _classify_spl_payload(payload)
    except ValueError:
        raise PayloadValidationError("spl payload is invalid") from None
    if state != "approved":
        raise PayloadValidationError("spl payload must be approved")


def _validate_spb_payload(payload: dict[str, Any], journal_path: Path) -> None:
    _validate_exact_fields(
        payload, tuple(_BINDING_FIELDS), capability=manifest.CAPABILITY_SPB
    )
    _non_blank_payload(payload, tuple(_BINDING_FIELDS), manifest.CAPABILITY_SPB)
    state = _read_link_state(journal_path)
    instance_id = state.get("instance_id") if isinstance(state, dict) else None
    if not isinstance(instance_id, str) or not instance_id:
        raise PayloadValidationError("spb requires prepared runtime identity")
    if payload.get("instance_id") != instance_id:
        raise PayloadValidationError(
            "spb payload field instance_id does not match runtime",
            error_code="spb_instance_mismatch",
        )
    try:
        _binding_from_payload(payload)
    except ValueError:
        raise PayloadValidationError("spb payload is invalid") from None


def _validate_spp_payload(payload: dict[str, Any]) -> None:
    _validate_exact_fields(payload, _SPP_FIELDS, capability=manifest.CAPABILITY_SPP)
    try:
        spp._validate_handoff_payload(payload)
    except ValueError:
        raise PayloadValidationError("spp payload is invalid") from None


def validate_payload(
    capability: str, payload: dict[str, Any], journal_path: Path
) -> None:
    if capability == manifest.CAPABILITY_SCOUT:
        _validate_scout_payload(payload)
    elif capability == manifest.CAPABILITY_SPL:
        _validate_spl_payload(payload)
    elif capability == manifest.CAPABILITY_SPB:
        _validate_spb_payload(payload, journal_path)
    elif capability == manifest.CAPABILITY_SPP:
        _validate_spp_payload(payload)
    else:
        raise PayloadValidationError("unsupported capability")


def _runtime_status(
    journal_path: Path, intent_payload: dict[str, Any] | None
) -> envelope.CapabilityEnvelope:
    config = _read_config(journal_path)
    setup = config.get("setup")
    completed = setup.get("completed_at") if isinstance(setup, dict) else None
    active = (
        isinstance(completed, (int, float))
        and not isinstance(completed, bool)
        and completed > 0
    )
    link_state = _read_link_state(journal_path)
    ready = active and isinstance(link_state, dict) and _ca_present(journal_path)
    cap_state = _cap_intent_state(intent_payload, manifest.CAPABILITY_RUNTIME)
    if ready:
        return _cap(manifest.CAPABILITY_RUNTIME, envelope.CAP_READY)
    if cap_state in {intent.INTENT_PREPARED, intent.INTENT_APPLY_STARTED}:
        return _cap(
            manifest.CAPABILITY_RUNTIME, envelope.CAP_DEGRADED, "apply_interrupted"
        )
    return _cap(manifest.CAPABILITY_RUNTIME, envelope.CAP_NOT_APPLIED)


def _scout_status(
    config: dict[str, Any], intent_payload: dict[str, Any] | None
) -> envelope.CapabilityEnvelope:
    state = _cap_intent_state(intent_payload, manifest.CAPABILITY_SCOUT)
    block = config.get("services", {}).get("scout")
    applied = isinstance(block, dict) and block.get("state") != "pending"
    key = config.get("env", {}).get("GOOGLE_API_KEY")
    has_key = isinstance(key, str) and bool(key)
    complete = applied and has_key
    if complete and state is None:
        return _cap(
            manifest.CAPABILITY_SCOUT, envelope.CAP_DEGRADED, "unmanaged_existing_state"
        )
    if complete and state == intent.INTENT_APPLY_STARTED:
        return _cap(
            manifest.CAPABILITY_SCOUT, envelope.CAP_DEGRADED, "intent_finalize_missing"
        )
    if complete:
        live_fingerprint = _secret_sha256(key)
        stored_fingerprint = block.get(scout.KEY_FINGERPRINT_FIELD)
        recorded_fingerprint = _observed_string(
            intent_payload, manifest.CAPABILITY_SCOUT, scout.KEY_FINGERPRINT_FIELD
        )
        expected = recorded_fingerprint or (
            stored_fingerprint if isinstance(stored_fingerprint, str) else None
        )
        if expected is not None and (
            live_fingerprint != expected or stored_fingerprint != expected
        ):
            return _cap(
                manifest.CAPABILITY_SCOUT,
                envelope.CAP_DEGRADED,
                "scout_key_fingerprint_mismatch",
            )
        return _cap(manifest.CAPABILITY_SCOUT, envelope.CAP_READY)
    if _is_disabled_intent(state) or state in {None, intent.INTENT_PREPARED}:
        return _cap(manifest.CAPABILITY_SCOUT, envelope.CAP_NOT_APPLIED)
    if _is_applied_intent(state):
        return _cap(
            manifest.CAPABILITY_SCOUT, envelope.CAP_DEGRADED, "scout_block_missing"
        )
    return _cap(manifest.CAPABILITY_SCOUT, envelope.CAP_NOT_APPLIED)


def _spl_status(
    journal_path: Path,
    config: dict[str, Any],
    intent_payload: dict[str, Any] | None,
) -> envelope.CapabilityEnvelope:
    state = _cap_intent_state(intent_payload, manifest.CAPABILITY_SPL)
    posture = _posture(config)
    link_state = _read_link_state(journal_path)
    token = _read_service_token(journal_path)
    complete = posture == "spl" and token is not None
    if complete and state is None:
        return _cap(
            manifest.CAPABILITY_SPL, envelope.CAP_DEGRADED, "unmanaged_existing_state"
        )
    if complete and state == intent.INTENT_APPLY_STARTED:
        return _cap(
            manifest.CAPABILITY_SPL, envelope.CAP_DEGRADED, "intent_finalize_missing"
        )
    if complete:
        recorded_instance_id = _observed_string(
            intent_payload, manifest.CAPABILITY_SPL, "instance_id"
        )
        live_instance_id = (
            link_state.get("instance_id") if isinstance(link_state, dict) else None
        )
        if (
            recorded_instance_id is not None
            and live_instance_id != recorded_instance_id
        ):
            return _cap(
                manifest.CAPABILITY_SPL,
                envelope.CAP_DEGRADED,
                "spl_identity_missing",
            )
        return _cap(manifest.CAPABILITY_SPL, envelope.CAP_READY)
    if posture == "spl" and token is None:
        return _cap(manifest.CAPABILITY_SPL, envelope.CAP_DEGRADED, "spl_token_missing")
    if _is_disabled_intent(state) or state in {None, intent.INTENT_PREPARED}:
        return _cap(manifest.CAPABILITY_SPL, envelope.CAP_NOT_APPLIED)
    if _is_applied_intent(state):
        return _cap(
            manifest.CAPABILITY_SPL, envelope.CAP_DEGRADED, "spl_posture_not_spl"
        )
    return _cap(manifest.CAPABILITY_SPL, envelope.CAP_NOT_APPLIED)


def _spb_status(
    journal_path: Path,
    config: dict[str, Any],
    intent_payload: dict[str, Any] | None,
) -> envelope.CapabilityEnvelope:
    state = _cap_intent_state(intent_payload, manifest.CAPABILITY_SPB)
    binding = _read_hosted_binding(journal_path)
    backup = _backup_config(config)
    complete = (
        isinstance(binding, dict)
        and backup.get("mode") == "operated"
        and backup.get("enabled") is True
    )
    if complete and state is None:
        return _cap(
            manifest.CAPABILITY_SPB, envelope.CAP_DEGRADED, "unmanaged_existing_state"
        )
    if complete and state == intent.INTENT_APPLY_STARTED:
        return _cap(
            manifest.CAPABILITY_SPB, envelope.CAP_DEGRADED, "intent_finalize_missing"
        )
    recorded_instance_id = _observed_string(
        intent_payload, manifest.CAPABILITY_SPB, "instance_id"
    )
    live_instance_id = binding.get("instance_id") if isinstance(binding, dict) else None
    if (
        binding is not None
        and recorded_instance_id is not None
        and live_instance_id != recorded_instance_id
    ):
        return _cap(
            manifest.CAPABILITY_SPB, envelope.CAP_DEGRADED, "spb_instance_mismatch"
        )
    if complete:
        return _cap(manifest.CAPABILITY_SPB, envelope.CAP_READY)
    if binding is None and _is_applied_intent(state):
        return _cap(
            manifest.CAPABILITY_SPB, envelope.CAP_DEGRADED, "spb_binding_missing"
        )
    if (
        binding is not None
        or backup.get("mode") == "operated"
        or backup.get("enabled") is True
    ):
        return _cap(
            manifest.CAPABILITY_SPB,
            envelope.CAP_DEGRADED,
            "spb_backup_config_incomplete",
        )
    if _is_disabled_intent(state) or state in {None, intent.INTENT_PREPARED}:
        return _cap(manifest.CAPABILITY_SPB, envelope.CAP_NOT_APPLIED)
    return _cap(manifest.CAPABILITY_SPB, envelope.CAP_NOT_APPLIED)


def _spp_status(
    config: dict[str, Any], intent_payload: dict[str, Any] | None
) -> envelope.CapabilityEnvelope:
    state = _cap_intent_state(intent_payload, manifest.CAPABILITY_SPP)
    block = confidential_fingerprint_provenance_block(config)
    local = config.get("providers", {}).get("local", {})
    credential = local.get("credential") if isinstance(local, dict) else None
    complete = isinstance(block, dict) and bool(credential)
    if complete and state is None:
        return _cap(
            manifest.CAPABILITY_SPP, envelope.CAP_DEGRADED, "unmanaged_existing_state"
        )
    if complete and state == intent.INTENT_APPLY_STARTED:
        return _cap(
            manifest.CAPABILITY_SPP, envelope.CAP_DEGRADED, "intent_finalize_missing"
        )
    if complete:
        recorded_fingerprint = _observed_string(
            intent_payload,
            manifest.CAPABILITY_SPP,
            spp.CREDENTIAL_FINGERPRINT_FIELD,
        )
        live_fingerprint = block.get(spp.CREDENTIAL_FINGERPRINT_FIELD)
        if (
            recorded_fingerprint is not None
            and live_fingerprint != recorded_fingerprint
        ):
            return _cap(
                manifest.CAPABILITY_SPP,
                envelope.CAP_DEGRADED,
                "spp_credential_fingerprint_mismatch",
            )
        return _cap(manifest.CAPABILITY_SPP, envelope.CAP_READY)
    if _is_disabled_intent(state) or state in {None, intent.INTENT_PREPARED}:
        return _cap(manifest.CAPABILITY_SPP, envelope.CAP_NOT_APPLIED)
    if _is_applied_intent(state):
        return _cap(manifest.CAPABILITY_SPP, envelope.CAP_DEGRADED, "spp_block_missing")
    return _cap(manifest.CAPABILITY_SPP, envelope.CAP_NOT_APPLIED)


def observe_capabilities(
    journal_path: str | Path,
    intent_payload: dict[str, Any] | None = None,
) -> tuple[envelope.CapabilityEnvelope, ...]:
    journal = Path(journal_path)
    config = _read_config(journal)
    by_name = {
        manifest.CAPABILITY_SCOUT: _scout_status(config, intent_payload),
        manifest.CAPABILITY_SPL: _spl_status(journal, config, intent_payload),
        manifest.CAPABILITY_SPB: _spb_status(journal, config, intent_payload),
        manifest.CAPABILITY_SPP: _spp_status(config, intent_payload),
        manifest.CAPABILITY_RUNTIME: _runtime_status(journal, intent_payload),
    }
    return tuple(by_name[name] for name in manifest.CAPABILITY_ORDER)


def top_state(capabilities: tuple[envelope.CapabilityEnvelope, ...]) -> str:
    if any(cap.state == envelope.CAP_CLEANUP_FAILED for cap in capabilities):
        return envelope.TOP_CLEANUP_FAILED
    if any(cap.state == envelope.CAP_DEGRADED for cap in capabilities):
        return envelope.TOP_DEGRADED
    return envelope.TOP_OK


def prepare_runtime(
    journal_path: Path, run_id: str
) -> tuple[envelope.CapabilityEnvelope, ...]:
    intent.ensure_prepared(journal_path, run_id)
    owner = manifest.synthetic_owner_metadata(run_id)

    def apply(config: dict[str, Any]) -> JournalConfigMutation[None]:
        changed = False
        setup = config.setdefault("setup", {})
        if setup.get("completed_at") != owner.setup_completed_at:
            setup["completed_at"] = owner.setup_completed_at
            changed = True
        identity = config.setdefault("identity", {})
        for key, value in {
            "name": owner.identity_name,
            "preferred": owner.identity_preferred,
            "timezone": owner.identity_timezone,
        }.items():
            if identity.get(key) != value:
                identity[key] = value
                changed = True
        journal = config.setdefault("journal", {})
        if journal.get("name") != owner.journal_name:
            journal["name"] = owner.journal_name
            changed = True
        return JournalConfigMutation(changed=changed, value=None)

    mutate_journal_config(apply, journal_path=journal_path)
    LinkState.load_or_create(default_label=owner.home_label)
    load_or_generate_ca(ca_dir())
    return observe_capabilities(
        journal_path, intent.require_intent(journal_path, run_id)
    )


def apply_capability(
    journal_path: Path,
    run_id: str,
    capability: str,
    payload: dict[str, Any],
) -> tuple[envelope.CapabilityEnvelope, ...]:
    validate_payload(capability, payload, journal_path)
    intent.require_intent(journal_path, run_id)
    intent.update_capability(
        journal_path,
        run_id,
        capability,
        state=intent.INTENT_APPLY_STARTED,
    )
    if capability == manifest.CAPABILITY_SCOUT:
        scout.provision_scout_handoff(payload)
        observed = _read_config(journal_path).get("services", {}).get("scout", {})
        intent.update_capability(
            journal_path,
            run_id,
            capability,
            state=intent.INTENT_APPLIED,
            observed={
                "key_fingerprint_sha256": observed.get("key_fingerprint_sha256")
                if isinstance(observed, dict)
                else None
            },
        )
    elif capability == manifest.CAPABILITY_SPL:
        spl.enable_spl()
        state = _read_link_state(journal_path) or {}
        intent.update_capability(
            journal_path,
            run_id,
            capability,
            state=intent.INTENT_APPLIED,
            observed={"instance_id": state.get("instance_id")},
        )
    elif capability == manifest.CAPABILITY_SPB:
        binding = _binding_from_payload(payload)
        backup_state.generate_and_store_keys()
        backup_state.set_recovery_key_confirmed(True)
        save_hosted_binding(binding)
        backup_state.set_mode("operated")
        backup_state.set_enabled(True)
        intent.update_capability(
            journal_path,
            run_id,
            capability,
            state=intent.INTENT_APPLIED,
            observed={
                "instance_id": binding.instance_id,
                "bucket": binding.bucket,
                "prefix": binding.prefix,
            },
        )
    elif capability == manifest.CAPABILITY_SPP:
        spp.provision_confidential_handoff(payload)
        observed = (
            _read_config(journal_path).get("services", {}).get("confidential", {})
        )
        intent.update_capability(
            journal_path,
            run_id,
            capability,
            state=intent.INTENT_APPLIED,
            observed={
                "credential_fingerprint_sha256": observed.get(
                    "credential_fingerprint_sha256"
                )
                if isinstance(observed, dict)
                else None
            },
        )
    else:
        raise PayloadValidationError("unsupported capability")
    return observe_capabilities(
        journal_path, intent.require_intent(journal_path, run_id)
    )


def _disable_spb() -> None:
    backup_state.clear_backup_config()
    delete_hosted_binding()


def _disable_preexisting_residuals(
    name: str,
    prior: envelope.CapabilityEnvelope | None,
) -> list[str]:
    if prior is None or prior.state != envelope.CAP_DEGRADED:
        return []
    carried = _DISABLE_CARRIED_RESIDUALS.get(name, frozenset())
    return [residual for residual in prior.residuals if residual in carried]


def _record_disable_exception(
    name: str,
    exc: Exception,
    residuals: list[str],
) -> None:
    if isinstance(exc, JournalConfigPostCommitError):
        residual = "post_commit_failed"
    elif isinstance(exc, OSError):
        residual = "local_artifact_io_failed"
    else:
        residual = "missing_expected_artifact"
    logger.exception(
        "sandbox profile disable failed for capability=%s exception_type=%s",
        name,
        type(exc).__name__,
    )
    residuals.append(residual)


def disable_capabilities(
    journal_path: Path,
    run_id: str,
    only: str | None = None,
) -> tuple[envelope.CapabilityEnvelope, ...]:
    current_intent = intent.require_intent(journal_path, run_id)
    before = {
        cap.name: cap for cap in observe_capabilities(journal_path, current_intent)
    }
    names = (
        (only,)
        if only is not None
        else (
            manifest.CAPABILITY_SPP,
            manifest.CAPABILITY_SPB,
            manifest.CAPABILITY_SPL,
            manifest.CAPABILITY_SCOUT,
        )
    )
    residuals_by_cap: dict[str, list[str]] = {name: [] for name in names}
    for name in names:
        residuals_by_cap[name].extend(
            _disable_preexisting_residuals(name, before.get(name))
        )
        intent.update_capability(
            journal_path,
            run_id,
            name,
            state=intent.INTENT_DISABLE_STARTED,
        )
        try:
            if name == manifest.CAPABILITY_SPP:
                outcome = spp.disable_confidential()
                if outcome.credential_preserved:
                    residuals_by_cap[name].append("spp_credential_ownership_conflict")
            elif name == manifest.CAPABILITY_SPB:
                _disable_spb()
            elif name == manifest.CAPABILITY_SPL:
                spl.disable_spl()
            elif name == manifest.CAPABILITY_SCOUT:
                outcome = scout.disable_scout()
                if outcome.env_key_preserved:
                    residuals_by_cap[name].append("unrelated_manual_key_preserved")
            elif name == manifest.CAPABILITY_RUNTIME:
                continue
        except Exception as exc:
            _record_disable_exception(name, exc, residuals_by_cap[name])
        state = (
            intent.INTENT_FAILED
            if any(
                residual != "unrelated_manual_key_preserved"
                for residual in residuals_by_cap[name]
            )
            else intent.INTENT_DISABLED
        )
        intent.update_capability(
            journal_path,
            run_id,
            name,
            state=state,
            residuals=tuple(residuals_by_cap[name]),
        )

    observed = observe_capabilities(
        journal_path, intent.require_intent(journal_path, run_id)
    )
    adjusted = []
    for cap in observed:
        residuals = list(cap.residuals)
        residuals.extend(residuals_by_cap.get(cap.name, []))
        residuals = list(dict.fromkeys(residuals))
        if cap.name in residuals_by_cap:
            if cap.state == envelope.CAP_READY and not residuals:
                residuals.append("cleanup_still_applied")
            if residuals:
                state = cap.state
                if residuals != ["unrelated_manual_key_preserved"]:
                    state = envelope.CAP_CLEANUP_FAILED
                adjusted.append(
                    envelope.CapabilityEnvelope(cap.name, state, tuple(residuals))
                )
                continue
            adjusted.append(cap)
        else:
            adjusted.append(cap)
    result = tuple(adjusted)
    if all(
        cap.state == envelope.CAP_NOT_APPLIED
        for cap in result
        if cap.name != manifest.CAPABILITY_RUNTIME
    ):
        intent.mark_disabled_if_complete(journal_path, run_id)
    return result
