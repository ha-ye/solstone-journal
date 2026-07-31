// SPDX-License-Identifier: AGPL-3.0-only
// Copyright (c) 2026 sol pbc

//! Pure length-delimited framing for the SPL HPKE command surface.
//!
//! This module performs no I/O. The command dispatcher owns stdin, stdout,
//! stderr, and process exit status.

use p256::{
    PublicKey,
    elliptic_curve::pkcs8::{DecodePrivateKey, DecodePublicKey},
};

use crate::{HpkeError, P256Secret, open_base, seal_base};

/// The fixed-count HPKE operation selected by the command dispatcher.
#[derive(Clone, Copy, Debug, Eq, PartialEq)]
pub enum HpkeCliOperation {
    /// Open a base-mode ciphertext from five framed request fields.
    OpenBase,
    /// Seal a base-mode plaintext from four framed request fields.
    SealBase,
}

/// A non-sensitive error class for the command dispatcher to report.
#[derive(Debug, thiserror::Error, Eq, PartialEq)]
pub enum HpkeCliError {
    /// A framed request was malformed, truncated, or exceeded the field limit.
    #[error("bad input")]
    BadInput,
    /// A complete request did not contain the operation's required field count.
    #[error("bad field count")]
    BadFieldCount,
    /// A PKCS#8 or SPKI P-256 key could not be decoded.
    #[error("bad key")]
    BadKey,
    /// HPKE suite validation, including the encapsulated key, failed.
    #[error("bad suite")]
    BadSuite,
    /// A base-mode open operation failed after request validation.
    #[error("open failed")]
    OpenFailed,
    /// A base-mode seal operation failed after request validation.
    #[error("seal failed")]
    SealFailed,
}

impl HpkeCliError {
    /// Returns the single non-sensitive line the command dispatcher may write.
    pub const fn class(&self) -> &'static str {
        match self {
            Self::BadInput => "bad-input",
            Self::BadFieldCount => "bad-field-count",
            Self::BadKey => "bad-key",
            Self::BadSuite => "bad-suite",
            Self::OpenFailed => "open-failed",
            Self::SealFailed => "seal-failed",
        }
    }
}

const MAX_FIELD_LEN: usize = 96 * 1024 * 1024;

/// Runs one fixed-count, u32-big-endian length-delimited HPKE operation.
///
/// `OpenBase` accepts exactly `(priv_pkcs8_der, info, enc, ct, aad)` and
/// returns one framed plaintext. `SealBase` accepts exactly
/// `(pub_spki_der, info, plaintext, aad)` and returns framed `(enc, ct)`.
/// This function is pure and performs no stdout or stderr I/O.
pub fn run_hpke_framed(operation: HpkeCliOperation, input: &[u8]) -> Result<Vec<u8>, HpkeCliError> {
    let fields = parse_fields(input)?;

    match operation {
        HpkeCliOperation::OpenBase => open_framed(&fields),
        HpkeCliOperation::SealBase => seal_framed(&fields),
    }
}

fn open_framed(fields: &[Vec<u8>]) -> Result<Vec<u8>, HpkeCliError> {
    let [private_key_der, info, enc, ciphertext, aad] = fields else {
        return Err(HpkeCliError::BadFieldCount);
    };
    let recipient_private_key =
        P256Secret::from_pkcs8_der(private_key_der).map_err(|_| HpkeCliError::BadKey)?;
    let plaintext =
        open_base(enc, &recipient_private_key, info, ciphertext, aad).map_err(map_open_error)?;

    frame_fields(&[plaintext.as_slice()])
}

fn seal_framed(fields: &[Vec<u8>]) -> Result<Vec<u8>, HpkeCliError> {
    let [recipient_public_key_der, info, plaintext, aad] = fields else {
        return Err(HpkeCliError::BadFieldCount);
    };
    PublicKey::from_public_key_der(recipient_public_key_der).map_err(|_| HpkeCliError::BadKey)?;
    let sealed =
        seal_base(recipient_public_key_der, info, plaintext, aad).map_err(map_seal_error)?;

    frame_fields(&[sealed.enc.as_slice(), sealed.ciphertext.as_slice()])
}

fn parse_fields(input: &[u8]) -> Result<Vec<Vec<u8>>, HpkeCliError> {
    let mut fields = Vec::new();
    let mut offset = 0_usize;

    while offset < input.len() {
        let length_end = offset
            .checked_add(std::mem::size_of::<u32>())
            .ok_or(HpkeCliError::BadInput)?;
        let length_bytes = input
            .get(offset..length_end)
            .ok_or(HpkeCliError::BadInput)?;
        let length_array: [u8; 4] = length_bytes
            .try_into()
            .map_err(|_| HpkeCliError::BadInput)?;
        let length = usize::try_from(u32::from_be_bytes(length_array))
            .map_err(|_| HpkeCliError::BadInput)?;
        if length > MAX_FIELD_LEN {
            return Err(HpkeCliError::BadInput);
        }

        let field_end = length_end
            .checked_add(length)
            .ok_or(HpkeCliError::BadInput)?;
        let field = input
            .get(length_end..field_end)
            .ok_or(HpkeCliError::BadInput)?;
        fields.push(field.to_vec());
        offset = field_end;
    }

    Ok(fields)
}

fn frame_fields(fields: &[&[u8]]) -> Result<Vec<u8>, HpkeCliError> {
    let mut framed = Vec::new();

    for field in fields {
        if field.len() > MAX_FIELD_LEN {
            return Err(HpkeCliError::BadInput);
        }
        let length = u32::try_from(field.len()).map_err(|_| HpkeCliError::BadInput)?;
        framed.extend_from_slice(&length.to_be_bytes());
        framed.extend_from_slice(field);
    }

    Ok(framed)
}

fn map_open_error(error: HpkeError) -> HpkeCliError {
    match error {
        HpkeError::InvalidRecipientPrivateKey | HpkeError::InvalidRecipientPublicKey => {
            HpkeCliError::BadKey
        }
        HpkeError::InvalidEncapsulatedKey => HpkeCliError::BadSuite,
        HpkeError::InvalidSenderPublicKey | HpkeError::Crypto(_) => HpkeCliError::OpenFailed,
    }
}

fn map_seal_error(error: HpkeError) -> HpkeCliError {
    match error {
        HpkeError::InvalidRecipientPrivateKey
        | HpkeError::InvalidRecipientPublicKey
        | HpkeError::InvalidSenderPublicKey => HpkeCliError::BadKey,
        HpkeError::InvalidEncapsulatedKey => HpkeCliError::BadSuite,
        HpkeError::Crypto(_) => HpkeCliError::SealFailed,
    }
}

#[cfg(test)]
mod tests {
    use p256::elliptic_curve::pkcs8::{EncodePrivateKey, EncodePublicKey};

    use super::{HpkeCliError, HpkeCliOperation, P256Secret, frame_fields, run_hpke_framed};

    fn recipient_private_key() -> Result<P256Secret, HpkeCliError> {
        P256Secret::from_slice(&[0x42; 32]).map_err(|_| HpkeCliError::BadKey)
    }

    #[test]
    fn seal_response_feeds_open_with_exact_plaintext() -> Result<(), HpkeCliError> {
        let recipient_private_key = recipient_private_key()?;
        let private_key_der = recipient_private_key
            .to_pkcs8_der()
            .map_err(|_| HpkeCliError::BadKey)?;
        let public_key_der = recipient_private_key
            .public_key()
            .to_public_key_der()
            .map_err(|_| HpkeCliError::BadKey)?;
        let info = b"0123456789abcdef";
        let plaintext = b"framed SPL HPKE plaintext";
        let aad = b"framed-aad";
        let seal_request = frame_fields(&[public_key_der.as_bytes(), info, plaintext, aad])?;

        let seal_response = run_hpke_framed(HpkeCliOperation::SealBase, &seal_request)?;
        let sealed_fields = super::parse_fields(&seal_response)?;
        let [enc, ciphertext] = sealed_fields.as_slice() else {
            return Err(HpkeCliError::BadFieldCount);
        };
        let open_request = frame_fields(&[
            private_key_der.as_bytes(),
            info,
            enc.as_slice(),
            ciphertext.as_slice(),
            aad,
        ])?;

        let open_response = run_hpke_framed(HpkeCliOperation::OpenBase, &open_request)?;

        assert_eq!(open_response, frame_fields(&[plaintext])?);
        Ok(())
    }

    #[test]
    fn rejects_truncated_and_oversize_fields() {
        let truncated = [0_u8, 0, 0, 1];
        let oversize = [0x06_u8, 0, 0, 1];

        assert_eq!(
            run_hpke_framed(HpkeCliOperation::SealBase, &truncated),
            Err(HpkeCliError::BadInput)
        );
        assert_eq!(
            run_hpke_framed(HpkeCliOperation::SealBase, &oversize),
            Err(HpkeCliError::BadInput)
        );
    }

    #[test]
    fn rejects_wrong_field_count() -> Result<(), HpkeCliError> {
        let request = frame_fields(&[b"one", b"two", b"three"])?;

        assert_eq!(
            run_hpke_framed(HpkeCliOperation::SealBase, &request),
            Err(HpkeCliError::BadFieldCount)
        );
        Ok(())
    }

    #[test]
    fn rejects_malformed_pkcs8_private_key() -> Result<(), HpkeCliError> {
        let request = frame_fields(&[b"not-a-private-key", b"info", b"enc", b"ct", b"aad"])?;

        assert_eq!(
            run_hpke_framed(HpkeCliOperation::OpenBase, &request),
            Err(HpkeCliError::BadKey)
        );
        Ok(())
    }

    #[test]
    fn error_display_and_class_never_echo_input() -> Result<(), HpkeCliError> {
        let secret_input = b"must-not-appear-in-errors";
        let error = match run_hpke_framed(HpkeCliOperation::SealBase, secret_input) {
            Ok(_) => return Err(HpkeCliError::SealFailed),
            Err(error) => error,
        };

        assert_eq!(error.class(), "bad-input");
        assert!(!error.to_string().contains("must-not-appear-in-errors"));
        Ok(())
    }
}
