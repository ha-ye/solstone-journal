# SPDX-License-Identifier: AGPL-3.0-only
# Copyright (c) 2026 sol pbc

"""OpenAPI fragment for the native-client link routes."""

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


_LOCAL_ENDPOINT_SCHEMA = {
    "type": "array",
    "items": {
        "type": "object",
        "additionalProperties": True,
        "properties": {
            "ip": {"type": "string"},
            "port": {"type": "integer"},
            "scope": {"type": "string"},
        },
        "required": ["ip", "port", "scope"],
    },
}

OPERATIONS: list[OperationSpec] = [
    OperationSpec(
        operation_id="link.pairStart",
        method="POST",
        rule="/app/link/pair-start",
        summary="Start link pairing",
        description=(
            "Create a short-lived pairing nonce and return the link payload a "
            "native client can scan or open."
        ),
        request=RequestSpec(
            fields=(
                FieldSpec("device_label", "string"),
                FieldSpec("role", "string"),
            ),
            example={"device_label": "Jer iPhone", "role": "phone"},
        ),
        responses=(
            ResponseSpec(
                status=200,
                description="Pairing nonce and link payload.",
                named_fields=(
                    FieldSpec("nonce", "string", required=True),
                    FieldSpec("pair_link", "string", required=True),
                    FieldSpec("expires_in", "integer", required=True),
                    FieldSpec("rotating", "boolean", required=True),
                    FieldSpec("device_label", "string", required=True),
                    FieldSpec("ca_fingerprint", "string", required=True),
                ),
                example={
                    "nonce": "5f0d8c8b9f1e48b0a5f80b98f3d5e9b0",
                    "pair_link": "https://solstone.link/pair#0ABCD...",
                    "expires_in": 300,
                    "rotating": False,
                    "device_label": "Jer iPhone",
                    "ca_fingerprint": "9c5f2e0c8e6a42f0a32e55e5cf7f5b4a",
                },
            ),
            _json_error(
                400,
                ("invalid_operation_for_state", "pairing_request_invalid"),
                "Pair-start request rejected by handler validation.",
            ),
            _json_error(
                403,
                ("pl_revoked",),
                "Access gate rejected a revoked paired-link identity.",
            ),
        ),
    ),
    OperationSpec(
        operation_id="link.pair",
        method="POST",
        rule="/app/link/pair",
        summary="Complete link pairing",
        description=(
            "Accept a client CSR plus nonce, then return a signed certificate "
            "and home attestation."
        ),
        parameters=(
            ParamSpec(
                "token",
                "query",
                required=False,
                description="Pairing nonce; the body nonce can be used instead.",
            ),
        ),
        request=RequestSpec(
            fields=(
                FieldSpec("csr", "string", required=True),
                FieldSpec("nonce", "string"),
                FieldSpec("device_label", "string"),
                FieldSpec("sender_instance_id", "string"),
            ),
            example={
                "csr": "-----BEGIN CERTIFICATE REQUEST-----\n...\n-----END CERTIFICATE REQUEST-----\n",
                "nonce": "5f0d8c8b9f1e48b0a5f80b98f3d5e9b0",
                "device_label": "Jer iPhone",
                "sender_instance_id": "ios-01",
            },
        ),
        responses=(
            ResponseSpec(
                status=200,
                description="Signed link material for the paired client.",
                named_fields=(
                    FieldSpec("client_cert", "string", required=True),
                    FieldSpec(
                        "ca_chain",
                        "array",
                        required=True,
                        item_type="string",
                    ),
                    FieldSpec("instance_id", "string", required=True),
                    FieldSpec("home_label", "string", required=True),
                    FieldSpec("home_attestation", "string", required=True),
                    FieldSpec("fingerprint", "string", required=True),
                    FieldSpec(
                        "local_endpoints", "array", raw_schema=_LOCAL_ENDPOINT_SCHEMA
                    ),
                ),
                example={
                    "client_cert": "-----BEGIN CERTIFICATE-----\n...\n-----END CERTIFICATE-----\n",
                    "ca_chain": [
                        "-----BEGIN CERTIFICATE-----\n...\n-----END CERTIFICATE-----\n"
                    ],
                    "instance_id": "4d1f3d57-4f39-4930-b8f8-5e6f2a84d51a",
                    "home_label": "home",
                    "home_attestation": "eyJhbGciOi...",
                    "fingerprint": "sha256:abc123",
                    "local_endpoints": [
                        {"ip": "192.168.1.10", "port": 7657, "scope": "lan"}
                    ],
                },
            ),
            _json_error(
                400,
                (
                    "missing_required_field",
                    "pairing_key_invalid",
                    "pairing_request_invalid",
                ),
                "Pair request rejected by handler validation.",
            ),
            _json_error(
                403,
                ("pl_revoked",),
                "Access gate rejected a revoked paired-link identity.",
            ),
            _json_error(
                410,
                ("operation_no_longer_available",),
                "Nonce expired or was already used.",
            ),
        ),
    ),
    OperationSpec(
        operation_id="link.unpair",
        method="POST",
        rule="/app/link/unpair",
        summary="Unpair a device",
        description="Remove a paired client by fingerprint or device label.",
        request=RequestSpec(
            fields=(
                FieldSpec("fingerprint", "string"),
                FieldSpec("device_label", "string"),
            ),
            example={"fingerprint": "sha256:abc123"},
        ),
        responses=(
            ResponseSpec(
                status=200,
                description="The revoked fingerprint.",
                named_fields=(FieldSpec("unpaired", "string", required=True),),
                example={"unpaired": "sha256:abc123"},
            ),
            _json_error(
                400,
                ("missing_required_field",),
                "Neither fingerprint nor device label was supplied.",
            ),
            _json_error(
                403,
                ("pl_revoked",),
                "Access gate rejected a revoked paired-link identity.",
            ),
            _json_error(
                404,
                ("paired_device_not_found",),
                "No paired device matched the request.",
            ),
        ),
    ),
    OperationSpec(
        operation_id="link.localEndpoints",
        method="GET",
        rule="/app/link/local-endpoints",
        summary="List local link endpoints",
        description="Return loopback-only LAN endpoint hints for link clients.",
        responses=(
            ResponseSpec(
                status=200,
                description="Current local endpoint advertisement.",
                named_fields=(
                    FieldSpec("v", "integer", required=True),
                    FieldSpec(
                        "endpoints",
                        "array",
                        required=True,
                        raw_schema=_LOCAL_ENDPOINT_SCHEMA,
                    ),
                    FieldSpec("ttl_s", "integer", required=True),
                    FieldSpec("generated_at", "string", required=True),
                ),
                example={
                    "v": 1,
                    "endpoints": [{"ip": "192.168.1.10", "port": 7657, "scope": "lan"}],
                    "ttl_s": 3600,
                    "generated_at": "2026-06-18T12:00:00Z",
                },
            ),
            _json_error(
                403,
                ("pl_revoked",),
                "Access gate rejected a revoked paired-link identity.",
            ),
            ResponseSpec(
                status=404,
                description="Non-loopback request; bare Flask abort, no reason body.",
            ),
        ),
    ),
    OperationSpec(
        operation_id="link.status",
        method="GET",
        rule="/app/link/api/status",
        summary="Read link status",
        description="Return the current link service posture and reachability view.",
        responses=(
            ResponseSpec(
                status=200,
                description="Link status snapshot.",
                named_fields=(
                    FieldSpec(
                        "instance_id",
                        "string",
                        required=True,
                        raw_schema={"type": ["string", "null"]},
                    ),
                    FieldSpec(
                        "home_label",
                        "string",
                        required=True,
                        raw_schema={"type": ["string", "null"]},
                    ),
                    FieldSpec("enrolled", "boolean", required=True),
                    FieldSpec("relay_url", "string", required=True),
                    FieldSpec(
                        "ca_fingerprint",
                        "string",
                        required=True,
                        raw_schema={"type": ["string", "null"]},
                    ),
                    FieldSpec("lan_accessible", "boolean", required=True),
                    FieldSpec("posture", "string", required=True),
                    FieldSpec("reachability", "string", required=True),
                    FieldSpec("relay_state", "string", required=True),
                    FieldSpec(
                        "home_address",
                        "string",
                        required=True,
                        raw_schema={"type": ["string", "null"]},
                    ),
                    FieldSpec(
                        "vpn",
                        "object",
                        required=True,
                        raw_schema={
                            "type": "object",
                            "additionalProperties": True,
                            "properties": {
                                "active": {"type": ["string", "null"]},
                                "candidates": {
                                    "type": "array",
                                    "items": {"type": "object"},
                                },
                            },
                        },
                    ),
                ),
                example={
                    "instance_id": "4d1f3d57-4f39-4930-b8f8-5e6f2a84d51a",
                    "home_label": "home",
                    "enrolled": True,
                    "relay_url": "https://relay.solstone.local",
                    "ca_fingerprint": "9c5f2e0c8e6a42f0a32e55e5cf7f5b4a",
                    "lan_accessible": True,
                    "posture": "lan",
                    "reachability": "local",
                    "relay_state": "not_configured",
                    "home_address": None,
                    "vpn": {"active": None, "candidates": []},
                },
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
