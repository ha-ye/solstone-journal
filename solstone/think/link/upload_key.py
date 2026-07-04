# SPDX-License-Identifier: AGPL-3.0-only
# Copyright (c) 2026 sol pbc

"""Home upload HPKE key material for browser blob uplink."""

from __future__ import annotations

from dataclasses import dataclass

from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric import ec

from solstone.think.journal_io import atomic_replace, hold_lock
from solstone.think.link.paths import upload_private_key_path


@dataclass(frozen=True)
class UploadKey:
    private_key: ec.EllipticCurvePrivateKey
    public_spki_der: bytes


def load_upload_key() -> UploadKey:
    """Load the existing home upload HPKE key."""

    path = upload_private_key_path()
    encoded = path.read_bytes()
    key = serialization.load_pem_private_key(encoded, password=None)
    if not isinstance(key, ec.EllipticCurvePrivateKey):
        raise ValueError("upload HPKE private key must be EC P-256")
    if not isinstance(key.curve, ec.SECP256R1):
        raise ValueError("upload HPKE private key must be EC P-256")
    return UploadKey(
        private_key=key,
        public_spki_der=key.public_key().public_bytes(
            serialization.Encoding.DER,
            serialization.PublicFormat.SubjectPublicKeyInfo,
        ),
    )


def load_or_generate_upload_key() -> UploadKey:
    """Load the upload key, creating it once if absent."""

    path = upload_private_key_path()
    with hold_lock(path):
        try:
            return load_upload_key()
        except FileNotFoundError:
            key = ec.generate_private_key(ec.SECP256R1())
            encoded = key.private_bytes(
                encoding=serialization.Encoding.PEM,
                format=serialization.PrivateFormat.PKCS8,
                encryption_algorithm=serialization.NoEncryption(),
            )
            atomic_replace(path, encoded, mode=0o600)
            return load_upload_key()
