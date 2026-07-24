# SPDX-License-Identifier: AGPL-3.0-only
# Copyright (c) 2026 sol pbc

"""OpenAPI fragment for the native-client entities routes."""

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


def _path(name: str, description: str) -> ParamSpec:
    return ParamSpec(name, "path", required=True, description=description)


def _query(name: str, description: str, type_: str = "string") -> ParamSpec:
    return ParamSpec(name, "query", type=type_, description=description)


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
        operation_id="entities.list",
        method="GET",
        rule="/app/entities/api/<facet_name>",
        summary="List facet entities",
        description="Return attached and detected entities for a facet.",
        parameters=(_path("facet_name", "Facet slug."),),
        responses=(
            _ok(
                "Facet entity lists.",
                (
                    FieldSpec(
                        "attached", "array", required=True, raw_schema=_FREE_ARRAY
                    ),
                    FieldSpec(
                        "detected", "array", required=True, raw_schema=_FREE_ARRAY
                    ),
                ),
            ),
            _json_error(
                500,
                ("entity_operation_failed",),
                "Facet entity state could not be read.",
            ),
        ),
    ),
    OperationSpec(
        operation_id="entities.detected.list",
        method="GET",
        rule="/app/entities/api/<facet_name>/detected",
        summary="List detected entities",
        description="Return detected entities for one facet day.",
        parameters=(
            _path("facet_name", "Facet slug."),
            _query("day", "Journal day."),
        ),
        responses=(
            _ok(
                "Detected entities.",
                (FieldSpec("items", "array", required=True, raw_schema=_FREE_ARRAY),),
            ),
            _json_error(
                400,
                ("missing_required_field",),
                "The day query parameter is required.",
            ),
        ),
    ),
    OperationSpec(
        operation_id="entities.resolve",
        method="GET",
        rule="/app/entities/api/<facet_name>/resolve",
        summary="Resolve one facet entity",
        description="Resolve a CLI entity query without mutating state.",
        parameters=(
            _path("facet_name", "Facet slug."),
            _query("name", "Entity query."),
        ),
        responses=(
            _ok(
                "Resolved entity payload.",
                (
                    FieldSpec("facet_exists", "boolean", required=True),
                    FieldSpec("resolved", "object", raw_schema=_FREE_OBJECT),
                    FieldSpec(
                        "candidates", "array", required=True, raw_schema=_FREE_ARRAY
                    ),
                    FieldSpec("blocked", "boolean", required=True),
                    FieldSpec("blocked_name", "string"),
                ),
            ),
            _json_error(
                400,
                ("missing_required_field",),
                "The name query parameter is required.",
            ),
        ),
    ),
    OperationSpec(
        operation_id="entities.detect",
        method="POST",
        rule="/app/entities/api/<facet_name>/detected",
        summary="Record detected entity",
        description="Record a detected entity for one facet day.",
        parameters=(_path("facet_name", "Facet slug."),),
        request=_body(
            (
                FieldSpec("day", "string", required=True),
                FieldSpec("type", "string", required=True),
                FieldSpec("entity", "string", required=True),
                FieldSpec("description", "string", required=True),
            ),
            {
                "day": "20260724",
                "type": "person",
                "entity": "Ada",
                "description": "compiler notes",
            },
        ),
        responses=(
            _ok(
                "Detected entity result.", (FieldSpec("name", "string", required=True),)
            ),
            _json_error(
                400,
                (
                    "entity_blocked",
                    "invalid_entity_type",
                    "invalid_request_value",
                    "missing_required_field",
                ),
                "The detected-entity request was invalid.",
            ),
            _json_error(503, ("entity_busy",), "Entity state is busy."),
        ),
    ),
    OperationSpec(
        operation_id="entities.attach",
        method="POST",
        rule="/app/entities/api/<facet_name>/attach",
        summary="Attach entity to facet",
        description="Attach or reactivate one entity in a facet.",
        parameters=(_path("facet_name", "Facet slug."),),
        request=_body(
            (
                FieldSpec("type", "string", required=True),
                FieldSpec("name", "string", required=True),
                FieldSpec("description", "string"),
            ),
            {"type": "person", "name": "Ada", "description": "compiler notes"},
        ),
        responses=(
            _ok(
                "Attached entity.",
                (
                    FieldSpec("id", "string"),
                    FieldSpec("name", "string"),
                    FieldSpec("type", "string"),
                    FieldSpec("description", "string"),
                    FieldSpec("attached_at", "number"),
                    FieldSpec("updated_at", "number"),
                ),
            ),
            _json_error(
                400,
                (
                    "entity_already_exists",
                    "entity_blocked",
                    "entity_not_found",
                    "invalid_entity_type",
                    "missing_request_body",
                    "missing_required_field",
                ),
                "The attach request was invalid.",
            ),
            _json_error(503, ("entity_busy",), "Entity state is busy."),
        ),
    ),
    OperationSpec(
        operation_id="entities.update",
        method="POST",
        rule="/app/entities/api/<facet_name>/update-description",
        summary="Update facet entity description",
        description="Update the description for an attached facet entity.",
        parameters=(_path("facet_name", "Facet slug."),),
        request=_body(
            (
                FieldSpec("entity_id", "string", required=True),
                FieldSpec("description", "string", required=True),
                FieldSpec("entity", "string"),
                FieldSpec("name", "string"),
            ),
            {"entity_id": "person-ada", "description": "new notes"},
        ),
        responses=(
            _ok(
                "Updated entity relationship.",
                (
                    FieldSpec(
                        "entity", "object", required=True, raw_schema=_FREE_OBJECT
                    ),
                ),
            ),
            _json_error(
                400,
                ("missing_required_field",),
                "The update request was missing a required field.",
            ),
            _json_error(404, ("entity_not_found",), "Entity was not attached."),
            _json_error(503, ("entity_busy",), "Entity state is busy."),
        ),
    ),
    OperationSpec(
        operation_id="entities.update-detected",
        method="POST",
        rule="/app/entities/api/<facet_name>/update-detected",
        summary="Update detected entity",
        description="Update the description for a detected entity row.",
        parameters=(_path("facet_name", "Facet slug."),),
        request=_body(
            (
                FieldSpec("day", "string", required=True),
                FieldSpec("entity", "string", required=True),
                FieldSpec("description", "string", required=True),
            ),
            {"day": "20260724", "entity": "Ada", "description": "new notes"},
        ),
        responses=(
            _ok(
                "Updated detected entity.",
                (FieldSpec("entity", "object", raw_schema=_FREE_OBJECT),),
            ),
            _json_error(
                400,
                ("invalid_request_value", "missing_required_field"),
                "The detected-entity update was invalid.",
            ),
            _json_error(503, ("entity_busy",), "Entity state is busy."),
        ),
    ),
    OperationSpec(
        operation_id="entities.aka",
        method="POST",
        rule="/app/entities/api/<facet_name>/aka",
        summary="Add entity alias",
        description="Add one alias to an attached entity.",
        parameters=(_path("facet_name", "Facet slug."),),
        request=_body(
            (
                FieldSpec("entity_id", "string", required=True),
                FieldSpec("aka", "string", required=True),
                FieldSpec("exclude_name", "string", required=True),
                FieldSpec("entity", "string"),
            ),
            {
                "entity_id": "person-ada",
                "aka": "Ada",
                "exclude_name": "Ada Lovelace",
            },
        ),
        responses=(
            _ok(
                "Updated alias list.",
                (FieldSpec("aka", "array", raw_schema=_FREE_ARRAY),),
            ),
            _json_error(
                400,
                ("entity_alias_conflict", "missing_required_field"),
                "The alias request was invalid.",
            ),
            _json_error(404, ("entity_not_found",), "Entity was not attached."),
            _json_error(503, ("entity_busy",), "Entity state is busy."),
        ),
    ),
    OperationSpec(
        operation_id="entities.move",
        method="POST",
        rule="/app/entities/api/move",
        summary="Move facet entity",
        description="Move one resolved entity between facets.",
        request=_body(
            (
                FieldSpec("entity", "string", required=True),
                FieldSpec("from_facet", "string", required=True),
                FieldSpec("to_facet", "string", required=True),
                FieldSpec("merge", "boolean"),
                FieldSpec("consent", "boolean"),
            ),
            {"entity": "Ada", "from_facet": "work", "to_facet": "personal"},
        ),
        responses=(
            _ok(
                "Move result.",
                (
                    FieldSpec("entity", "string", required=True),
                    FieldSpec("moved_from", "string", required=True),
                    FieldSpec("moved_to", "string", required=True),
                    FieldSpec("merged", "boolean", required=True),
                ),
            ),
            _json_error(
                400,
                (
                    "entity_already_exists",
                    "entity_operation_failed",
                    "missing_required_field",
                ),
                "The move request was invalid or failed.",
            ),
        ),
    ),
    OperationSpec(
        operation_id="entities.record-merge-candidate",
        method="POST",
        rule="/app/entities/api/record-merge-candidate",
        summary="Record merge candidate",
        description="Record or update one entity merge candidate.",
        request=_body(
            (
                FieldSpec("facet", "string", required=True),
                FieldSpec("day", "string", required=True),
                FieldSpec("source", "string", required=True),
                FieldSpec("target", "string", required=True),
                FieldSpec("evidence", "string", required=True),
                FieldSpec("basis", "string"),
                FieldSpec("detections", "integer"),
                FieldSpec("needs", "integer"),
            ),
            {
                "facet": "work",
                "day": "20260724",
                "source": "Ada",
                "target": "Ada Lovelace",
                "evidence": "same person",
            },
        ),
        responses=(
            _ok(
                "Merge candidate row.",
                (
                    FieldSpec("row", "object", required=True, raw_schema=_FREE_OBJECT),
                    FieldSpec("created", "boolean", required=True),
                ),
            ),
            _json_error(
                400,
                ("invalid_request_value", "missing_required_field"),
                "The merge-candidate request was invalid.",
            ),
            _json_error(503, ("entity_busy",), "Entity state is busy."),
        ),
    ),
    OperationSpec(
        operation_id="entities.merge-candidates",
        method="GET",
        rule="/app/entities/api/merge-candidates",
        summary="List merge candidates",
        description="Return recorded entity merge candidates.",
        parameters=(
            _query("facet", "Facet filter."),
            _query("status", "Status filter."),
        ),
        responses=(
            _ok(
                "Merge candidate collection.",
                (FieldSpec("items", "array", required=True, raw_schema=_FREE_ARRAY),),
            ),
        ),
    ),
    OperationSpec(
        operation_id="entities.accept-merge-candidate",
        method="POST",
        rule="/app/entities/api/accept-merge-candidate",
        summary="Preview or accept merge candidate",
        description="Preview or commit one recorded entity merge candidate.",
        request=_body(
            (
                FieldSpec("facet", "string", required=True),
                FieldSpec("source_slug", "string", required=True),
                FieldSpec("target_slug", "string", required=True),
                FieldSpec("commit", "boolean"),
            ),
            {
                "facet": "work",
                "source_slug": "ada",
                "target_slug": "ada-lovelace",
                "commit": False,
            },
        ),
        responses=(
            _ok("Merge candidate result."),
            _json_error(
                400,
                ("missing_required_field",),
                "The merge-candidate request was missing a required field.",
            ),
            _json_error(503, ("entity_busy",), "Entity state is busy."),
        ),
    ),
    OperationSpec(
        operation_id="entities.dismiss-merge-candidate",
        method="POST",
        rule="/app/entities/api/dismiss-merge-candidate",
        summary="Dismiss merge candidate",
        description="Dismiss one recorded entity merge candidate.",
        request=_body(
            (
                FieldSpec("facet", "string", required=True),
                FieldSpec("source_slug", "string", required=True),
                FieldSpec("target_slug", "string", required=True),
            ),
            {"facet": "work", "source_slug": "ada", "target_slug": "ada-lovelace"},
        ),
        responses=(
            _ok("Merge candidate dismissal result."),
            _json_error(
                400,
                ("missing_required_field",),
                "The merge-candidate request was missing a required field.",
            ),
            _json_error(503, ("entity_busy",), "Entity state is busy."),
        ),
    ),
    OperationSpec(
        operation_id="entities.merge",
        method="POST",
        rule="/app/entities/api/merge",
        summary="Plan or commit entity merge",
        description="Plan or commit a journal entity merge.",
        request=_body(
            (
                FieldSpec("source_slug", "string", required=True),
                FieldSpec("target_slug", "string", required=True),
                FieldSpec("commit", "boolean"),
                FieldSpec("keep_source_as_aka", "boolean"),
            ),
            {"source_slug": "ada", "target_slug": "ada-lovelace", "commit": False},
        ),
        responses=(
            _ok("Merge plan or commit result."),
            _json_error(
                400,
                (
                    "entity_blocked",
                    "entity_not_found",
                    "entity_operation_failed",
                    "invalid_request_value",
                    "missing_required_field",
                    "operation_no_longer_available",
                ),
                "The merge request was invalid or failed.",
            ),
            _json_error(503, ("entity_busy",), "Entity state is busy."),
        ),
    ),
    OperationSpec(
        operation_id="entities.undo-merge",
        method="POST",
        rule="/app/entities/api/merge/<merge_id>/undo",
        summary="Undo entity merge",
        description="Undo one recorded journal entity merge.",
        parameters=(_path("merge_id", "Merge operation id."),),
        request=RequestSpec(example={}),
        responses=(
            _ok("Undo result."),
            _json_error(
                400,
                (
                    "entity_blocked",
                    "entity_not_found",
                    "entity_operation_failed",
                    "invalid_request_value",
                    "operation_no_longer_available",
                ),
                "The undo request was invalid or failed.",
            ),
            _json_error(503, ("entity_busy",), "Entity state is busy."),
        ),
    ),
    OperationSpec(
        operation_id="entities.entity-history",
        method="GET",
        rule="/app/entities/api/journal/entity/<entity_id>/history",
        summary="List durable entity history",
        description="Return durable identity version history for a journal entity.",
        parameters=(_path("entity_id", "Journal entity id."),),
        responses=(
            _ok(
                "Entity history.",
                (
                    FieldSpec("entity_id", "string", required=True),
                    FieldSpec("items", "array", required=True, raw_schema=_FREE_ARRAY),
                ),
            ),
            _json_error(404, ("entity_not_found",), "Entity was not found."),
            _json_error(
                500,
                ("entity_operation_failed",),
                "Entity history could not be read.",
            ),
        ),
    ),
    OperationSpec(
        operation_id="entities.restore-version",
        method="POST",
        rule="/app/entities/api/journal/entity/<entity_id>/restore",
        summary="Restore durable entity version",
        description="Restore one ordinary durable identity version.",
        parameters=(_path("entity_id", "Journal entity id."),),
        request=_body(
            (FieldSpec("version_id", "string", required=True),),
            {"version_id": "v1"},
        ),
        responses=(
            _ok(
                "Restore result.",
                (
                    FieldSpec("restored", "boolean", required=True),
                    FieldSpec("entity", "object", raw_schema=_FREE_OBJECT),
                    FieldSpec("event", "object", raw_schema=_FREE_OBJECT),
                ),
            ),
            _json_error(
                400,
                ("invalid_request_value", "missing_required_field"),
                "The restore request was invalid.",
            ),
            _json_error(404, ("entity_not_found",), "Entity or version was not found."),
            _json_error(503, ("entity_busy",), "Entity state is busy."),
            _json_error(
                500,
                ("entity_operation_failed",),
                "Entity history restore failed.",
            ),
        ),
    ),
    OperationSpec(
        operation_id="entities.ambiguities",
        method="GET",
        rule="/app/entities/api/ambiguities",
        summary="List entity ambiguities",
        description="Return persisted entity resolution ambiguities.",
        parameters=(_query("status", "Optional status filter."),),
        responses=(
            _ok(
                "Ambiguity collection.",
                (FieldSpec("items", "array", required=True, raw_schema=_FREE_ARRAY),),
            ),
            _json_error(
                400,
                ("invalid_request_value",),
                "The ambiguity status filter was invalid.",
            ),
            _json_error(
                500,
                ("entity_operation_failed",),
                "Ambiguities could not be read.",
            ),
        ),
    ),
    OperationSpec(
        operation_id="entities.resolve-ambiguity",
        method="POST",
        rule="/app/entities/api/ambiguities/<ambiguity_id>/resolve",
        summary="Resolve entity ambiguity",
        description="Resolve one ambiguity to an existing scoped entity.",
        parameters=(_path("ambiguity_id", "Ambiguity id."),),
        request=_body(
            (FieldSpec("entity_id", "string", required=True),),
            {"entity_id": "person-ada"},
        ),
        responses=(
            _ok(
                "Resolved ambiguity.",
                (
                    FieldSpec(
                        "ambiguity", "object", required=True, raw_schema=_FREE_OBJECT
                    ),
                    FieldSpec("entity", "object", raw_schema=_FREE_OBJECT),
                ),
            ),
            _json_error(
                400,
                ("invalid_request_value", "missing_required_field"),
                "The ambiguity resolution request was invalid.",
            ),
            _json_error(404, ("entity_not_found",), "Ambiguity was not found."),
            _json_error(503, ("entity_busy",), "Entity state is busy."),
        ),
    ),
    OperationSpec(
        operation_id="entities.observations",
        method="GET",
        rule="/app/entities/api/<facet_name>/observations",
        summary="List entity observations",
        description="Return observations for one attached entity.",
        parameters=(
            _path("facet_name", "Facet slug."),
            _query("name", "Resolved entity name."),
        ),
        responses=(
            _ok(
                "Observation collection.",
                (FieldSpec("items", "array", required=True, raw_schema=_FREE_ARRAY),),
            ),
            _json_error(
                400,
                ("missing_required_field",),
                "The name query parameter is required.",
            ),
        ),
    ),
    OperationSpec(
        operation_id="entities.observe",
        method="POST",
        rule="/app/entities/api/<facet_name>/observe",
        summary="Record entity observation",
        description="Add one observation to an attached entity.",
        parameters=(_path("facet_name", "Facet slug."),),
        request=_body(
            (
                FieldSpec("name", "string", required=True),
                FieldSpec("content", "string", required=True),
                FieldSpec("source_day", "string"),
                FieldSpec("entity", "string"),
            ),
            {"name": "Ada", "content": "Met today."},
        ),
        responses=(
            _ok(
                "Observation result.",
                (FieldSpec("result", "object", raw_schema=_FREE_OBJECT),),
            ),
            _json_error(
                400,
                ("invalid_request_value", "missing_required_field"),
                "The observation request was invalid.",
            ),
            _json_error(503, ("entity_busy",), "Entity state is busy."),
        ),
    ),
    OperationSpec(
        operation_id="entities.search",
        method="GET",
        rule="/app/entities/api/search",
        summary="Search entities",
        description="Search indexed entities for the native CLI.",
        parameters=(
            _query("query", "Search text."),
            _query("type", "Entity type."),
            _query("facet", "Facet slug."),
            _query("since", "Since day."),
            _query("limit", "Maximum rows.", "integer"),
        ),
        responses=(
            _ok(
                "Search result collection.",
                (FieldSpec("items", "array", required=True, raw_schema=_FREE_ARRAY),),
            ),
        ),
    ),
    OperationSpec(
        operation_id="entities.network",
        method="GET",
        rule="/app/entities/api/network",
        summary="Read entity network",
        description="Return one-hop recorded connections for an entity.",
        parameters=(
            _query("entity", "Entity query."),
            _query("kinds", "Repeated or comma-separated relationship kind filter."),
            _query("facet", "Facet slug."),
            _query("day_from", "Start day."),
            _query("day_to", "End day."),
            _query("limit", "Maximum rows.", "integer"),
            _query("evidence_limit", "Evidence rows per peer.", "integer"),
            _query("include_principal", "Include principal entity.", "boolean"),
        ),
        responses=(
            _ok("Entity network payload."),
            _json_error(
                400,
                (
                    "entity_operation_failed",
                    "invalid_request_value",
                    "missing_required_field",
                ),
                "The network request was invalid.",
            ),
            _json_error(
                503,
                ("edge_index_unavailable",),
                "Edge index is not available.",
            ),
        ),
    ),
    OperationSpec(
        operation_id="entities.history",
        method="GET",
        rule="/app/entities/api/history",
        summary="Read entity edge history",
        description="Return newest-first derived edge evidence for an entity pair.",
        parameters=(
            _query("entity", "Entity query."),
            _query("peer", "Peer entity query."),
            _query("kinds", "Repeated or comma-separated relationship kind filter."),
            _query("facet", "Facet slug."),
            _query("day_from", "Start day."),
            _query("day_to", "End day."),
            _query("limit", "Maximum rows.", "integer"),
            _query("offset", "Offset.", "integer"),
        ),
        responses=(
            _ok("Entity edge history payload."),
            _json_error(
                400,
                (
                    "entity_operation_failed",
                    "invalid_request_value",
                    "missing_required_field",
                ),
                "The edge-history request was invalid.",
            ),
            _json_error(
                503,
                ("edge_index_unavailable",),
                "Edge index is not available.",
            ),
        ),
    ),
    OperationSpec(
        operation_id="entities.overview",
        method="GET",
        rule="/app/entities/api/overview",
        summary="Read entity network overview",
        description="Return a global derived edge network summary.",
        parameters=(
            _query("kinds", "Repeated or comma-separated relationship kind filter."),
            _query("facet", "Facet slug."),
            _query("day_from", "Start day."),
            _query("day_to", "End day."),
            _query("limit", "Maximum rows.", "integer"),
        ),
        responses=(
            _ok("Network overview payload."),
            _json_error(
                400,
                ("invalid_request_value",),
                "The overview request was invalid.",
            ),
            _json_error(
                503,
                ("edge_index_unavailable",),
                "Edge index is not available.",
            ),
        ),
    ),
]
