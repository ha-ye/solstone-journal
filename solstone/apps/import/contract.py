# SPDX-License-Identifier: AGPL-3.0-only
# Copyright (c) 2026 sol pbc

"""OpenAPI fragment for the native-client import routes."""

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


_NULLABLE_STRING = {"type": ["string", "null"]}
_NULLABLE_INTEGER = {"type": ["integer", "null"]}
_FREE_OBJECT = {"type": "object", "additionalProperties": True}
_STATUS_SCHEMA = {"type": "string", "enum": ["staged", "duplicate"]}
_SOURCE_SCHEMA = {"type": "string", "enum": ["audio", "image", "document", "text"]}
_ACTION_SCHEMA = {"type": "string", "enum": ["start", "do_not_start"]}
_SOURCE_INFERENCE_SCHEMA = {
    "type": "string",
    "enum": ["extension", "content_type", "default"],
}
_METADATA_SCHEMA = {
    "type": "object",
    "additionalProperties": True,
    "properties": {
        "original_filename": _NULLABLE_STRING,
        "mime_type": _NULLABLE_STRING,
        "imported_via": _NULLABLE_STRING,
        "observer_handle": _NULLABLE_STRING,
        "source_hint": _NULLABLE_STRING,
        "client": _FREE_OBJECT,
    },
    "required": [
        "original_filename",
        "mime_type",
        "imported_via",
        "observer_handle",
        "source_hint",
        "client",
    ],
}
_DIAGNOSTICS_SCHEMA = {
    "type": "object",
    "additionalProperties": True,
    "properties": {
        "timestamp_detection_method": {"type": "string"},
        "timestamp_detection_model_called": {"type": "boolean"},
        "timestamp_detection_no_match_reason": _NULLABLE_STRING,
        "source_inference": _SOURCE_INFERENCE_SCHEMA,
    },
    "required": [
        "timestamp_detection_method",
        "timestamp_detection_model_called",
        "timestamp_detection_no_match_reason",
        "source_inference",
    ],
}
_DUPLICATE_SCHEMA = {
    "type": "object",
    "additionalProperties": True,
    "properties": {
        "import_id": {"type": "string"},
        "imported_at": _NULLABLE_STRING,
        "entry_count": _NULLABLE_INTEGER,
        "state": {"type": "string", "enum": ["imported", "staged"]},
    },
    "required": ["import_id", "imported_at", "entry_count", "state"],
}

_SAVE_RESPONSE_FIELDS = (
    FieldSpec("schema_version", "integer", required=True),
    FieldSpec("status", "string", required=True, raw_schema=_STATUS_SCHEMA),
    FieldSpec("replay", "boolean", required=True),
    FieldSpec("path", "string", required=True),
    FieldSpec("timestamp", "string", required=True),
    FieldSpec("client_item_id", "string", required=True),
    FieldSpec("source", "string", required=True, raw_schema=_SOURCE_SCHEMA),
    FieldSpec("facet", "string", required=True, raw_schema=_NULLABLE_STRING),
    FieldSpec("setting", "string", required=True, raw_schema=_NULLABLE_STRING),
    FieldSpec(
        "recommended_action",
        "string",
        required=True,
        raw_schema=_ACTION_SCHEMA,
    ),
    FieldSpec("metadata", "object", required=True, raw_schema=_METADATA_SCHEMA),
    FieldSpec("diagnostics", "object", required=True, raw_schema=_DIAGNOSTICS_SCHEMA),
    FieldSpec("duplicate", "object", raw_schema=_DUPLICATE_SCHEMA),
    FieldSpec("in_progress", "boolean"),
)

_SAVE_RESPONSE_EXAMPLE = {
    "schema_version": 1,
    "status": "staged",
    "replay": False,
    "path": "/journal/imports/20260618_143022/source.m4a",
    "timestamp": "20260618_143022",
    "client_item_id": "ios-item-4f8b",
    "source": "audio",
    "facet": None,
    "setting": None,
    "recommended_action": "start",
    "metadata": {
        "original_filename": "source.m4a",
        "mime_type": "audio/mp4",
        "imported_via": "ios",
        "observer_handle": None,
        "source_hint": None,
        "client": {},
    },
    "diagnostics": {
        "timestamp_detection_method": "upload_fallback",
        "timestamp_detection_model_called": False,
        "timestamp_detection_no_match_reason": None,
        "source_inference": "extension",
    },
}
_JOURNAL_SOURCE_ERROR = _json_error(
    404,
    ("journal_source_problem",),
    "The named journal source was not found.",
)
_JOURNAL_SOURCE_RESOLVE_ERRORS = (
    _json_error(
        404,
        ("journal_source_problem", "import_not_found"),
        "The journal source or staged import item was not found.",
    ),
    _json_error(
        400,
        ("invalid_request_value",),
        "The requested resolution action or value was invalid.",
    ),
)


OPERATIONS: list[OperationSpec] = [
    OperationSpec(
        operation_id="import.save",
        method="POST",
        rule="/app/import/api/save",
        summary="Save import source",
        description=(
            "Save an uploaded import file or pasted text into imports staging. "
            "Submit either file or text. client_item_id is required for "
            "idempotent native-client staging."
        ),
        request=RequestSpec(
            content_type="multipart/form-data",
            fields=(
                FieldSpec("client_item_id", "string", required=True),
                FieldSpec(
                    "file",
                    "string",
                    raw_schema={"type": "string", "format": "binary"},
                ),
                FieldSpec("text", "string"),
                FieldSpec("facet", "string"),
                FieldSpec("setting", "string"),
                FieldSpec("source_hint", "string"),
                FieldSpec("imported_via", "string"),
                FieldSpec("observer_handle", "string"),
                FieldSpec("deterministic_only", "boolean"),
                FieldSpec("client", "object"),
            ),
            description="Multipart body with either file or text.",
        ),
        responses=(
            ResponseSpec(
                status=200,
                description=(
                    "Import source staged, replayed, or identified as a duplicate."
                ),
                named_fields=_SAVE_RESPONSE_FIELDS,
                example=_SAVE_RESPONSE_EXAMPLE,
            ),
            _json_error(
                400,
                ("ingest_no_files", "missing_required_field"),
                "Required fields were missing or neither file nor text was supplied.",
            ),
            _json_error(
                409,
                ("import_client_id_conflict",),
                "client_item_id already names different staged content.",
            ),
            _json_error(
                403,
                ("pl_revoked",),
                "Access gate rejected a revoked paired-link identity.",
            ),
        ),
    ),
    OperationSpec(
        operation_id="import.savePath",
        method="POST",
        rule="/app/import/api/save-path",
        summary="Save import source path",
        description=(
            "Register a local filesystem path for import staging using the same "
            "idempotent summary response as import.save."
        ),
        request=RequestSpec(
            fields=(
                FieldSpec("client_item_id", "string", required=True),
                FieldSpec("path", "string", required=True),
                FieldSpec("facet", "string"),
                FieldSpec("setting", "string"),
                FieldSpec("source_hint", "string"),
                FieldSpec("imported_via", "string"),
                FieldSpec("observer_handle", "string"),
                FieldSpec("client", "object"),
            ),
            example={
                "client_item_id": "ios-path-1357",
                "path": "/Users/sol/Documents/Notes",
                "source_hint": "obsidian",
                "client": {},
            },
        ),
        responses=(
            ResponseSpec(
                status=200,
                description=(
                    "Import path staged, replayed, or identified as a duplicate."
                ),
                named_fields=_SAVE_RESPONSE_FIELDS,
                example={
                    **_SAVE_RESPONSE_EXAMPLE,
                    "path": "/Users/sol/Documents/Notes",
                    "client_item_id": "ios-path-1357",
                    "source": "text",
                    "metadata": {
                        **_SAVE_RESPONSE_EXAMPLE["metadata"],
                        "original_filename": "Notes",
                        "mime_type": None,
                        "source_hint": "obsidian",
                    },
                },
            ),
            _json_error(
                400,
                ("missing_required_field",),
                "client_item_id or path was missing.",
            ),
            _json_error(
                404,
                ("file_not_found",),
                "The local path did not exist.",
            ),
            _json_error(
                409,
                ("import_client_id_conflict",),
                "client_item_id already names different staged content.",
            ),
            _json_error(
                403,
                ("pl_revoked",),
                "Access gate rejected a revoked paired-link identity.",
            ),
        ),
    ),
    OperationSpec(
        operation_id="import.meta",
        method="POST",
        rule="/app/import/api/meta",
        summary="Update import metadata",
        description="Update allowlisted metadata fields on a not-yet-started import.",
        request=RequestSpec(
            fields=(
                FieldSpec("path", "string", required=True),
                FieldSpec("facet", "string"),
                FieldSpec("setting", "string"),
                FieldSpec("original_filename", "string"),
                FieldSpec("mime_type", "string"),
                FieldSpec("source_hint", "string"),
                FieldSpec("observer_handle", "string"),
                FieldSpec("imported_via", "string"),
                FieldSpec("client", "object"),
            ),
            example={
                "path": "/journal/imports/20260618_143022/source.m4a",
                "facet": "work",
            },
        ),
        responses=(
            ResponseSpec(
                status=200,
                description="Import metadata updated.",
                named_fields=(
                    FieldSpec("status", "string", required=True),
                    FieldSpec("path", "string", required=True),
                    FieldSpec("timestamp", "string", required=True),
                    FieldSpec("updated", "object", required=True),
                ),
                example={
                    "status": "ok",
                    "path": "/journal/imports/20260618_143022/source.m4a",
                    "timestamp": "20260618_143022",
                    "updated": {"facet": "work"},
                },
            ),
            _json_error(
                400,
                ("invalid_operation_for_state", "missing_required_field"),
                "The import path was missing or the import state is terminal.",
            ),
            _json_error(
                404,
                ("import_not_found",),
                "Import metadata was not found.",
            ),
            _json_error(
                500,
                ("import_metadata_failed",),
                "Import metadata could not be read or updated.",
            ),
            _json_error(
                403,
                ("pl_revoked",),
                "Access gate rejected a revoked paired-link identity.",
            ),
        ),
    ),
    OperationSpec(
        operation_id="import.start",
        method="POST",
        rule="/app/import/api/start",
        summary="Start import",
        description=(
            "Start processing a previously saved import source. Saved import "
            "metadata is authoritative for facet, setting, and source routing."
        ),
        request=RequestSpec(
            fields=(
                FieldSpec("path", "string", required=True),
                FieldSpec("timestamp", "string", required=True),
                FieldSpec("force", "boolean"),
            ),
            example={
                "path": "/journal/imports/20260618_143022/source.txt",
                "timestamp": "20260618_143022",
                "force": False,
            },
        ),
        responses=(
            ResponseSpec(
                status=200,
                description="Import task requested.",
                named_fields=(
                    FieldSpec("status", "string", required=True),
                    FieldSpec("task_id", "string", required=True),
                ),
                example={"status": "ok", "task_id": "1781803200000"},
            ),
            _json_error(
                400,
                ("invalid_operation_for_state", "missing_required_field"),
                "Path or timestamp was missing, or the import is terminal.",
            ),
            _json_error(
                404,
                ("import_not_found",),
                "Import metadata or directory was not found.",
            ),
            _json_error(
                409,
                ("import_conflict",),
                "Import target timestamp already exists.",
            ),
            _json_error(
                500,
                ("import_metadata_failed",),
                "Import metadata could not be read or updated.",
            ),
            _json_error(
                503,
                ("import_queue_unreachable",),
                "your journal's background service isn't running. start it, then try again.",
            ),
            _json_error(
                403,
                ("pl_revoked",),
                "Access gate rejected a revoked paired-link identity.",
            ),
        ),
    ),
    OperationSpec(
        operation_id="import.list-staged",
        method="GET",
        rule="/app/import/api/journal-sources/<name>/staged",
        summary="List staged journal-source imports",
        description="Return staged entity, facet, and config items for a journal source.",
        parameters=(
            ParamSpec("name", "path"),
            ParamSpec("area", "query"),
        ),
        responses=(
            ResponseSpec(
                status=200,
                description="Staged import collection.",
                named_fields=(
                    FieldSpec(
                        "items",
                        "array",
                        required=True,
                        raw_schema={
                            "type": "array",
                            "items": _FREE_OBJECT,
                        },
                    ),
                    FieldSpec("total", "integer", required=True),
                ),
            ),
            _JOURNAL_SOURCE_ERROR,
            _json_error(
                400,
                ("invalid_request_value",),
                "The requested staged area was invalid.",
            ),
        ),
    ),
    OperationSpec(
        operation_id="import.resolve-entity",
        method="POST",
        rule="/app/import/api/journal-sources/<name>/resolve-entity",
        summary="Resolve staged journal-source entity",
        description="Merge, create, or skip one staged entity from a journal source.",
        parameters=(ParamSpec("name", "path"),),
        request=RequestSpec(
            fields=(
                FieldSpec("source_id", "string", required=True),
                FieldSpec("action", "string", required=True),
                FieldSpec("target", "string"),
            ),
            example={
                "source_id": "person_ada",
                "action": "merge",
                "target": "ada",
            },
        ),
        responses=(
            ResponseSpec(
                status=200,
                description="Entity resolution applied.",
                free_form=True,
            ),
            *_JOURNAL_SOURCE_RESOLVE_ERRORS,
        ),
    ),
    OperationSpec(
        operation_id="import.resolve-staged-facet",
        method="POST",
        rule="/app/import/api/journal-sources/<name>/resolve-facet",
        summary="Resolve staged journal-source facet",
        description="Apply or skip one staged facet file from a journal source.",
        parameters=(ParamSpec("name", "path"),),
        request=RequestSpec(
            fields=(
                FieldSpec("staged_file", "string", required=True),
                FieldSpec("mode", "string", required=True),
            ),
            example={"staged_file": "work/facet/foo.staged.json", "mode": "apply"},
        ),
        responses=(
            ResponseSpec(
                status=200,
                description="Facet resolution applied.",
                named_fields=(FieldSpec("status", "string", required=True),),
            ),
            *_JOURNAL_SOURCE_RESOLVE_ERRORS,
        ),
    ),
    OperationSpec(
        operation_id="import.resolve-config",
        method="POST",
        rule="/app/import/api/journal-sources/<name>/resolve-config",
        summary="Resolve staged journal-source config field",
        description="Apply or keep one staged config field from a journal source.",
        parameters=(ParamSpec("name", "path"),),
        request=RequestSpec(
            fields=(
                FieldSpec("field", "string", required=True),
                FieldSpec("action", "string", required=True),
            ),
            example={"field": "identity.name", "action": "apply"},
        ),
        responses=(
            ResponseSpec(
                status=200,
                description="Config field resolution applied.",
                named_fields=(FieldSpec("status", "string", required=True),),
            ),
            *_JOURNAL_SOURCE_RESOLVE_ERRORS,
        ),
    ),
    OperationSpec(
        operation_id="import.resolve-config-all",
        method="POST",
        rule="/app/import/api/journal-sources/<name>/resolve-config-all",
        summary="Resolve staged journal-source config category",
        description="Apply all staged config fields in one category.",
        parameters=(ParamSpec("name", "path"),),
        request=RequestSpec(
            fields=(FieldSpec("category", "string", required=True),),
            example={"category": "transferable"},
        ),
        responses=(
            ResponseSpec(
                status=200,
                description="Config category resolution applied.",
                named_fields=(FieldSpec("count", "integer", required=True),),
            ),
            *_JOURNAL_SOURCE_RESOLVE_ERRORS,
        ),
    ),
]

__all__ = ["OPERATIONS"]
