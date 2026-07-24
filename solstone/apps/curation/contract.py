# SPDX-License-Identifier: AGPL-3.0-only
# Copyright (c) 2026 sol pbc

"""OpenAPI fragment for native-client curation facet routes."""

from __future__ import annotations

from solstone.convey.contract import FieldSpec, OperationSpec, RequestSpec, ResponseSpec


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
_CANDIDATE_ITEMS = {"type": "array", "items": _FREE_OBJECT}
_MUTATION_ERRORS = (
    _json_error(
        400,
        ("missing_required_field",),
        "The request did not include a candidate name_key.",
    ),
    _json_error(
        503,
        ("entity_busy",),
        "Facet suggestions could not be locked.",
    ),
)

OPERATIONS: list[OperationSpec] = [
    OperationSpec(
        operation_id="facets.list-candidates",
        method="GET",
        rule="/app/curation/api/facet/candidates",
        summary="List facet review candidates",
        description="Return recorded facet review candidates for local CLI filtering.",
        responses=(
            ResponseSpec(
                status=200,
                description="Facet candidate collection.",
                named_fields=(
                    FieldSpec(
                        "items",
                        "array",
                        required=True,
                        raw_schema=_CANDIDATE_ITEMS,
                    ),
                ),
            ),
        ),
    ),
    OperationSpec(
        operation_id="facets.accept",
        method="POST",
        rule="/app/curation/api/facet/accept",
        summary="Accept a facet review candidate",
        description="Accept one facet review candidate by name_key.",
        request=RequestSpec(
            fields=(FieldSpec("name_key", "string", required=True),),
            example={"name_key": "compiler"},
        ),
        responses=(
            ResponseSpec(
                status=200,
                description="Facet candidate accept result.",
                named_fields=(
                    FieldSpec("status", "string", required=True),
                    FieldSpec("kind", "string"),
                    FieldSpec("key", "string"),
                    FieldSpec("facet_slug", "string"),
                    FieldSpec("candidate", "object", raw_schema=_FREE_OBJECT),
                ),
            ),
            *_MUTATION_ERRORS,
        ),
    ),
    OperationSpec(
        operation_id="facets.dismiss",
        method="POST",
        rule="/app/curation/api/facet/dismiss",
        summary="Dismiss a facet review candidate",
        description="Dismiss one facet review candidate by name_key.",
        request=RequestSpec(
            fields=(FieldSpec("name_key", "string", required=True),),
            example={"name_key": "compiler"},
        ),
        responses=(
            ResponseSpec(
                status=200,
                description="Facet candidate dismiss result.",
                named_fields=(
                    FieldSpec("status", "string", required=True),
                    FieldSpec("kind", "string"),
                    FieldSpec("key", "string"),
                    FieldSpec("candidate", "object", raw_schema=_FREE_OBJECT),
                ),
            ),
            *_MUTATION_ERRORS,
        ),
    ),
]

__all__ = ["OPERATIONS"]
