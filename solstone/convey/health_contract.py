# SPDX-License-Identifier: AGPL-3.0-only
# Copyright (c) 2026 sol pbc

"""OpenAPI fragment for the native-client health routes."""

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
_STRING_ARRAY = {"type": "array", "items": {"type": "string"}}
_NOTES_ARRAY = {
    "type": "array",
    "items": _FREE_OBJECT,
}

_DAY_QUERY = ParamSpec("day", "query", description="Canonical day in YYYYMMDD form.")
_HEALTH_REPORT_FIELDS = (
    FieldSpec("generated_at", "integer", required=True),
    FieldSpec(
        "range",
        "array",
        required=True,
        raw_schema=_STRING_ARRAY,
    ),
    FieldSpec("facets", "array", required=True, raw_schema=_STRING_ARRAY),
    FieldSpec("capture_health", "object", required=True, raw_schema=_FREE_OBJECT),
    FieldSpec("synthesis_health", "object", required=True, raw_schema=_FREE_OBJECT),
    FieldSpec("consumer_signal", "object", required=True, raw_schema=_FREE_OBJECT),
    FieldSpec("segment_backlog", "object", required=True, raw_schema=_FREE_OBJECT),
    FieldSpec("notes", "array", required=True, raw_schema=_NOTES_ARRAY),
    FieldSpec("brain_health", "object", required=True, raw_schema=_FREE_OBJECT),
)
_HEALTH_ERRORS = (
    _json_error(
        400,
        ("invalid_request_value",),
        "The supplied day or day range was not valid.",
    ),
    _json_error(
        500,
        ("health_report_failed",),
        "The health report could not be built.",
    ),
)


OPERATIONS: list[OperationSpec] = [
    OperationSpec(
        operation_id="health.summary",
        method="GET",
        rule="/api/health/summary",
        summary="Summarize journal health signals",
        description="Return journal-data trust signals for one day.",
        parameters=(_DAY_QUERY,),
        responses=(
            ResponseSpec(
                status=200,
                description="Journal-data trust summary.",
                named_fields=_HEALTH_REPORT_FIELDS,
            ),
            *_HEALTH_ERRORS,
        ),
    ),
    OperationSpec(
        operation_id="health.full",
        method="GET",
        rule="/api/health/full",
        summary="Read full journal health report",
        description="Return the full journal-data trust report for one day.",
        parameters=(_DAY_QUERY,),
        responses=(
            ResponseSpec(
                status=200,
                description="Full journal-data trust report.",
                named_fields=_HEALTH_REPORT_FIELDS,
            ),
            *_HEALTH_ERRORS,
        ),
    ),
    OperationSpec(
        operation_id="health.for_range",
        method="GET",
        rule="/api/health/range",
        summary="Read journal health report for a day range",
        description="Return journal-data trust signals for an inclusive day range.",
        parameters=(
            ParamSpec(
                "day_from",
                "query",
                description="Inclusive start day in YYYYMMDD form.",
            ),
            ParamSpec(
                "day_to",
                "query",
                description="Inclusive end day in YYYYMMDD form.",
            ),
        ),
        responses=(
            ResponseSpec(
                status=200,
                description="Journal-data trust report for the requested range.",
                named_fields=_HEALTH_REPORT_FIELDS,
            ),
            *_HEALTH_ERRORS,
        ),
    ),
    OperationSpec(
        operation_id="health.pipeline",
        method="GET",
        rule="/api/health/pipeline",
        summary="Read pipeline health summary",
        description=(
            "Return the day-level think pipeline health summary for an explicit "
            "canonical day. Clients own today/yesterday/default date selection."
        ),
        parameters=(
            ParamSpec(
                "day",
                "query",
                required=True,
                description="Canonical day in YYYYMMDD form.",
            ),
        ),
        responses=(
            ResponseSpec(
                status=200,
                description="Pipeline health summary.",
                free_form=True,
            ),
            _json_error(
                400,
                ("invalid_request_value", "missing_required_field"),
                "day was missing or not a canonical calendar date.",
            ),
            _json_error(
                500,
                ("health_report_failed",),
                "The pipeline health summary could not be built.",
            ),
        ),
    ),
]

__all__ = ["OPERATIONS"]
