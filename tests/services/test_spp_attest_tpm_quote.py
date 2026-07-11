# SPDX-License-Identifier: AGPL-3.0-only
# Copyright (c) 2026 sol pbc

from __future__ import annotations

import hashlib
from pathlib import Path

import pytest

from solstone.think.services.spp_attest import VerificationError
from solstone.think.services.spp_attest.binding import composite_binding_hash
from solstone.think.services.spp_attest.tpm_quote import (
    TpmQuoteVerifier,
    _parse_pcrs,
    _parse_quote_msg,
    verify_quote,
)

FIXTURE_DIR = Path(__file__).resolve().parents[1] / "fixtures" / "spp_attest"
BINDING_HEX = "268901922d7b8444139f3d3e3edfcc3dd860491e313b243d94fb97ba5b312ea2"
PCR_DIGEST_HEX = "114baecc10432c71b9e3358af011650788eb0899bd450c844cc4dafad9d3a777"
PCR_SHA256_HEX = "b162f46105c80d3e45028e37cc649404c9d65297ad1cda8f953208582060b0e3"


def _binding() -> bytes:
    return composite_binding_hash(
        nonce=bytes.fromhex("".join((FIXTURE_DIR / "nonce.hex").read_text().split())),
        channel_binding=(FIXTURE_DIR / "guest_x25519.pub.der").read_bytes(),
        envelope_tlv=(FIXTURE_DIR / "gpu-envelope.tlv").read_bytes(),
    )


def _verify_quote(
    *,
    quote_msg: bytes | None = None,
    quote_sig: bytes | None = None,
    quote_pcrs: bytes | None = None,
    binding: bytes | None = None,
) -> None:
    verify_quote(
        ak_pub_pem=(FIXTURE_DIR / "akpub.pem").read_bytes(),
        quote_msg=quote_msg
        if quote_msg is not None
        else (FIXTURE_DIR / "quote.msg").read_bytes(),
        quote_sig=quote_sig
        if quote_sig is not None
        else (FIXTURE_DIR / "quote.sig").read_bytes(),
        quote_pcrs=quote_pcrs
        if quote_pcrs is not None
        else (FIXTURE_DIR / "quote.pcrs").read_bytes(),
        expected_binding=binding if binding is not None else _binding(),
    )


def _first_digest_size_offset(pcrs: bytes) -> int:
    selection_count_offset = 0
    selection_count_size = 4
    selection_slot_count = 8
    selection_slot_size = 16
    offset = selection_count_offset + selection_count_size
    offset += selection_slot_count * selection_slot_size
    digest_list_count_size = 4
    offset += digest_list_count_size
    digest_count_size = 4
    return offset + digest_count_size


def _mutate_first_digest_buffer(pcrs: bytes) -> bytes:
    data = bytearray(pcrs)
    size_offset = _first_digest_size_offset(pcrs)
    buffer_start = size_offset + 2
    data[buffer_start] ^= 0x01
    return bytes(data)


def _mutate_first_selection_byte(pcrs: bytes) -> bytes:
    data = bytearray(pcrs)
    selection_count_size = 4
    hash_alg_size = 2
    sizeof_select_size = 1
    pcr_select_start = selection_count_size + hash_alg_size + sizeof_select_size
    data[pcr_select_start] ^= 0x01
    return bytes(data)


def test_verify_quote_accepts_fixture_bytes_and_vectors() -> None:
    quote_msg = (FIXTURE_DIR / "quote.msg").read_bytes()
    quote_pcrs = (FIXTURE_DIR / "quote.pcrs").read_bytes()

    _verify_quote()

    quote = _parse_quote_msg(quote_msg, _binding())
    selections, digest_buffers = _parse_pcrs(quote_pcrs)
    assert quote.extra_data.hex() == BINDING_HEX
    assert quote.pcr_digest.hex() == PCR_DIGEST_HEX
    assert hashlib.sha256(b"".join(digest_buffers)).hexdigest() == PCR_DIGEST_HEX
    assert hashlib.sha256(quote_pcrs).hexdigest() == PCR_SHA256_HEX
    assert selections[0].selected_pcrs() == (0, 2, 4, 7, 8, 9, 15, 16, 22, 23)


def test_tpm_quote_verifier_facade_accepts_fixture_paths() -> None:
    TpmQuoteVerifier().verify(
        FIXTURE_DIR / "akpub.pem",
        FIXTURE_DIR / "quote.msg",
        FIXTURE_DIR / "quote.sig",
        FIXTURE_DIR / "quote.pcrs",
        BINDING_HEX,
    )


def test_verify_quote_rejects_flipped_signature_byte() -> None:
    quote_sig = bytearray((FIXTURE_DIR / "quote.sig").read_bytes())
    quote_sig[-1] ^= 0x01

    with pytest.raises(VerificationError, match="signature invalid"):
        _verify_quote(quote_sig=bytes(quote_sig))


def test_verify_quote_rejects_wrong_extra_data_binding() -> None:
    binding = bytearray(_binding())
    binding[0] ^= 0x01

    with pytest.raises(VerificationError, match="extraData mismatch"):
        _verify_quote(binding=bytes(binding))


def test_verify_quote_rejects_mutated_pcr_value() -> None:
    quote_pcrs = _mutate_first_digest_buffer((FIXTURE_DIR / "quote.pcrs").read_bytes())

    with pytest.raises(VerificationError, match="PCR digest mismatch"):
        _verify_quote(quote_pcrs=quote_pcrs)


def test_verify_quote_rejects_pcr_selection_mismatch() -> None:
    quote_pcrs = _mutate_first_selection_byte((FIXTURE_DIR / "quote.pcrs").read_bytes())

    with pytest.raises(VerificationError, match="selection"):
        _verify_quote(quote_pcrs=quote_pcrs)


def test_verify_quote_rejects_unsupported_signature_algorithm() -> None:
    quote_sig = bytearray((FIXTURE_DIR / "quote.sig").read_bytes())
    quote_sig[0:2] = (0x0015).to_bytes(2, "big")

    with pytest.raises(VerificationError, match="signature alg"):
        _verify_quote(quote_sig=bytes(quote_sig))


def test_verify_quote_rejects_unsupported_signature_hash_algorithm() -> None:
    quote_sig = bytearray((FIXTURE_DIR / "quote.sig").read_bytes())
    quote_sig[2:4] = (0x0004).to_bytes(2, "big")

    with pytest.raises(VerificationError, match="hashAlg"):
        _verify_quote(quote_sig=bytes(quote_sig))


def test_verify_quote_rejects_trailing_quote_msg_bytes() -> None:
    quote_msg = (FIXTURE_DIR / "quote.msg").read_bytes() + b"\x00"

    with pytest.raises(VerificationError, match="trailing"):
        _verify_quote(quote_msg=quote_msg)


def test_verify_quote_rejects_digest_size_over_64() -> None:
    quote_pcrs = bytearray((FIXTURE_DIR / "quote.pcrs").read_bytes())
    size_offset = _first_digest_size_offset(bytes(quote_pcrs))
    quote_pcrs[size_offset : size_offset + 2] = (65).to_bytes(2, "little")

    with pytest.raises(VerificationError, match="exceeds 64"):
        _verify_quote(quote_pcrs=bytes(quote_pcrs))


def test_verify_quote_rejects_trailing_pcrs_bytes() -> None:
    with pytest.raises(VerificationError, match="trailing"):
        _verify_quote(quote_pcrs=(FIXTURE_DIR / "quote.pcrs").read_bytes() + b"\x00")
