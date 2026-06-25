# SPDX-License-Identifier: AGPL-3.0-only
# Copyright (c) 2026 sol pbc

"""OpenAPI fragment for the native-client root routes."""

from __future__ import annotations

from solstone.convey.contract import OperationSpec, ResponseSpec

NATIVE_CHAT_EVENT_KINDS = [
    "owner_message",
    "sol_message",
    "talent_queued",
    "talent_spawned",
    "talent_finished",
    "talent_errored",
    "chat_queue_depth",
    "result",
    "chat_error",
]


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


OPERATIONS: list[OperationSpec] = [
    OperationSpec(
        operation_id="callosum.rootEvents",
        method="GET",
        rule="/sse/events",
        summary="Stream root Callosum events",
        description=(
            "Open the root Server-Sent Events feed for Callosum events. Frames: "
            "heartbeat comments `: heartbeat\\n\\n`; data frames "
            "`data: {json}\\n\\n` carrying CallosumEvent JSON for all tracts, "
            "including chat. This stream emits no `event: error` frames."
        ),
        responses=(
            ResponseSpec(
                status=200,
                description=(
                    "Callosum event stream. Heartbeat frames are comments "
                    "`: heartbeat\\n\\n`; data frames are `data: {json}\\n\\n` "
                    "and carry CallosumEvent JSON for all tracts, including chat."
                ),
                content_type="text/event-stream",
                free_form=True,
                raw_schema={"$ref": "#/components/schemas/CallosumEvent"},
                example={
                    "tract": "chat",
                    "event": "owner_message",
                    "ts": 1781803200000,
                    "message": "What changed?",
                },
                extensions={
                    "x-chat-events": {
                        "description": (
                            "CallosumEvent kinds on tract 'chat' a native client "
                            "branches on. Payloads remain open "
                            "(CallosumEvent.additionalProperties)."
                        ),
                        "kinds": NATIVE_CHAT_EVENT_KINDS,
                    }
                },
            ),
            _json_error(
                403,
                ("pl_revoked",),
                "Access gate rejected a revoked paired-link identity before stream open.",
            ),
        ),
    )
]

__all__ = ["OPERATIONS"]
