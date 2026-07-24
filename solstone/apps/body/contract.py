# SPDX-License-Identifier: AGPL-3.0-only
# Copyright (c) 2026 sol pbc

"""OpenAPI fragment for the native-client body routes."""

from __future__ import annotations

from solstone.convey.contract import FieldSpec, OperationSpec, ParamSpec, ResponseSpec


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

OPERATIONS: list[OperationSpec] = [
    OperationSpec(
        operation_id="body.status",
        method="GET",
        rule="/app/body/api/status",
        summary="Read imported body data status",
        description="Return import and normalized-record coverage for body data.",
        responses=(
            ResponseSpec(
                status=200,
                description="Imported body data status.",
                named_fields=(
                    FieldSpec(
                        "imports", "array", required=True, raw_schema=_FREE_ARRAY
                    ),
                    FieldSpec(
                        "normalized",
                        "object",
                        required=True,
                        raw_schema=_FREE_OBJECT,
                    ),
                    FieldSpec(
                        "coverage_window",
                        "object",
                        required=True,
                        raw_schema=_FREE_OBJECT,
                    ),
                ),
            ),
        ),
    ),
    OperationSpec(
        operation_id="body.day",
        method="GET",
        rule="/app/body/api/day/<day>",
        summary="Read one body-data day",
        description="Return imported body data for one canonical day.",
        parameters=(
            ParamSpec(
                "day",
                "path",
                description="Canonical day in YYYYMMDD form.",
            ),
        ),
        responses=(
            ResponseSpec(
                status=200,
                description="Imported body data for one day.",
                named_fields=(
                    FieldSpec("day", "string", required=True),
                    FieldSpec("entry_total", "integer", required=True),
                    FieldSpec(
                        "glucose",
                        "object",
                        required=True,
                        raw_schema=_FREE_OBJECT,
                    ),
                ),
            ),
            _json_error(
                400,
                ("invalid_day",),
                "The requested day was not valid.",
            ),
        ),
    ),
    OperationSpec(
        operation_id="body.window",
        method="GET",
        rule="/app/body/api/window",
        summary="Read body-data window context",
        description="Return imported body context for a bounded time window.",
        parameters=(
            ParamSpec("from", "query", required=True, description="ISO window start."),
            ParamSpec("to", "query", required=True, description="ISO window end."),
        ),
        responses=(
            ResponseSpec(
                status=200,
                description="Imported body data for a time window.",
                named_fields=(
                    FieldSpec("from", "string", required=True),
                    FieldSpec("to", "string", required=True),
                    FieldSpec("entry_total", "integer", required=True),
                    FieldSpec("brief_label", "string"),
                ),
            ),
            _json_error(
                400,
                ("invalid_request_value",),
                "The requested window was not valid.",
            ),
        ),
    ),
]

__all__ = ["OPERATIONS"]
