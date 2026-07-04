# SPDX-License-Identifier: AGPL-3.0-only
# Copyright (c) 2026 sol pbc

"""HPKE helpers for SPL blob uplink and browser pairing."""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass

from cryptography.hazmat.primitives.asymmetric import ec
from cryptography.hazmat.primitives.serialization import load_der_public_key

ExportFn = Callable[[bytes, int], bytes]


@dataclass(frozen=True)
class OpenedHpke:
    plaintext: bytes
    export: ExportFn


@dataclass(frozen=True)
class SealedHpke:
    enc: bytes
    ciphertext: bytes


def _suite():
    from pyhpke import AEADId, CipherSuite, KDFId, KEMId

    return CipherSuite.new(
        KEMId.DHKEM_P256_HKDF_SHA256,
        KDFId.HKDF_SHA256,
        AEADId.AES256_GCM,
    )


def _kem_key_from_private(key: ec.EllipticCurvePrivateKey):
    from pyhpke import KEMKey

    return KEMKey.from_pyca_cryptography_key(key)


def _kem_key_from_public_der(spki_der: bytes):
    from pyhpke import KEMKey

    public_key = load_der_public_key(spki_der)
    if not isinstance(public_key, ec.EllipticCurvePublicKey):
        raise ValueError("HPKE public key must be EC P-256")
    if not isinstance(public_key.curve, ec.SECP256R1):
        raise ValueError("HPKE public key must be EC P-256")
    return KEMKey.from_pyca_cryptography_key(public_key)


def open_auth(
    enc: bytes,
    home_priv: ec.EllipticCurvePrivateKey,
    info: bytes,
    sender_pub_der: bytes,
    ct: bytes,
    aad: bytes,
) -> OpenedHpke:
    """Open an HPKE auth-mode ciphertext from a registered sender."""

    suite = _suite()
    ctx = suite.create_recipient_context(
        enc,
        _kem_key_from_private(home_priv),
        info=info,
        pks=_kem_key_from_public_der(sender_pub_der),
    )
    return OpenedHpke(
        plaintext=ctx.open(ct, aad),
        export=lambda exporter_context, length: ctx.export(exporter_context, length),
    )


def open_base(
    enc: bytes,
    home_priv: ec.EllipticCurvePrivateKey,
    info: bytes,
    ct: bytes,
    aad: bytes,
) -> bytes:
    """Open an HPKE base-mode ciphertext sealed to the home upload key."""

    suite = _suite()
    ctx = suite.create_recipient_context(
        enc,
        _kem_key_from_private(home_priv),
        info=info,
    )
    return ctx.open(ct, aad)


def seal_base(
    recipient_pub_der: bytes,
    info: bytes,
    plaintext: bytes,
    aad: bytes,
) -> SealedHpke:
    """Seal an HPKE base-mode ciphertext to a recipient public key."""

    suite = _suite()
    enc, ctx = suite.create_sender_context(
        _kem_key_from_public_der(recipient_pub_der),
        info=info,
    )
    return SealedHpke(enc=enc, ciphertext=ctx.seal(plaintext, aad))
