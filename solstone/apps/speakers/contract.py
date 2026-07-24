# SPDX-License-Identifier: AGPL-3.0-only
# Copyright (c) 2026 sol pbc

"""OpenAPI fragment for the native-client speakers routes."""

from __future__ import annotations

from solstone.convey.contract import (
    FieldSpec,
    OperationSpec,
    ParamSpec,
    RequestSpec,
    ResponseSpec,
)


def _error(reason_codes: tuple[str, ...]) -> ResponseSpec:
    return ResponseSpec(
        status=400, description="Speaker command rejected.", reason_codes=reason_codes
    )


_OPEN_OBJECT = {"type": "object", "additionalProperties": True}


OPERATIONS: list[OperationSpec] = [
    OperationSpec(
        operation_id="speakers.attribute-segment",
        method="POST",
        rule="/app/speakers/api/attribute-segment",
        summary="Speakers attribute-segment",
        description="Native speakers CLI route.",
        request=RequestSpec(raw_schema=_OPEN_OBJECT),
        responses=(
            ResponseSpec(
                status=200,
                description="Speaker command result.",
                free_form=True,
                raw_schema=_OPEN_OBJECT,
            ),
            _error(
                (
                    "invalid_day",
                    "invalid_segment_or_stream",
                    "missing_required_field",
                    "speaker_labels_busy",
                    "speaker_owner_centroid_required",
                    "speaker_voiceprint_busy",
                )
            ),
        ),
    ),
    OperationSpec(
        operation_id="speakers.backfill",
        method="POST",
        rule="/app/speakers/api/backfill",
        summary="Speakers backfill",
        description="Native speakers CLI route.",
        request=RequestSpec(raw_schema=_OPEN_OBJECT),
        responses=(
            ResponseSpec(
                status=200,
                description="Speaker command result.",
                free_form=True,
                raw_schema=_OPEN_OBJECT,
            ),
        ),
    ),
    OperationSpec(
        operation_id="speakers.backfill-last-seen",
        method="POST",
        rule="/app/speakers/api/backfill-last-seen",
        summary="Speakers backfill-last-seen",
        description="Native speakers CLI route.",
        request=RequestSpec(raw_schema=_OPEN_OBJECT),
        responses=(
            ResponseSpec(
                status=200,
                description="Speaker command result.",
                free_form=True,
                raw_schema=_OPEN_OBJECT,
            ),
        ),
    ),
    OperationSpec(
        operation_id="speakers.bootstrap",
        method="POST",
        rule="/app/speakers/api/bootstrap",
        summary="Speakers bootstrap",
        description="Native speakers CLI route.",
        request=RequestSpec(raw_schema=_OPEN_OBJECT),
        responses=(
            ResponseSpec(
                status=200,
                description="Speaker command result.",
                free_form=True,
                raw_schema=_OPEN_OBJECT,
            ),
            _error(("speaker_owner_centroid_required",)),
        ),
    ),
    OperationSpec(
        operation_id="speakers.build-from-tags",
        method="POST",
        rule="/app/speakers/api/owner/build-from-tags",
        summary="Speakers build-from-tags",
        description="Native speakers CLI route.",
        request=RequestSpec(raw_schema=_OPEN_OBJECT),
        responses=(
            ResponseSpec(
                status=200,
                description="Speaker command result.",
                free_form=True,
                raw_schema=_OPEN_OBJECT,
            ),
            _error(("entity_not_found", "speaker_voiceprint_busy")),
        ),
    ),
    OperationSpec(
        operation_id="speakers.confirm-owner",
        method="POST",
        rule="/app/speakers/api/owner/confirm-cli",
        summary="Speakers confirm-owner",
        description="Native speakers CLI route.",
        request=RequestSpec(raw_schema=_OPEN_OBJECT),
        responses=(
            ResponseSpec(
                status=200,
                description="Speaker command result.",
                free_form=True,
                raw_schema=_OPEN_OBJECT,
            ),
            _error(("speaker_command_failed", "speaker_voiceprint_busy")),
        ),
    ),
    OperationSpec(
        operation_id="speakers.correct",
        method="POST",
        rule="/app/speakers/api/correct-attribution",
        summary="Speakers correct",
        description="Native speakers CLI route.",
        request=RequestSpec(raw_schema=_OPEN_OBJECT),
        responses=(
            ResponseSpec(
                status=200,
                description="Speaker command result.",
                free_form=True,
                raw_schema=_OPEN_OBJECT,
            ),
            _error(
                (
                    "entity_blocked",
                    "invalid_day",
                    "invalid_segment_or_stream",
                    "missing_request_body",
                    "missing_required_field",
                    "speaker_labels_busy",
                    "speaker_not_found",
                    "speaker_owner_voice_too_close",
                    "speaker_review_unavailable",
                    "speaker_sentence_missing",
                    "speaker_voiceprint_busy",
                )
            ),
        ),
    ),
    OperationSpec(
        operation_id="speakers.day-segments",
        method="GET",
        rule="/app/speakers/api/segments-cli/{day}",
        summary="Speakers day-segments",
        description="Native speakers CLI route.",
        parameters=(
            ParamSpec("day", "path", required=True),
            ParamSpec("limit", "query", "integer"),
        ),
        responses=(
            ResponseSpec(
                status=200,
                description="Speaker command result.",
                free_form=True,
                raw_schema=_OPEN_OBJECT,
            ),
            _error(("invalid_day", "invalid_request_value")),
        ),
    ),
    OperationSpec(
        operation_id="speakers.detect",
        method="POST",
        rule="/app/speakers/api/owner/detect",
        summary="Speakers detect",
        description="Native speakers CLI route.",
        request=RequestSpec(raw_schema=_OPEN_OBJECT),
        responses=(
            ResponseSpec(
                status=200,
                description="Speaker command result.",
                free_form=True,
                raw_schema=_OPEN_OBJECT,
            ),
            _error(("speaker_voiceprint_busy",)),
        ),
    ),
    OperationSpec(
        operation_id="speakers.discover",
        method="POST",
        rule="/app/speakers/api/discovery/scan",
        summary="Speakers discover",
        description="Native speakers CLI route.",
        request=RequestSpec(raw_schema=_OPEN_OBJECT),
        responses=(
            ResponseSpec(
                status=200,
                description="Speaker command result.",
                free_form=True,
                raw_schema=_OPEN_OBJECT,
            ),
        ),
    ),
    OperationSpec(
        operation_id="speakers.dismiss-cluster",
        method="POST",
        rule="/app/speakers/api/discovery/dismiss",
        summary="Speakers dismiss-cluster",
        description="Native speakers CLI route.",
        request=RequestSpec(raw_schema=_OPEN_OBJECT),
        responses=(
            ResponseSpec(
                status=200,
                description="Speaker command result.",
                free_form=True,
                raw_schema=_OPEN_OBJECT,
            ),
            _error(
                (
                    "invalid_request_value",
                    "missing_required_field",
                    "speaker_command_failed",
                    "speaker_review_unavailable",
                )
            ),
        ),
    ),
    OperationSpec(
        operation_id="speakers.dismissals",
        method="GET",
        rule="/app/speakers/api/discovery/dismissals",
        summary="Speakers dismissals",
        description="Native speakers CLI route.",
        responses=(
            ResponseSpec(
                status=200,
                description="Speaker command result.",
                free_form=True,
                raw_schema=_OPEN_OBJECT,
            ),
        ),
    ),
    OperationSpec(
        operation_id="speakers.identify",
        method="POST",
        rule="/app/speakers/api/discovery/identify-cli",
        summary="Speakers identify",
        description="Native speakers CLI route.",
        request=RequestSpec(raw_schema=_OPEN_OBJECT),
        responses=(
            ResponseSpec(
                status=200,
                description="Speaker command result.",
                free_form=True,
                raw_schema=_OPEN_OBJECT,
            ),
            _error(
                (
                    "invalid_entity_type",
                    "invalid_request_value",
                    "missing_required_field",
                    "speaker_command_failed",
                    "speaker_identify_conflict",
                    "speaker_identify_operation_not_found",
                    "speaker_identify_recoverable",
                    "speaker_identify_repair_required",
                    "speaker_labels_busy",
                    "speaker_not_found",
                    "speaker_voiceprint_busy",
                )
            ),
        ),
    ),
    OperationSpec(
        operation_id="speakers.identify-operation",
        method="GET",
        rule="/app/speakers/api/discovery/identify/operations/{operation_id}",
        summary="Speakers identify-operation",
        description="Native speakers CLI route.",
        parameters=(ParamSpec("operation_id", "path", required=True),),
        responses=(
            ResponseSpec(
                status=200,
                description="Speaker command result.",
                free_form=True,
                raw_schema=_OPEN_OBJECT,
            ),
            _error(("speaker_identify_operation_not_found",)),
        ),
    ),
    OperationSpec(
        operation_id="speakers.identify-operations",
        method="GET",
        rule="/app/speakers/api/discovery/identify/operations",
        summary="Speakers identify-operations",
        description="Native speakers CLI route.",
        responses=(
            ResponseSpec(
                status=200,
                description="Speaker command result.",
                free_form=True,
                raw_schema=_OPEN_OBJECT,
            ),
        ),
    ),
    OperationSpec(
        operation_id="speakers.identify-undo",
        method="POST",
        rule="/app/speakers/api/discovery/identify/undo",
        summary="Speakers identify-undo",
        description="Native speakers CLI route.",
        request=RequestSpec(raw_schema=_OPEN_OBJECT),
        responses=(
            ResponseSpec(
                status=200,
                description="Speaker command result.",
                free_form=True,
                raw_schema=_OPEN_OBJECT,
            ),
            _error(
                (
                    "invalid_entity_type",
                    "invalid_request_value",
                    "missing_required_field",
                    "speaker_command_failed",
                    "speaker_identify_conflict",
                    "speaker_identify_operation_not_found",
                    "speaker_identify_recoverable",
                    "speaker_identify_repair_required",
                    "speaker_labels_busy",
                    "speaker_not_found",
                    "speaker_voiceprint_busy",
                )
            ),
        ),
    ),
    OperationSpec(
        operation_id="speakers.keep-separate-list",
        method="GET",
        rule="/app/speakers/api/name-variants/keep-separate",
        summary="Speakers keep-separate-list",
        description="Native speakers CLI route.",
        responses=(
            ResponseSpec(
                status=200,
                description="Speaker command result.",
                free_form=True,
                raw_schema=_OPEN_OBJECT,
            ),
        ),
    ),
    OperationSpec(
        operation_id="speakers.link-import",
        method="POST",
        rule="/app/speakers/api/link-import",
        summary="Speakers link-import",
        description="Native speakers CLI route.",
        request=RequestSpec(raw_schema=_OPEN_OBJECT),
        responses=(
            ResponseSpec(
                status=200,
                description="Speaker command result.",
                free_form=True,
                raw_schema=_OPEN_OBJECT,
            ),
            _error(("speaker_command_failed",)),
        ),
    ),
    OperationSpec(
        operation_id="speakers.merge-names",
        method="POST",
        rule="/app/speakers/api/merge-names",
        summary="Speakers merge-names",
        description="Native speakers CLI route.",
        request=RequestSpec(raw_schema=_OPEN_OBJECT),
        responses=(
            ResponseSpec(
                status=200,
                description="Speaker command result.",
                free_form=True,
                raw_schema=_OPEN_OBJECT,
            ),
            _error(("speaker_command_failed",)),
        ),
    ),
    OperationSpec(
        operation_id="speakers.owner-ready",
        method="POST",
        rule="/app/speakers/api/owner/ready",
        summary="Speakers owner-ready",
        description="Native speakers CLI route.",
        request=RequestSpec(raw_schema=_OPEN_OBJECT),
        responses=(
            ResponseSpec(
                status=200,
                description="Speaker command result.",
                free_form=True,
                raw_schema=_OPEN_OBJECT,
            ),
        ),
    ),
    OperationSpec(
        operation_id="speakers.presence",
        method="GET",
        rule="/app/speakers/api/discovery/cluster/{cluster_id}/presence",
        summary="Speakers presence",
        description="Native speakers CLI route.",
        parameters=(ParamSpec("cluster_id", "path", required=True),),
        responses=(
            ResponseSpec(
                status=200,
                description="Speaker command result.",
                free_form=True,
                raw_schema=_OPEN_OBJECT,
            ),
            _error(("speaker_review_unavailable",)),
        ),
    ),
    OperationSpec(
        operation_id="speakers.propagate-correction",
        method="POST",
        rule="/app/speakers/api/propagate-correction",
        summary="Speakers propagate-correction",
        description="Native speakers CLI route.",
        request=RequestSpec(raw_schema=_OPEN_OBJECT),
        responses=(
            ResponseSpec(
                status=200,
                description="Speaker command result.",
                free_form=True,
                raw_schema=_OPEN_OBJECT,
            ),
            _error(
                (
                    "entity_blocked",
                    "invalid_request_value",
                    "missing_required_field",
                    "speaker_labels_busy",
                    "speaker_not_found",
                    "speaker_voiceprint_busy",
                )
            ),
        ),
    ),
    OperationSpec(
        operation_id="speakers.rebuild-owner",
        method="POST",
        rule="/app/speakers/api/owner/rebuild",
        summary="Speakers rebuild-owner",
        description="Native speakers CLI route.",
        request=RequestSpec(raw_schema=_OPEN_OBJECT),
        responses=(
            ResponseSpec(
                status=200,
                description="Speaker command result.",
                free_form=True,
                raw_schema=_OPEN_OBJECT,
            ),
            _error(("speaker_voiceprint_busy",)),
        ),
    ),
    OperationSpec(
        operation_id="speakers.reject-owner",
        method="POST",
        rule="/app/speakers/api/owner/reject-cli",
        summary="Speakers reject-owner",
        description="Native speakers CLI route.",
        request=RequestSpec(raw_schema=_OPEN_OBJECT),
        responses=(
            ResponseSpec(
                status=200,
                description="Speaker command result.",
                free_form=True,
                raw_schema=_OPEN_OBJECT,
            ),
            _error(("speaker_voiceprint_busy",)),
        ),
    ),
    OperationSpec(
        operation_id="speakers.resolve-names",
        method="POST",
        rule="/app/speakers/api/resolve-names",
        summary="Speakers resolve-names",
        description="Native speakers CLI route.",
        request=RequestSpec(raw_schema=_OPEN_OBJECT),
        responses=(
            ResponseSpec(
                status=200,
                description="Speaker command result.",
                free_form=True,
                raw_schema=_OPEN_OBJECT,
            ),
        ),
    ),
    OperationSpec(
        operation_id="speakers.seed-from-imports",
        method="POST",
        rule="/app/speakers/api/seed-from-imports",
        summary="Speakers seed-from-imports",
        description="Native speakers CLI route.",
        request=RequestSpec(raw_schema=_OPEN_OBJECT),
        responses=(
            ResponseSpec(
                status=200,
                description="Speaker command result.",
                free_form=True,
                raw_schema=_OPEN_OBJECT,
            ),
            _error(("speaker_owner_centroid_required",)),
        ),
    ),
    OperationSpec(
        operation_id="speakers.sentences",
        method="GET",
        rule="/app/speakers/api/review-cli/{day}/{stream}/{segment_key}/{source}",
        summary="Speakers sentences",
        description="Native speakers CLI route.",
        parameters=(
            ParamSpec("day", "path", required=True),
            ParamSpec("stream", "path", required=True),
            ParamSpec("segment_key", "path", required=True),
            ParamSpec("source", "path", required=True),
        ),
        responses=(
            ResponseSpec(
                status=200,
                description="Speaker command result.",
                free_form=True,
                raw_schema=_OPEN_OBJECT,
            ),
            _error(
                (
                    "invalid_day",
                    "invalid_segment_or_stream",
                    "speaker_review_unavailable",
                )
            ),
        ),
    ),
    OperationSpec(
        operation_id="speakers.status",
        method="GET",
        rule="/app/speakers/api/status",
        summary="Speakers status",
        description="Native speakers CLI route.",
        responses=(
            ResponseSpec(
                status=200,
                description="Speaker command result.",
                free_form=True,
                raw_schema=_OPEN_OBJECT,
            ),
        ),
    ),
    OperationSpec(
        operation_id="speakers.suggest",
        method="GET",
        rule="/app/speakers/api/suggest",
        summary="Speakers suggest",
        description="Native speakers CLI route.",
        parameters=(ParamSpec("limit", "query", "integer"),),
        responses=(
            ResponseSpec(
                status=200,
                description="Speaker command result.",
                free_form=True,
                raw_schema=_OPEN_OBJECT,
            ),
            _error(("invalid_request_value",)),
        ),
    ),
    OperationSpec(
        operation_id="speakers.tag-owner",
        method="POST",
        rule="/app/speakers/api/owner/tag-cli",
        summary="Speakers tag-owner",
        description="Native speakers CLI route.",
        request=RequestSpec(raw_schema=_OPEN_OBJECT),
        responses=(
            ResponseSpec(
                status=200,
                description="Speaker command result.",
                free_form=True,
                raw_schema=_OPEN_OBJECT,
            ),
            _error(
                (
                    "entity_blocked",
                    "invalid_day",
                    "invalid_request_value",
                    "invalid_segment_or_stream",
                    "missing_request_body",
                    "missing_required_field",
                    "speaker_attribution_state_invalid",
                    "speaker_labels_busy",
                    "speaker_not_found",
                    "speaker_owner_identity_required",
                    "speaker_owner_voice_too_close",
                    "speaker_review_unavailable",
                    "speaker_sentence_missing",
                    "speaker_voiceprint_busy",
                )
            ),
        ),
    ),
    OperationSpec(
        operation_id="speakers.wipe",
        method="POST",
        rule="/app/speakers/api/wipe",
        summary="Speakers wipe",
        description="Native speakers CLI route.",
        request=RequestSpec(raw_schema=_OPEN_OBJECT),
        responses=(
            ResponseSpec(
                status=200,
                description="Speaker command result.",
                free_form=True,
                raw_schema=_OPEN_OBJECT,
            ),
        ),
    ),
]
