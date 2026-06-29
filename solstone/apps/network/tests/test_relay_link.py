# SPDX-License-Identifier: AGPL-3.0-only
# Copyright (c) 2026 sol pbc

from __future__ import annotations

import pytest

from solstone.apps.network.crockford32 import decode as crockford_decode
from solstone.apps.network.crockford32 import encode as crockford_encode
from solstone.apps.network.relay_link import (
    CA_FP_TAG_SPKI_SHA256,
    PAIR_WINDOW_VERSION,
    decode_pair_window_link,
    derive_rk,
    encode_pair_window_link,
)

S = bytes.fromhex("0123456789abcdef")
CA_FP_SPKI = "deadbeefcafebabe0123456789abcdef"
RK_HEX = "e34481a4cde647ba9c9fb29a59e18271"
DEFAULT_BLOB_HEX = "060123456789abcdef01deadbeefcafebabe0123456789abcdef00"
DEFAULT_LINK = "https://go.solstone.app/p#0R0J6HB7H6NWVVR1VTPVXVYAZTXBW0938NKRKAYDXW00"
CUSTOM_ORIGIN = "https://relay.example"
CUSTOM_BLOB_HEX = (
    "060123456789abcdef01deadbeefcafebabe0123456789abcdef"
    "1568747470733a2f2f72656c61792e6578616d706c65"
)
CUSTOM_LINK = (
    "https://go.solstone.app/p#"
    "0R0J6HB7H6NWVVR1VTPVXVYAZTXBW0938NKRKAYDXWAPGX3ME1SKMBSFE9JPRRBS5SJQGRBDE1P6A"
)


def _fragment(link: str) -> str:
    return link.rsplit("#", 1)[1]


def test_derive_rk_matches_conformance_vector() -> None:
    assert derive_rk(S).hex() == RK_HEX


def test_derive_rk_requires_eight_byte_s() -> None:
    with pytest.raises(ValueError):
        derive_rk(b"too short")


def test_default_origin_encode_matches_conformance_vector() -> None:
    link = encode_pair_window_link(S, CA_FP_SPKI, relay_origin=None)

    assert link == DEFAULT_LINK
    assert crockford_decode(_fragment(link)).hex() == DEFAULT_BLOB_HEX


def test_custom_origin_encode_matches_conformance_vector() -> None:
    link = encode_pair_window_link(S, CA_FP_SPKI, relay_origin=CUSTOM_ORIGIN)

    assert link == CUSTOM_LINK
    assert crockford_decode(_fragment(link)).hex() == CUSTOM_BLOB_HEX


def test_decode_pair_window_link_round_trips_default_origin() -> None:
    parsed = decode_pair_window_link(DEFAULT_LINK)

    assert parsed.s == S
    assert parsed.ca_fp_spki == bytes.fromhex(CA_FP_SPKI)
    assert parsed.relay_origin is None
    assert derive_rk(parsed.s).hex() == RK_HEX


def test_decode_pair_window_link_round_trips_custom_origin() -> None:
    parsed = decode_pair_window_link(CUSTOM_LINK)

    assert parsed.s == S
    assert parsed.ca_fp_spki == bytes.fromhex(CA_FP_SPKI)
    assert parsed.relay_origin == CUSTOM_ORIGIN
    assert derive_rk(parsed.s).hex() == RK_HEX


def test_decode_pair_window_link_rejects_wrong_version() -> None:
    blob = bytes([0x05]) + bytes.fromhex(DEFAULT_BLOB_HEX)[1:]

    with pytest.raises(ValueError):
        decode_pair_window_link(f"https://go.solstone.app/p#{crockford_encode(blob)}")


def test_decode_pair_window_link_rejects_truncated_blob() -> None:
    blob = bytes.fromhex(DEFAULT_BLOB_HEX)[:-1]

    with pytest.raises(ValueError):
        decode_pair_window_link(f"https://go.solstone.app/p#{crockford_encode(blob)}")


def test_decode_pair_window_link_rejects_wrong_ca_fp_tag() -> None:
    blob = (
        bytes([PAIR_WINDOW_VERSION])
        + S
        + bytes([CA_FP_TAG_SPKI_SHA256 + 1])
        + bytes.fromhex(CA_FP_SPKI)
        + b"\x00"
    )

    with pytest.raises(ValueError):
        decode_pair_window_link(f"https://go.solstone.app/p#{crockford_encode(blob)}")
