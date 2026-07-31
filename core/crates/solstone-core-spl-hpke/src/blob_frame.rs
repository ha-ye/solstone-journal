// SPDX-License-Identifier: AGPL-3.0-only
// Copyright (c) 2026 sol pbc

//! Byte-exact framing for blob uplink v1.
//!
//! This module deliberately has no transport, archive, ledger, or HPKE state.
//! It validates the fixed offer header and constructs the two response frames
//! so the receiver can keep policy and I/O outside the wire contract.

use hmac::{Hmac, Mac};
use sha2::Sha256;
use thiserror::Error;

pub const OFFER_LEN: usize = 67;
pub const READY_LEN: usize = 6;
pub const ACK_LEN: usize = 38;
pub const SENDER_FINGERPRINT_LEN: usize = 32;
pub const BLOB_ID_LEN: usize = 16;
pub const MAX_CIPHERTEXT_LEN: u64 = 80 * 1024 * 1024;

const OFFER_MAGIC: [u8; 4] = *b"SBO1";
const READY_MAGIC: [u8; 4] = *b"SBR1";
const ACK_MAGIC: [u8; 4] = *b"SBA1";
const VERSION: u8 = 0x01;
const KEM_ID: u16 = 0x0010;
const KDF_ID: u16 = 0x0001;
const AEAD_ID: u16 = 0x0002;
const ACK_LABEL: &[u8] = b"spl-blob-ack";

/// A validated blob offer header, retained verbatim for HPKE associated data.
#[derive(Debug, Clone, Copy, PartialEq, Eq)]
pub struct Offer {
    pub header: [u8; OFFER_LEN],
    pub sender_fingerprint: [u8; SENDER_FINGERPRINT_LEN],
    pub blob_id: [u8; BLOB_ID_LEN],
    pub ciphertext_len: u64,
}

/// The reason an `SBO1`-prefixed header is malformed.
#[derive(Debug, Clone, Copy, PartialEq, Eq, Error)]
pub enum MalformedOffer {
    #[error("blob offer header must be exactly {OFFER_LEN} bytes")]
    WrongLength,
    #[error("blob offer HPKE suite is unsupported")]
    UnsupportedSuite,
    #[error("blob offer ciphertext length exceeds the 80 MiB cap")]
    CiphertextTooLarge,
}

/// Errors from the blob-wire framing boundary.
///
/// `NotOfferMagic` is intentionally distinct from `MalformedOffer`: a receiver
/// sends `READY(0x01)` only for the latter, because it has already identified an
/// `SBO1` offer attempt.
#[derive(Debug, Clone, Copy, PartialEq, Eq, Error)]
pub enum BlobFrameError {
    #[error("blob offer does not start with SBO1")]
    NotOfferMagic,
    #[error("malformed SBO1 offer: {0}")]
    MalformedOffer(#[from] MalformedOffer),
    #[error("ACK HMAC key was rejected")]
    AckKey,
}

/// Parse and validate an `SBO1` blob offer header without performing I/O.
pub fn parse_offer(header: &[u8]) -> Result<Offer, BlobFrameError> {
    if header.get(..OFFER_MAGIC.len()) != Some(&OFFER_MAGIC) {
        return Err(BlobFrameError::NotOfferMagic);
    }
    if header.len() != OFFER_LEN {
        return Err(MalformedOffer::WrongLength.into());
    }

    let suite: [u8; 7] = checked_offer_range(header, 4..11)?;
    let [
        version,
        kem_high,
        kem_low,
        kdf_high,
        kdf_low,
        aead_high,
        aead_low,
    ] = suite;
    let kem_id = u16::from_be_bytes([kem_high, kem_low]);
    let kdf_id = u16::from_be_bytes([kdf_high, kdf_low]);
    let aead_id = u16::from_be_bytes([aead_high, aead_low]);
    if (version, kem_id, kdf_id, aead_id) != (VERSION, KEM_ID, KDF_ID, AEAD_ID) {
        return Err(MalformedOffer::UnsupportedSuite.into());
    }

    let ciphertext_len = u64::from_be_bytes(checked_offer_range(header, 59..67)?);
    if ciphertext_len > MAX_CIPHERTEXT_LEN {
        return Err(MalformedOffer::CiphertextTooLarge.into());
    }

    let raw_header = checked_offer_range(header, 0..OFFER_LEN)?;
    let sender_fingerprint = checked_offer_range(header, 11..43)?;
    let blob_id = checked_offer_range(header, 43..59)?;

    Ok(Offer {
        header: raw_header,
        sender_fingerprint,
        blob_id,
        ciphertext_len,
    })
}

fn checked_offer_range<const N: usize>(
    header: &[u8],
    range: core::ops::Range<usize>,
) -> Result<[u8; N], BlobFrameError> {
    header
        .get(range)
        .and_then(|bytes| bytes.try_into().ok())
        .ok_or(MalformedOffer::WrongLength.into())
}

/// Construct a byte-exact `SBR1` ready/refuse response.
#[must_use]
pub fn ready(status: u8) -> [u8; READY_LEN] {
    [
        READY_MAGIC[0],
        READY_MAGIC[1],
        READY_MAGIC[2],
        READY_MAGIC[3],
        VERSION,
        status,
    ]
}

/// Construct a byte-exact authenticated `SBA1` acknowledgement.
pub fn ack(
    blob_id: &[u8; BLOB_ID_LEN],
    status: u8,
    k_ack: &[u8],
) -> Result<[u8; ACK_LEN], BlobFrameError> {
    let mut mac = Hmac::<Sha256>::new_from_slice(k_ack).map_err(|_| BlobFrameError::AckKey)?;
    mac.update(ACK_LABEL);
    mac.update(&[status]);
    mac.update(blob_id);
    let tag = mac.finalize().into_bytes();

    let mut frame = [0_u8; ACK_LEN];
    frame[..4].copy_from_slice(&ACK_MAGIC);
    frame[4] = VERSION;
    frame[5] = status;
    frame[6..22].copy_from_slice(blob_id);
    frame[22..].copy_from_slice(&tag[..16]);
    Ok(frame)
}

#[cfg(test)]
mod tests {
    use super::{BlobFrameError, MAX_CIPHERTEXT_LEN, MalformedOffer, ack, parse_offer, ready};

    fn valid_offer() -> [u8; 67] {
        let mut header = [0_u8; 67];
        header[..11].copy_from_slice(&[
            b'S', b'B', b'O', b'1', 0x01, 0x00, 0x10, 0x00, 0x01, 0x00, 0x02,
        ]);
        for (index, byte) in header[11..43].iter_mut().enumerate() {
            *byte = index as u8;
        }
        for (index, byte) in header[43..59].iter_mut().enumerate() {
            *byte = 0xa0 + index as u8;
        }
        header[59..].copy_from_slice(&4097_u64.to_be_bytes());
        header
    }

    #[test]
    fn parses_all_offer_fields_and_retains_the_exact_header() {
        let header = valid_offer();
        let parsed = parse_offer(&header);
        assert!(parsed.is_ok());
        let offer = match parsed {
            Ok(offer) => offer,
            Err(_) => return,
        };

        assert_eq!(offer.header, header);
        assert_eq!(
            offer.sender_fingerprint,
            core::array::from_fn(|index| index as u8)
        );
        assert_eq!(
            offer.blob_id,
            core::array::from_fn(|index| 0xa0 + index as u8)
        );
        assert_eq!(offer.ciphertext_len, 4097);
    }

    #[test]
    fn malformed_sbo1_and_non_sbo1_headers_have_distinct_errors() {
        let mut malformed = valid_offer();
        malformed[4] = 0x02;
        assert!(matches!(
            parse_offer(&malformed),
            Err(BlobFrameError::MalformedOffer(
                MalformedOffer::UnsupportedSuite
            ))
        ));

        let mut wrong_magic = valid_offer();
        wrong_magic[0] = b'X';
        assert!(matches!(
            parse_offer(&wrong_magic),
            Err(BlobFrameError::NotOfferMagic)
        ));
    }

    #[test]
    fn sbo1_prefixed_truncated_offer_is_malformed_not_non_offer() {
        let mut truncated = valid_offer().to_vec();
        let _ = truncated.pop();
        assert!(matches!(
            parse_offer(&truncated),
            Err(BlobFrameError::MalformedOffer(MalformedOffer::WrongLength))
        ));
    }

    #[test]
    fn ciphertext_length_cap_is_strict() {
        let mut at_cap = valid_offer();
        at_cap[59..].copy_from_slice(&MAX_CIPHERTEXT_LEN.to_be_bytes());
        assert!(parse_offer(&at_cap).is_ok());

        let mut over_cap = valid_offer();
        over_cap[59..].copy_from_slice(&(MAX_CIPHERTEXT_LEN + 1).to_be_bytes());
        assert!(matches!(
            parse_offer(&over_cap),
            Err(BlobFrameError::MalformedOffer(
                MalformedOffer::CiphertextTooLarge
            ))
        ));
    }

    #[test]
    fn ready_frame_is_exact() {
        assert_eq!(ready(0x00), [b'S', b'B', b'R', b'1', 0x01, 0x00]);
        assert_eq!(ready(0x01), [b'S', b'B', b'R', b'1', 0x01, 0x01]);
    }

    #[test]
    fn ack_matches_an_independent_hmac_sha256_known_answer() {
        let key: [u8; 32] = core::array::from_fn(|index| index as u8);
        let blob_id: [u8; 16] = core::array::from_fn(|index| 0xa0 + index as u8);
        let result = ack(&blob_id, 0x00, &key);
        assert!(result.is_ok());
        let frame = match result {
            Ok(frame) => frame,
            Err(_) => return,
        };

        assert_eq!(
            frame,
            [
                b'S', b'B', b'A', b'1', 0x01, 0x00, 0xa0, 0xa1, 0xa2, 0xa3, 0xa4, 0xa5, 0xa6, 0xa7,
                0xa8, 0xa9, 0xaa, 0xab, 0xac, 0xad, 0xae, 0xaf, 0x44, 0xba, 0x19, 0x82, 0xe3, 0xbd,
                0x35, 0x1c, 0x22, 0xfc, 0xca, 0x5c, 0x3d, 0x4e, 0x4f, 0x15,
            ]
        );
    }
}
