// SPDX-License-Identifier: AGPL-3.0-only
// Copyright (c) 2026 sol pbc

//! Pure preparation of an authenticated SPL blob for later ingestion.

use solstone_core_spl_hpke::{HpkeError, Offer, P256Secret, open_auth};
use thiserror::Error;

use crate::{BlobArchiveError, ValidatedBlobArchive, parse_blob_archive};

const BLOB_INFO_PREFIX: &[u8] = b"spl-blob-v1";
const ACK_EXPORT_CONTEXT: &[u8] = b"spl-blob-ack-v1";
const ACK_KEY_LEN: usize = 32;
const BLOB_INFO_LEN: usize = BLOB_INFO_PREFIX.len() + 16 + 32;

/// A blob which has passed authenticated decryption and archive validation.
#[derive(Debug)]
pub struct PreparedAuthenticatedBlob {
    /// The offer retained verbatim for acknowledgement construction.
    pub offer: Offer,
    /// Archive content which is safe for the caller to ingest.
    pub archive: ValidatedBlobArchive,
    /// The RFC 9180 exporter key for the later `SBA1` acknowledgement.
    pub acknowledgement_key: [u8; ACK_KEY_LEN],
}

/// Failures while preparing an authenticated blob without any side effects.
#[derive(Debug, Error)]
pub enum AuthenticatedBlobError {
    /// The transfer did not contain exactly the length declared by its offer.
    #[error("blob ciphertext length mismatch: expected {expected}, got {actual}")]
    CiphertextLength {
        /// The length from the validated offer header.
        expected: u64,
        /// The bytes supplied by the transport.
        actual: usize,
    },

    /// The encrypted blob could not be authenticated and opened.
    #[error("blob HPKE open failed: {0}")]
    Hpke(#[from] HpkeError),

    /// The opened plaintext was not a safe SPL blob archive.
    #[error("blob archive validation failed: {0}")]
    Archive(#[from] BlobArchiveError),

    /// The HPKE exporter returned a length other than the protocol's 32 bytes.
    #[error("blob acknowledgement exporter produced {actual} bytes, expected {ACK_KEY_LEN}")]
    ExporterLength {
        /// The number of bytes returned by the HPKE exporter.
        actual: usize,
    },
}

/// Authenticate, validate, and prepare a received browser blob.
///
/// The caller supplies only already-read protocol data. This function neither
/// performs I/O nor decides authorization, admission, acknowledgement status,
/// or ingestion. It preserves the offer's exact 67-byte header as HPKE AAD.
pub fn prepare_authenticated_blob(
    offer: Offer,
    enc: &[u8],
    ciphertext: &[u8],
    recipient_private_key: &P256Secret,
    sender_public_key_spki_der: &[u8],
    instance_id: &[u8; 16],
) -> Result<PreparedAuthenticatedBlob, AuthenticatedBlobError> {
    if !ciphertext_length_matches(ciphertext.len(), offer.ciphertext_len) {
        return Err(AuthenticatedBlobError::CiphertextLength {
            expected: offer.ciphertext_len,
            actual: ciphertext.len(),
        });
    }

    let info = blob_info(instance_id, &offer.sender_fingerprint);
    let opened = open_auth(
        enc,
        recipient_private_key,
        &info,
        sender_public_key_spki_der,
        ciphertext,
        &offer.header,
    )?;
    let archive = parse_blob_archive(&opened.plaintext)?;
    let acknowledgement_key = acknowledgement_key(&opened)?;

    Ok(PreparedAuthenticatedBlob {
        offer,
        archive,
        acknowledgement_key,
    })
}

fn ciphertext_length_matches(actual: usize, expected: u64) -> bool {
    match u64::try_from(actual) {
        Ok(actual) => actual == expected,
        Err(_) => false,
    }
}

fn blob_info(instance_id: &[u8; 16], sender_fingerprint: &[u8; 32]) -> [u8; BLOB_INFO_LEN] {
    let mut info = [0_u8; BLOB_INFO_LEN];
    let prefix_end = BLOB_INFO_PREFIX.len();
    let instance_end = prefix_end + instance_id.len();
    info[..prefix_end].copy_from_slice(BLOB_INFO_PREFIX);
    info[prefix_end..instance_end].copy_from_slice(instance_id);
    info[instance_end..].copy_from_slice(sender_fingerprint);
    info
}

fn acknowledgement_key(
    opened: &solstone_core_spl_hpke::OpenedHpke,
) -> Result<[u8; ACK_KEY_LEN], AuthenticatedBlobError> {
    let exported = opened.export(ACK_EXPORT_CONTEXT, ACK_KEY_LEN)?;
    let actual = exported.len();
    exported
        .try_into()
        .map_err(|_| AuthenticatedBlobError::ExporterLength { actual })
}

#[cfg(test)]
mod tests {
    use std::io::Cursor;

    use flate2::{Compression, write::GzEncoder};
    use hpke::{
        Deserializable, OpModeS, Serializable,
        aead::AesGcm256,
        kdf::HkdfSha256,
        kem::{DhP256HkdfSha256, Kem as KemTrait},
        setup_sender,
    };
    use p256::elliptic_curve::{pkcs8::EncodePublicKey, sec1::ToSec1Point};
    use solstone_core_spl_hpke::{OFFER_LEN, Offer, P256Secret, parse_offer};
    use tar::{Builder, Header};

    use super::{AuthenticatedBlobError, blob_info, prepare_authenticated_blob};

    type SuiteKem = DhP256HkdfSha256;

    #[test]
    fn blob_info_is_the_exact_protocol_concatenation() {
        let instance_id = [0x11; 16];
        let sender_fingerprint = [0x22; 32];
        let info = blob_info(&instance_id, &sender_fingerprint);

        assert_eq!(&info[..11], b"spl-blob-v1");
        assert_eq!(&info[11..27], &instance_id);
        assert_eq!(&info[27..], &sender_fingerprint);
    }

    #[test]
    fn rejects_a_ciphertext_length_mismatch_before_hpke() -> Result<(), Box<dyn std::error::Error>>
    {
        let offer = Offer {
            header: [0_u8; OFFER_LEN],
            sender_fingerprint: [0_u8; 32],
            blob_id: [0_u8; 16],
            ciphertext_len: 4,
        };
        let recipient_private_key = P256Secret::from_slice(&[0x42; 32])?;
        let result = prepare_authenticated_blob(
            offer,
            b"",
            b"bad",
            &recipient_private_key,
            b"",
            &[0_u8; 16],
        );

        assert!(matches!(
            result,
            Err(AuthenticatedBlobError::CiphertextLength {
                expected: 4,
                actual: 3
            })
        ));
        Ok(())
    }

    #[test]
    fn opens_an_authenticated_archive_and_derives_the_acknowledgement_key()
    -> Result<(), Box<dyn std::error::Error>> {
        let recipient_private_key = P256Secret::from_slice(&[0x42; 32])?;
        let sender_private_key = P256Secret::from_slice(&[0x43; 32])?;
        let sender_public_key_spki_der = sender_private_key
            .public_key()
            .to_public_key_der()?
            .as_bytes()
            .to_vec();
        let archive = valid_archive()?;
        let instance_id = [0x11; 16];
        let offer = valid_offer(archive.len() + 16)?;
        let info = blob_info(&instance_id, &offer.sender_fingerprint);

        let recipient_public_key = hpke_public_key(&recipient_private_key)?;
        let sender_private_key = hpke_private_key(&sender_private_key)?;
        let sender_public_key = hpke_public_key_from_private_key(&sender_private_key)?;
        let (encapsulated_key, mut sender_context) = setup_sender::<AesGcm256, HkdfSha256, SuiteKem>(
            &OpModeS::Auth((sender_private_key, sender_public_key)),
            &recipient_public_key,
            &info,
        )?;
        let ciphertext = sender_context.seal(&archive, &offer.header)?;
        let mut expected_acknowledgement_key = [0_u8; 32];
        sender_context.export(b"spl-blob-ack-v1", &mut expected_acknowledgement_key)?;

        let prepared = prepare_authenticated_blob(
            offer,
            &encapsulated_key.to_bytes(),
            &ciphertext,
            &recipient_private_key,
            &sender_public_key_spki_der,
            &instance_id,
        )?;

        assert_eq!(prepared.offer, offer);
        assert_eq!(prepared.archive.metadata.day, "20260731");
        assert_eq!(prepared.archive.metadata.segment, "120001_3");
        assert_eq!(prepared.archive.entries.len(), 1);
        assert_eq!(prepared.archive.entries[0].name, "entry.bin");
        assert_eq!(prepared.archive.entries[0].bytes, b"body");
        assert_eq!(prepared.acknowledgement_key, expected_acknowledgement_key);
        Ok(())
    }

    fn valid_archive() -> Result<Vec<u8>, Box<dyn std::error::Error>> {
        let encoder = GzEncoder::new(Vec::new(), Compression::default());
        let mut builder = Builder::new(encoder);
        append_regular(
            &mut builder,
            "blob.json",
            br#"{"v":1,"day":"20260731","segment":"120001_3","host":"home.example","meta":{}}"#,
        )?;
        append_regular(&mut builder, "entry.bin", b"body")?;
        let encoder = builder.into_inner()?;
        Ok(encoder.finish()?)
    }

    fn append_regular(
        builder: &mut Builder<GzEncoder<Vec<u8>>>,
        name: &str,
        bytes: &[u8],
    ) -> Result<(), std::io::Error> {
        let mut header = Header::new_gnu();
        header.set_size(bytes.len() as u64);
        header.set_mode(0o600);
        header.set_cksum();
        builder.append_data(&mut header, name, Cursor::new(bytes))
    }

    fn valid_offer(ciphertext_len: usize) -> Result<Offer, Box<dyn std::error::Error>> {
        let mut header = [0_u8; OFFER_LEN];
        header[..11].copy_from_slice(&[
            b'S', b'B', b'O', b'1', 0x01, 0x00, 0x10, 0x00, 0x01, 0x00, 0x02,
        ]);
        header[11..43].copy_from_slice(&[0x22; 32]);
        header[43..59].copy_from_slice(&[0x33; 16]);
        header[59..].copy_from_slice(&u64::try_from(ciphertext_len)?.to_be_bytes());
        Ok(parse_offer(&header)?)
    }

    fn hpke_private_key(
        private_key: &P256Secret,
    ) -> Result<<SuiteKem as KemTrait>::PrivateKey, hpke::HpkeError> {
        <SuiteKem as KemTrait>::PrivateKey::from_bytes(private_key.to_bytes().as_slice())
    }

    fn hpke_public_key(
        private_key: &P256Secret,
    ) -> Result<<SuiteKem as KemTrait>::PublicKey, hpke::HpkeError> {
        let point = private_key.public_key().to_sec1_point(false);
        <SuiteKem as KemTrait>::PublicKey::from_bytes(point.as_bytes())
    }

    fn hpke_public_key_from_private_key(
        private_key: &<SuiteKem as KemTrait>::PrivateKey,
    ) -> Result<<SuiteKem as KemTrait>::PublicKey, hpke::HpkeError> {
        Ok(<SuiteKem as KemTrait>::sk_to_pk(private_key))
    }
}
