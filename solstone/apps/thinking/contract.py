# SPDX-License-Identifier: AGPL-3.0-only
# Copyright (c) 2026 sol pbc

"""OpenAPI fragment for the native-client thinking routes."""

from __future__ import annotations

from solstone.convey.contract import (
    FieldSpec,
    OperationSpec,
    ParamSpec,
    RequestSpec,
    ResponseSpec,
)

_FREE_OBJECT = {"type": "object", "additionalProperties": True}
_FREE_ARRAY = {"type": "array", "items": _FREE_OBJECT}
_SETTINGS_ERROR = ("settings_operation_failed",)
_LOCAL_MODEL_ERRORS = ("invalid_request_value", "settings_operation_failed")
_KEY_ERRORS = (
    "config_busy",
    "invalid_config_value",
    "invalid_request_value",
    "missing_request_body",
    "settings_operation_failed",
)
_PROVIDER_UPDATE_ERRORS = (
    "config_busy",
    "invalid_config_value",
    "invalid_operation_for_state",
    "invalid_request_value",
    "missing_request_body",
    "missing_required_field",
    "settings_operation_failed",
)
_LONG_POLL_START_ERRORS = (
    "invalid_operation_for_state",
    "service_busy",
    "settings_operation_failed",
)
_LOCAL_ENDPOINT_SET_ERRORS = (
    "config_busy",
    "invalid_config_value",
    "invalid_operation_for_state",
    "invalid_request_value",
    "missing_request_body",
    "missing_required_field",
    "settings_operation_failed",
)


def _query(name: str, description: str, type_: str = "string") -> ParamSpec:
    return ParamSpec(name, "query", type=type_, description=description)


def _json_error(reason_codes: tuple[str, ...]) -> ResponseSpec:
    return ResponseSpec(
        status=400,
        description="Request rejected with a standard Convey error envelope.",
        reason_codes=reason_codes,
    )


def _ok(description: str, fields: tuple[FieldSpec, ...] = ()) -> ResponseSpec:
    return ResponseSpec(
        status=200,
        description=description,
        named_fields=fields,
        free_form=not fields,
    )


def _body(fields: tuple[FieldSpec, ...], example: dict[str, object]) -> RequestSpec:
    return RequestSpec(fields=fields, example=example)


OPERATIONS: list[OperationSpec] = [
    OperationSpec(
        operation_id="thinking.scout.status",
        method="GET",
        rule="/app/thinking/api/scout",
        summary="Read Scout status",
        description="Return Scout state and operation metadata.",
        responses=(
            _ok(
                "Scout status.",
                (
                    FieldSpec("state", "string"),
                    FieldSpec("operation", "object", raw_schema=_FREE_OBJECT),
                ),
            ),
            _json_error(_SETTINGS_ERROR),
        ),
    ),
    OperationSpec(
        operation_id="thinking.scout.check",
        method="POST",
        rule="/app/thinking/api/scout/check",
        summary="Check Scout",
        description="Check Scout status over the connection.",
        responses=(
            _ok("Scout check result."),
            _json_error(_SETTINGS_ERROR),
        ),
    ),
    OperationSpec(
        operation_id="thinking.scout.enable",
        method="POST",
        rule="/app/thinking/api/scout/enable",
        summary="Enable Scout",
        description="Start Scout enablement and return operation metadata.",
        responses=(
            _ok("Scout enable operation."),
            _json_error(_LONG_POLL_START_ERRORS),
        ),
    ),
    OperationSpec(
        operation_id="thinking.scout.refresh",
        method="POST",
        rule="/app/thinking/api/scout/refresh",
        summary="Refresh Scout",
        description="Refresh Scout state and return operation metadata.",
        responses=(
            _ok("Scout refresh operation."),
            _json_error(_LONG_POLL_START_ERRORS),
        ),
    ),
    OperationSpec(
        operation_id="thinking.scout.disable",
        method="POST",
        rule="/app/thinking/api/scout/disable",
        summary="Disable Scout",
        description="Disable Scout and return status metadata.",
        responses=(
            _ok("Scout disable result."),
            _json_error(_SETTINGS_ERROR),
        ),
    ),
    OperationSpec(
        operation_id="thinking.confidential.enable",
        method="POST",
        rule="/app/thinking/api/confidential/enable",
        summary="Enable confidential processing",
        description="Start confidential processing enrollment.",
        responses=(
            _ok("Confidential enable operation."),
            _json_error(_LONG_POLL_START_ERRORS),
        ),
    ),
    OperationSpec(
        operation_id="thinking.confidential.disable",
        method="POST",
        rule="/app/thinking/api/confidential/disable",
        summary="Disable confidential processing",
        description="Disable confidential processing state.",
        responses=(
            _ok("Confidential disable result."),
            _json_error(_SETTINGS_ERROR),
        ),
    ),
    OperationSpec(
        operation_id="thinking.confidential.recheck",
        method="POST",
        rule="/app/thinking/api/confidential/recheck",
        summary="Recheck confidential processing",
        description="Recheck confidential attestation and provider state.",
        responses=(
            _ok("Confidential recheck result."),
            _json_error(("invalid_operation_for_state", "settings_operation_failed")),
        ),
    ),
    OperationSpec(
        operation_id="thinking.keys.get",
        method="GET",
        rule="/app/thinking/api/keys",
        summary="Read AI keys",
        description="Return configured AI key status.",
        responses=(
            _ok(
                "AI key state.",
                (
                    FieldSpec("api_keys", "object", raw_schema=_FREE_OBJECT),
                    FieldSpec("env", "object", raw_schema=_FREE_OBJECT),
                    FieldSpec("key_validation", "object", raw_schema=_FREE_OBJECT),
                ),
            ),
            _json_error(_KEY_ERRORS),
        ),
    ),
    OperationSpec(
        operation_id="thinking.keys.update",
        method="PUT",
        rule="/app/thinking/api/keys",
        summary="Update AI key",
        description="Set or clear one AI key in journal config.",
        request=_body(
            (
                FieldSpec("env_var", "string", required=True),
                FieldSpec("value", "string", required=True),
            ),
            {"env_var": "OPENAI_API_KEY", "value": "sk-test"},
        ),
        responses=(
            _ok("Updated key validation state."),
            _json_error(_KEY_ERRORS),
        ),
    ),
    OperationSpec(
        operation_id="thinking.keys.validate",
        method="GET",
        rule="/app/thinking/api/validate-keys",
        summary="Validate AI keys",
        description="Validate AI keys without persisting the cache.",
        responses=(
            _ok("Key validation result."),
            _json_error(("config_busy", "settings_operation_failed")),
        ),
    ),
    OperationSpec(
        operation_id="thinking.keys.validate-cache",
        method="POST",
        rule="/app/thinking/api/validate-keys",
        summary="Validate and cache AI keys",
        description="Validate AI keys and persist the cache result.",
        responses=(
            _ok("Cached key validation result."),
            _json_error(("config_busy", "settings_operation_failed")),
        ),
    ),
    OperationSpec(
        operation_id="thinking.local.availability",
        method="GET",
        rule="/app/thinking/api/local/availability",
        summary="Read local model availability",
        description="Return local model availability for an optional model id.",
        parameters=(_query("model", "Local model id."),),
        responses=(
            _ok("Local availability payload."),
            _json_error(_LOCAL_MODEL_ERRORS),
        ),
    ),
    OperationSpec(
        operation_id="thinking.local.bootstrap",
        method="POST",
        rule="/app/thinking/api/local/bootstrap",
        summary="Start local model bootstrap",
        description="Start local model setup for an optional model id.",
        parameters=(_query("model", "Local model id."),),
        responses=(
            _ok("Local bootstrap payload."),
            _json_error(_LOCAL_MODEL_ERRORS),
        ),
    ),
    OperationSpec(
        operation_id="thinking.local.bootstrap-status",
        method="GET",
        rule="/app/thinking/api/local/bootstrap/status",
        summary="Read local model bootstrap status",
        description="Return local model setup status for an optional model id.",
        parameters=(_query("model", "Local model id."),),
        responses=(
            _ok("Local bootstrap status payload."),
            _json_error(_LOCAL_MODEL_ERRORS),
        ),
    ),
    OperationSpec(
        operation_id="thinking.local.models",
        method="GET",
        rule="/app/thinking/api/local/models",
        summary="List local models",
        description="Return local model definitions.",
        responses=(
            _ok(
                "Local model list.",
                (FieldSpec("models", "array", raw_schema=_FREE_ARRAY),),
            ),
            _json_error(_SETTINGS_ERROR),
        ),
    ),
    OperationSpec(
        operation_id="thinking.local.endpoint.set",
        method="POST",
        rule="/app/thinking/api/local/endpoint",
        summary="Set local endpoint",
        description="Configure the BYO local OpenAI-compatible endpoint.",
        request=_body(
            (
                FieldSpec("endpoint_url", "string", required=True),
                FieldSpec("served_model_id", "string", required=True),
                FieldSpec("credential", "string"),
            ),
            {
                "endpoint_url": "http://localhost:8000/v1",
                "served_model_id": "local-model",
            },
        ),
        responses=(
            _ok("Local endpoint state."),
            _json_error(_LOCAL_ENDPOINT_SET_ERRORS),
        ),
    ),
    OperationSpec(
        operation_id="thinking.local.endpoint.delete",
        method="DELETE",
        rule="/app/thinking/api/local/endpoint",
        summary="Clear local endpoint",
        description="Clear the BYO local provider endpoint.",
        responses=(
            _ok("Cleared local endpoint state."),
            _json_error(
                (
                    "config_busy",
                    "invalid_operation_for_state",
                    "settings_operation_failed",
                )
            ),
        ),
    ),
    OperationSpec(
        operation_id="thinking.providers.get",
        method="GET",
        rule="/app/thinking/api/providers",
        summary="Read thinking providers",
        description="Return provider status, active lane, key status, and local override.",
        responses=(
            _ok(
                "Provider status.",
                (
                    FieldSpec("providers", "array", raw_schema=_FREE_ARRAY),
                    FieldSpec("provider_status", "object", raw_schema=_FREE_OBJECT),
                    FieldSpec("active_lane", "object", raw_schema=_FREE_OBJECT),
                    FieldSpec("active", "object", raw_schema=_FREE_OBJECT),
                    FieldSpec("local_override", "object", raw_schema=_FREE_OBJECT),
                    FieldSpec("api_keys", "object", raw_schema=_FREE_OBJECT),
                    FieldSpec("key_validation", "object", raw_schema=_FREE_OBJECT),
                ),
            ),
            _json_error(("invalid_request_value", "settings_operation_failed")),
        ),
    ),
    OperationSpec(
        operation_id="thinking.providers.update",
        method="POST",
        rule="/app/thinking/api/providers",
        summary="Update active thinking provider",
        description="Set the active provider lane and optional model.",
        request=_body(
            (
                FieldSpec("lane", "string", required=True),
                FieldSpec("provider", "string", required=True),
                FieldSpec("model", "string"),
            ),
            {"lane": "byo", "provider": "openai", "model": "gpt-5"},
        ),
        responses=(
            _ok("Updated provider state."),
            _json_error(_PROVIDER_UPDATE_ERRORS),
        ),
    ),
    OperationSpec(
        operation_id="thinking.providers.update-put",
        method="PUT",
        rule="/app/thinking/api/providers",
        summary="Update active thinking provider via PUT",
        description="PUT variant of the provider update route.",
        request=_body(
            (
                FieldSpec("lane", "string", required=True),
                FieldSpec("provider", "string", required=True),
                FieldSpec("model", "string"),
            ),
            {"lane": "byo", "provider": "openai", "model": "gpt-5"},
        ),
        responses=(
            _ok("Updated provider state."),
            _json_error(_PROVIDER_UPDATE_ERRORS),
        ),
    ),
    OperationSpec(
        operation_id="thinking.local.provider-status",
        method="GET",
        rule="/app/thinking/api/providers/local/status",
        summary="Read local provider status",
        description="Return local provider readiness/runtime status.",
        responses=(
            _ok("Local provider status."),
            _json_error(_SETTINGS_ERROR),
        ),
    ),
]
