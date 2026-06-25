# SPDX-License-Identifier: AGPL-3.0-only
# Copyright (c) 2026 sol pbc

"""OpenAPI fragment for the native-client import routes."""

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


_NULLABLE_STRING = {"type": ["string", "null"]}


OPERATIONS: list[OperationSpec] = [
    OperationSpec(
        operation_id="import.save",
        method="POST",
        rule="/app/import/api/save",
        summary="Save import source",
        description=(
            "Save an uploaded import file or pasted text into imports staging. "
            "Submit either file or text."
        ),
        request=RequestSpec(
            content_type="multipart/form-data",
            fields=(
                FieldSpec(
                    "file",
                    "string",
                    raw_schema={"type": "string", "format": "binary"},
                ),
                FieldSpec("text", "string"),
                FieldSpec("facet", "string"),
                FieldSpec("setting", "string"),
                FieldSpec("imported_via", "string"),
                FieldSpec("observer_handle", "string"),
                FieldSpec("deterministic_only", "boolean"),
            ),
            description="Multipart body with either file or text.",
        ),
        responses=(
            ResponseSpec(
                status=200,
                description="Import source saved.",
                named_fields=(
                    FieldSpec("path", "string", required=True),
                    FieldSpec("timestamp", "string", required=True),
                    FieldSpec(
                        "facet",
                        "string",
                        required=True,
                        raw_schema=_NULLABLE_STRING,
                    ),
                    FieldSpec(
                        "setting",
                        "string",
                        required=True,
                        raw_schema=_NULLABLE_STRING,
                    ),
                    FieldSpec(
                        "timestamp_detection_method",
                        "string",
                        required=True,
                        description=(
                            "Timestamp detection method: deterministic, model, "
                            "upload_fallback, or explicit."
                        ),
                    ),
                    FieldSpec(
                        "timestamp_detection_model_called",
                        "boolean",
                        required=True,
                    ),
                    FieldSpec(
                        "timestamp_detection_no_match_reason",
                        "string",
                        required=True,
                        raw_schema=_NULLABLE_STRING,
                    ),
                ),
                example={
                    "path": "/journal/imports/20260618_143022/source.txt",
                    "timestamp": "20260618_143022",
                    "facet": None,
                    "setting": None,
                    "timestamp_detection_method": "deterministic",
                    "timestamp_detection_model_called": False,
                    "timestamp_detection_no_match_reason": None,
                },
            ),
            _json_error(
                400,
                ("ingest_no_files",),
                "Neither file nor text was supplied.",
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
        description="Start processing a previously saved import source.",
        request=RequestSpec(
            fields=(
                FieldSpec("path", "string", required=True),
                FieldSpec("timestamp", "string", required=True),
                FieldSpec("source", "string"),
                FieldSpec("force", "boolean"),
            ),
            example={
                "path": "/journal/imports/20260618_143022/source.txt",
                "timestamp": "20260618_143022",
                "source": "manual",
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
                ("missing_required_field",),
                "Path or timestamp was missing.",
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
                403,
                ("pl_revoked",),
                "Access gate rejected a revoked paired-link identity.",
            ),
        ),
    ),
]

__all__ = ["OPERATIONS"]
