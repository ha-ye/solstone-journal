# SPDX-License-Identifier: AGPL-3.0-only
# Copyright (c) 2026 sol pbc

"""OpenAPI fragment for the native-client chat routes."""

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


_NULLABLE_OPEN_OBJECT = {
    "type": ["object", "null"],
    "additionalProperties": True,
}


OPERATIONS: list[OperationSpec] = [
    OperationSpec(
        operation_id="chat.postMessage",
        method="POST",
        rule="/api/chat",
        summary="Post chat message",
        description="Accept an owner chat message and enqueue or start the chat turn.",
        request=RequestSpec(
            fields=(
                FieldSpec("message", "string", required=True),
                FieldSpec("app", "string"),
                FieldSpec("path", "string"),
                FieldSpec("facet", "string"),
                FieldSpec("source", "object"),
            ),
            example={
                "message": "What changed this morning?",
                "app": "today",
                "path": "/app/today",
                "facet": "work",
                "source": {"kind": "native"},
            },
        ),
        responses=(
            ResponseSpec(
                status=200,
                description="Chat message accepted.",
                named_fields=(
                    FieldSpec("use_id", "string", required=True),
                    FieldSpec("queued", "boolean", required=True),
                ),
                example={"use_id": "1781803200000", "queued": False},
            ),
            _json_error(
                400,
                ("missing_required_field",),
                "Message text was missing.",
            ),
            _json_error(
                429,
                ("chat_queue_full",),
                "Chat queue was full.",
            ),
            _json_error(
                503,
                ("agent_unavailable",),
                "Agent service was unavailable.",
            ),
            _json_error(
                403,
                ("pl_revoked",),
                "Access gate rejected a revoked paired-link identity.",
            ),
        ),
    ),
    OperationSpec(
        operation_id="chat.session",
        method="GET",
        rule="/api/chat/session",
        summary="Get chat session",
        description="Return the reduced state for today's chat stream.",
        responses=(
            ResponseSpec(
                status=200,
                description=(
                    "Current chat session state. Nested message and talent payloads "
                    "remain open."
                ),
                named_fields=(
                    FieldSpec(
                        "latest_sol_message",
                        "object",
                        required=True,
                        raw_schema=_NULLABLE_OPEN_OBJECT,
                    ),
                    FieldSpec("active_talents", "array", required=True),
                    FieldSpec("queued_talents", "array", required=True),
                    FieldSpec("completed_talents", "array", required=True),
                    FieldSpec("errored_talents", "array", required=True),
                    FieldSpec(
                        "chat_error",
                        "object",
                        required=True,
                        raw_schema=_NULLABLE_OPEN_OBJECT,
                    ),
                    FieldSpec("queue_depth", "integer", required=True),
                ),
                example={
                    "latest_sol_message": None,
                    "active_talents": [],
                    "queued_talents": [],
                    "completed_talents": [],
                    "errored_talents": [],
                    "chat_error": None,
                    "queue_depth": 0,
                },
            ),
            _json_error(
                403,
                ("pl_revoked",),
                "Access gate rejected a revoked paired-link identity.",
            ),
        ),
    ),
    OperationSpec(
        operation_id="chat.declineOffer",
        method="POST",
        rule="/api/chat/offer/decline",
        summary="Decline support offer",
        description="Record that the owner declined the pending support offer.",
        responses=(
            ResponseSpec(
                status=200,
                description="Support offer declined.",
                named_fields=(FieldSpec("ok", "boolean", required=True),),
                example={"ok": True},
            ),
            _json_error(
                403,
                ("pl_revoked",),
                "Access gate rejected a revoked paired-link identity.",
            ),
        ),
    ),
    OperationSpec(
        operation_id="chat.supportDraftConfirm",
        method="POST",
        rule="/api/chat/support/draft/confirm",
        summary="Confirm support draft",
        description="Submit a captured support draft.",
        request=RequestSpec(
            fields=(FieldSpec("draft_id", "string", required=True),),
            example={"draft_id": "draft-1781803200000"},
        ),
        responses=(
            ResponseSpec(
                status=200,
                description="Support draft confirmation result.",
                named_fields=(
                    FieldSpec("ok", "boolean", required=True),
                    FieldSpec("outcome", "string", required=True),
                    FieldSpec("ticket_id", "object", raw_schema={}),
                ),
                example={"ok": True, "outcome": "submitted", "ticket_id": "12345"},
            ),
            _json_error(
                400,
                ("missing_required_field",),
                "Draft identifier was missing.",
            ),
            _json_error(
                403,
                ("pl_revoked",),
                "Access gate rejected a revoked paired-link identity.",
            ),
        ),
    ),
    OperationSpec(
        operation_id="chat.supportDraftCancel",
        method="POST",
        rule="/api/chat/support/draft/cancel",
        summary="Cancel support draft",
        description="Cancel a captured support draft without submitting it.",
        request=RequestSpec(
            fields=(FieldSpec("draft_id", "string", required=True),),
            example={"draft_id": "draft-1781803200000"},
        ),
        responses=(
            ResponseSpec(
                status=200,
                description="Support draft cancellation result.",
                named_fields=(
                    FieldSpec("ok", "boolean", required=True),
                    FieldSpec("outcome", "string", required=True),
                ),
                example={"ok": True, "outcome": "cancelled"},
            ),
            _json_error(
                400,
                ("missing_required_field",),
                "Draft identifier was missing.",
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
