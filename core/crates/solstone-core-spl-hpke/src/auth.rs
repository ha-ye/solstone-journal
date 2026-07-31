// SPDX-License-Identifier: AGPL-3.0-only
// Copyright (c) 2026 sol pbc

//! Auth-mode HPKE using the RFC 9180 P-256 / SHA-256 / AES-256-GCM suite.

use hpke::{
    Deserializable, OpModeR,
    aead::{AeadCtxR, AesGcm256},
    kdf::HkdfSha256,
    kem::{DhP256HkdfSha256, Kem as KemTrait},
    setup_receiver,
};
use p256::{
    PublicKey,
    elliptic_curve::{pkcs8::DecodePublicKey, sec1::ToSec1Point},
};

use crate::base::{HpkeError, P256Secret};

type SuiteKem = DhP256HkdfSha256;
type ReceiverContext = AeadCtxR<AesGcm256, HkdfSha256, SuiteKem>;

/// A successfully opened RFC 9180 auth-mode ciphertext.
///
/// The recipient context remains private so callers can derive protocol-bound
/// exporter values from the same exchange without deriving anything from the
/// plaintext.
pub struct OpenedHpke {
    /// The authenticated plaintext supplied by the sender.
    pub plaintext: Vec<u8>,
    recipient_context: ReceiverContext,
}

impl OpenedHpke {
    /// Derives bytes from this ciphertext's RFC 9180 recipient context.
    ///
    /// Blob acknowledgements use `context = b"spl-blob-ack-v1"` and `len = 32`.
    pub fn export(&self, context: &[u8], len: usize) -> Result<Vec<u8>, HpkeError> {
        let mut output = vec![0_u8; len];
        self.recipient_context.export(context, &mut output)?;
        Ok(output)
    }
}

/// Opens an RFC 9180 auth-mode ciphertext from a registered P-256 sender.
///
/// This is fixed to DHKEM(P-256, HKDF-SHA256) / HKDF-SHA256 / AES-256-GCM.
/// The sender identity is DER SPKI and must contain an EC P-256 public key;
/// no other curve or malformed key is accepted as a fallback.
pub fn open_auth(
    enc: &[u8],
    recipient_priv: &P256Secret,
    info: &[u8],
    sender_pub_spki_der: &[u8],
    ct: &[u8],
    aad: &[u8],
) -> Result<OpenedHpke, HpkeError> {
    let sender_public_key = sender_hpke_public_key(sender_pub_spki_der)?;
    let encapped_key = <SuiteKem as KemTrait>::EncappedKey::from_bytes(enc)
        .map_err(|_| HpkeError::InvalidEncapsulatedKey)?;
    let recipient_private_key = recipient_hpke_private_key(recipient_priv)?;
    let mut recipient_context = setup_receiver::<AesGcm256, HkdfSha256, SuiteKem>(
        &OpModeR::Auth(sender_public_key),
        &recipient_private_key,
        &encapped_key,
        info,
    )?;
    let plaintext = recipient_context.open(ct, aad)?;

    Ok(OpenedHpke {
        plaintext,
        recipient_context,
    })
}

fn sender_hpke_public_key(
    sender_pub_spki_der: &[u8],
) -> Result<<SuiteKem as KemTrait>::PublicKey, HpkeError> {
    let sender_public_key = PublicKey::from_public_key_der(sender_pub_spki_der)
        .map_err(|_| HpkeError::InvalidSenderPublicKey)?;
    let encoded_point = sender_public_key.to_sec1_point(false);

    <SuiteKem as KemTrait>::PublicKey::from_bytes(encoded_point.as_bytes())
        .map_err(|_| HpkeError::InvalidSenderPublicKey)
}

fn recipient_hpke_private_key(
    recipient_priv: &P256Secret,
) -> Result<<SuiteKem as KemTrait>::PrivateKey, HpkeError> {
    let private_bytes = recipient_priv.to_bytes();

    <SuiteKem as KemTrait>::PrivateKey::from_bytes(private_bytes.as_slice())
        .map_err(|_| HpkeError::InvalidRecipientPrivateKey)
}

#[cfg(test)]
mod tests {
    use super::{HpkeError, P256Secret, open_auth};
    use p256::{PublicKey, elliptic_curve::pkcs8::EncodePublicKey};

    const ED25519_SPKI: [u8; 44] = [
        0x30, 0x2a, 0x30, 0x05, 0x06, 0x03, 0x2b, 0x65, 0x70, 0x03, 0x21, 0x00, 0xd7, 0x5a, 0x98,
        0x01, 0x82, 0xb1, 0x0a, 0xb7, 0xd5, 0x4b, 0xfe, 0xd3, 0xc9, 0x64, 0x07, 0x3a, 0x0e, 0xe1,
        0x72, 0xf3, 0xda, 0xa6, 0x23, 0x25, 0xaf, 0x02, 0x1a, 0x68, 0xf7, 0x07, 0x51, 0x1a,
    ];

    fn fixture_recipient_private_key() -> Result<P256Secret, HpkeError> {
        P256Secret::from_slice(&[
            0xd9, 0xf1, 0x09, 0x96, 0xa0, 0x2c, 0xd6, 0xc9, 0xdb, 0xda, 0x1d, 0x1f, 0x22, 0x5f,
            0x18, 0xf7, 0x81, 0xea, 0x3c, 0x89, 0x3b, 0x8c, 0x2a, 0x6c, 0xb2, 0xe2, 0x66, 0xe5,
            0x9f, 0x3c, 0xd9, 0xa9,
        ])
        .map_err(|_| HpkeError::InvalidRecipientPrivateKey)
    }

    fn fixture_sender_spki_der() -> Result<Vec<u8>, HpkeError> {
        let sender_public_key = PublicKey::from_sec1_bytes(&[
            0x04, 0xec, 0xe9, 0xb4, 0x8c, 0xc9, 0x8e, 0xe0, 0x3b, 0xa7, 0x42, 0xfe, 0x12, 0x18,
            0xa3, 0xfb, 0xec, 0x96, 0x0c, 0xc3, 0x4b, 0x6e, 0x1d, 0xef, 0xdc, 0xd3, 0x28, 0x52,
            0x76, 0xf3, 0x90, 0x28, 0xe9, 0x5b, 0x90, 0xf9, 0x52, 0x66, 0x07, 0x56, 0x58, 0x88,
            0x76, 0x6a, 0x11, 0x01, 0xf4, 0x29, 0xdc, 0x3e, 0xc8, 0x73, 0x64, 0xb5, 0xc8, 0xc6,
            0x13, 0xf0, 0xa0, 0x81, 0x88, 0x19, 0x50, 0x42, 0x7f,
        ])
        .map_err(|_| HpkeError::InvalidSenderPublicKey)?;

        sender_public_key
            .to_public_key_der()
            .map(|der| der.as_bytes().to_vec())
            .map_err(|_| HpkeError::InvalidSenderPublicKey)
    }

    #[test]
    fn opens_and_exports_the_rfc_9180_auth_mode_fixture() -> Result<(), HpkeError> {
        let recipient_priv = fixture_recipient_private_key()?;
        let sender_pub_spki_der = fixture_sender_spki_der()?;
        let opened = open_auth(
            &[
                0x04, 0xa7, 0xae, 0xac, 0x79, 0xfd, 0xa4, 0x02, 0x67, 0x4e, 0xf2, 0x47, 0xc1, 0x2d,
                0x6f, 0x5f, 0xdf, 0xd2, 0x14, 0x98, 0xd8, 0x96, 0xb6, 0x7f, 0xf0, 0x4e, 0xc1, 0x81,
                0x38, 0x2d, 0x45, 0x16, 0xb7, 0x66, 0x2b, 0xe3, 0x2b, 0x4a, 0x2a, 0xe8, 0x17, 0xc2,
                0xd5, 0x71, 0x04, 0xec, 0xb6, 0xfc, 0xaa, 0x52, 0x74, 0x38, 0x93, 0x98, 0x10, 0x61,
                0x2d, 0x1b, 0x3d, 0x0a, 0xf3, 0x6f, 0xfc, 0x66, 0xce,
            ],
            &recipient_priv,
            b"Ode on a Grecian Urn",
            &sender_pub_spki_der,
            &[
                0x59, 0xb9, 0x89, 0x0a, 0xab, 0xf9, 0x4c, 0x1d, 0x50, 0x2c, 0x39, 0xd8, 0xd3, 0x56,
                0x98, 0x9a, 0xb0, 0x88, 0x0e, 0xd4, 0x3e, 0x98, 0x42, 0x55, 0xdb, 0x7b, 0x32, 0xa8,
                0xd7, 0xb0, 0xad, 0x5b, 0xeb, 0xa7, 0x99, 0xa4, 0xec, 0x32, 0x6a, 0x0d, 0xdc, 0xa3,
                0xdd, 0x5e, 0x5d,
            ],
            b"Count-0",
        )?;

        assert_eq!(opened.plaintext, b"Beauty is truth, truth beauty");
        assert_eq!(
            opened.export(b"", 32)?,
            [
                0x6c, 0x03, 0x86, 0xae, 0x15, 0xb1, 0xb8, 0x34, 0xa5, 0x24, 0x7c, 0xa5, 0x59, 0x5b,
                0x4e, 0x10, 0x23, 0x47, 0xcb, 0xcd, 0xc6, 0x5d, 0xe6, 0x48, 0x32, 0xf3, 0x60, 0x08,
                0xce, 0x9c, 0x94, 0x83,
            ]
        );
        Ok(())
    }

    #[test]
    fn rejects_malformed_and_non_p256_sender_spki() -> Result<(), HpkeError> {
        let recipient_priv = fixture_recipient_private_key()?;
        let malformed = open_auth(
            &[0; 65],
            &recipient_priv,
            b"info",
            &[0x30, 0x01, 0x00],
            b"ciphertext",
            b"",
        );
        let non_p256 = open_auth(
            &[0; 65],
            &recipient_priv,
            b"info",
            &ED25519_SPKI,
            b"ciphertext",
            b"",
        );

        assert!(matches!(malformed, Err(HpkeError::InvalidSenderPublicKey)));
        assert!(matches!(non_p256, Err(HpkeError::InvalidSenderPublicKey)));
        Ok(())
    }

    #[test]
    fn reports_an_invalid_encapsulated_key_separately() -> Result<(), HpkeError> {
        let recipient_priv = fixture_recipient_private_key()?;
        let sender_pub_spki_der = fixture_sender_spki_der()?;
        let opened = open_auth(
            &[0; 65],
            &recipient_priv,
            b"info",
            &sender_pub_spki_der,
            b"ciphertext",
            b"",
        );

        assert!(matches!(opened, Err(HpkeError::InvalidEncapsulatedKey)));
        Ok(())
    }
}
