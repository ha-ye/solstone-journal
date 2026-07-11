# SPDX-License-Identifier: AGPL-3.0-only
# Copyright (c) 2026 sol pbc

from __future__ import annotations

import hashlib
from pathlib import Path

import pytest

from solstone.think.services.spp_attest import VerificationError
from solstone.think.services.spp_attest.tlv import (
    decode_gpu_envelope,
    extract_spdm_nonce,
)

FIXTURE_DIR = Path(__file__).resolve().parents[1] / "fixtures" / "spp_attest"
GPU_ENVELOPE_SHA256 = "1475f7e95bb35ca86eca946a581964428dfc39c9f1fb5ca5e61ab018fde376d3"


def _tlv_bytes() -> bytes:
    return (FIXTURE_DIR / "gpu-envelope.tlv").read_bytes()


def _field_spans(data: bytes) -> dict[int, tuple[int, int, int]]:
    count = int.from_bytes(data[8:10], "big")
    offset = 10
    spans: dict[int, tuple[int, int, int]] = {}
    for _index in range(count):
        header_start = offset
        field_id = int.from_bytes(data[offset : offset + 2], "big")
        length = int.from_bytes(data[offset + 2 : offset + 6], "big")
        value_start = offset + 6
        value_end = value_start + length
        spans[field_id] = (header_start, value_start, value_end)
        offset = value_end
    return spans


def _replace_field(data: bytes, field_id: int, value: bytes) -> bytes:
    spans = _field_spans(data)
    header_start, value_start, value_end = spans[field_id]
    updated = bytearray(data)
    updated[header_start + 2 : header_start + 6] = len(value).to_bytes(4, "big")
    return bytes(updated[:value_start] + value + updated[value_end:])


def test_decode_gpu_envelope_extracts_nonce_structurally() -> None:
    data = _tlv_bytes()

    envelope = decode_gpu_envelope(data)

    assert [field.field_id for field in envelope.fields] == [1, 2, 3, 4, 5, 6, 7]
    assert len(envelope.nonce) == 32
    assert extract_spdm_nonce(envelope.spdm_report) == envelope.nonce
    assert hashlib.sha256(data).hexdigest() == GPU_ENVELOPE_SHA256


def test_decode_gpu_envelope_rejects_wrong_magic() -> None:
    data = bytearray(_tlv_bytes())
    data[0] ^= 0x01

    with pytest.raises(VerificationError, match="magic"):
        decode_gpu_envelope(bytes(data))


def test_decode_gpu_envelope_rejects_wrong_field_count() -> None:
    data = bytearray(_tlv_bytes())
    data[8:10] = (6).to_bytes(2, "big")

    with pytest.raises(VerificationError, match="field_count"):
        decode_gpu_envelope(bytes(data))


def test_decode_gpu_envelope_rejects_duplicate_and_missing_field_id() -> None:
    data = bytearray(_tlv_bytes())
    header_start, _value_start, _value_end = _field_spans(data)[7]
    data[header_start : header_start + 2] = (6).to_bytes(2, "big")

    with pytest.raises(VerificationError, match="duplicate.*missing"):
        decode_gpu_envelope(bytes(data))


def test_decode_gpu_envelope_rejects_out_of_order_field_id() -> None:
    data = bytearray(_tlv_bytes())
    spans = _field_spans(data)
    field3_header, _field3_start, _field3_end = spans[3]
    field4_header, _field4_start, _field4_end = spans[4]
    data[field3_header : field3_header + 2] = (4).to_bytes(2, "big")
    data[field4_header : field4_header + 2] = (3).to_bytes(2, "big")

    with pytest.raises(VerificationError, match="out of order"):
        decode_gpu_envelope(bytes(data))


def test_decode_gpu_envelope_rejects_unknown_field_id() -> None:
    data = bytearray(_tlv_bytes())
    header_start, _value_start, _value_end = _field_spans(data)[7]
    data[header_start : header_start + 2] = (8).to_bytes(2, "big")

    with pytest.raises(VerificationError, match="unknown field"):
        decode_gpu_envelope(bytes(data))


def test_decode_gpu_envelope_rejects_trailing_bytes() -> None:
    with pytest.raises(VerificationError, match="trailing"):
        decode_gpu_envelope(_tlv_bytes() + b"\x00")


def test_decode_gpu_envelope_rejects_field_length_overrun() -> None:
    data = bytearray(_tlv_bytes())
    header_start, _value_start, value_end = _field_spans(data)[7]
    length = value_end - _value_start
    data[header_start + 2 : header_start + 6] = (length + 1).to_bytes(4, "big")

    with pytest.raises(VerificationError, match="overruns"):
        decode_gpu_envelope(bytes(data))


def test_decode_gpu_envelope_rejects_short_nonce_field() -> None:
    data = _replace_field(_tlv_bytes(), 1, b"short")

    with pytest.raises(VerificationError, match="field 1 nonce"):
        decode_gpu_envelope(data)


def test_extract_spdm_nonce_rejects_bad_header() -> None:
    data = bytearray(_tlv_bytes())
    _header_start, value_start, _value_end = _field_spans(data)[2]
    data[value_start] ^= 0x01

    with pytest.raises(VerificationError, match="GET_MEASUREMENTS"):
        decode_gpu_envelope(bytes(data))


def test_extract_spdm_nonce_rejects_short_report() -> None:
    data = _replace_field(_tlv_bytes(), 2, b"\x11\xe0\x01\xff")

    with pytest.raises(VerificationError, match="too short"):
        decode_gpu_envelope(data)
