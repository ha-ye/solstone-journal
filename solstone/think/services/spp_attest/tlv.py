# SPDX-License-Identifier: AGPL-3.0-only
# Copyright (c) 2026 sol pbc

"""Decode SPP GPU TLV envelopes."""

from __future__ import annotations

from dataclasses import dataclass

from solstone.think.services.spp_attest.errors import VerificationError

GPU_ENVELOPE_MAGIC = b"SPPGPU1\x00"
GPU_ENVELOPE_FIELD_COUNT = 7
GPU_ENVELOPE_FIELD_IDS = tuple(range(1, GPU_ENVELOPE_FIELD_COUNT + 1))
SPDM_GET_MEASUREMENTS_HEADER = bytes.fromhex("11e001ff")
SPDM_NONCE_OFFSET = 4
SPDM_NONCE_SIZE = 32


@dataclass(frozen=True, slots=True)
class GpuEnvelopeField:
    field_id: int
    value: bytes


@dataclass(frozen=True, slots=True)
class GpuEnvelope:
    fields: tuple[GpuEnvelopeField, ...]
    nonce: bytes
    spdm_report: bytes

    def field(self, field_id: int) -> bytes:
        for item in self.fields:
            if item.field_id == field_id:
                return item.value
        raise KeyError(field_id)


def decode_gpu_envelope(data: bytes) -> GpuEnvelope:
    """Decode a SPPGPU1 TLV envelope and validate its fixed field set."""

    if len(data) < len(GPU_ENVELOPE_MAGIC) + 2:
        raise VerificationError("GPU envelope is too short for SPPGPU1 header")
    if data[: len(GPU_ENVELOPE_MAGIC)] != GPU_ENVELOPE_MAGIC:
        raise VerificationError("GPU envelope magic mismatch")

    field_count_offset = len(GPU_ENVELOPE_MAGIC)
    field_count = int.from_bytes(
        data[field_count_offset : field_count_offset + 2], "big"
    )
    if field_count != GPU_ENVELOPE_FIELD_COUNT:
        raise VerificationError(
            f"GPU envelope field_count={field_count}, expected {GPU_ENVELOPE_FIELD_COUNT}"
        )

    fields: list[GpuEnvelopeField] = []
    seen: set[int] = set()
    field_ids: list[int] = []
    offset = field_count_offset + 2
    last_field_id = 0
    for index in range(field_count):
        if offset + 6 > len(data):
            raise VerificationError(
                f"GPU envelope field {index + 1} header is truncated"
            )
        field_id = int.from_bytes(data[offset : offset + 2], "big")
        length = int.from_bytes(data[offset + 2 : offset + 6], "big")
        offset += 6

        if field_id < last_field_id:
            raise VerificationError(
                f"GPU envelope field id {field_id} is out of order after {last_field_id}"
            )

        end = offset + length
        if end > len(data):
            raise VerificationError(
                f"GPU envelope field {field_id} length overruns buffer"
            )
        fields.append(GpuEnvelopeField(field_id=field_id, value=data[offset:end]))
        seen.add(field_id)
        field_ids.append(field_id)
        last_field_id = field_id
        offset = end

    if offset != len(data):
        raise VerificationError("GPU envelope has trailing bytes")

    unknown = sorted(
        field_id for field_id in seen if field_id not in GPU_ENVELOPE_FIELD_IDS
    )
    if unknown:
        raise VerificationError(
            "GPU envelope unknown field id(s): "
            + ", ".join(str(item) for item in unknown)
        )

    duplicates = sorted(field_id for field_id in seen if field_ids.count(field_id) > 1)
    missing = sorted(set(GPU_ENVELOPE_FIELD_IDS) - seen)
    if duplicates or missing:
        parts: list[str] = []
        if duplicates:
            parts.append(
                "duplicate field id(s): " + ", ".join(str(item) for item in duplicates)
            )
        if missing:
            parts.append(
                "missing field id(s): " + ", ".join(str(item) for item in missing)
            )
        raise VerificationError("GPU envelope " + "; ".join(parts))

    by_id = {field.field_id: field.value for field in fields}
    nonce = by_id[1]
    if len(nonce) != SPDM_NONCE_SIZE:
        raise VerificationError(
            f"GPU envelope field 1 nonce is {len(nonce)} bytes, expected 32"
        )

    spdm_report = by_id[2]
    extract_spdm_nonce(spdm_report)
    return GpuEnvelope(fields=tuple(fields), nonce=nonce, spdm_report=spdm_report)


def extract_spdm_nonce(spdm_report: bytes) -> bytes:
    """Return the SPDM GET_MEASUREMENTS nonce at its structural offset."""

    nonce_end = SPDM_NONCE_OFFSET + SPDM_NONCE_SIZE
    if len(spdm_report) < nonce_end:
        raise VerificationError(
            "SPDM report is too short to carry a GET_MEASUREMENTS nonce"
        )
    if spdm_report[: len(SPDM_GET_MEASUREMENTS_HEADER)] != SPDM_GET_MEASUREMENTS_HEADER:
        raise VerificationError(
            "SPDM report does not start with GET_MEASUREMENTS header"
        )
    return spdm_report[SPDM_NONCE_OFFSET:nonce_end]
