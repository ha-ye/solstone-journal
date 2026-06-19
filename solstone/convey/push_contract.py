# SPDX-License-Identifier: AGPL-3.0-only
# Copyright (c) 2026 sol pbc

"""OpenAPI fragment for the native-client push routes."""

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


OPERATIONS: list[OperationSpec] = [
    OperationSpec(
        operation_id="push.register",
        method="POST",
        rule="/api/push/register",
        summary="Register push device",
        description=(
            "Register or replace an iOS push token for the paired device "
            "fingerprint on the current connection."
        ),
        request=RequestSpec(
            fields=(
                FieldSpec("device_token", "string", required=True),
                FieldSpec("bundle_id", "string", required=True),
                FieldSpec("environment", "string", required=True),
                FieldSpec("platform", "string", required=True),
            ),
            example={
                "device_token": "abcdef0123456789",
                "bundle_id": "org.solpbc.solstone-swift",
                "environment": "development",
                "platform": "ios",
            },
        ),
        responses=(
            ResponseSpec(
                status=200,
                description="Push token registered.",
                named_fields=(
                    FieldSpec("registered", "boolean", required=True),
                    FieldSpec("device_count", "integer", required=True),
                ),
                example={"registered": True, "device_count": 1},
            ),
            _json_error(
                400,
                ("invalid_json_request", "push_request_invalid"),
                "Push registration request failed validation.",
            ),
            _json_error(
                403,
                ("pl_revoked",),
                "Access gate rejected a revoked paired-link identity.",
            ),
        ),
    ),
    OperationSpec(
        operation_id="push.unregister",
        method="DELETE",
        rule="/api/push/register",
        summary="Unregister push device",
        description="Remove the push token for the current paired device fingerprint.",
        responses=(
            ResponseSpec(
                status=200,
                description="Push token removal result.",
                named_fields=(
                    FieldSpec("removed", "boolean", required=True),
                    FieldSpec("device_count", "integer", required=True),
                ),
                example={"removed": True, "device_count": 0},
            ),
            _json_error(
                400,
                ("push_request_invalid",),
                "No paired device fingerprint was available on the connection.",
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
