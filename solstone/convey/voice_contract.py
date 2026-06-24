# SPDX-License-Identifier: AGPL-3.0-only
# Copyright (c) 2026 sol pbc

"""OpenAPI fragment for the native-client voice routes."""

from __future__ import annotations

from solstone.convey.contract import (
    FieldSpec,
    OperationSpec,
    ParamSpec,
    RequestSpec,
    ResponseSpec,
)


def _json_error(
    status: int,
    reason_codes: tuple[str, ...],
    description: str,
) -> ResponseSpec:
    return ResponseSpec(
        status=status,
        description=description,
        reason_codes=reason_codes,
    )


OPERATIONS: list[OperationSpec] = [
    OperationSpec(
        operation_id="voice.session",
        method="POST",
        rule="/api/voice/session",
        summary="Create voice session",
        description="Mint an ephemeral key for an OpenAI Realtime voice session.",
        responses=(
            ResponseSpec(
                status=200,
                description="Voice session key created.",
                named_fields=(FieldSpec("ephemeral_key", "string", required=True),),
                example={"ephemeral_key": "ek_..."},
            ),
            _json_error(
                400,
                ("invalid_json_request",),
                "Optional JSON body was malformed or not an object.",
            ),
            _json_error(
                500,
                ("voice_unavailable",),
                "Voice runtime or session minting failed.",
            ),
            _json_error(
                503,
                ("provider_key_missing", "voice_unavailable"),
                "Voice provider configuration or brain readiness was unavailable.",
            ),
            _json_error(
                403,
                ("pl_revoked",),
                "Access gate rejected a revoked paired-link identity.",
            ),
        ),
    ),
    OperationSpec(
        operation_id="voice.connect",
        method="POST",
        rule="/api/voice/connect",
        summary="Connect voice sideband",
        description="Connect the server-side voice sideband for an active call.",
        request=RequestSpec(
            fields=(FieldSpec("call_id", "string", required=True),),
            example={"call_id": "call_123"},
        ),
        responses=(
            ResponseSpec(
                status=200,
                description="Voice sideband connection started.",
                named_fields=(FieldSpec("status", "string", required=True),),
                example={"status": "connected"},
            ),
            _json_error(
                400,
                ("invalid_json_request", "invalid_request_value"),
                "JSON body was malformed or call_id was missing.",
            ),
            _json_error(
                500,
                ("voice_unavailable",),
                "Voice runtime was unavailable.",
            ),
            _json_error(
                503,
                ("provider_key_missing",),
                "Voice provider key was not configured.",
            ),
            _json_error(
                403,
                ("pl_revoked",),
                "Access gate rejected a revoked paired-link identity.",
            ),
        ),
    ),
    OperationSpec(
        operation_id="voice.navHints",
        method="GET",
        rule="/api/voice/nav-hints",
        summary="Drain voice navigation hints",
        description="Drain queued navigation hints for the given voice call.",
        parameters=(
            ParamSpec(
                "call_id",
                "query",
                required=True,
                description="Voice call identifier.",
            ),
        ),
        responses=(
            ResponseSpec(
                status=200,
                description="Navigation hints drained.",
                named_fields=(
                    FieldSpec("hints", "array", required=True),
                    FieldSpec("consumed", "boolean", required=True),
                ),
                example={"hints": [], "consumed": True},
            ),
            _json_error(
                400,
                ("invalid_request_value",),
                "Call identifier was missing.",
            ),
            _json_error(
                403,
                ("pl_revoked",),
                "Access gate rejected a revoked paired-link identity.",
            ),
        ),
    ),
    OperationSpec(
        operation_id="voice.observerActions",
        method="GET",
        rule="/api/voice/observer-actions",
        summary="Drain voice observer actions",
        description="Drain queued observer actions for the given voice call.",
        parameters=(
            ParamSpec(
                "call_id",
                "query",
                required=False,
                description="Voice call identifier.",
            ),
        ),
        responses=(
            ResponseSpec(
                status=200,
                description="Observer actions drained.",
                named_fields=(
                    FieldSpec("actions", "array", required=True),
                    FieldSpec("consumed", "boolean", required=True),
                ),
                example={"actions": [], "consumed": True},
            ),
            _json_error(
                403,
                ("pl_revoked",),
                "Access gate rejected a revoked paired-link identity.",
            ),
        ),
    ),
    OperationSpec(
        operation_id="voice.status",
        method="GET",
        rule="/api/voice/status",
        summary="Get voice status",
        description="Return current voice readiness and session status.",
        responses=(
            ResponseSpec(
                status=200,
                description="Voice status.",
                named_fields=(
                    FieldSpec("brain_ready", "boolean", required=True),
                    FieldSpec(
                        "brain_age_seconds",
                        "integer",
                        required=True,
                        raw_schema={"type": ["integer", "null"]},
                    ),
                    FieldSpec("openai_configured", "boolean", required=True),
                    FieldSpec("active_sessions", "integer", required=True),
                ),
                example={
                    "brain_ready": True,
                    "brain_age_seconds": 120,
                    "openai_configured": True,
                    "active_sessions": 1,
                },
            ),
            _json_error(
                403,
                ("pl_revoked",),
                "Access gate rejected a revoked paired-link identity.",
            ),
        ),
    ),
]

__all__ = ["OPERATIONS"]
