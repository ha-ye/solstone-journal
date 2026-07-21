# SPDX-License-Identifier: AGPL-3.0-only
# Copyright (c) 2026 sol pbc

"""Thinking app routes."""

from __future__ import annotations

import copy
import logging
import os
from collections.abc import Callable
from datetime import datetime, timezone
from typing import Any
from urllib.parse import urlsplit

from flask import Blueprint, current_app, jsonify, request

from solstone.apps.thinking import copy as thinking_copy
from solstone.apps.thinking import local_bootstrap, local_recovery, scout_lane
from solstone.apps.thinking.copy import thinking_copy_payload
from solstone.apps.thinking.google_model_pins import (
    GOOGLE_MODEL_RESOLUTION_TARGETS_FIELD,
    GOOGLE_PRO_ALIAS_SLOT_TOKENS,
    GOOGLE_PROVIDER,
    read_google_exact_model_advisory,
    read_google_pro_alias_slots,
)
from solstone.apps.thinking.model_tiers import MODEL_TIERS
from solstone.apps.utils import log_app_action
from solstone.convey.reasons import (
    CONFIG_BUSY,
    INVALID_CONFIG_VALUE,
    INVALID_OPERATION_FOR_STATE,
    INVALID_REQUEST_VALUE,
    MISSING_REQUEST_BODY,
    MISSING_REQUIRED_FIELD,
    SERVICE_BUSY,
    SETTINGS_OPERATION_FAILED,
)
from solstone.convey.utils import error_response
from solstone.observe.transcribe.config import confidential_audio_enabled
from solstone.think.brain_health import (
    BrainPresentation,
    build_brain_presentation,
    build_brain_snapshot,
    request_brain_refresh,
)
from solstone.think.journal_config import (
    JournalConfigMutation,
    mutate_journal_config,
)
from solstone.think.journal_io import LockTimeout
from solstone.think.models import (
    DEFAULT_MODEL_BY_PROVIDER,
    LOCAL_MODEL,
    NO_BRAIN_PROVIDER,
    resolve_provider,
)
from solstone.think.providers import (
    PROVIDER_REGISTRY,
    build_provider_status,
    get_provider_list,
    validate_key,
    validate_model,
)
from solstone.think.providers.local_endpoint import (
    normalize_local_endpoint_url,
    resolve_local_endpoint,
)
from solstone.think.providers.runtime_health import (
    RuntimeHealthConflictError,
    RuntimeHealthMalformedError,
    RuntimeHealthUnavailableError,
)
from solstone.think.services import (
    operations,
    scout,
    scout_handoff,
    spp,
    spp_handoff,
    spp_transport,
)
from solstone.think.services.constants import SERVICE_SPP
from solstone.think.utils import CorruptConfigError
from solstone.think.utils import get_config as get_journal_config

logger = logging.getLogger(__name__)

thinking_bp = Blueprint(
    "app:thinking",
    __name__,
    url_prefix="/app/thinking",
    static_folder="static",
    static_url_path="/static",
)

AI_KEY_ENV_VARS = [
    "GOOGLE_API_KEY",
    "ANTHROPIC_API_KEY",
    "OPENAI_API_KEY",
]
AI_ENV_TO_PROVIDER = {
    "GOOGLE_API_KEY": "google",
    "ANTHROPIC_API_KEY": "anthropic",
    "OPENAI_API_KEY": "openai",
}
AI_PROVIDERS = frozenset(AI_ENV_TO_PROVIDER.values())
AI_PROVIDER_TO_ENV = {
    provider: env_var for env_var, provider in AI_ENV_TO_PROVIDER.items()
}
CLOUD_BYO_PROVIDERS = frozenset({"anthropic", "google", "openai"})
LANES = {"byo", "confidential", "local"}
GOOGLE_PRO_ALIAS_TARGETS_TEXT = ", ".join(sorted(GOOGLE_PRO_ALIAS_SLOT_TOKENS))
GENERIC_THINKING_ERROR = (
    "something went wrong - try again, and if it persists, check the health dashboard"
)


def _thinking_operation_failed(detail: str = GENERIC_THINKING_ERROR) -> Any:
    return error_response(SETTINGS_OPERATION_FAILED, detail=detail)


def _config_busy_response() -> Any:
    return error_response(CONFIG_BUSY, detail="settings are busy; try again")


_CONFIDENTIAL_PHASE_TO_PRODUCT = {
    "starting": "starting",
    "waiting": "waiting",
    "enabled": "not_verified",
    "early_access": "early_access",
    "error": "repair_needed",
}


def _remap_confidential_operation(raw: dict[str, Any] | None) -> dict[str, Any] | None:
    if raw is None:
        return None
    payload = dict(raw)
    phase = str(payload.get("phase") or "")
    payload["phase"] = _CONFIDENTIAL_PHASE_TO_PRODUCT.get(phase, phase)
    return payload


def _start_scout_operation(
    kind: str,
    portal_url: str | None,
    flow: Callable[[], operations.HandoffResult],
) -> Any:
    try:
        payload = operations.start_operation("scout", kind, portal_url, flow)
    except operations.OperationBusyError:
        return error_response(SERVICE_BUSY, detail="operation already running")
    return (
        jsonify(
            {
                "success": True,
                "service": "scout",
                "operation": scout_lane.remap_operation(payload),
            }
        ),
        202,
    )


def _start_confidential_operation(
    kind: str,
    portal_url: str | None,
    flow: Callable[[], operations.HandoffResult],
) -> Any:
    try:
        payload = operations.start_operation(SERVICE_SPP, kind, portal_url, flow)
    except operations.OperationBusyError:
        return error_response(SERVICE_BUSY, detail="operation already running")
    return (
        jsonify(
            {
                "success": True,
                "service": SERVICE_SPP,
                "operation": _remap_confidential_operation(payload),
            }
        ),
        202,
    )


def _read_local_provider_config(config: dict[str, Any]) -> dict[str, Any]:
    providers_config = config.get("providers", {})
    if not isinstance(providers_config, dict):
        return {}
    local_config = providers_config.get("local", {})
    return local_config if isinstance(local_config, dict) else {}


def _ensure_local_provider_config(config: dict[str, Any]) -> dict[str, Any]:
    providers_config = config.get("providers")
    if not isinstance(providers_config, dict):
        providers_config = {}
        config["providers"] = providers_config
    local_config = providers_config.get("local")
    if not isinstance(local_config, dict):
        local_config = {}
        providers_config["local"] = local_config
    return local_config


def _local_credential_configured(local_config: dict[str, Any]) -> bool:
    return bool(str(local_config.get("credential") or "").strip())


def _local_endpoint_public_payload(config: dict[str, Any]) -> dict[str, object]:
    local_config = _read_local_provider_config(config)
    endpoint_url = str(local_config.get("endpoint_url") or "").strip()
    served_model_id = str(local_config.get("served_model_id") or "").strip()
    return {
        "enabled": bool(endpoint_url and served_model_id),
        "endpoint_url": endpoint_url,
        "served_model_id": served_model_id,
        "credential_configured": _local_credential_configured(local_config),
    }


def _local_override_payload(config: dict[str, Any]) -> dict[str, object]:
    endpoint = resolve_local_endpoint()
    local_config = _read_local_provider_config(config)
    return {
        "enabled": not endpoint.is_bundled,
        "endpoint_url": "" if endpoint.is_bundled else endpoint.base_url,
        "served_model_id": "" if endpoint.is_bundled else endpoint.served_model_id,
        "credential_configured": _local_credential_configured(local_config),
    }


def _masked_local_endpoint_changes(
    before: dict[str, Any],
    after: dict[str, Any],
    *,
    credential_touched: bool,
) -> dict[str, dict[str, object]]:
    changed_fields: dict[str, dict[str, object]] = {}
    for key in ("endpoint_url", "served_model_id"):
        old_value = str(before.get(key) or "")
        new_value = str(after.get(key) or "")
        if old_value != new_value:
            changed_fields[key] = {"old": old_value, "new": new_value}

    if credential_touched:
        old_credential = str(before.get("credential") or "")
        new_credential = str(after.get("credential") or "")
        if old_credential != new_credential:
            changed_fields["credential"] = {
                "old": "***" if old_credential else "",
                "new": "***" if new_credential else "",
            }
    return changed_fields


def _validate_local_endpoint_url(endpoint_url: str) -> str | Any:
    normalized = normalize_local_endpoint_url(endpoint_url)
    parsed = urlsplit(normalized)
    if parsed.scheme not in {"http", "https"} or not parsed.netloc:
        return error_response(
            INVALID_CONFIG_VALUE,
            detail="endpoint_url must be an http or https URL with a host",
        )
    return normalized


def _active_settings(providers_config: dict[str, Any]) -> dict[str, Any]:
    active_config = providers_config.get("active", {})
    if not isinstance(active_config, dict):
        active_config = {}
    provider, model = resolve_provider("generate")
    return {
        "provider": provider,
        "model": active_config.get("model") or model,
    }


def _lane_for_provider(
    provider: str,
    *,
    local_endpoint_configured: bool,
    confidential_provenance_present: bool,
) -> str:
    if provider == NO_BRAIN_PROVIDER:
        return "none"
    if provider == "local":
        if local_endpoint_configured:
            return "confidential" if confidential_provenance_present else "byo"
        return "local"
    return "byo"


def _local_endpoint_configured_for_config(config: dict[str, Any]) -> bool:
    local_config = _read_local_provider_config(config)
    endpoint_url = str(local_config.get("endpoint_url") or "").strip()
    served_model_id = str(local_config.get("served_model_id") or "").strip()
    return bool(endpoint_url and served_model_id)


def _confidential_lane_active_for_config(config: dict[str, Any]) -> bool:
    providers_config = config.get("providers")
    if not isinstance(providers_config, dict):
        providers_config = {}
    active_config = providers_config.get("active")
    if not isinstance(active_config, dict):
        active_config = {}
    provider = active_config.get("provider")
    provider = provider if isinstance(provider, str) else ""
    return (
        _lane_for_provider(
            provider,
            local_endpoint_configured=_local_endpoint_configured_for_config(config),
            confidential_provenance_present=(
                spp.confidential_provenance_block(dict(config)) is not None
            ),
        )
        == "confidential"
    )


def _active_lane_payload(
    active_settings: dict[str, Any],
    transcribe_config: dict[str, Any],
    *,
    presentation: BrainPresentation,
    confidential_provenance_present: bool,
) -> dict[str, Any]:
    endpoint = resolve_local_endpoint()
    local_endpoint_configured = not endpoint.is_bundled
    active = _lane_for_provider(
        str(active_settings.get("provider") or ""),
        local_endpoint_configured=local_endpoint_configured,
        confidential_provenance_present=confidential_provenance_present,
    )
    return {
        "lane": active,
        "scout_enabled": scout.is_scout_enabled(),
        "scout_provenance_configured": scout.scout_provenance() is not None,
        "confidential_enabled": spp.is_confidential_enabled(),
        "confidential_audio": confidential_audio_enabled(transcribe_config),
        "confidential_provenance_configured": confidential_provenance_present,
        "confidential_operation": _remap_confidential_operation(
            operations.operation_for_service(SERVICE_SPP)
        ),
        "confidential_attestation": presentation["confidential_attestation"],
    }


def _api_key_status(config: dict[str, Any]) -> dict[str, bool]:
    env_config = config.get("env", {})
    return {
        provider: bool(env_config.get(env_var) or os.getenv(env_var))
        for env_var, provider in AI_ENV_TO_PROVIDER.items()
    }


def _env_key_status(config: dict[str, Any]) -> dict[str, bool]:
    env_config = config.get("env", {})
    return {
        env_var: bool(env_config.get(env_var) or os.getenv(env_var))
        for env_var in AI_KEY_ENV_VARS
    }


def _filtered_ai_key_validation(config: dict[str, Any]) -> dict[str, Any]:
    key_validation = config.get("providers", {}).get("key_validation", {})
    if not isinstance(key_validation, dict):
        return {}
    return {
        key: value
        for key, value in key_validation.items()
        if key in {"google", "openai", "anthropic"}
    }


def _keys_payload(config: dict[str, Any]) -> dict[str, Any]:
    return {
        "api_keys": _api_key_status(config),
        "env": _env_key_status(config),
        "key_validation": _filtered_ai_key_validation(config),
        "scout_enabled": scout.is_scout_enabled(),
    }


def _validation_payload(result: dict[str, Any], **identity: str) -> dict[str, Any]:
    """Map a transport validation result to a browser-safe payload.

    The browser `api()` wrapper throws on any top-level `error`, so failures
    carry `message` instead.
    """

    payload: dict[str, Any] = {"valid": result.get("valid") is True, **identity}
    if not payload["valid"]:
        payload["reason_code"] = result.get("reason_code")
        payload["message"] = result.get("error") or ""
    return payload


def _compute_ai_key_validation(config: dict[str, Any]) -> dict[str, Any]:
    """Validate configured AI provider keys without mutating config."""

    env_config = config.get("env", {})
    if not isinstance(env_config, dict):
        env_config = {}
    key_validation: dict[str, Any] = {}

    for env_var, provider in AI_ENV_TO_PROVIDER.items():
        api_key = env_config.get(env_var, "")
        if api_key:
            result = validate_key(provider, api_key)
            result["timestamp"] = datetime.now(timezone.utc).isoformat()
            key_validation[provider] = result

    return key_validation


def _stored_api_key(config: dict[str, Any], provider: str) -> str:
    env_config = config.get("env", {})
    if not isinstance(env_config, dict):
        return ""
    return str(env_config.get(AI_PROVIDER_TO_ENV[provider]) or "").strip()


def _validate_cloud_byo_provider(provider: Any) -> str | Any:
    if provider not in CLOUD_BYO_PROVIDERS:
        return error_response(
            INVALID_REQUEST_VALUE,
            detail=(
                f"Invalid provider: {provider}. "
                f"Must be one of: {', '.join(sorted(CLOUD_BYO_PROVIDERS))}"
            ),
        )
    return str(provider)


def _validate_top_level_model(
    model: Any,
    *,
    error_code: str = INVALID_CONFIG_VALUE,
) -> str | Any:
    if not isinstance(model, str) or not model.strip():
        return error_response(
            error_code,
            detail="model must be a non-empty string.",
        )
    return model.strip()


def _validate_google_model_resolution_targets(value: Any) -> list[str] | Any:
    if not isinstance(value, list):
        return error_response(
            INVALID_CONFIG_VALUE,
            detail=(
                f"{GOOGLE_MODEL_RESOLUTION_TARGETS_FIELD} must be a list of: "
                f"{GOOGLE_PRO_ALIAS_TARGETS_TEXT}"
            ),
        )
    targets: list[str] = []
    unknown: list[str] = []
    for item in value:
        if not isinstance(item, str) or item not in GOOGLE_PRO_ALIAS_SLOT_TOKENS:
            unknown.append(str(item))
            continue
        if item not in targets:
            targets.append(item)
    if unknown:
        return error_response(
            INVALID_CONFIG_VALUE,
            detail=(
                "Invalid Google model resolution targets: "
                f"{', '.join(sorted(unknown))}. Must be one of: "
                f"{GOOGLE_PRO_ALIAS_TARGETS_TEXT}"
            ),
        )
    return targets


def _provider_status_payload(
    providers_list: list[dict[str, Any]],
    presentation: BrainPresentation,
) -> dict[str, dict[str, Any]]:
    if not presentation["spp_active"]:
        return build_provider_status(providers_list)
    return build_provider_status(
        providers_list,
        local_status={
            "configured": True,
            "selected": True,
            "generate_ready": presentation["spp_readiness"]["generate_ready"],
            "cogitate_ready": presentation["spp_readiness"]["cogitate_ready"],
            "issues": list(presentation["spp_readiness"]["issues"]),
        },
    )


def _provider_payload(config: dict[str, Any], local_model_id: str) -> dict[str, Any]:
    providers_config = config.get("providers", {})
    if not isinstance(providers_config, dict):
        providers_config = {}

    active_settings = _active_settings(providers_config)

    providers_list = get_provider_list()
    local_status = local_bootstrap.get_state(local_model_id)
    confidential_provenance_present = spp.confidential_provenance() is not None
    presentation = build_brain_presentation(
        datetime.now(timezone.utc),
        surface="thinking",
        spp_configured=confidential_provenance_present,
    )

    return {
        "providers": providers_list,
        "provider_status": _provider_status_payload(providers_list, presentation),
        "brain": presentation["brain"],
        "active_lane": _active_lane_payload(
            active_settings,
            config.get("transcribe", {})
            if isinstance(config.get("transcribe", {}), dict)
            else {},
            presentation=presentation,
            confidential_provenance_present=confidential_provenance_present,
        ),
        "active": active_settings,
        "model_tiers": MODEL_TIERS,
        "configuration_guidance": read_google_exact_model_advisory(config),
        "byo_models": providers_config.get("byo_models", {}),
        "api_keys": _api_key_status(config),
        "key_validation": _filtered_ai_key_validation(config),
        "local": local_status,
        "local_runtime": local_recovery.runtime_view(),
        "local_override": _local_override_payload(config),
        "local_backend": "mlx" if local_bootstrap._is_mlx_backend() else "local",
        "scout_enabled": scout.is_scout_enabled(),
    }


def _local_model_error(model: str) -> Any:
    return error_response(
        INVALID_REQUEST_VALUE,
        detail=(
            f"Unknown local model: {model}. "
            f"Must be one of: {', '.join(local_bootstrap.local_model_ids())}"
        ),
    )


def _local_model_from_request() -> tuple[str | None, Any | None]:
    raw = request.args.get("model")
    model = local_bootstrap.accepted_request_model(raw)
    if model is None:
        return None, _local_model_error(raw or LOCAL_MODEL)
    return model, None


def _initial_payload() -> dict[str, Any]:
    try:
        config = get_journal_config()
        local_model_id = local_bootstrap.accepted_request_model(None) or LOCAL_MODEL
        return {
            "providers": _provider_payload(config, local_model_id),
            "keys": _keys_payload(config),
        }
    except Exception:
        logger.exception("error loading initial thinking payload")
        return {"providers": {}, "keys": {}}


@thinking_bp.route("/")
def index() -> Any:
    return current_app.send_static_file("shell.html")


@thinking_bp.route("/api/state")
def api_state() -> Any:
    payload = _initial_payload()
    return jsonify(
        {
            "providers": payload.get("providers", {}),
            "keys": payload.get("keys", {}),
            "copy": thinking_copy_payload(),
        }
    )


@thinking_bp.route("/api/scout")
def scout_status() -> Any:
    try:
        return jsonify({"success": True, **scout_lane.status_payload()})
    except Exception:
        logger.exception("error loading scout status")
        return _thinking_operation_failed()


@thinking_bp.route("/api/scout/check", methods=["POST"])
def scout_check() -> Any:
    try:
        return jsonify({"success": True, **scout_lane.status_payload(force=True)})
    except Exception:
        logger.exception("error checking scout status")
        return _thinking_operation_failed()


@thinking_bp.route("/api/scout/enable", methods=["POST"])
def scout_enable() -> Any:
    try:
        state = scout_lane.resting_state()
        if state == thinking_copy.SCOUT_STATE_ON:
            return error_response(
                INVALID_OPERATION_FOR_STATE,
                detail="Scout is already on.",
            )
        if state == thinking_copy.SCOUT_STATE_MANUAL_KEY_PRESENT:
            return error_response(
                INVALID_OPERATION_FOR_STATE,
                detail=thinking_copy.SCOUT_MANUAL_KEY_BLOCK_COPY,
            )
        consent_url, nonce, base_url = scout_handoff.build_scout_handoff_url()
        return _start_scout_operation(
            "enable",
            consent_url,
            lambda: scout_handoff.run_scout_handoff(
                refresh=False,
                nonce=nonce,
                base_url=base_url,
            ),
        )
    except Exception:
        logger.exception("error enabling scout")
        return _thinking_operation_failed()


@thinking_bp.route("/api/scout/refresh", methods=["POST"])
def scout_refresh() -> Any:
    try:
        state = scout_lane.resting_state()
        if state not in {
            thinking_copy.SCOUT_STATE_REQUESTED,
            thinking_copy.SCOUT_STATE_ON,
        }:
            return error_response(
                INVALID_OPERATION_FOR_STATE,
                detail="Scout refresh isn't available right now.",
            )
        consent_url, nonce, base_url = scout_handoff.build_scout_handoff_url()
        return _start_scout_operation(
            "refresh",
            consent_url,
            lambda: scout_handoff.run_scout_handoff(
                refresh=True,
                nonce=nonce,
                base_url=base_url,
            ),
        )
    except Exception:
        logger.exception("error refreshing scout")
        return _thinking_operation_failed()


@thinking_bp.route("/api/scout/disable", methods=["POST"])
def scout_disable() -> Any:
    try:
        outcome = scout.disable_scout()
        return jsonify(
            {
                "success": True,
                "service": "scout",
                "result": {
                    "was_enabled": outcome.was_enabled,
                    "env_key_preserved": outcome.env_key_preserved,
                },
                "status": scout_lane.status_payload(),
            }
        )
    except Exception:
        logger.exception("error disabling scout")
        return _thinking_operation_failed()


@thinking_bp.route("/api/confidential/enable", methods=["POST"])
def confidential_enable() -> Any:
    try:
        if spp.confidential_provenance() is not None:
            return error_response(
                INVALID_OPERATION_FOR_STATE,
                detail="confidential processing is already set up.",
            )
        consent_url, nonce, base_url = spp_handoff.build_confidential_handoff_url()
        return _start_confidential_operation(
            "enable",
            consent_url,
            lambda: spp_handoff.run_confidential_handoff(
                refresh=False,
                nonce=nonce,
                base_url=base_url,
            ),
        )
    except Exception:
        logger.exception("error enabling confidential processing")
        return _thinking_operation_failed()


@thinking_bp.route("/api/confidential/disable", methods=["POST"])
def confidential_disable() -> Any:
    try:
        outcome = spp.disable_confidential()
        spp_transport.teardown_confidential_transport()
        return jsonify(
            {
                "success": True,
                "service": SERVICE_SPP,
                "result": {
                    "was_enabled": outcome.was_enabled,
                    "credential_preserved": outcome.credential_preserved,
                },
            }
        )
    except Exception:
        logger.exception("error disabling confidential processing")
        return _thinking_operation_failed()


@thinking_bp.route("/api/confidential/recheck", methods=["POST"])
def confidential_recheck() -> Any:
    try:
        confidential_provenance_present = spp.confidential_provenance() is not None
        presentation = build_brain_presentation(
            datetime.now(timezone.utc),
            surface="thinking",
            spp_configured=confidential_provenance_present,
        )
        if presentation["confidential_attestation"]["state"] in {"off", "inactive"}:
            return error_response(
                INVALID_OPERATION_FOR_STATE,
                detail="confidential processing is not active.",
            )
        return _brain_check_response()
    except Exception:
        logger.exception("error rechecking confidential processing")
        return _thinking_operation_failed()


@thinking_bp.route("/api/keys/check", methods=["POST"])
def keys_check() -> Any:
    try:
        request_data = request.get_json(silent=True)
        if not isinstance(request_data, dict):
            return error_response(MISSING_REQUEST_BODY, detail="No data provided")
        env_var = request_data.get("env_var")
        if not isinstance(env_var, str) or env_var not in AI_KEY_ENV_VARS:
            return error_response(
                INVALID_CONFIG_VALUE,
                detail=f"Invalid env var: {env_var}. Must be one of: {', '.join(AI_KEY_ENV_VARS)}",
            )
        value = request_data.get("value", "")
        if value is not None and not isinstance(value, str):
            return error_response(
                INVALID_REQUEST_VALUE, detail="value must be a string"
            )
        candidate = str(value or "").strip()
        if not candidate:
            return error_response(
                INVALID_REQUEST_VALUE, detail="value must not be empty"
            )
        provider = AI_ENV_TO_PROVIDER[env_var]
        result = validate_key(provider, candidate)
        return jsonify(_validation_payload(result, provider=provider))
    except Exception:
        logger.exception("error checking thinking key")
        return _thinking_operation_failed()


@thinking_bp.route("/api/keys", methods=["GET", "PUT"])
def keys() -> Any:
    try:
        if request.method == "GET":
            return jsonify(_keys_payload(get_journal_config()))

        request_data = request.get_json(silent=True)
        if not isinstance(request_data, dict):
            return error_response(MISSING_REQUEST_BODY, detail="No data provided")
        env_var = request_data.get("env_var") or request_data.get("key")
        if not isinstance(env_var, str) or env_var not in AI_KEY_ENV_VARS:
            return error_response(
                INVALID_CONFIG_VALUE,
                detail=f"Invalid env var: {env_var}. Must be one of: {', '.join(AI_KEY_ENV_VARS)}",
            )
        value = request_data.get("value", "")
        if value is not None and not isinstance(value, str):
            return error_response(
                INVALID_REQUEST_VALUE, detail="value must be a string"
            )
        provider = AI_ENV_TO_PROVIDER[env_var]

        new_value = str(value or "").strip()
        validation = None
        if new_value:
            validation = validate_key(provider, new_value)
            validation["timestamp"] = datetime.now(timezone.utc).isoformat()

        def apply(config: dict[str, Any]) -> JournalConfigMutation[dict[str, Any]]:
            env = config.get("env")
            if not isinstance(env, dict):
                env = {}
                config["env"] = env
            providers_config = config.get("providers")
            if not isinstance(providers_config, dict):
                providers_config = {}
                config["providers"] = providers_config
            key_validation = providers_config.get("key_validation")
            if not isinstance(key_validation, dict):
                key_validation = {}
                providers_config["key_validation"] = key_validation
            old_value = env.get(env_var)
            if new_value:
                env[env_var] = new_value
                assert validation is not None
                prior_validation = key_validation.get(provider)
                key_validation[provider] = validation
            else:
                prior_validation = key_validation.get(provider)
                env.pop(env_var, None)
                key_validation.pop(provider, None)
                byo_models = providers_config.get("byo_models")
                if isinstance(byo_models, dict):
                    byo_models.pop(provider, None)
            next_validation = key_validation.get(provider)
            changed = (
                old_value != (new_value or None) or prior_validation != next_validation
            )
            return JournalConfigMutation(
                changed=changed,
                value={
                    "config": config,
                    "old_value": old_value,
                },
            )

        result = mutate_journal_config(apply)
        config = result.value["config"]
        old_value = result.value["old_value"]
        if new_value:
            os.environ[env_var] = new_value
        else:
            os.environ.pop(env_var, None)

        if old_value != (str(value or "").strip() or None):
            log_app_action(
                app="thinking",
                facet=None,
                action="env_update",
                params={"changed_fields": {env_var: {"old": "***", "new": "***"}}},
            )

        return jsonify(
            {
                "success": True,
                "env_var": env_var,
                "set": bool(str(value or "").strip()),
                "validation": validation,
                **_keys_payload(config),
            }
        )
    except CorruptConfigError:
        raise
    except LockTimeout:
        return _config_busy_response()
    except Exception:
        logger.exception("error updating thinking keys")
        return _thinking_operation_failed()


@thinking_bp.route("/api/validate-keys", methods=["GET", "POST"])
def validate_all_keys() -> Any:
    """Re-validate configured AI keys."""

    try:
        config = get_journal_config()
        snapshot_env = config.get("env", {})
        if not isinstance(snapshot_env, dict):
            snapshot_env = {}
        validated_values = {
            provider: str(snapshot_env.get(env_var) or "").strip()
            for env_var, provider in AI_ENV_TO_PROVIDER.items()
        }
        key_validation = _compute_ai_key_validation(config)
        if request.method == "GET":
            return jsonify({"key_validation": key_validation})

        def apply(config: dict[str, Any]) -> JournalConfigMutation[dict[str, Any]]:
            providers_config = config.get("providers")
            if not isinstance(providers_config, dict):
                providers_config = {}
                config["providers"] = providers_config
            existing = providers_config.get("key_validation")
            if not isinstance(existing, dict):
                existing = {}
                providers_config["key_validation"] = existing
            current_env = config.get("env", {})
            if not isinstance(current_env, dict):
                current_env = {}
            changed = False
            for env_var, provider in AI_ENV_TO_PROVIDER.items():
                current_value = str(current_env.get(env_var) or "").strip()
                if current_value != validated_values[provider]:
                    continue
                result = key_validation.get(provider)
                if result is None:
                    if provider in existing:
                        existing.pop(provider, None)
                        changed = True
                elif existing.get(provider) != result:
                    existing[provider] = result
                    changed = True
            persisted = {
                provider: existing[provider]
                for provider in AI_ENV_TO_PROVIDER.values()
                if provider in existing
            }
            return JournalConfigMutation(changed=changed, value=persisted)

        persisted = mutate_journal_config(apply).value
        return jsonify({"success": True, "key_validation": persisted})
    except LockTimeout:
        return _config_busy_response()
    except Exception:
        logger.exception("error validating thinking keys")
        return _thinking_operation_failed()


@thinking_bp.route("/api/validate-model", methods=["POST"])
def validate_model_route() -> Any:
    """Validate that a stored provider key can see a model."""

    try:
        request_data = request.get_json(silent=True)
        if not isinstance(request_data, dict) or not request_data:
            return error_response(MISSING_REQUEST_BODY, detail="No data provided")

        provider = _validate_cloud_byo_provider(request_data.get("provider"))
        if not isinstance(provider, str):
            return provider

        model = _validate_top_level_model(
            request_data.get("model"),
            error_code=INVALID_REQUEST_VALUE,
        )
        if not isinstance(model, str):
            return model

        config = get_journal_config()
        api_key = _stored_api_key(config, provider)
        if not api_key:
            return jsonify(
                _validation_payload(
                    {
                        "valid": False,
                        "reason_code": "key_missing",
                        "error": "No stored API key for provider.",
                    },
                    provider=provider,
                    model=model,
                )
            )

        result = validate_model(provider, model, api_key)
        return jsonify(_validation_payload(result, provider=provider, model=model))
    except Exception:
        logger.exception("error validating thinking model")
        return _thinking_operation_failed()


@thinking_bp.route("/api/local/availability")
def get_local_availability() -> Any:
    try:
        model, error = _local_model_from_request()
        if error is not None:
            return error
        assert model is not None
        return jsonify(local_bootstrap.get_availability_payload(model))
    except Exception:
        logger.exception("error loading local provider availability")
        return _thinking_operation_failed()


@thinking_bp.route("/api/local/bootstrap", methods=["POST"])
def start_local_bootstrap() -> Any:
    try:
        model, error = _local_model_from_request()
        if error is not None:
            return error
        assert model is not None
        payload, status = local_bootstrap.start_bootstrap(model)
        return jsonify(payload), status
    except local_bootstrap.LocalBootstrapUnavailableError as exc:
        return error_response(INVALID_REQUEST_VALUE, detail=str(exc))
    except local_bootstrap.LocalBootstrapStartError as exc:
        logger.exception("error starting local provider bootstrap")
        return _thinking_operation_failed(str(exc))
    except Exception:
        logger.exception("error starting local provider bootstrap")
        return _thinking_operation_failed()


@thinking_bp.route("/api/local/bootstrap/status")
def get_local_bootstrap_status() -> Any:
    try:
        model, error = _local_model_from_request()
        if error is not None:
            return error
        assert model is not None
        return jsonify(local_bootstrap.get_state(model))
    except Exception:
        logger.exception("error loading local provider bootstrap status")
        return _thinking_operation_failed()


@thinking_bp.route("/api/local/runtime")
def get_local_runtime() -> Any:
    try:
        return jsonify(local_recovery.runtime_view())
    except Exception:
        logger.exception("error loading local runtime recovery state")
        return _thinking_operation_failed()


@thinking_bp.route("/api/local/runtime/retry", methods=["POST"])
def retry_local_runtime() -> Any:
    request_data = request.get_json(silent=True)
    if not isinstance(request_data, dict):
        return error_response(MISSING_REQUEST_BODY, detail="No data provided")
    expected_fields = {
        "health_revision",
        "retry_revision",
        "desired_fingerprint_sha256",
    }
    if set(request_data) != expected_fields:
        return error_response(
            INVALID_REQUEST_VALUE,
            detail="runtime retry requires the current recovery state",
        )
    health_revision = request_data["health_revision"]
    retry_revision = request_data["retry_revision"]
    desired_fingerprint = request_data["desired_fingerprint_sha256"]
    if (
        isinstance(health_revision, bool)
        or not isinstance(health_revision, int)
        or health_revision < 0
        or isinstance(retry_revision, bool)
        or not isinstance(retry_revision, int)
        or retry_revision < 0
        or not isinstance(desired_fingerprint, str)
        or not desired_fingerprint
    ):
        return error_response(
            INVALID_REQUEST_VALUE,
            detail="runtime retry requires the current recovery state",
        )
    try:
        return jsonify(
            local_recovery.request_retry(
                health_revision=health_revision,
                retry_revision=retry_revision,
                desired_fingerprint_sha256=desired_fingerprint,
            )
        )
    except RuntimeHealthConflictError:
        return error_response(
            INVALID_OPERATION_FOR_STATE,
            detail="local status changed; check again",
        )
    except (RuntimeHealthMalformedError, RuntimeHealthUnavailableError):
        logger.exception("local runtime retry state is unavailable")
        return _thinking_operation_failed(
            "local status can't be changed right now; check again"
        )
    except Exception:
        logger.exception("error requesting local runtime retry")
        return _thinking_operation_failed()


@thinking_bp.route("/api/local/models")
def get_local_models() -> Any:
    try:
        return jsonify(local_bootstrap.list_local_models())
    except Exception:
        logger.exception("error loading local provider models")
        return _thinking_operation_failed()


@thinking_bp.route("/api/local/endpoint", methods=["POST"])
def update_local_endpoint() -> Any:
    try:
        request_data = request.get_json(silent=True)
        if not isinstance(request_data, dict):
            return error_response(MISSING_REQUEST_BODY, detail="No data provided")

        raw_endpoint_url = request_data.get("endpoint_url")
        if not isinstance(raw_endpoint_url, str) or not raw_endpoint_url.strip():
            return error_response(MISSING_REQUIRED_FIELD, detail="endpoint_url")
        endpoint_url = _validate_local_endpoint_url(raw_endpoint_url)
        if not isinstance(endpoint_url, str):
            return endpoint_url

        raw_served_model_id = request_data.get("served_model_id")
        if not isinstance(raw_served_model_id, str) or not raw_served_model_id.strip():
            return error_response(MISSING_REQUIRED_FIELD, detail="served_model_id")
        served_model_id = raw_served_model_id.strip()

        credential_touched = "credential" in request_data
        raw_credential = request_data.get("credential")
        if (
            credential_touched
            and raw_credential is not None
            and not isinstance(raw_credential, str)
        ):
            return error_response(INVALID_REQUEST_VALUE, detail="credential")

        def apply(config: dict[str, Any]) -> JournalConfigMutation[dict[str, Any]]:
            local_config = _ensure_local_provider_config(config)
            before = dict(local_config)
            local_config["endpoint_url"] = endpoint_url
            local_config["served_model_id"] = served_model_id
            if credential_touched:
                credential = str(raw_credential or "").strip()
                if credential:
                    local_config["credential"] = credential
                else:
                    local_config.pop("credential", None)
            changed_fields = _masked_local_endpoint_changes(
                before,
                local_config,
                credential_touched=credential_touched,
            )
            return JournalConfigMutation(
                changed=bool(changed_fields),
                value={
                    "config": config,
                    "changed_fields": changed_fields,
                },
            )

        result = mutate_journal_config(apply)
        config = result.value["config"]
        changed_fields = result.value["changed_fields"]
        if changed_fields:
            log_app_action(
                app="thinking",
                facet=None,
                action="local_endpoint_update",
                params={"changed_fields": changed_fields},
            )
        return jsonify(
            {
                "success": True,
                "local_endpoint": _local_endpoint_public_payload(config),
            }
        )
    except CorruptConfigError:
        raise
    except LockTimeout:
        return _config_busy_response()
    except Exception:
        logger.exception("error updating local endpoint")
        return _thinking_operation_failed()


@thinking_bp.route("/api/local/endpoint", methods=["DELETE"])
def clear_local_endpoint() -> Any:
    try:

        def apply(config: dict[str, Any]) -> JournalConfigMutation[dict[str, Any]]:
            local_config = _ensure_local_provider_config(config)
            before = dict(local_config)
            for key in ("endpoint_url", "served_model_id", "credential"):
                local_config.pop(key, None)
            changed_fields = _masked_local_endpoint_changes(
                before,
                local_config,
                credential_touched=True,
            )
            return JournalConfigMutation(
                changed=bool(changed_fields),
                value={
                    "config": config,
                    "changed_fields": changed_fields,
                },
            )

        result = mutate_journal_config(apply)
        config = result.value["config"]
        changed_fields = result.value["changed_fields"]
        if changed_fields:
            log_app_action(
                app="thinking",
                facet=None,
                action="local_endpoint_clear",
                params={"changed_fields": changed_fields},
            )
        return jsonify(
            {
                "success": True,
                "local_endpoint": _local_endpoint_public_payload(config),
            }
        )
    except CorruptConfigError:
        raise
    except LockTimeout:
        return _config_busy_response()
    except Exception:
        logger.exception("error clearing local endpoint")
        return _thinking_operation_failed()


@thinking_bp.route("/api/providers")
def get_providers() -> Any:
    try:
        config = get_journal_config()
        raw_local_model = request.args.get("local_model")
        local_model_id = local_bootstrap.accepted_request_model(raw_local_model)
        if local_model_id is None:
            return _local_model_error(raw_local_model or LOCAL_MODEL)
        return jsonify(_provider_payload(config, local_model_id))
    except Exception:
        logger.exception("error loading providers")
        return _thinking_operation_failed()


def _brain_check_response() -> Any:
    ok = request_brain_refresh(surface="thinking")
    try:
        brain = build_brain_snapshot(datetime.now(timezone.utc), surface="thinking")
    except Exception:
        logger.exception("error loading brain health")
        return _thinking_operation_failed()
    response: dict[str, Any] = {"ok": ok, "brain": brain}
    if not ok:
        response["error"] = "check_not_started"
    return jsonify(response)


@thinking_bp.post("/api/brain/check")
def check_brain() -> Any:
    return _brain_check_response()


@thinking_bp.route("/api/providers/local/status")
def get_local_provider_status() -> Any:
    """Return local provider readiness status."""

    try:
        confidential_provenance_present = spp.confidential_provenance() is not None
        presentation = build_brain_presentation(
            datetime.now(timezone.utc),
            surface="thinking",
            spp_configured=confidential_provenance_present,
        )
        providers_list = get_provider_list()
        local_provider = next(
            provider for provider in providers_list if provider["name"] == "local"
        )
        provider_status = _provider_status_payload([local_provider], presentation)
        return jsonify(provider_status["local"])
    except Exception:
        logger.exception("error loading local provider status")
        return _thinking_operation_failed()


def _validate_provider(provider: Any, field: str = "provider") -> str | Any:
    if provider not in PROVIDER_REGISTRY:
        return error_response(
            INVALID_CONFIG_VALUE,
            detail=(
                f"Invalid {field}: {provider}. "
                f"Must be one of: {', '.join(sorted(PROVIDER_REGISTRY.keys()))}"
            ),
        )
    return str(provider)


def _remember_byo_model(
    config: dict[str, Any],
    old_providers: dict[str, Any],
    changed_fields: dict[str, Any],
    provider: str,
    model: str,
) -> None:
    byo_models = config["providers"].get("byo_models")
    if not isinstance(byo_models, dict):
        byo_models = {}
        config["providers"]["byo_models"] = byo_models
    old_byo_models = old_providers.get("byo_models")
    if not isinstance(old_byo_models, dict):
        old_byo_models = {}
    old_model = old_byo_models.get(provider)
    if old_model != model:
        changed_fields[f"byo_models.{provider}"] = {
            "old": old_model,
            "new": model,
        }
    byo_models[provider] = model


def _set_active_provider(
    config: dict[str, Any],
    old_providers: dict[str, Any],
    changed_fields: dict[str, Any],
    provider: str,
    model: str | None,
) -> None:
    old_active = old_providers.get("active", {})
    if not isinstance(old_active, dict):
        old_active = {}

    if model is None:
        if old_active.get("provider") == provider:
            old_model = old_active.get("model")
            if isinstance(old_model, str) and old_model.strip():
                model = old_model.strip()
        if model is None and provider in CLOUD_BYO_PROVIDERS:
            remembered_models = config["providers"].get("byo_models")
            if not isinstance(remembered_models, dict):
                remembered_models = {}
            remembered = remembered_models.get(provider)
            if isinstance(remembered, str) and remembered.strip():
                model = remembered.strip()
        if model is None:
            model = DEFAULT_MODEL_BY_PROVIDER[provider]

    new_active = {"provider": provider, "model": model}
    for field, value in new_active.items():
        if old_active.get(field) != value:
            changed_fields[f"active.{field}"] = {
                "old": old_active.get(field),
                "new": value,
            }
    config["providers"]["active"] = new_active


def _update_confidential_prior_model(
    config: dict[str, Any],
    changed_fields: dict[str, Any],
    model: str,
) -> None:
    services = config.get("services")
    if isinstance(services, dict):
        confidential = services.get("confidential")
    else:
        confidential = None
    if not isinstance(confidential, dict):
        return
    prior_active = confidential.get("prior_active")
    if not isinstance(prior_active, dict):
        return
    old_model = prior_active.get("model")
    if old_model == model:
        return
    changed_fields["services.confidential.prior_active.model"] = {
        "old": old_model,
        "new": model,
    }
    prior_active["model"] = model


def _lane_provider(request_data: dict[str, Any]) -> str | Any:
    lane = request_data["lane"]
    if lane not in LANES:
        return error_response(
            INVALID_CONFIG_VALUE,
            detail=f"Invalid lane: {lane}. Must be one of: {', '.join(sorted(LANES))}",
        )
    endpoint = resolve_local_endpoint()
    local_endpoint_configured = not endpoint.is_bundled
    if lane == "confidential":
        if spp.confidential_provenance() is None:
            return error_response(
                INVALID_OPERATION_FOR_STATE,
                detail="confidential lane activation must use the confidential enable flow.",
            )
        return "local"
    if lane == "local":
        if local_endpoint_configured:
            return error_response(
                INVALID_OPERATION_FOR_STATE,
                detail="clear your endpoint URL first to run the bundled local model.",
            )
        return "local"
    provider = request_data.get("provider")
    if provider in {None, ""}:
        return error_response(
            INVALID_CONFIG_VALUE,
            detail="No BYO provider selected. Must be one of: anthropic, google, local, openai",
        )
    if provider not in {"anthropic", "google", "local", "openai"}:
        return error_response(
            INVALID_CONFIG_VALUE,
            detail="Invalid provider for BYO lane. Must be one of: anthropic, google, local, openai",
        )
    if provider == "local" and not local_endpoint_configured:
        return error_response(
            INVALID_OPERATION_FOR_STATE,
            detail="save your endpoint URL first to use your own endpoint.",
        )
    return str(provider)


@thinking_bp.route("/api/providers", methods=["PUT", "POST"])
def update_providers() -> Any:
    try:
        request_data = request.get_json(silent=True)
        if not isinstance(request_data, dict) or not request_data:
            return error_response(MISSING_REQUEST_BODY, detail="No data provided")
        unknown = set(request_data) - {
            "lane",
            "provider",
            "model",
            GOOGLE_MODEL_RESOLUTION_TARGETS_FIELD,
        }
        if unknown:
            return error_response(
                INVALID_CONFIG_VALUE,
                detail=f"Unknown provider fields: {', '.join(sorted(unknown))}",
            )
        if "lane" not in request_data:
            return error_response(MISSING_REQUIRED_FIELD, detail="lane")
        has_resolution_targets = GOOGLE_MODEL_RESOLUTION_TARGETS_FIELD in request_data
        requested_targets: tuple[str, ...] = ()
        if has_resolution_targets:
            validated_targets = _validate_google_model_resolution_targets(
                request_data[GOOGLE_MODEL_RESOLUTION_TARGETS_FIELD]
            )
            if not isinstance(validated_targets, list):
                return validated_targets
            requested_targets = tuple(validated_targets)

        provider = _lane_provider(request_data)
        if not isinstance(provider, str):
            return provider

        model: str | None = None
        if "model" in request_data:
            if request_data["lane"] != "byo" or provider not in CLOUD_BYO_PROVIDERS:
                return error_response(
                    INVALID_CONFIG_VALUE,
                    detail=(
                        "model is only valid with cloud BYO providers: "
                        "anthropic, google, openai."
                    ),
                )
            model = _validate_top_level_model(request_data["model"])
            if not isinstance(model, str):
                return model
        if has_resolution_targets and (
            request_data["lane"] != "byo"
            or provider != GOOGLE_PROVIDER
            or model is None
        ):
            return error_response(
                INVALID_CONFIG_VALUE,
                detail=(
                    f"{GOOGLE_MODEL_RESOLUTION_TARGETS_FIELD} is only valid with "
                    "Google BYO model saves."
                ),
            )

        def apply(config: dict[str, Any]) -> JournalConfigMutation[dict[str, Any]]:
            providers_config = config.get("providers")
            if not isinstance(providers_config, dict):
                providers_config = {}
                config["providers"] = providers_config
            old_providers = copy.deepcopy(providers_config)
            changed_fields: dict[str, Any] = {}
            reported_targets = set(read_google_pro_alias_slots(config))
            effective_targets = set(requested_targets) & reported_targets
            restore_only = (
                "confidential_prior" in requested_targets
                and _confidential_lane_active_for_config(config)
            )
            if model is not None:
                _remember_byo_model(
                    config,
                    old_providers,
                    changed_fields,
                    provider,
                    model,
                )
            # Active and remembered alias targets are satisfied by the existing writes.
            if not restore_only:
                _set_active_provider(
                    config,
                    old_providers,
                    changed_fields,
                    provider,
                    model,
                )
            if model is not None and "confidential_prior" in effective_targets:
                _update_confidential_prior_model(config, changed_fields, model)
            return JournalConfigMutation(
                changed=bool(changed_fields),
                value=changed_fields,
            )

        changed_fields = mutate_journal_config(apply).value
        if changed_fields:
            log_app_action(
                app="thinking",
                facet=None,
                action="providers_update",
                params={"changed_fields": changed_fields},
            )
        return get_providers()
    except LockTimeout:
        return _config_busy_response()
    except Exception:
        logger.exception("error saving providers")
        return _thinking_operation_failed()


def _build_generator_info(key: str, info: dict[str, Any]) -> dict[str, Any]:
    return {
        "key": key,
        "title": info.get("title", info.get("label", key)),
        "description": info.get("description", ""),
        "source": info.get("source", "system"),
        "app": info.get("app"),
        "disabled": info.get("disabled", False),
    }


@thinking_bp.route("/api/generators")
def get_generators() -> Any:
    try:
        from solstone.think.talent import get_talent_configs

        all_generators = get_talent_configs(type="generate", include_disabled=True)
        segment = []
        daily = []
        for key, info in all_generators.items():
            gen_info = _build_generator_info(key, info)
            schedule = info.get("schedule")
            if schedule == "segment":
                segment.append(gen_info)
            elif schedule == "daily":
                daily.append(gen_info)
        return jsonify({"segment": segment, "daily": daily})
    except Exception:
        logger.exception("error loading generators")
        return _thinking_operation_failed()


@thinking_bp.route("/api/generators", methods=["PUT"])
def update_generators() -> Any:
    try:
        request_data = request.get_json()
        if not request_data:
            return error_response(MISSING_REQUEST_BODY, detail="No data provided")

        from solstone.think.talent import key_to_context

        for key, updates in request_data.items():
            if not isinstance(updates, dict):
                continue
            if "disabled" in updates and not isinstance(updates["disabled"], bool):
                return error_response(
                    INVALID_CONFIG_VALUE,
                    detail=f"disabled must be boolean for {key}",
                )
            if "extract" in updates and not isinstance(updates["extract"], bool):
                return error_response(
                    INVALID_CONFIG_VALUE,
                    detail=f"extract must be boolean for {key}",
                )

        def apply(config: dict[str, Any]) -> JournalConfigMutation[dict[str, Any]]:
            contexts = config.get("talent_overrides")
            if not isinstance(contexts, dict):
                contexts = {}
                config["talent_overrides"] = contexts
            old_contexts = copy.deepcopy(contexts)
            changed_fields: dict[str, Any] = {}

            for key, updates in request_data.items():
                if not isinstance(updates, dict):
                    continue
                context_key = key_to_context(key)
                ctx_config = contexts.get(context_key, {})
                old_ctx = old_contexts.get(context_key, {})
                if "disabled" in updates:
                    ctx_config["disabled"] = updates["disabled"]
                if "extract" in updates:
                    ctx_config["extract"] = updates["extract"]
                if ctx_config:
                    if old_ctx != ctx_config:
                        changed_fields[f"contexts.{context_key}"] = {
                            "old": old_ctx if old_ctx else None,
                            "new": ctx_config,
                        }
                    contexts[context_key] = ctx_config

            return JournalConfigMutation(
                changed=bool(changed_fields),
                value=changed_fields,
            )

        changed_fields = mutate_journal_config(apply).value
        if changed_fields:
            log_app_action(
                app="thinking",
                facet=None,
                action="generators_update",
                params={"changed_fields": changed_fields},
            )
        return get_generators()
    except LockTimeout:
        return _config_busy_response()
    except Exception:
        logger.exception("error saving generators")
        return _thinking_operation_failed()
