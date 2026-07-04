# SPDX-License-Identifier: AGPL-3.0-only
# Copyright (c) 2026 sol pbc

from __future__ import annotations

from cryptography.hazmat.primitives.asymmetric import ec

from solstone.think.spl import hpke


def _b(hex_value: str) -> bytes:
    return bytes.fromhex(hex_value)


def test_rfc9180_auth_mode_fixture_opens_and_exports() -> None:
    from pyhpke import KEMKey

    info = _b("4f6465206f6e2061204772656369616e2055726e")
    sk_rm = _b("d9f10996a02cd6c9dbda1d1f225f18f781ea3c893b8c2a6cb2e266e59f3cd9a9")
    pk_sm = _b(
        "04ece9b48cc98ee03ba742fe1218a3fbec960cc34b6e1defdcd3285276f39028"
        "e95b90f9526607565888766a1101f429dc3ec87364b5c8c613f0a081881950427f"
    )
    enc = _b(
        "04a7aeac79fda402674ef247c12d6f5fdfd21498d896b67ff04ec181382d4516"
        "b7662be32b4a2ae817c2d57104ecb6fcaa527438939810612d1b3d0af36ffc66ce"
    )
    aad = _b("436f756e742d30")
    pt = _b("4265617574792069732074727574682c20747275746820626561757479")
    ct = _b(
        "59b9890aabf94c1d502c39d8d356989ab0880ed43e984255db7b32a8d7b0ad"
        "5beba799a4ec326a0ddca3dd5e5d"
    )
    export_expected = _b(
        "6c0386ae15b1b834a5247ca5595b4e102347cbcdc65de64832f36008ce9c9483"
    )

    home_private = ec.derive_private_key(int.from_bytes(sk_rm, "big"), ec.SECP256R1())
    sender_public = ec.EllipticCurvePublicKey.from_encoded_point(
        ec.SECP256R1(),
        pk_sm,
    )
    suite = hpke._suite()
    ctx = suite.create_recipient_context(
        enc,
        KEMKey.from_pyca_cryptography_key(home_private),
        info=info,
        pks=KEMKey.from_pyca_cryptography_key(sender_public),
    )

    assert ctx.open(ct, aad) == pt
    assert ctx.export(b"", 32) == export_expected
