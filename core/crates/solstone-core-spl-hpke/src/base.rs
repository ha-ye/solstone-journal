// SPDX-License-Identifier: AGPL-3.0-only
// Copyright (c) 2026 sol pbc

//! Base-mode HPKE using the RFC 9180 P-256 / SHA-256 / AES-256-GCM suite.

use hpke::{
    Deserializable, OpModeR, OpModeS, Serializable,
    aead::AesGcm256,
    kdf::HkdfSha256,
    kem::{DhP256HkdfSha256, Kem as KemTrait},
    setup_receiver, setup_sender,
};
use p256::{
    PublicKey,
    elliptic_curve::{pkcs8::DecodePublicKey, sec1::ToSec1Point},
};

type SuiteKem = DhP256HkdfSha256;

/// A P-256 recipient private key used by the SPL home service.
pub type P256Secret = p256::SecretKey;

/// The RFC 9180 wire components produced by base-mode sealing.
#[derive(Debug, PartialEq, Eq)]
pub struct SealedHpke {
    /// The 65-byte uncompressed P-256 HPKE encapsulated key.
    pub enc: Vec<u8>,
    /// The AEAD ciphertext, including its authentication tag.
    pub ciphertext: Vec<u8>,
}

/// Errors returned by the fixed RFC 9180 HPKE suite.
#[derive(Debug, thiserror::Error)]
pub enum HpkeError {
    /// The supplied SPKI is malformed or does not contain a P-256 public key.
    #[error("recipient public key must be a valid P-256 SPKI document")]
    InvalidRecipientPublicKey,

    /// The authenticated sender SPKI is malformed or does not contain P-256.
    #[error("sender public key must be a valid P-256 SPKI document")]
    InvalidSenderPublicKey,

    /// The encapsulated key is not a valid P-256 HPKE public key.
    #[error("encapsulated key must be a valid P-256 HPKE public key")]
    InvalidEncapsulatedKey,

    /// The recipient private key cannot be represented by the fixed P-256 KEM.
    #[error("recipient private key is invalid for P-256 HPKE")]
    InvalidRecipientPrivateKey,

    /// The underlying HPKE operation failed.
    #[error("HPKE operation failed")]
    Crypto(#[from] hpke::HpkeError),
}

/// Opens an RFC 9180 base-mode ciphertext for a P-256 recipient.
///
/// `info` is the caller's domain separator. Browser pairing uses its 16-byte
/// instance identifier with an empty `aad`; blob encryption may supply nonempty
/// authenticated associated data.
pub fn open_base(
    enc: &[u8],
    recipient_priv: &P256Secret,
    info: &[u8],
    ct: &[u8],
    aad: &[u8],
) -> Result<Vec<u8>, HpkeError> {
    let encapped_key = <SuiteKem as KemTrait>::EncappedKey::from_bytes(enc)
        .map_err(|_| HpkeError::InvalidEncapsulatedKey)?;
    let recipient_private_key = hpke_private_key(recipient_priv)?;
    let mut receiver_context = setup_receiver::<AesGcm256, HkdfSha256, SuiteKem>(
        &OpModeR::Base,
        &recipient_private_key,
        &encapped_key,
        info,
    )?;

    receiver_context.open(ct, aad).map_err(HpkeError::from)
}

/// Seals a plaintext for a DER SPKI P-256 recipient using RFC 9180 base mode.
///
/// The returned encapsulated key is always the suite's 65-byte uncompressed
/// P-256 point. Only P-256 SPKI recipients are accepted.
pub fn seal_base(
    recipient_pub_spki_der: &[u8],
    info: &[u8],
    plaintext: &[u8],
    aad: &[u8],
) -> Result<SealedHpke, HpkeError> {
    let recipient_public_key = hpke_public_key(recipient_pub_spki_der)?;
    let (encapped_key, mut sender_context) = setup_sender::<AesGcm256, HkdfSha256, SuiteKem>(
        &OpModeS::Base,
        &recipient_public_key,
        info,
    )?;
    let ciphertext = sender_context.seal(plaintext, aad)?;

    Ok(SealedHpke {
        enc: encapped_key.to_bytes().to_vec(),
        ciphertext,
    })
}

fn hpke_public_key(
    recipient_pub_spki_der: &[u8],
) -> Result<<SuiteKem as KemTrait>::PublicKey, HpkeError> {
    let recipient_public_key = PublicKey::from_public_key_der(recipient_pub_spki_der)
        .map_err(|_| HpkeError::InvalidRecipientPublicKey)?;
    let encoded_point = recipient_public_key.to_sec1_point(false);

    <SuiteKem as KemTrait>::PublicKey::from_bytes(encoded_point.as_bytes())
        .map_err(|_| HpkeError::InvalidRecipientPublicKey)
}

fn hpke_private_key(
    recipient_priv: &P256Secret,
) -> Result<<SuiteKem as KemTrait>::PrivateKey, HpkeError> {
    let private_bytes = recipient_priv.to_bytes();

    <SuiteKem as KemTrait>::PrivateKey::from_bytes(private_bytes.as_slice())
        .map_err(|_| HpkeError::InvalidRecipientPrivateKey)
}

#[cfg(test)]
mod tests {
    use super::{HpkeError, P256Secret, open_base, seal_base};
    use p256::elliptic_curve::pkcs8::EncodePublicKey;

    const ED25519_SPKI: [u8; 44] = [
        0x30, 0x2a, 0x30, 0x05, 0x06, 0x03, 0x2b, 0x65, 0x70, 0x03, 0x21, 0x00, 0xd7, 0x5a, 0x98,
        0x01, 0x82, 0xb1, 0x0a, 0xb7, 0xd5, 0x4b, 0xfe, 0xd3, 0xc9, 0x64, 0x07, 0x3a, 0x0e, 0xe1,
        0x72, 0xf3, 0xda, 0xa6, 0x23, 0x25, 0xaf, 0x02, 0x1a, 0x68, 0xf7, 0x07, 0x51, 0x1a,
    ];

    fn recipient_private_key() -> Result<P256Secret, HpkeError> {
        P256Secret::from_slice(&[0x42; 32]).map_err(|_| HpkeError::InvalidRecipientPrivateKey)
    }

    fn recipient_spki_der(recipient_priv: &P256Secret) -> Result<Vec<u8>, HpkeError> {
        recipient_priv
            .public_key()
            .to_public_key_der()
            .map(|der| der.as_bytes().to_vec())
            .map_err(|_| HpkeError::InvalidRecipientPublicKey)
    }

    #[test]
    fn seal_then_open_with_nonempty_info_and_aad() -> Result<(), HpkeError> {
        let recipient_priv = recipient_private_key()?;
        let recipient_spki_der = recipient_spki_der(&recipient_priv)?;
        let info = b"blob-transfer-v1";
        let aad = b"blob:00000000-0000-0000-0000-000000000001";
        let plaintext = b"SPL HPKE round trip";

        let sealed = seal_base(&recipient_spki_der, info, plaintext, aad)?;
        let opened = open_base(&sealed.enc, &recipient_priv, info, &sealed.ciphertext, aad)?;

        assert_eq!(sealed.enc.len(), 65);
        assert_eq!(opened, plaintext);
        Ok(())
    }

    #[test]
    fn opens_browser_pairing_with_empty_aad() -> Result<(), HpkeError> {
        let recipient_priv = recipient_private_key()?;
        let recipient_spki_der = recipient_spki_der(&recipient_priv)?;
        let info = b"0123456789abcdef";
        let plaintext = b"browser pairing payload";

        let sealed = seal_base(&recipient_spki_der, info, plaintext, b"")?;
        let opened = open_base(&sealed.enc, &recipient_priv, info, &sealed.ciphertext, b"")?;

        assert_eq!(opened, plaintext);
        Ok(())
    }

    #[test]
    fn rejects_malformed_and_non_p256_spki() {
        let malformed = seal_base(&[0x30, 0x01, 0x00], b"info", b"payload", b"");
        let non_p256 = seal_base(&ED25519_SPKI, b"info", b"payload", b"");

        assert!(matches!(
            malformed,
            Err(HpkeError::InvalidRecipientPublicKey)
        ));
        assert!(matches!(
            non_p256,
            Err(HpkeError::InvalidRecipientPublicKey)
        ));
    }
}
