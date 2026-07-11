# SPDX-License-Identifier: AGPL-3.0-only
# Copyright (c) 2026 sol pbc

from __future__ import annotations

import hashlib
from pathlib import Path

import pytest

from solstone.think.services.spp_attest import VerificationError
from solstone.think.services.spp_attest.binding import (
    check_envelope_nonce,
    composite_binding_hash,
)
from solstone.think.services.spp_attest.tlv import decode_gpu_envelope

FIXTURE_DIR = Path(__file__).resolve().parents[1] / "fixtures" / "spp_attest"
BINDING_HEX = "268901922d7b8444139f3d3e3edfcc3dd860491e313b243d94fb97ba5b312ea2"
GPU_ENVELOPE_SHA256 = "1475f7e95bb35ca86eca946a581964428dfc39c9f1fb5ca5e61ab018fde376d3"


def _nonce() -> bytes:
    return bytes.fromhex("".join((FIXTURE_DIR / "nonce.hex").read_text().split()))


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


def test_composite_binding_hash_matches_quote_extra_data() -> None:
    tlv = _tlv_bytes()
    binding = composite_binding_hash(
        nonce=_nonce(),
        channel_binding=(FIXTURE_DIR / "guest_x25519.pub.der").read_bytes(),
        envelope_tlv=tlv,
    )

    assert binding.hex() == BINDING_HEX
    assert hashlib.sha256(tlv).hexdigest() == GPU_ENVELOPE_SHA256


def test_check_envelope_nonce_accepts_owner_and_spdm_nonce() -> None:
    envelope = decode_gpu_envelope(_tlv_bytes())

    check_envelope_nonce(envelope, _nonce())


def test_check_envelope_nonce_rejects_foreign_field_one_nonce() -> None:
    data = bytearray(_tlv_bytes())
    _header_start, value_start, _value_end = _field_spans(data)[1]
    data[value_start] ^= 0x01
    envelope = decode_gpu_envelope(bytes(data))

    with pytest.raises(VerificationError, match="field-1 nonce"):
        check_envelope_nonce(envelope, _nonce())


def test_check_envelope_nonce_rejects_spdm_nonce_splice() -> None:
    data = bytearray(_tlv_bytes())
    _header_start, value_start, _value_end = _field_spans(data)[2]
    data[value_start + 4] ^= 0x01
    envelope = decode_gpu_envelope(bytes(data))

    with pytest.raises(VerificationError, match="SPDM report nonce"):
        check_envelope_nonce(envelope, _nonce())


def test_check_envelope_nonce_rejects_wrong_owner_nonce() -> None:
    envelope = decode_gpu_envelope(_tlv_bytes())
    wrong_nonce = b"\x00" * 32

    with pytest.raises(VerificationError, match="owner nonce|field-1 nonce"):
        check_envelope_nonce(envelope, wrong_nonce)
