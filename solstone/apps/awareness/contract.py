# SPDX-License-Identifier: AGPL-3.0-only
# Copyright (c) 2026 sol pbc

"""OpenAPI fragment for the native-client awareness routes."""

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


_FREE_OBJECT = {"type": "object", "additionalProperties": True}
_FREE_ARRAY = {"type": "array", "items": _FREE_OBJECT}
_COLLECTION_FIELDS = (
    FieldSpec("items", "array", required=True, raw_schema=_FREE_ARRAY),
    FieldSpec("total", "integer", required=True),
)
OPERATIONS: list[OperationSpec] = [
    OperationSpec(
        operation_id="awareness.status",
        method="GET",
        rule="/app/awareness/api/state",
        summary="Read awareness state",
        description="Return current awareness state. Native clients select sections locally.",
        parameters=(ParamSpec("section", "query"),),
        responses=(
            ResponseSpec(
                status=200,
                description="Awareness state.",
                free_form=True,
            ),
            _json_error(
                404,
                ("awareness_section_not_found",),
                "The requested awareness section was not present.",
            ),
        ),
    ),
    OperationSpec(
        operation_id="awareness.imports.read",
        method="GET",
        rule="/app/awareness/api/imports",
        summary="Read import awareness state",
        description="Return import tracking state.",
        responses=(
            ResponseSpec(
                status=200,
                description="Import tracking state.",
                free_form=True,
            ),
        ),
    ),
    OperationSpec(
        operation_id="awareness.imports",
        method="POST",
        rule="/app/awareness/api/imports",
        summary="Update import awareness state",
        description="Record one import awareness event.",
        request=RequestSpec(
            fields=(
                FieldSpec("record", "string"),
                FieldSpec("declined", "boolean"),
                FieldSpec("nudge", "boolean"),
            ),
            example={"record": "apple_health"},
        ),
        responses=(
            ResponseSpec(
                status=200,
                description="Updated import tracking state.",
                free_form=True,
            ),
            _json_error(
                400,
                (
                    "invalid_json_request",
                    "invalid_request_value",
                    "missing_request_body",
                ),
                "The request body was missing, malformed, or ambiguous.",
            ),
            _json_error(
                503,
                ("awareness_busy",),
                "The awareness imports state could not be locked.",
            ),
        ),
    ),
    OperationSpec(
        operation_id="awareness.log-read",
        method="GET",
        rule="/app/awareness/api/log",
        summary="Read awareness log entries",
        description="Return paginated awareness log entries.",
        parameters=(
            ParamSpec("limit", "query"),
            ParamSpec("offset", "query"),
            ParamSpec("day", "query"),
            ParamSpec("kind", "query"),
        ),
        responses=(
            ResponseSpec(
                status=200,
                description="Awareness log collection.",
                named_fields=_COLLECTION_FIELDS,
            ),
        ),
    ),
    OperationSpec(
        operation_id="awareness.log",
        method="POST",
        rule="/app/awareness/api/log",
        summary="Append an awareness log entry",
        description="Append one awareness log entry.",
        request=RequestSpec(
            fields=(
                FieldSpec("kind", "string", required=True),
                FieldSpec("key", "string"),
                FieldSpec("message", "string"),
                FieldSpec("data", "object", raw_schema=_FREE_OBJECT),
            ),
            example={"kind": "observation", "message": "Observed import need."},
        ),
        responses=(
            ResponseSpec(
                status=201,
                description="Created awareness log entry.",
                free_form=True,
            ),
            _json_error(
                400,
                (
                    "invalid_json_request",
                    "missing_request_body",
                    "missing_required_field",
                ),
                "The request body was missing, malformed, or incomplete.",
            ),
        ),
    ),
]

__all__ = ["OPERATIONS"]
