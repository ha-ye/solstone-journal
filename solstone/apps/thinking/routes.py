# SPDX-License-Identifier: AGPL-3.0-only
# Copyright (c) 2026 sol pbc

"""Thinking app routes."""

from __future__ import annotations

import copy
import json
import logging
import os
from collections.abc import Callable
from datetime import datetime, timezone
from pathlib import Path
from typing import Any
from urllib.parse import urlsplit

from flask import Blueprint, jsonify, render_template, request

from solstone.apps.thinking import copy as thinking_copy
from solstone.apps.thinking import local_bootstrap, scout_lane
from solstone.apps.thinking.copy import thinking_copy_payload
from solstone.apps.thinking.vertex_credentials import (
    delete_vertex_credentials,
    save_vertex_credentials,
)
from solstone.apps.utils import log_app_action
from solstone.convey import state
from solstone.convey.readiness_snapshot import build_readiness_snapshot
from solstone.convey.reasons import (
    FILE_NOT_FOUND,
    FILE_READ_FAILED,
    INVALID_CONFIG_VALUE,
    INVALID_JSON_REQUEST,
    INVALID_OPERATION_FOR_STATE,
    INVALID_REQUEST_VALUE,
    MISSING_REQUEST_BODY,
    MISSING_REQUIRED_FIELD,
    SERVICE_BUSY,
    SETTINGS_OPERATION_FAILED,
)
from solstone.convey.utils import error_response
from solstone.think.journal_config import (
    hold_config_lock,
    read_journal_config,
    write_journal_config,
)
from solstone.think.models import LOCAL_MODEL, TYPE_DEFAULTS
from solstone.think.providers import (
    PROVIDER_REGISTRY,
    build_provider_status,
    get_provider_list,
    validate_key,
)
from solstone.think.providers.google import validate_vertex_credentials
from solstone.think.providers.local_endpoint import (
    normalize_local_endpoint_url,
    resolve_local_endpoint,
)
from solstone.think.services import operations, scout, scout_handoff
from solstone.think.utils import CorruptConfigError, get_journal
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
VALID_TIERS = {1, 2, 3}
LANES = {"scout", "byo", "local"}
GENERIC_THINKING_ERROR = (
    "something went wrong - try again, and if it persists, check the health dashboard"
)


def _thinking_operation_failed(detail: str = GENERIC_THINKING_ERROR) -> Any:
    return error_response(SETTINGS_OPERATION_FAILED, detail=detail)


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


def _type_settings(providers_config: dict[str, Any]) -> dict[str, dict[str, Any]]:
    settings: dict[str, dict[str, Any]] = {}
    for agent_type in ("generate", "cogitate"):
        defaults = TYPE_DEFAULTS[agent_type]
        type_config = providers_config.get(agent_type, {})
        if not isinstance(type_config, dict):
            type_config = {}
        settings[agent_type] = {
            "provider": type_config.get("provider", defaults["provider"]),
            "tier": type_config.get("tier", defaults["tier"]),
            "backup": type_config.get("backup", defaults["backup"]),
        }
    return settings


def _lane_for_provider(provider: str) -> str:
    if provider == "local":
        return "local"
    if provider == "google" and scout.is_scout_enabled():
        return "scout"
    return "byo"


def _active_lane_payload(
    type_settings: dict[str, dict[str, Any]],
) -> dict[str, Any]:
    per_type = {
        agent_type: _lane_for_provider(str(settings.get("provider") or ""))
        for agent_type, settings in type_settings.items()
    }
    lanes = set(per_type.values())
    active = next(iter(lanes)) if len(lanes) == 1 else "advanced"
    return {
        "lane": active,
        "generate": per_type.get("generate"),
        "cogitate": per_type.get("cogitate"),
        "split": active == "advanced",
        "scout_enabled": scout.is_scout_enabled(),
        "scout_provenance_configured": scout.scout_provenance() is not None,
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
        if key in {"google", "openai", "anthropic", "google_vertex"}
    }


def _keys_payload(config: dict[str, Any]) -> dict[str, Any]:
    return {
        "api_keys": _api_key_status(config),
        "env": _env_key_status(config),
        "key_validation": _filtered_ai_key_validation(config),
        "scout_enabled": scout.is_scout_enabled(),
    }


def _compute_ai_key_validation(config: dict[str, Any]) -> dict[str, Any]:
    """Validate configured AI provider keys without mutating config."""

    env_config = config.get("env", {})
    key_validation: dict[str, Any] = {}

    for env_var, provider in AI_ENV_TO_PROVIDER.items():
        api_key = env_config.get(env_var, "")
        if api_key:
            result = validate_key(provider, api_key)
            result["timestamp"] = datetime.now(timezone.utc).isoformat()
            key_validation[provider] = result

    providers_config = config.get("providers", {})
    if providers_config.get("google_backend") == "vertex" and providers_config.get(
        "vertex_credentials"
    ):
        result = validate_vertex_credentials(
            providers_config["vertex_credentials"],
        )
        result["timestamp"] = datetime.now(timezone.utc).isoformat()
        key_validation["google_vertex"] = result

    return key_validation


def _provider_payload(config: dict[str, Any], local_model_id: str) -> dict[str, Any]:
    from solstone.think.models import get_context_registry
    from solstone.think.talent import get_talent_configs, key_to_context

    providers_config = config.get("providers", {})
    if not isinstance(providers_config, dict):
        providers_config = {}

    type_settings = _type_settings(providers_config)
    contexts = providers_config.get("contexts", {})
    if not isinstance(contexts, dict):
        contexts = {}

    context_defaults: dict[str, dict[str, Any]] = {}
    for pattern, ctx_config in get_context_registry().items():
        context_defaults[pattern] = {
            "tier": ctx_config["tier"],
            "label": ctx_config["label"],
            "group": ctx_config["group"],
        }
        if "type" in ctx_config:
            context_defaults[pattern]["type"] = ctx_config["type"]

    talent_configs = get_talent_configs(include_disabled=True)
    for key, info in talent_configs.items():
        context_key = key_to_context(key)
        if context_key in context_defaults:
            if "schedule" in info:
                context_defaults[context_key]["schedule"] = info["schedule"]
            context_defaults[context_key]["disabled"] = info.get("disabled", False)

    providers_list = get_provider_list()
    vertex_creds_path = providers_config.get("vertex_credentials")
    vertex_creds_configured = False
    vertex_creds_email = ""
    if vertex_creds_path and Path(vertex_creds_path).exists():
        vertex_creds_configured = True
        try:
            creds_data = json.loads(Path(vertex_creds_path).read_text())
            vertex_creds_email = creds_data.get("client_email", "")
        except Exception:
            pass

    local_status = local_bootstrap.get_state(local_model_id)
    ai_readiness = build_readiness_snapshot(
        local_model_id=local_model_id,
        include_local=not local_bootstrap._is_mlx_backend(),
    )

    return {
        "providers": providers_list,
        "provider_status": build_provider_status(
            providers_list,
            vertex_creds_configured,
        ),
        "ai_readiness": ai_readiness,
        "active_lane": _active_lane_payload(type_settings),
        "generate": type_settings["generate"],
        "cogitate": type_settings["cogitate"],
        "contexts": contexts,
        "context_defaults": context_defaults,
        "api_keys": _api_key_status(config),
        "key_validation": _filtered_ai_key_validation(config),
        "local": local_status,
        "local_override": _local_override_payload(config),
        "local_backend": "mlx" if local_bootstrap._is_mlx_backend() else "local",
        "google_backend": providers_config.get("google_backend", "auto"),
        "vertex_credentials_configured": vertex_creds_configured,
        "vertex_credentials_email": vertex_creds_email,
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
def index() -> str:
    return render_template(
        "app.html",
        thinking_copy=thinking_copy_payload(),
        thinking_initial=_initial_payload(),
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
        if env_var == "GOOGLE_API_KEY" and scout.is_scout_enabled() and value:
            return error_response(
                INVALID_CONFIG_VALUE,
                detail="Gemini is managed by scout; choose another BYO provider.",
            )

        validation = None
        with hold_config_lock():
            config = read_journal_config()
            env = config.setdefault("env", {})
            providers_config = config.setdefault("providers", {})
            key_validation = providers_config.setdefault("key_validation", {})
            old_value = env.get(env_var)
            new_value = str(value or "").strip()
            if new_value:
                env[env_var] = new_value
                os.environ[env_var] = new_value
                validation = validate_key(provider, new_value)
                validation["timestamp"] = datetime.now(timezone.utc).isoformat()
                key_validation[provider] = validation
            else:
                env.pop(env_var, None)
                os.environ.pop(env_var, None)
                key_validation.pop(provider, None)
            write_journal_config(config)

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
    except Exception:
        logger.exception("error updating thinking keys")
        return _thinking_operation_failed()


@thinking_bp.route("/api/validate-keys", methods=["GET", "POST"])
def validate_all_keys() -> Any:
    """Re-validate configured AI keys and Vertex credentials."""

    try:
        config = get_journal_config()
        key_validation = _compute_ai_key_validation(config)
        if request.method == "GET":
            return jsonify({"key_validation": key_validation})

        providers_config = config.setdefault("providers", {})
        existing = providers_config.setdefault("key_validation", {})
        for key in ("google", "openai", "anthropic", "google_vertex"):
            existing.pop(key, None)
        existing.update(key_validation)
        write_journal_config(config)
        return jsonify({"success": True, "key_validation": key_validation})
    except Exception:
        logger.exception("error validating thinking keys")
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

        with hold_config_lock():
            config = read_journal_config()
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
            write_journal_config(config)

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
    except Exception:
        logger.exception("error updating local endpoint")
        return _thinking_operation_failed()


@thinking_bp.route("/api/local/endpoint", methods=["DELETE"])
def clear_local_endpoint() -> Any:
    try:
        with hold_config_lock():
            config = read_journal_config()
            local_config = _ensure_local_provider_config(config)
            before = dict(local_config)
            for key in ("endpoint_url", "served_model_id", "credential"):
                local_config.pop(key, None)
            changed_fields = _masked_local_endpoint_changes(
                before,
                local_config,
                credential_touched=True,
            )
            write_journal_config(config)

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


@thinking_bp.route("/api/providers/local/status")
def get_local_provider_status() -> Any:
    """Return local provider readiness status."""

    try:
        providers_list = get_provider_list()
        local_provider = next(
            provider for provider in providers_list if provider["name"] == "local"
        )
        provider_status = build_provider_status([local_provider], False)
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


def _apply_type_update(
    config: dict[str, Any],
    old_providers: dict[str, Any],
    changed_fields: dict[str, Any],
    agent_type: str,
    type_data: dict[str, Any],
) -> Any | None:
    if agent_type not in config["providers"]:
        config["providers"][agent_type] = {}
    old_type = old_providers.get(agent_type, {})

    if "provider" in type_data:
        provider = _validate_provider(type_data["provider"])
        if not isinstance(provider, str):
            return provider
        if old_type.get("provider") != provider:
            changed_fields[f"{agent_type}.provider"] = {
                "old": old_type.get("provider"),
                "new": provider,
            }
        config["providers"][agent_type]["provider"] = provider

    if "tier" in type_data:
        tier = type_data["tier"]
        if tier not in VALID_TIERS:
            return error_response(
                INVALID_CONFIG_VALUE,
                detail=f"Invalid tier: {tier}. Must be 1, 2, or 3.",
            )
        if old_type.get("tier") != tier:
            changed_fields[f"{agent_type}.tier"] = {
                "old": old_type.get("tier"),
                "new": tier,
            }
        config["providers"][agent_type]["tier"] = tier

    if "backup" in type_data:
        backup = _validate_provider(type_data["backup"], "backup provider")
        if not isinstance(backup, str):
            return backup
        if old_type.get("backup") != backup:
            changed_fields[f"{agent_type}.backup"] = {
                "old": old_type.get("backup"),
                "new": backup,
            }
        config["providers"][agent_type]["backup"] = backup

    return None


def _lane_provider(request_data: dict[str, Any]) -> str | Any:
    lane = request_data.get("lane") or request_data.get("active_lane")
    if lane not in LANES:
        return error_response(
            INVALID_CONFIG_VALUE,
            detail=f"Invalid lane: {lane}. Must be one of: {', '.join(sorted(LANES))}",
        )
    if lane == "local":
        return "local"
    if lane == "scout":
        if not scout.is_scout_enabled():
            return error_response(
                INVALID_CONFIG_VALUE,
                detail="Scout is not ready on this journal.",
            )
        return "google"
    provider = request_data.get("provider")
    if provider in {None, ""}:
        return error_response(
            INVALID_CONFIG_VALUE,
            detail="No BYO provider selected. Must be one of: anthropic, google, openai",
        )
    if provider not in {"anthropic", "google", "openai"}:
        return error_response(
            INVALID_CONFIG_VALUE,
            detail="Invalid provider for BYO lane. Must be one of: anthropic, google, openai",
        )
    if provider == "google" and scout.is_scout_enabled():
        return error_response(
            INVALID_CONFIG_VALUE,
            detail="Gemini is managed by scout; choose Claude or GPT for BYO cloud.",
        )
    return str(provider)


@thinking_bp.route("/api/providers", methods=["PUT", "POST"])
def update_providers() -> Any:
    try:
        request_data = request.get_json()
        if not request_data:
            return error_response(MISSING_REQUEST_BODY, detail="No data provided")

        config = get_journal_config()
        old_providers = copy.deepcopy(config.get("providers", {}))
        config.setdefault("providers", {})
        changed_fields: dict[str, Any] = {}

        if "lane" in request_data or "active_lane" in request_data:
            provider = _lane_provider(request_data)
            if not isinstance(provider, str):
                return provider
            lane_update = {"provider": provider}
            for optional in ("tier", "backup"):
                if optional in request_data:
                    lane_update[optional] = request_data[optional]
            for agent_type in ("generate", "cogitate"):
                error = _apply_type_update(
                    config,
                    old_providers,
                    changed_fields,
                    agent_type,
                    lane_update,
                )
                if error is not None:
                    return error

        for agent_type in ("generate", "cogitate"):
            if agent_type not in request_data:
                continue
            type_data = request_data[agent_type]
            if not isinstance(type_data, dict):
                return error_response(INVALID_REQUEST_VALUE, detail=agent_type)
            error = _apply_type_update(
                config,
                old_providers,
                changed_fields,
                agent_type,
                type_data,
            )
            if error is not None:
                return error

        if "contexts" in request_data:
            contexts_data = request_data["contexts"]
            if not isinstance(contexts_data, dict):
                return error_response(INVALID_REQUEST_VALUE, detail="contexts")
            config["providers"].setdefault("contexts", {})
            old_contexts = old_providers.get("contexts", {})
            for pattern, ctx_config in contexts_data.items():
                old_ctx = old_contexts.get(pattern)
                if ctx_config is None:
                    if pattern in config["providers"]["contexts"]:
                        changed_fields[f"contexts.{pattern}"] = {
                            "old": old_ctx,
                            "new": None,
                        }
                        del config["providers"]["contexts"][pattern]
                    continue
                if not isinstance(ctx_config, dict):
                    return error_response(
                        INVALID_CONFIG_VALUE,
                        detail=f"context for {pattern} must be an object or null",
                    )
                if "provider" in ctx_config:
                    provider = _validate_provider(
                        ctx_config["provider"],
                        f"provider for {pattern}",
                    )
                    if not isinstance(provider, str):
                        return provider
                if "tier" in ctx_config and ctx_config["tier"] not in VALID_TIERS:
                    return error_response(
                        INVALID_CONFIG_VALUE,
                        detail=f"Invalid tier for {pattern}: {ctx_config['tier']}",
                    )
                if "disabled" in ctx_config and not isinstance(
                    ctx_config["disabled"],
                    bool,
                ):
                    return error_response(
                        INVALID_CONFIG_VALUE,
                        detail=f"disabled for {pattern} must be a boolean",
                    )
                if "extract" in ctx_config and not isinstance(
                    ctx_config["extract"],
                    bool,
                ):
                    return error_response(
                        INVALID_CONFIG_VALUE,
                        detail=f"extract for {pattern} must be a boolean",
                    )
                if ctx_config:
                    if old_ctx != ctx_config:
                        changed_fields[f"contexts.{pattern}"] = {
                            "old": old_ctx,
                            "new": ctx_config,
                        }
                    config["providers"]["contexts"][pattern] = ctx_config

        if "google_backend" in request_data:
            backend = request_data["google_backend"]
            if backend not in ("auto", "aistudio", "vertex"):
                return error_response(
                    INVALID_CONFIG_VALUE,
                    detail=(
                        f"Invalid google_backend: {backend}. "
                        "Must be 'auto', 'aistudio', or 'vertex'."
                    ),
                )
            old_val = old_providers.get("google_backend", "auto")
            if old_val != backend:
                changed_fields["google_backend"] = {"old": old_val, "new": backend}
            config["providers"]["google_backend"] = backend

        if "vertex_credentials" in request_data:
            vertex_creds_value = request_data["vertex_credentials"]
            if vertex_creds_value:
                try:
                    creds_data = (
                        json.loads(vertex_creds_value)
                        if isinstance(vertex_creds_value, str)
                        else vertex_creds_value
                    )
                except json.JSONDecodeError:
                    return error_response(
                        INVALID_JSON_REQUEST,
                        detail="Invalid JSON in vertex_credentials",
                    )
                required_fields = (
                    "type",
                    "project_id",
                    "client_email",
                    "private_key",
                )
                missing = [
                    field for field in required_fields if field not in creds_data
                ]
                if missing:
                    return error_response(
                        MISSING_REQUIRED_FIELD,
                        detail=f"Missing required fields: {', '.join(missing)}",
                    )
                creds_file = save_vertex_credentials(
                    creds_data,
                    Path(state.journal_root),
                )
                old_val = old_providers.get("vertex_credentials", "")
                creds_path_str = str(creds_file)
                if old_val != creds_path_str:
                    changed_fields["vertex_credentials"] = {
                        "old": old_val,
                        "new": creds_path_str,
                    }
                config["providers"]["vertex_credentials"] = creds_path_str
                validation = validate_vertex_credentials(creds_path_str)
                config["providers"].setdefault("key_validation", {})
                config["providers"]["key_validation"]["google_vertex"] = {
                    **validation,
                    "timestamp": datetime.now(timezone.utc).isoformat(),
                }
            else:
                old_path = config["providers"].get("vertex_credentials")
                if old_path:
                    changed_fields["vertex_credentials"] = {
                        "old": old_path,
                        "new": None,
                    }
                    delete_vertex_credentials(old_path, Path(state.journal_root))
                    config["providers"].pop("vertex_credentials", None)
                    config["providers"].get("key_validation", {}).pop(
                        "google_vertex",
                        None,
                    )

        write_journal_config(config)
        if changed_fields:
            log_app_action(
                app="thinking",
                facet=None,
                action="providers_update",
                params={"changed_fields": changed_fields},
            )
        return get_providers()
    except Exception:
        logger.exception("error saving providers")
        return _thinking_operation_failed()


@thinking_bp.route("/api/vertex-credentials/import", methods=["POST"])
def import_vertex_credentials() -> Any:
    """Import Vertex credentials from a server-side path."""

    try:
        request_data = request.get_json()
        if not isinstance(request_data, dict):
            return error_response(MISSING_REQUEST_BODY, detail="No data provided")

        raw_path = request_data.get("path")
        if not isinstance(raw_path, str) or not raw_path.strip():
            return error_response(MISSING_REQUIRED_FIELD, detail="path")

        source = Path(raw_path)
        if not source.exists():
            return error_response(FILE_NOT_FOUND, detail=raw_path)

        try:
            creds_data = json.loads(source.read_text(encoding="utf-8"))
        except json.JSONDecodeError:
            return error_response(INVALID_JSON_REQUEST, detail=raw_path)
        except OSError:
            return error_response(FILE_READ_FAILED, detail=raw_path)

        required_fields = ("type", "project_id", "client_email", "private_key")
        missing = [field for field in required_fields if field not in creds_data]
        if missing:
            return error_response(
                MISSING_REQUIRED_FIELD,
                detail=", ".join(missing),
            )

        creds_file = save_vertex_credentials(creds_data, Path(get_journal()))
        config = get_journal_config()
        config.setdefault("providers", {})
        config["providers"]["vertex_credentials"] = str(creds_file)

        validation = None
        if not bool(request_data.get("skip_validation", False)):
            validation = validate_vertex_credentials(str(creds_file))
            validation["timestamp"] = datetime.now(timezone.utc).isoformat()
            config["providers"].setdefault("key_validation", {})
            config["providers"]["key_validation"]["google_vertex"] = validation

        write_journal_config(config)

        return jsonify(
            {
                "configured": True,
                "email": creds_data.get("client_email", ""),
                "path": str(creds_file),
                "validation": validation,
            }
        )
    except Exception:
        logger.exception("error importing vertex credentials")
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

        config = get_journal_config()
        old_providers = copy.deepcopy(config.get("providers", {}))
        config.setdefault("providers", {})
        config["providers"].setdefault("contexts", {})
        old_contexts = old_providers.get("contexts", {})
        changed_fields: dict[str, Any] = {}

        from solstone.think.talent import key_to_context

        for key, updates in request_data.items():
            if not isinstance(updates, dict):
                continue
            context_key = key_to_context(key)
            ctx_config = config["providers"]["contexts"].get(context_key, {})
            old_ctx = old_contexts.get(context_key, {})
            if "disabled" in updates:
                if not isinstance(updates["disabled"], bool):
                    return error_response(
                        INVALID_CONFIG_VALUE,
                        detail=f"disabled must be boolean for {key}",
                    )
                ctx_config["disabled"] = updates["disabled"]
            if "extract" in updates:
                if not isinstance(updates["extract"], bool):
                    return error_response(
                        INVALID_CONFIG_VALUE,
                        detail=f"extract must be boolean for {key}",
                    )
                ctx_config["extract"] = updates["extract"]
            if ctx_config:
                if old_ctx != ctx_config:
                    changed_fields[f"contexts.{context_key}"] = {
                        "old": old_ctx if old_ctx else None,
                        "new": ctx_config,
                    }
                config["providers"]["contexts"][context_key] = ctx_config

        write_journal_config(config)
        if changed_fields:
            log_app_action(
                app="thinking",
                facet=None,
                action="generators_update",
                params={"changed_fields": changed_fields},
            )
        return get_generators()
    except Exception:
        logger.exception("error saving generators")
        return _thinking_operation_failed()
