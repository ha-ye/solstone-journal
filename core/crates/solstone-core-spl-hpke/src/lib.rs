// SPDX-License-Identifier: AGPL-3.0-only
// Copyright (c) 2026 sol pbc

//! RFC 9180 primitives for the SPL home-service conversion.

mod auth;
mod base;
mod blob_frame;
mod cli_framing;

pub use auth::{OpenedHpke, open_auth};
pub use base::{HpkeError, P256Secret, SealedHpke, open_base, seal_base};
pub use blob_frame::{
    ACK_LEN, BLOB_ID_LEN, BlobFrameError, MAX_CIPHERTEXT_LEN, MalformedOffer, OFFER_LEN, Offer,
    READY_LEN, SENDER_FINGERPRINT_LEN, ack, parse_offer, ready,
};
pub use cli_framing::{HpkeCliError, HpkeCliOperation, run_hpke_framed};
