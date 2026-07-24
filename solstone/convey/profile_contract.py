# SPDX-License-Identifier: AGPL-3.0-only
# Copyright (c) 2026 sol pbc

"""OpenAPI fragment for the native-client profile routes."""

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
_PROFILE_FIELDS = (
    FieldSpec("name", "string", required=True),
    FieldSpec("type", "string", required=True),
    FieldSpec("facets", "array", required=True, item_type="string"),
    FieldSpec("is_self", "boolean", required=True),
    FieldSpec("cadence", "object", required=True, raw_schema=_FREE_OBJECT),
    FieldSpec("open_with_them", "array", required=True, raw_schema=_FREE_ARRAY),
    FieldSpec("closed_with_them_30d", "array", required=True, raw_schema=_FREE_ARRAY),
    FieldSpec(
        "decisions_involving_them",
        "array",
        required=True,
        raw_schema=_FREE_ARRAY,
    ),
)
_BRIEF_FIELDS = (
    FieldSpec("entity_id", "string", required=True),
    FieldSpec("name", "string", required=True),
    FieldSpec("type", "string", required=True),
    FieldSpec("description", "string", required=True),
    FieldSpec("last_seen", "string", required=True),
    FieldSpec("open_loop_count", "integer", required=True),
    FieldSpec("decisions_count_30d", "integer", required=True),
)
_CADENCE_FIELDS = (
    FieldSpec("recent_interactions_count_30d", "integer", required=True),
    FieldSpec("last_seen", "string", required=True),
    FieldSpec("avg_interval_days", "number", required=True),
    FieldSpec("gone_quiet_since", "string", required=True),
)
_ENTITY_NOT_FOUND = _json_error(
    404,
    ("entity_not_found",),
    "No profile entity matched the supplied name.",
)


OPERATIONS: list[OperationSpec] = [
    OperationSpec(
        operation_id="profile.full",
        method="GET",
        rule="/api/profile/<name>",
        summary="Read a full entity profile",
        description="Return full relationship profile context for one entity.",
        parameters=(
            ParamSpec("name", "path"),
            ParamSpec("facets", "query"),
            ParamSpec("include_mentions", "query"),
        ),
        responses=(
            ResponseSpec(
                status=200,
                description="Full profile.",
                named_fields=_PROFILE_FIELDS,
            ),
            _ENTITY_NOT_FOUND,
        ),
    ),
    OperationSpec(
        operation_id="profile.brief",
        method="GET",
        rule="/api/profile/<name>/brief",
        summary="Read a brief entity profile",
        description="Return compact profile fields for one entity.",
        parameters=(ParamSpec("name", "path"),),
        responses=(
            ResponseSpec(
                status=200,
                description="Brief profile.",
                named_fields=_BRIEF_FIELDS,
            ),
            _ENTITY_NOT_FOUND,
        ),
    ),
    OperationSpec(
        operation_id="profile.cadence",
        method="GET",
        rule="/api/profile/<name>/cadence",
        summary="Read entity cadence",
        description="Return cadence metrics for one entity.",
        parameters=(
            ParamSpec("name", "path"),
            ParamSpec("include_mentions", "query"),
        ),
        responses=(
            ResponseSpec(
                status=200,
                description="Profile cadence.",
                named_fields=_CADENCE_FIELDS,
            ),
            _ENTITY_NOT_FOUND,
        ),
    ),
    OperationSpec(
        operation_id="profile.list-active",
        method="GET",
        rule="/api/profiles/active",
        summary="List active profile ids",
        description="Return paginated active profile entity ids.",
        parameters=(
            ParamSpec("window_days", "query"),
            ParamSpec("limit", "query"),
            ParamSpec("offset", "query"),
        ),
        responses=(
            ResponseSpec(
                status=200,
                description="Active profile id collection.",
                named_fields=(
                    FieldSpec("items", "array", required=True, item_type="string"),
                    FieldSpec("total", "integer", required=True),
                ),
            ),
            _json_error(
                400,
                ("invalid_request_value",),
                "The window_days query parameter was invalid.",
            ),
        ),
    ),
]

__all__ = ["OPERATIONS"]
