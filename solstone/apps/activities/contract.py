# SPDX-License-Identifier: AGPL-3.0-only
# Copyright (c) 2026 sol pbc

"""OpenAPI fragment for the native-client activities routes."""

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
_ACTIVITY_ITEMS = {
    "type": "array",
    "items": _FREE_OBJECT,
}
_PARTICIPATION_SCHEMA = {
    "type": "array",
    "items": _FREE_OBJECT,
}

_DAY_PARAM = ParamSpec("day", "path", description="Canonical day in YYYYMMDD form.")
_SPAN_ID_PARAM = ParamSpec("span_id", "path", description="Activity record span id.")
_FACET_PARAM = ParamSpec("facet", "query", description="Facet slug.")

_RECORD_RESPONSE_FIELDS = (
    FieldSpec("record", "object", required=True, raw_schema=_FREE_OBJECT),
    FieldSpec("markdown", "string", required=True),
)


OPERATIONS: list[OperationSpec] = [
    OperationSpec(
        operation_id="activities.list",
        method="GET",
        rule="/app/activities/api/day/<day>/records",
        summary="List activity records",
        description=(
            "Return the activity records for one day. Native clients pass "
            "facet when scoped and include_hidden as the Python-compatible 1/0 "
            "query value."
        ),
        parameters=(
            _DAY_PARAM,
            _FACET_PARAM,
            ParamSpec(
                "include_hidden",
                "query",
                description="Python-compatible 1/0 include-hidden selector.",
            ),
        ),
        responses=(
            ResponseSpec(
                status=200,
                description="Activity record list.",
                named_fields=(
                    FieldSpec(
                        "items",
                        "array",
                        required=True,
                        raw_schema=_ACTIVITY_ITEMS,
                    ),
                ),
                example={"items": []},
            ),
        ),
    ),
    OperationSpec(
        operation_id="activities.get",
        method="GET",
        rule="/app/activities/api/day/<day>/record/<span_id>",
        summary="Get one activity record",
        description="Return one activity record plus the markdown rendering.",
        parameters=(_DAY_PARAM, _SPAN_ID_PARAM, _FACET_PARAM),
        responses=(
            ResponseSpec(
                status=200,
                description="Activity record and markdown rendering.",
                named_fields=_RECORD_RESPONSE_FIELDS,
            ),
            _json_error(
                404,
                ("activity_not_found",),
                "No activity record matched the day, facet, and span id.",
            ),
        ),
    ),
    OperationSpec(
        operation_id="activities.create",
        method="POST",
        rule="/app/activities/api/day/<day>/records",
        summary="Create an activity record",
        description="Create a user or cogitate activity record for one day.",
        parameters=(_DAY_PARAM, _FACET_PARAM),
        request=RequestSpec(
            fields=(
                FieldSpec("title", "string", required=True),
                FieldSpec("source", "string"),
                FieldSpec("activity", "string"),
                FieldSpec("since_segment", "string"),
                FieldSpec("description", "string"),
                FieldSpec("details", "string"),
                FieldSpec("participation", "array", raw_schema=_PARTICIPATION_SCHEMA),
            ),
            example={"title": "Design review", "source": "user"},
        ),
        responses=(
            ResponseSpec(
                status=200,
                description="Created activity record and markdown rendering.",
                named_fields=_RECORD_RESPONSE_FIELDS,
            ),
            _json_error(
                400,
                ("activity_invalid",),
                "The request body did not describe a valid activity record.",
            ),
            _json_error(
                404,
                ("activity_not_found",),
                "The requested activity type was not available for the facet.",
            ),
            _json_error(
                409,
                ("activity_already_exists",),
                "The activity record already exists.",
            ),
            _json_error(
                503,
                ("activities_busy",),
                "The activities store could not be locked.",
            ),
        ),
    ),
    OperationSpec(
        operation_id="activities.update",
        method="POST",
        rule="/app/activities/api/day/<day>/record/<span_id>/update",
        summary="Update an activity record",
        description="Patch mutable fields on one activity record.",
        parameters=(_DAY_PARAM, _SPAN_ID_PARAM, _FACET_PARAM),
        request=RequestSpec(
            fields=(
                FieldSpec("patch", "object", required=True, raw_schema=_FREE_OBJECT),
                FieldSpec("note", "string"),
            ),
            example={"patch": {"title": "Design review"}, "note": "updated fields"},
        ),
        responses=(
            ResponseSpec(
                status=200,
                description="Updated activity record and markdown rendering.",
                named_fields=_RECORD_RESPONSE_FIELDS,
            ),
            _json_error(
                404,
                ("activity_not_found",),
                "No activity record matched the day, facet, and span id.",
            ),
            _json_error(
                503,
                ("activities_busy",),
                "The activities store could not be locked.",
            ),
        ),
    ),
    OperationSpec(
        operation_id="activities.mute",
        method="POST",
        rule="/app/activities/api/day/<day>/record/<span_id>/mute",
        summary="Mute an activity record",
        description="Hide an activity record without deleting it.",
        parameters=(_DAY_PARAM, _SPAN_ID_PARAM, _FACET_PARAM),
        request=RequestSpec(fields=(FieldSpec("reason", "string"),)),
        responses=(
            ResponseSpec(
                status=200,
                description="Muted activity record and markdown rendering.",
                named_fields=_RECORD_RESPONSE_FIELDS,
            ),
            _json_error(
                404,
                ("activity_not_found",),
                "No activity record matched the day, facet, and span id.",
            ),
            _json_error(
                503,
                ("activities_busy",),
                "The activities store could not be locked.",
            ),
        ),
    ),
    OperationSpec(
        operation_id="activities.unmute",
        method="POST",
        rule="/app/activities/api/day/<day>/record/<span_id>/unmute",
        summary="Unmute an activity record",
        description="Restore a previously hidden activity record.",
        parameters=(_DAY_PARAM, _SPAN_ID_PARAM, _FACET_PARAM),
        request=RequestSpec(fields=(FieldSpec("reason", "string"),)),
        responses=(
            ResponseSpec(
                status=200,
                description="Unmuted activity record and markdown rendering.",
                named_fields=_RECORD_RESPONSE_FIELDS,
            ),
            _json_error(
                404,
                ("activity_not_found",),
                "No activity record matched the day, facet, and span id.",
            ),
            _json_error(
                503,
                ("activities_busy",),
                "The activities store could not be locked.",
            ),
        ),
    ),
]

__all__ = ["OPERATIONS"]
