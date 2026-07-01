# SPDX-License-Identifier: AGPL-3.0-only
# Copyright (c) 2026 sol pbc

"""Relay-form pair-window (0x06) link encoding + RK derivation for spl posture.

One authoritative home for the cross-side primitives: the home (routes.py) and the
sol CLI relay-pairing client both import derive_rk + the 0x06 codec from here.
"""

from __future__ import annotations

import hashlib
import hmac
from dataclasses import dataclass

from solstone.apps.network.copy import PAIR_LINK_HOST, PAIR_LINK_PATH
from solstone.apps.network.crockford32 import decode as crockford_decode
from solstone.apps.network.crockford32 import encode as crockford_encode

PAIR_WINDOW_VERSION = 0x06
CA_FP_TAG_SPKI_SHA256 = 0x01
RK_INFO = b"spl-pair-window-v1"
RK_LENGTH = 16
S_BYTES = 8


@dataclass(frozen=True)
class ParsedPairWindow:
    s: bytes
    ca_fp_spki: bytes  # first 16 bytes of SHA-256 over CA DER SPKI
    relay_origin: str | None  # None => well-known default relay


def _hkdf_sha256(ikm: bytes, salt: bytes, info: bytes, length: int) -> bytes:
    if not salt:
        salt = b"\x00" * hashlib.sha256().digest_size
    prk = hmac.new(salt, ikm, hashlib.sha256).digest()
    okm = b""
    block = b""
    counter = 1
    while len(okm) < length:
        block = hmac.new(prk, block + info + bytes([counter]), hashlib.sha256).digest()
        okm += block
        counter += 1
    return okm[:length]


def derive_rk(s: bytes) -> bytes:
    """RK = HKDF-SHA256(IKM=S, salt="", info="spl-pair-window-v1", L=16)."""
    if len(s) != S_BYTES:
        raise ValueError(f"S must be {S_BYTES} bytes")
    return _hkdf_sha256(s, b"", RK_INFO, RK_LENGTH)


def encode_pair_window_link(
    s: bytes,
    ca_fp_spki: str,
    *,
    relay_origin: str | None,
) -> str:
    if len(s) != S_BYTES:
        raise ValueError(f"S must be {S_BYTES} bytes")
    ca_fp_bytes = bytes.fromhex(ca_fp_spki)
    if len(ca_fp_bytes) < 16:
        raise ValueError("ca_fp_spki must contain at least 16 bytes")

    if relay_origin is None:
        origin_field = b"\x00"
    else:
        origin_bytes = relay_origin.encode("utf-8")
        if not origin_bytes:
            raise ValueError("relay_origin must not be empty")
        if len(origin_bytes) > 255:
            raise ValueError("relay_origin must be 255 bytes or fewer")
        origin_field = bytes([len(origin_bytes)]) + origin_bytes

    blob = (
        bytes([PAIR_WINDOW_VERSION])
        + s
        + bytes([CA_FP_TAG_SPKI_SHA256])
        + ca_fp_bytes[:16]
        + origin_field
    )
    return f"https://{PAIR_LINK_HOST}{PAIR_LINK_PATH}#{crockford_encode(blob)}"


def decode_pair_window_link(link: str) -> ParsedPairWindow:
    fragment = link.rsplit("#", 1)[-1].strip()
    blob = crockford_decode(fragment)
    # version(1) + S(8) + tag(1) + ca_fp(16) + selector(1) = 27 minimum
    if len(blob) < 27:
        raise ValueError("pair-window blob too short")
    if blob[0] != PAIR_WINDOW_VERSION:
        raise ValueError(f"unsupported pair-link version: {blob[0]:#04x}")
    s = blob[1:9]
    if blob[9] != CA_FP_TAG_SPKI_SHA256:
        raise ValueError(f"unsupported ca_fp tag: {blob[9]:#04x}")
    ca_fp_spki = blob[10:26]
    selector = blob[26]
    if selector == 0x00:
        if len(blob) != 27:
            raise ValueError("default-origin blob has trailing bytes")
        relay_origin: str | None = None
    else:
        origin_bytes = blob[27 : 27 + selector]
        if len(origin_bytes) != selector:
            raise ValueError("relay_origin length mismatch")
        if len(blob) != 27 + selector:
            raise ValueError("pair-window blob has trailing bytes")
        relay_origin = origin_bytes.decode("utf-8")
    return ParsedPairWindow(s=s, ca_fp_spki=ca_fp_spki, relay_origin=relay_origin)
