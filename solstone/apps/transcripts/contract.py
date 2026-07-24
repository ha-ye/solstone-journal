# SPDX-License-Identifier: AGPL-3.0-only
# Copyright (c) 2026 sol pbc

"""OpenAPI fragment for the native-client transcript routes."""

from __future__ import annotations

from solstone.convey.contract import (
    FieldSpec,
    OperationSpec,
    ParamSpec,
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
_INVALID_DAY = _json_error(
    404,
    ("invalid_day",),
    "The supplied day was invalid or not found.",
)
_INVALID_SEGMENT_OR_STREAM = _json_error(
    404,
    ("invalid_segment_or_stream",),
    "The supplied segment or stream was invalid.",
)
_INVALID_DAY_OR_SEGMENT = _json_error(
    404,
    ("invalid_day", "invalid_segment_or_stream"),
    "The supplied day, segment, or stream was invalid.",
)


OPERATIONS: list[OperationSpec] = [
    OperationSpec(
        operation_id="transcripts.read",
        method="GET",
        rule="/app/transcripts/api/read/<day>",
        summary="Read transcript markdown",
        description="Return transcript markdown for a day, span, segment, or range.",
        parameters=(
            ParamSpec("day", "path"),
            ParamSpec("transcripts", "query"),
            ParamSpec("percepts", "query"),
            ParamSpec("agents", "query"),
            ParamSpec("start", "query"),
            ParamSpec("end", "query"),
            ParamSpec("segment", "query"),
            ParamSpec("segments", "query"),
            ParamSpec("stream", "query"),
        ),
        responses=(
            ResponseSpec(
                status=200,
                description="Transcript markdown.",
                named_fields=(FieldSpec("markdown", "string", required=True),),
            ),
            _INVALID_DAY_OR_SEGMENT,
        ),
    ),
    OperationSpec(
        operation_id="transcripts.scan",
        method="GET",
        rule="/app/transcripts/api/day/<day>",
        summary="Read transcript day scan",
        description="Return transcript and percept ranges plus segment metadata.",
        parameters=(ParamSpec("day", "path"),),
        responses=(
            ResponseSpec(
                status=200,
                description="Transcript day scan.",
                named_fields=(
                    FieldSpec("audio", "array", required=True, raw_schema=_FREE_ARRAY),
                    FieldSpec("screen", "array", required=True, raw_schema=_FREE_ARRAY),
                    FieldSpec(
                        "segments",
                        "array",
                        required=True,
                        raw_schema=_FREE_ARRAY,
                    ),
                ),
            ),
            _INVALID_DAY,
        ),
    ),
    OperationSpec(
        operation_id="transcripts.segments",
        method="GET",
        rule="/app/transcripts/api/segments/<day>",
        summary="List transcript segments",
        description="Return segment selector rows for one day.",
        parameters=(ParamSpec("day", "path"),),
        responses=(
            ResponseSpec(
                status=200,
                description="Segment rows.",
                named_fields=(
                    FieldSpec(
                        "segments",
                        "array",
                        required=True,
                        raw_schema=_FREE_ARRAY,
                    ),
                ),
            ),
            _INVALID_DAY,
        ),
    ),
    OperationSpec(
        operation_id="transcripts.speakers",
        method="GET",
        rule="/app/transcripts/api/segment/<day>/<stream>/<segment_key>",
        summary="Read transcript segment speakers",
        description="Return segment timeline chunks and speaker-label metadata.",
        parameters=(
            ParamSpec("day", "path"),
            ParamSpec("stream", "path"),
            ParamSpec("segment_key", "path"),
        ),
        responses=(
            ResponseSpec(
                status=200,
                description="Segment timeline and speaker metadata.",
                named_fields=(
                    FieldSpec("chunks", "array", required=True, raw_schema=_FREE_ARRAY),
                    FieldSpec(
                        "speaker_labels",
                        "object",
                        required=True,
                        raw_schema=_FREE_OBJECT,
                    ),
                ),
            ),
            _INVALID_DAY_OR_SEGMENT,
        ),
    ),
    OperationSpec(
        operation_id="transcripts.stats",
        method="GET",
        rule="/app/transcripts/api/stats/<month>",
        summary="Read transcript month stats",
        description="Return day-count mapping for a transcript coverage month.",
        parameters=(ParamSpec("month", "path"),),
        responses=(
            ResponseSpec(
                status=200,
                description="Month day-count mapping.",
                free_form=True,
            ),
            _json_error(
                400,
                ("invalid_month",),
                "The supplied month was invalid.",
            ),
        ),
    ),
    OperationSpec(
        operation_id="transcripts.ranges",
        method="GET",
        rule="/app/transcripts/api/ranges/<day>",
        summary="Read transcript day ranges",
        description="Return transcript and percept ranges for one day.",
        parameters=(ParamSpec("day", "path"),),
        responses=(
            ResponseSpec(
                status=200,
                description="Day transcript and percept ranges.",
                named_fields=(
                    FieldSpec("audio", "array", required=True, raw_schema=_FREE_ARRAY),
                    FieldSpec("screen", "array", required=True, raw_schema=_FREE_ARRAY),
                ),
            ),
            _INVALID_DAY,
        ),
    ),
]

__all__ = ["OPERATIONS"]
