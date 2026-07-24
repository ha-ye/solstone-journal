# SPDX-License-Identifier: AGPL-3.0-only
# Copyright (c) 2026 sol pbc

"""OpenAPI fragment for the native-client ledger routes."""

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
_ITEMS_ARRAY = {"type": "array", "items": _FREE_OBJECT}
_COLLECTION_FIELDS = (
    FieldSpec("items", "array", required=True, raw_schema=_ITEMS_ARRAY),
    FieldSpec("total", "integer", required=True),
)
_ITEM_FIELDS = (
    FieldSpec("id", "string", required=True),
    FieldSpec("state", "string", required=True),
    FieldSpec("owner", "string", required=True),
    FieldSpec("summary", "string", required=True),
    FieldSpec("counterparty", "string"),
    FieldSpec("age_days", "integer", required=True),
    FieldSpec("when", "string"),
    FieldSpec("opened_at", "string", required=True),
    FieldSpec("closed_at", "string"),
)


OPERATIONS: list[OperationSpec] = [
    OperationSpec(
        operation_id="ledger.list",
        method="GET",
        rule="/api/ledger",
        summary="List ledger items",
        description="Return paginated ledger items.",
        parameters=(
            ParamSpec("state", "query"),
            ParamSpec("owner", "query"),
            ParamSpec("counterparty", "query"),
            ParamSpec("age_days_gte", "query"),
            ParamSpec("closed_since", "query"),
            ParamSpec("sort", "query"),
            ParamSpec("facets", "query"),
            ParamSpec("limit", "query"),
            ParamSpec("offset", "query"),
        ),
        responses=(
            ResponseSpec(
                status=200,
                description="Ledger item collection.",
                named_fields=_COLLECTION_FIELDS,
            ),
            _json_error(
                400,
                ("invalid_day", "invalid_request_value"),
                "One query parameter was invalid.",
            ),
        ),
    ),
    OperationSpec(
        operation_id="ledger.decisions",
        method="GET",
        rule="/api/ledger/decisions",
        summary="List ledger decisions",
        description="Return paginated deduplicated ledger decisions.",
        parameters=(
            ParamSpec("owner", "query"),
            ParamSpec("since", "query"),
            ParamSpec("involving", "query"),
            ParamSpec("facets", "query"),
            ParamSpec("limit", "query"),
            ParamSpec("offset", "query"),
        ),
        responses=(
            ResponseSpec(
                status=200,
                description="Ledger decision collection.",
                named_fields=_COLLECTION_FIELDS,
            ),
            _json_error(
                400,
                ("invalid_day",),
                "The since day was invalid.",
            ),
        ),
    ),
    OperationSpec(
        operation_id="ledger.get",
        method="GET",
        rule="/api/ledger/<item_id>",
        summary="Get a ledger item",
        description="Return one ledger item.",
        parameters=(ParamSpec("item_id", "path"),),
        responses=(
            ResponseSpec(
                status=200,
                description="Ledger item.",
                named_fields=_ITEM_FIELDS,
            ),
            _json_error(
                404,
                ("ledger_item_not_found",),
                "No ledger item matched the id.",
            ),
        ),
    ),
    OperationSpec(
        operation_id="ledger.close",
        method="POST",
        rule="/api/ledger/<item_id>/close",
        summary="Close a ledger item",
        description="Close or drop one ledger item.",
        parameters=(ParamSpec("item_id", "path"),),
        request=RequestSpec(
            fields=(
                FieldSpec("note", "string", required=True),
                FieldSpec("as_state", "string"),
            ),
            example={"note": "Handled in meeting.", "as_state": "closed"},
        ),
        responses=(
            ResponseSpec(
                status=200,
                description="Updated ledger item.",
                named_fields=_ITEM_FIELDS,
            ),
            _json_error(
                400,
                (
                    "invalid_json_request",
                    "invalid_request_value",
                    "missing_request_body",
                    "missing_required_field",
                ),
                "The request body was missing, malformed, or invalid.",
            ),
            _json_error(
                404,
                ("ledger_item_not_found",),
                "No ledger item matched the id.",
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
