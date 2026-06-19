# SPDX-License-Identifier: AGPL-3.0-only
# Copyright (c) 2026 sol pbc

"""OpenAPI fragment for the native-client observer routes."""

from __future__ import annotations

from solstone.convey.contract import (
    FieldSpec,
    OperationSpec,
    ParamSpec,
    RequestSpec,
    ResponseSpec,
)

_OBSERVER_AUTH_PARAMS = (
    ParamSpec(
        "X-Solstone-Observer",
        "header",
        required=False,
        description="Observer handle. Preferred over Authorization when present.",
    ),
    ParamSpec(
        "Authorization",
        "header",
        required=False,
        description="Bearer observer handle.",
    ),
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


def _observer_auth_errors() -> tuple[ResponseSpec, ResponseSpec]:
    return (
        _json_error(
            401,
            ("auth_key_invalid", "auth_required"),
            "Observer handle missing or invalid.",
        ),
        _json_error(
            403,
            ("feature_unavailable", "pl_revoked"),
            "Observer is disabled or revoked.",
        ),
    )


_DAY_SEGMENT_COUNT_MAP = {
    "type": "object",
    "additionalProperties": {
        "type": "object",
        "additionalProperties": True,
        "properties": {"segments": {"type": "integer"}},
    },
}

_MANIFEST_SEGMENT_MAP = {
    "type": "object",
    "additionalProperties": {
        "type": "object",
        "additionalProperties": True,
        "properties": {
            "files": {
                "type": "array",
                "items": {
                    "type": "object",
                    "additionalProperties": True,
                    "properties": {
                        "name": {"type": "string"},
                        "sha256": {"type": "string"},
                        "size": {"type": "integer"},
                    },
                    "required": ["name", "sha256", "size"],
                },
            }
        },
        "required": ["files"],
    },
}

_SEGMENTS_RESPONSE_SCHEMA = {
    "oneOf": [
        {"$ref": "#/components/schemas/SegmentsEnvelope"},
        {
            "type": "array",
            "items": {"$ref": "#/components/schemas/SegmentItem"},
        },
    ]
}

_SEGMENT_ITEM_EXAMPLE = {
    "key": "143022_300",
    "observed": True,
    "files": [
        {
            "name": "screen.png",
            "size": 2048,
            "sha256": "5f70bf18a086007016bb522ec180fd0b",
            "status": "present",
        }
    ],
}

OPERATIONS: list[OperationSpec] = [
    OperationSpec(
        operation_id="observer.register",
        method="POST",
        rule="/app/observer/register",
        summary="Register observer",
        description=(
            "Register a trusted local or paired-link observer and lock its stream "
            "identity."
        ),
        request=RequestSpec(
            fields=(
                FieldSpec("platform", "string", required=True),
                FieldSpec("hostname", "string", required=True),
                FieldSpec("stream_type", "string", required=True),
                FieldSpec("version", "string", required=True),
                FieldSpec("label", "string"),
            ),
            example={
                "platform": "linux",
                "hostname": "archon",
                "stream_type": "desktop",
                "version": "1.4.0",
                "label": "Archon desktop",
            },
        ),
        responses=(
            ResponseSpec(
                status=200,
                description="Observer registration descriptor.",
                named_fields=(
                    FieldSpec("key", "string", required=True),
                    FieldSpec("prefix", "string", required=True),
                    FieldSpec("name", "string", required=True),
                    FieldSpec("ingest_url", "string", required=True),
                    FieldSpec("protocol_version", "integer", required=True),
                ),
                example={
                    "key": "x7J7k2observerHandle",
                    "prefix": "x7J7k2ob",
                    "name": "archon",
                    "ingest_url": "/app/observer/ingest",
                    "protocol_version": 2,
                },
            ),
            _json_error(
                400,
                ("invalid_segment_or_stream", "missing_required_field"),
                "Register request failed validation.",
            ),
            _json_error(
                403,
                ("local_request_only",),
                "Register caller was not trusted localhost or paired link.",
            ),
            _json_error(
                500,
                ("settings_operation_failed",),
                "Observer record could not be saved.",
            ),
        ),
    ),
    OperationSpec(
        operation_id="observer.ingestUpload",
        method="POST",
        rule="/app/observer/ingest",
        summary="Upload observer segment files",
        description=(
            "Upload one capture segment as multipart form data and trigger local "
            "observe processing."
        ),
        parameters=_OBSERVER_AUTH_PARAMS,
        request=RequestSpec(
            content_type="multipart/form-data",
            fields=(
                FieldSpec("segment", "string", required=True),
                FieldSpec("day", "string", required=True),
                FieldSpec(
                    "files",
                    "array",
                    required=True,
                    raw_schema={
                        "type": "array",
                        "items": {"type": "string", "format": "binary"},
                    },
                ),
                FieldSpec("host", "string"),
                FieldSpec("platform", "string"),
                FieldSpec("meta", "string"),
            ),
            example={
                "segment": "143022_300",
                "day": "20260618",
                "files": ["screen.png", "audio.flac"],
                "host": "archon",
                "platform": "linux",
                "meta": '{"facet":"work"}',
            },
        ),
        responses=(
            ResponseSpec(
                status=200,
                description="Upload accepted, collision-adjusted, or duplicate.",
                named_fields=(
                    FieldSpec("status", "string", required=True),
                    FieldSpec("segment", "string"),
                    FieldSpec("files", "array", item_type="string"),
                    FieldSpec("bytes", "integer"),
                    FieldSpec("existing_segment", "string"),
                    FieldSpec("message", "string"),
                ),
                example={
                    "normal": {
                        "summary": "New segment accepted",
                        "value": {
                            "status": "ok",
                            "segment": "143022_300",
                            "files": ["screen.png", "audio.flac"],
                            "bytes": 524288,
                        },
                    },
                    "duplicate": {
                        "summary": "Duplicate segment",
                        "value": {
                            "status": "duplicate",
                            "existing_segment": "143022_300",
                            "message": "All files already received",
                        },
                    },
                },
            ),
            *_observer_auth_errors(),
            _json_error(
                400,
                (
                    "ingest_no_files",
                    "invalid_day",
                    "invalid_segment_or_stream",
                    "missing_required_field",
                ),
                "Upload request failed validation.",
            ),
            _json_error(
                422,
                ("ingest_contract_invalid",),
                "Uploaded contract-covered file failed journal contract validation.",
            ),
            _json_error(
                500,
                ("ingest_storage_failed",),
                "Uploaded file could not be stored.",
            ),
            _json_error(
                507,
                ("ingest_storage_failed",),
                "No segment slot was available after collision handling.",
            ),
        ),
    ),
    OperationSpec(
        operation_id="observer.ingestEvent",
        method="POST",
        rule="/app/observer/ingest/event",
        summary="Relay observer event",
        description="Relay an observer-originated event onto the local Callosum bus.",
        parameters=_OBSERVER_AUTH_PARAMS,
        request=RequestSpec(
            fields=(
                FieldSpec("tract", "string", required=True),
                FieldSpec("event", "string", required=True),
            ),
            example={
                "tract": "observe",
                "event": "status",
                "state": "recording",
            },
        ),
        responses=(
            ResponseSpec(
                status=200,
                description="Event relayed.",
                named_fields=(FieldSpec("status", "string", required=True),),
                example={"status": "ok"},
            ),
            *_observer_auth_errors(),
            _json_error(
                400,
                ("missing_required_field",),
                "Tract or event was missing.",
            ),
        ),
    ),
    OperationSpec(
        operation_id="observer.ingestManifest",
        method="GET",
        rule="/app/observer/ingest/manifest",
        summary="List observer manifest days",
        description="Return days with uploaded observer segment history.",
        parameters=_OBSERVER_AUTH_PARAMS,
        responses=(
            ResponseSpec(
                status=200,
                description="Available manifest days keyed by day.",
                named_fields=(
                    FieldSpec(
                        "days",
                        "object",
                        required=True,
                        raw_schema=_DAY_SEGMENT_COUNT_MAP,
                    ),
                ),
                example={"days": {"20260618": {"segments": 2}}},
            ),
            *_observer_auth_errors(),
        ),
    ),
    OperationSpec(
        operation_id="observer.ingestManifestDay",
        method="GET",
        rule="/app/observer/ingest/manifest/<day>",
        summary="Read transfer manifest for a day",
        description="Return file hashes and sizes for every segment on one day.",
        parameters=(
            *_OBSERVER_AUTH_PARAMS,
            ParamSpec("day", "path", required=True, description="YYYYMMDD day."),
        ),
        responses=(
            ResponseSpec(
                status=200,
                description="Day transfer manifest.",
                named_fields=(
                    FieldSpec("version", "integer", required=True),
                    FieldSpec("day", "string", required=True),
                    FieldSpec("created_at", "integer", required=True),
                    FieldSpec("host", "string", required=True),
                    FieldSpec(
                        "segments",
                        "object",
                        required=True,
                        raw_schema=_MANIFEST_SEGMENT_MAP,
                    ),
                ),
                example={
                    "version": 1,
                    "day": "20260618",
                    "created_at": 1781803200000,
                    "host": "archon",
                    "segments": {
                        "archon/143022_300": {
                            "files": [
                                {
                                    "name": "screen.png",
                                    "sha256": "5f70bf18a086007016bb522ec180fd0b",
                                    "size": 2048,
                                }
                            ]
                        }
                    },
                },
            ),
            *_observer_auth_errors(),
            _json_error(
                400,
                ("invalid_day",),
                "Day path parameter was invalid.",
            ),
        ),
    ),
    OperationSpec(
        operation_id="observer.ingestSegments",
        method="GET",
        rule="/app/observer/ingest/segments/<day>",
        summary="List uploaded observer segments",
        description=(
            "Return segment upload history for one day. Protocol version 2 and "
            "newer receive a collection envelope; older or absent protocol "
            "headers receive a legacy bare array."
        ),
        parameters=(
            *_OBSERVER_AUTH_PARAMS,
            ParamSpec("day", "path", required=True, description="YYYYMMDD day."),
            ParamSpec(
                "stream",
                "query",
                required=False,
                description="Fallback stream for legacy observer history rows.",
            ),
            ParamSpec(
                "X-Solstone-Protocol-Version",
                "header",
                type="integer",
                required=False,
                description=(
                    ">= current protocol version returns the collection envelope; "
                    "lower or absent returns the legacy bare array."
                ),
            ),
        ),
        responses=(
            ResponseSpec(
                status=200,
                description="Segment verification response.",
                raw_schema=_SEGMENTS_RESPONSE_SCHEMA,
                example={
                    "legacy": {
                        "summary": "Legacy bare array",
                        "value": [_SEGMENT_ITEM_EXAMPLE],
                    },
                    "v2": {
                        "summary": "Protocol v2 envelope",
                        "value": {
                            "items": [_SEGMENT_ITEM_EXAMPLE],
                            "total": 1,
                            "protocol_version": 2,
                        },
                    },
                },
            ),
            *_observer_auth_errors(),
            _json_error(
                400,
                ("invalid_day",),
                "Day path parameter was invalid.",
            ),
        ),
    ),
    OperationSpec(
        operation_id="observer.callosumStream",
        method="GET",
        rule="/app/observer/callosum",
        summary="Stream Callosum events",
        description=(
            "Open an observer-authenticated Server-Sent Events feed. Frames: data "
            "`data: {json}\\n\\n`; heartbeat `: heartbeat\\n\\n`; error "
            "`event: error\\ndata: {Error}\\n\\n`."
        ),
        parameters=_OBSERVER_AUTH_PARAMS,
        responses=(
            ResponseSpec(
                status=200,
                description=(
                    "Callosum event stream. Data frames carry CallosumEvent JSON; "
                    "heartbeat frames are comments; error frames carry Error JSON."
                ),
                content_type="text/event-stream",
                free_form=True,
                raw_schema={"$ref": "#/components/schemas/CallosumEvent"},
                example={
                    "tract": "observe",
                    "event": "observing",
                    "ts": 1781803200000,
                    "day": "20260618",
                    "segment": "143022_300",
                },
                extensions={
                    "x-sse-error-frame": {
                        "schema": {"$ref": "#/components/schemas/Error"},
                        "x-reason-codes": [
                            "auth_required",
                            "feature_unavailable",
                            "pl_revoked",
                        ],
                        "description": (
                            "In-stream `event: error` frames emit only these. "
                            "auth_key_invalid is excluded here because key "
                            "validity is established at stream open "
                            "(routes.py:259-261); mid-stream re-checks only "
                            "cover observer missing/revoked/disabled "
                            "(routes.py:281-292)."
                        ),
                    }
                },
            ),
            _json_error(
                401,
                (
                    "auth_key_invalid",
                    "auth_required",
                    "feature_unavailable",
                    "pl_revoked",
                ),
                (
                    "Stream-open observer auth failure; codes include 401 and "
                    "403 outcomes."
                ),
            ),
        ),
    ),
]

__all__ = ["OPERATIONS"]
