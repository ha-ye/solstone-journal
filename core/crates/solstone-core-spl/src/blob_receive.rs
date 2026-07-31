// SPDX-License-Identifier: AGPL-3.0-only
// Copyright (c) 2026 sol pbc

//! Receive one authenticated browser blob on a split WebSocket transport.
//!
//! This replaces `solstone/think/spl/blob_receiver.py:88-204`. Its externally
//! visible behavior is deliberately narrow: malformed `SBO1` and unavailable,
//! unauthorized, or incomplete ledgers receive `READY(0x01)` before close;
//! non-`SBO1` input receives no bytes; sender-admission saturation emits
//! `admission_saturated` then closes without a `READY`; accepted offers receive
//! `READY(0x00)`; and every later failure closes without an acknowledgement.
//! A duplicate ingested blob receives `ACK(0x01)`, while `ok` and `collision`
//! receive `ACK(0x00)`.
//!
//! The Python source does not emit a health or reason event for normal U2
//! receiver outcomes. This preserves that known U2 health-observability hole:
//! the sole event here is the required `admission_saturated` accounting event.

use std::{
    future::Future,
    pin::Pin,
    sync::Arc,
    time::Duration,
};

use bytes::Bytes;
use serde_json::{Value, json};
use solstone_core_spl_hpke::{BlobFrameError, OFFER_LEN, P256Secret, ack, parse_offer, ready};
use thiserror::Error;

use crate::{
    BlobAdmissionGate, BufferedWsReader, PreparedAuthenticatedBlob, ValidatedBlobArchive,
    WsByteSink, WsByteSource, prepare_authenticated_blob,
};

const ENC_LEN: usize = 65;
const MAX_SENDER_SPKI_DER_BYTES: usize = 16 * 1024;
const MAX_SENDER_SPKI_HEX_BYTES: usize = MAX_SENDER_SPKI_DER_BYTES * 2;

/// Configurable timeout and progress thresholds for one blob transfer.
#[derive(Clone, Copy, Debug, Eq, PartialEq)]
pub struct BlobReceiveTiming {
    /// Maximum time to receive the fixed 67-byte offer header.
    pub offer_deadline: Duration,
    /// Maximum time to receive the 65-byte HPKE encapsulated key.
    pub enc_deadline: Duration,
    /// Maximum time to receive the ciphertext.
    pub ciphertext_deadline: Duration,
    /// Rolling window used to enforce ciphertext transfer progress.
    pub ciphertext_progress_window: Duration,
    /// Minimum ciphertext bytes required in each rolling progress window.
    pub ciphertext_min_bytes_per_window: usize,
}

impl Default for BlobReceiveTiming {
    fn default() -> Self {
        Self {
            offer_deadline: Duration::from_secs(5),
            enc_deadline: Duration::from_secs(5),
            ciphertext_deadline: Duration::from_secs(300),
            ciphertext_progress_window: Duration::from_secs(10),
            ciphertext_min_bytes_per_window: 64 * 1024,
        }
    }
}

/// Immutable, owned dependencies held by the U2 blob-receive boundary.
///
/// These trait objects are constructed once by the service and transferred to
/// the relay client. The authorization ledger itself performs its required
/// freshness check on every lookup; putting it in an [`Arc`] is not a cache.
pub struct BlobDeps {
    /// Fresh, fail-closed browser authorization ledger reader.
    pub ledger: Arc<dyn BrowserLedger>,
    /// Load-only home upload HPKE key source.
    pub upload_key: Arc<dyn UploadKeySource>,
    /// The only side-effecting observer-ingest seam after archive validation.
    pub ingest: Arc<dyn BlobIngest>,
}

impl BlobDeps {
    /// Builds owned U2 dependencies for relay-client construction.
    #[must_use]
    pub fn new(
        ledger: Arc<dyn BrowserLedger>,
        upload_key: Arc<dyn UploadKeySource>,
        ingest: Arc<dyn BlobIngest>,
    ) -> Self {
        Self {
            ledger,
            upload_key,
            ingest,
        }
    }
}

/// One freshly read browser authorization record.
///
/// `None` from [`BrowserLedger::lookup`] means no browser authorizes that
/// fingerprint. A present row deliberately retains independently-null fields:
/// Python writes the observer handle in a second pass after registration.
#[derive(Clone, Debug, Eq, PartialEq)]
pub struct LedgerRow {
    /// Hex-encoded browser sender SPKI, if the registration has written it.
    pub pubkey_spki_hex: Option<String>,
    /// Observer-side ingest handle, attached by Python after registration.
    pub observer_handle: Option<String>,
}

/// Class-only failure from an authorization ledger lookup.
#[derive(Clone, Copy, Debug, Eq, Error, PartialEq)]
pub enum LedgerError {
    /// The backing ledger could not be statted or read.
    #[error("browser authorization ledger unavailable")]
    Unavailable,
    /// The backing ledger was not a valid JSON list.
    #[error("browser authorization ledger malformed")]
    Malformed,
}

/// Fresh, fail-closed browser ledger access.
pub trait BrowserLedger: Send + Sync {
    /// Re-checks the backing file mtime and reads any changed content on every
    /// lookup. `None` is absent or non-browser; a row with empty fields is
    /// intentionally distinct and denotes an incomplete Python registration.
    fn lookup(&self, fingerprint: &str) -> Result<Option<LedgerRow>, LedgerError>;
}

/// Class-only failure from load-only upload-key access.
#[derive(Clone, Copy, Debug, Eq, Error, PartialEq)]
pub enum KeyError {
    /// The configured upload key could not be read.
    #[error("home upload private key unavailable")]
    Unavailable,
    /// The configured upload key was not an unencrypted P-256 PKCS#8 PEM key.
    #[error("home upload private key invalid")]
    Invalid,
}

/// Load-only access to the existing home upload HPKE private key.
///
/// This seam intentionally has no generation operation. Browser pairing owns
/// the distinct load-or-generate path; receiving a blob must fail if its key is
/// absent rather than minting an incompatible replacement.
pub trait UploadKeySource: Send + Sync {
    /// Loads the provisioned P-256 upload key without creating any file.
    fn private_key(&self) -> Result<P256Secret, KeyError>;
}

/// The stable response categories returned by observer ingestion.
#[derive(Clone, Copy, Debug, Eq, PartialEq)]
pub enum BlobIngestStatus {
    /// The observer accepted a new blob.
    Ok,
    /// The observer had already ingested this blob.
    Duplicate,
    /// The observer accepted a distinct blob ID collision record.
    Collision,
}

/// A class-only failure from the ingestion boundary.
///
/// It intentionally carries no remote response, URL, token, or plaintext.
#[derive(Clone, Copy, Debug, Eq, Error, PartialEq)]
pub enum BlobIngestError {
    /// The observer ingest endpoint failed before it returned a response.
    #[error("blob ingestion failed")]
    Failed,
    /// Convey returned a status outside `ok`, `duplicate`, or `collision`.
    ///
    /// This is deliberately an error rather than a default acknowledgement:
    /// the receiver closes without an ACK just as Python's `_ack_status` does.
    #[error("blob ingestion returned an unexpected status")]
    UnexpectedStatus,
}

/// The object-safe asynchronous observer-ingestion seam.
pub trait BlobIngest: Send + Sync {
    /// Ingests an already authenticated and archive-validated browser blob.
    fn ingest<'a>(
        &'a self,
        archive: &'a ValidatedBlobArchive,
        observer_handle: &'a str,
    ) -> BlobIngestFuture<'a>;
}

/// The boxed, sendable future returned by [`BlobIngest`].
pub type BlobIngestFuture<'a> =
    Pin<Box<dyn Future<Output = Result<BlobIngestStatus, BlobIngestError>> + Send + 'a>>;

/// Maps an observer ingest response onto the closed acknowledgement vocabulary.
///
/// Concrete convey adapters must use this rather than defaulting unknown JSON
/// to [`BlobIngestStatus::Ok`]. Keeping the unknown case as an error preserves
/// the no-ack failure path in Python's `_ack_status`.
pub fn parse_convey_ingest_status(response: &Value) -> Result<BlobIngestStatus, BlobIngestError> {
    match response.get("status").and_then(Value::as_str) {
        Some("ok") => Ok(BlobIngestStatus::Ok),
        Some("duplicate") => Ok(BlobIngestStatus::Duplicate),
        Some("collision") => Ok(BlobIngestStatus::Collision),
        _ => Err(BlobIngestError::UnexpectedStatus),
    }
}

/// Synchronous Callosum emission needed for the admission-saturation event.
pub trait CallosumEmit: Send + Sync {
    /// Emits one named event with a JSON object payload.
    fn emit(&self, event: &'static str, payload: Value);
}

/// Internal receiver failures that could not be represented by wire behavior.
///
/// All variants are class-only: they never contain wire input, keys, observer
/// handles, tokens, URLs, ciphertext, or plaintext.
#[derive(Clone, Copy, Debug, Eq, Error, PartialEq)]
pub enum BlobError {
    /// The split WebSocket write half rejected a response frame.
    #[error("blob receiver response write failed")]
    ResponseWrite,
    /// The split WebSocket write half rejected a close operation.
    #[error("blob receiver close failed")]
    Close,
    /// The ledger's purported sender SPKI was not bounded, even-length hex.
    #[error("blob sender public key encoding was invalid")]
    SenderPublicKeyEncoding,
    /// An impossible checked ciphertext-length conversion failed.
    #[error("blob ciphertext length was not representable")]
    CiphertextLength,
    /// The fixed acknowledgement frame could not be constructed.
    #[error("blob acknowledgement construction failed")]
    Acknowledgement,
}

/// Receives, authenticates, validates, and ingests one browser blob offer.
///
/// `R` is the immutable read half and `W` the separate write half of the
/// WebSocket. The U4 relay pipes both directions concurrently, so this split
/// is a transport property rather than a reader escape hatch. All terminal
/// protocol failures attempt to close `sink`; only a write/close failure is
/// returned as [`BlobError`].
pub async fn receive_blob<R: WsByteSource, W: WsByteSink>(
    reader: &mut BufferedWsReader<R>,
    sink: &mut W,
    deps: &BlobDeps<'_>,
    gate: &BlobAdmissionGate,
    emit: &dyn CallosumEmit,
) -> Result<(), BlobError> {
    let header = match reader
        .read_exactly_bounded(OFFER_LEN, deps.timing.offer_deadline)
        .await
    {
        Ok(header) => header,
        Err(_) => return close_sink(sink).await,
    };

    let offer = match parse_offer(&header) {
        Ok(offer) => offer,
        Err(BlobFrameError::MalformedOffer(_)) => return send_ready_then_close(sink, 0x01).await,
        Err(BlobFrameError::NotOfferMagic) => return close_sink(sink).await,
        Err(BlobFrameError::AckKey) => return close_sink(sink).await,
    };

    let (sender_public_key_hex, observer_handle) =
        match deps.ledger.lookup_browser(&offer.sender_fingerprint) {
            BrowserLedgerLookup::LedgerUnavailable | BrowserLedgerLookup::NotAuthorized => {
                return send_ready_then_close(sink, 0x01).await;
            }
            BrowserLedgerLookup::Incomplete => return send_ready_then_close(sink, 0x01).await,
            BrowserLedgerLookup::Authorized {
                pubkey_spki,
                observer_handle,
            } => (pubkey_spki, observer_handle),
        };

    let sender = sender_fingerprint_key(&offer.sender_fingerprint);
    if !gate.try_acquire_sender(&sender) {
        emit.emit(
            "admission_saturated",
            json!({
                "reason": "relay_admission_saturated",
                "count": gate.saturated_count(),
            }),
        );
        return close_sink(sink).await;
    }

    let result = receive_permitted(
        reader,
        sink,
        deps,
        offer,
        &sender_public_key_hex,
        &observer_handle,
    )
    .await;
    gate.release_sender(&sender);
    result
}

async fn receive_permitted<R: WsByteSource, W: WsByteSink>(
    reader: &mut BufferedWsReader<R>,
    sink: &mut W,
    deps: &BlobDeps<'_>,
    offer: solstone_core_spl_hpke::Offer,
    sender_public_key_hex: &str,
    observer_handle: &str,
) -> Result<(), BlobError> {
    if let Err(error) = send_ready(sink, 0x00).await {
        return close_after_error(sink, error).await;
    }

    let encapsulated_key = match reader
        .read_exactly_bounded(ENC_LEN, deps.timing.enc_deadline)
        .await
    {
        Ok(encapsulated_key) => encapsulated_key,
        Err(_) => return close_sink(sink).await,
    };
    let ciphertext_len = match usize::try_from(offer.ciphertext_len) {
        Ok(ciphertext_len) => ciphertext_len,
        Err(_) => return close_after_error(sink, BlobError::CiphertextLength).await,
    };
    let ciphertext = match reader
        .read_exactly_progress(
            ciphertext_len,
            deps.timing.ciphertext_deadline,
            deps.timing.ciphertext_progress_window,
            deps.timing.ciphertext_min_bytes_per_window,
        )
        .await
    {
        Ok(ciphertext) => ciphertext,
        Err(_) => return close_sink(sink).await,
    };

    let sender_public_key_der = match decode_sender_public_key(sender_public_key_hex) {
        Ok(sender_public_key_der) => sender_public_key_der,
        Err(error) => {
            close_sink(sink).await?;
            return Err(error);
        }
    };
    let prepared = match prepare_authenticated_blob(
        offer,
        &encapsulated_key,
        &ciphertext,
        deps.recipient_private_key,
        &sender_public_key_der,
        &deps.instance_id,
    ) {
        Ok(prepared) => prepared,
        Err(_) => return close_sink(sink).await,
    };

    let status = match deps
        .ingestor
        .ingest(&prepared.archive, observer_handle)
        .await
    {
        Ok(status) => status,
        Err(_) => return close_sink(sink).await,
    };
    send_ack_then_close(sink, &prepared, status).await
}

async fn send_ready<W: WsByteSink>(sink: &mut W, status: u8) -> Result<(), BlobError> {
    sink.send(Bytes::copy_from_slice(&ready(status)))
        .await
        .map_err(|_| BlobError::ResponseWrite)
}

async fn send_ready_then_close<W: WsByteSink>(sink: &mut W, status: u8) -> Result<(), BlobError> {
    let sent = send_ready(sink, status).await;
    let closed = close_sink(sink).await;
    sent.and(closed)
}

async fn send_ack_then_close<W: WsByteSink>(
    sink: &mut W,
    prepared: &PreparedAuthenticatedBlob,
    status: BlobIngestStatus,
) -> Result<(), BlobError> {
    let status = match status {
        BlobIngestStatus::Ok | BlobIngestStatus::Collision => 0x00,
        BlobIngestStatus::Duplicate => 0x01,
    };
    let frame = match ack(
        &prepared.offer.blob_id,
        status,
        &prepared.acknowledgement_key,
    ) {
        Ok(frame) => frame,
        Err(_) => return close_after_error(sink, BlobError::Acknowledgement).await,
    };
    let sent = sink
        .send(Bytes::copy_from_slice(&frame))
        .await
        .map_err(|_| BlobError::ResponseWrite);
    let closed = close_sink(sink).await;
    sent.and(closed)
}

async fn close_sink<W: WsByteSink>(sink: &mut W) -> Result<(), BlobError> {
    sink.close().await.map_err(|_| BlobError::Close)
}

async fn close_after_error<W: WsByteSink>(sink: &mut W, error: BlobError) -> Result<(), BlobError> {
    match close_sink(sink).await {
        Ok(()) => Err(error),
        Err(close_error) => Err(close_error),
    }
}

fn decode_sender_public_key(input: &str) -> Result<Vec<u8>, BlobError> {
    let input_bytes = input.as_bytes();
    if input_bytes.len() > MAX_SENDER_SPKI_HEX_BYTES || !input_bytes.len().is_multiple_of(2) {
        return Err(BlobError::SenderPublicKeyEncoding);
    }

    let mut output = Vec::with_capacity(input_bytes.len() / 2);
    for pair in input_bytes.chunks_exact(2) {
        let high = pair.first().copied().and_then(hex_nibble);
        let low = pair.get(1).copied().and_then(hex_nibble);
        match (high, low) {
            (Some(high), Some(low)) => output.push((high << 4) | low),
            _ => return Err(BlobError::SenderPublicKeyEncoding),
        }
    }
    if output.is_empty() {
        return Err(BlobError::SenderPublicKeyEncoding);
    }
    Ok(output)
}

fn hex_nibble(byte: u8) -> Option<u8> {
    match byte {
        b'0'..=b'9' => Some(byte - b'0'),
        b'a'..=b'f' => Some(byte - b'a' + 10),
        b'A'..=b'F' => Some(byte - b'A' + 10),
        _ => None,
    }
}

fn sender_fingerprint_key(sender_fingerprint: &[u8; 32]) -> String {
    let mut fingerprint = String::with_capacity("sha256:".len() + sender_fingerprint.len() * 2);
    fingerprint.push_str("sha256:");
    for byte in sender_fingerprint {
        fingerprint.push(char::from(lowercase_hex_digit(byte >> 4)));
        fingerprint.push(char::from(lowercase_hex_digit(byte & 0x0f)));
    }
    fingerprint
}

fn lowercase_hex_digit(nibble: u8) -> u8 {
    match nibble {
        0..=9 => b'0' + nibble,
        10..=15 => b'a' + (nibble - 10),
        _ => b'0',
    }
}

#[cfg(test)]
mod tests {
    use std::{
        collections::VecDeque,
        error::Error,
        fs, future,
        io::Cursor,
        path::PathBuf,
        sync::{
            Mutex,
            atomic::{AtomicUsize, Ordering},
        },
        time::{SystemTime, UNIX_EPOCH},
    };

    use bytes::Bytes;
    use flate2::{Compression, write::GzEncoder};
    use hpke::{
        Deserializable, OpModeS, Serializable,
        aead::AesGcm256,
        kdf::HkdfSha256,
        kem::{DhP256HkdfSha256, Kem as KemTrait},
        setup_sender,
    };
    use p256::elliptic_curve::{pkcs8::EncodePublicKey, sec1::ToSec1Point};
    use serde_json::{Value, json};
    use solstone_core_spl_hpke::{Offer, P256Secret, ack, parse_offer};
    use tar::{Builder, Header};

    use super::{
        BlobDeps, BlobIngest, BlobIngestError, BlobIngestFuture, BlobIngestStatus, CallosumEmit,
        receive_blob,
    };
    use crate::{
        AuthorizedClientLedger, BlobAdmissionGate, BufferedWsReader, ValidatedBlobArchive,
        WsByteSink, WsByteSource, WsClosed,
    };

    static NEXT_DIRECTORY: AtomicUsize = AtomicUsize::new(0);

    type SuiteKem = DhP256HkdfSha256;

    struct AuthenticatedTransfer {
        offer: Offer,
        encapsulated_key: Vec<u8>,
        ciphertext: Vec<u8>,
        acknowledgement_key: [u8; 32],
        sender_public_key_hex: String,
    }

    #[derive(Default)]
    struct Frames {
        frames: VecDeque<Bytes>,
    }

    impl Frames {
        fn with_frames(frames: impl IntoIterator<Item = Bytes>) -> Self {
            Self {
                frames: frames.into_iter().collect(),
            }
        }
    }

    impl WsByteSource for Frames {
        fn next_message(
            &mut self,
        ) -> impl future::Future<Output = Result<Option<Bytes>, WsClosed>> + Send {
            future::ready(Ok(self.frames.pop_front()))
        }
    }

    #[derive(Default)]
    struct Sink {
        sent: Vec<Bytes>,
        closes: usize,
    }

    impl WsByteSink for Sink {
        fn send(
            &mut self,
            bytes: Bytes,
        ) -> impl future::Future<Output = Result<(), WsClosed>> + Send {
            self.sent.push(bytes);
            future::ready(Ok(()))
        }

        fn close(&mut self) -> impl future::Future<Output = Result<(), WsClosed>> + Send {
            self.closes = self.closes.saturating_add(1);
            future::ready(Ok(()))
        }
    }

    #[derive(Default)]
    struct Emitter(Mutex<Vec<(String, Value)>>);

    impl CallosumEmit for Emitter {
        fn emit(&self, event: &'static str, payload: Value) {
            let mut events = match self.0.lock() {
                Ok(events) => events,
                Err(poisoned) => poisoned.into_inner(),
            };
            events.push((event.to_owned(), payload));
        }
    }

    struct UnusedIngest;

    impl BlobIngest for UnusedIngest {
        fn ingest<'a>(
            &'a self,
            _archive: &'a ValidatedBlobArchive,
            _observer_handle: &'a str,
        ) -> BlobIngestFuture<'a> {
            Box::pin(future::ready(Err(BlobIngestError::Failed)))
        }
    }

    struct StatusIngest {
        status: BlobIngestStatus,
    }

    impl BlobIngest for StatusIngest {
        fn ingest<'a>(
            &'a self,
            _archive: &'a ValidatedBlobArchive,
            _observer_handle: &'a str,
        ) -> BlobIngestFuture<'a> {
            Box::pin(future::ready(Ok(self.status)))
        }
    }

    struct TestJournal {
        root: PathBuf,
    }

    impl TestJournal {
        fn create() -> Result<Self, Box<dyn Error>> {
            let sequence = NEXT_DIRECTORY.fetch_add(1, Ordering::Relaxed);
            let timestamp = SystemTime::now().duration_since(UNIX_EPOCH)?.as_nanos();
            let root = std::env::temp_dir().join(format!(
                "solstone-spl-blob-receive-{}-{timestamp}-{sequence}",
                std::process::id()
            ));
            fs::create_dir_all(root.join("link"))?;
            Ok(Self { root })
        }

        fn write_ledger(&self, body: &str) -> Result<(), Box<dyn Error>> {
            fs::write(self.root.join("link").join("authorized_clients.json"), body)?;
            Ok(())
        }
    }

    impl Drop for TestJournal {
        fn drop(&mut self) {
            let _ = fs::remove_dir_all(&self.root);
        }
    }

    #[tokio::test]
    async fn malformed_sbo1_and_nonmagic_offers_have_distinct_wire_shapes()
    -> Result<(), Box<dyn Error>> {
        let journal = TestJournal::create()?;
        let ledger = AuthorizedClientLedger::new(&journal.root);
        let private_key = recipient_private_key()?;
        let ingestor = UnusedIngest;
        let deps = BlobDeps::new(&ledger, &private_key, [0_u8; 16], &ingestor);
        let gate = BlobAdmissionGate::default();
        let emitter = Emitter::default();

        let mut malformed_header = valid_offer_header(0);
        malformed_header[4] = 0x02;
        let mut malformed_reader = BufferedWsReader::new(Frames::with_frames([Bytes::from(
            malformed_header.to_vec(),
        )]));
        let mut malformed_sink = Sink::default();
        receive_blob(
            &mut malformed_reader,
            &mut malformed_sink,
            &deps,
            &gate,
            &emitter,
        )
        .await?;
        assert_eq!(malformed_sink.sent, [Bytes::from_static(b"SBR1\x01\x01")]);
        assert_eq!(malformed_sink.closes, 1);

        let mut nonmagic = valid_offer_header(0);
        nonmagic[0] = b'X';
        let mut nonmagic_reader =
            BufferedWsReader::new(Frames::with_frames([Bytes::from(nonmagic.to_vec())]));
        let mut nonmagic_sink = Sink::default();
        receive_blob(
            &mut nonmagic_reader,
            &mut nonmagic_sink,
            &deps,
            &gate,
            &emitter,
        )
        .await?;
        assert!(nonmagic_sink.sent.is_empty());
        assert_eq!(nonmagic_sink.closes, 1);
        Ok(())
    }

    #[tokio::test]
    async fn unavailable_and_incomplete_ledgers_refuse_with_ready() -> Result<(), Box<dyn Error>> {
        let journal = TestJournal::create()?;
        let ledger = AuthorizedClientLedger::new(&journal.root);
        let private_key = recipient_private_key()?;
        let ingestor = UnusedIngest;
        let deps = BlobDeps::new(&ledger, &private_key, [0_u8; 16], &ingestor);
        let gate = BlobAdmissionGate::default();
        let emitter = Emitter::default();

        let mut unavailable_reader = BufferedWsReader::new(Frames::with_frames([Bytes::from(
            valid_offer_header(0).to_vec(),
        )]));
        let mut unavailable_sink = Sink::default();
        receive_blob(
            &mut unavailable_reader,
            &mut unavailable_sink,
            &deps,
            &gate,
            &emitter,
        )
        .await?;
        assert_eq!(unavailable_sink.sent, [Bytes::from_static(b"SBR1\x01\x01")]);

        journal.write_ledger(&browser_entry(None))?;
        let mut incomplete_reader = BufferedWsReader::new(Frames::with_frames([Bytes::from(
            valid_offer_header(0).to_vec(),
        )]));
        let mut incomplete_sink = Sink::default();
        receive_blob(
            &mut incomplete_reader,
            &mut incomplete_sink,
            &deps,
            &gate,
            &emitter,
        )
        .await?;
        assert_eq!(incomplete_sink.sent, [Bytes::from_static(b"SBR1\x01\x01")]);
        assert_eq!(incomplete_sink.closes, 1);
        Ok(())
    }

    #[tokio::test]
    async fn sender_saturation_emits_and_writes_no_wire_bytes() -> Result<(), Box<dyn Error>> {
        let journal = TestJournal::create()?;
        journal.write_ledger(&browser_entry(Some("observer-handle")))?;
        let ledger = AuthorizedClientLedger::new(&journal.root);
        let private_key = recipient_private_key()?;
        let ingestor = UnusedIngest;
        let deps = BlobDeps::new(&ledger, &private_key, [0_u8; 16], &ingestor);
        let gate = BlobAdmissionGate::new(2, 1);
        let sender = sender_key_for_test();
        assert!(gate.try_acquire_sender(&sender));
        let emitter = Emitter::default();
        let mut reader = BufferedWsReader::new(Frames::with_frames([Bytes::from(
            valid_offer_header(0).to_vec(),
        )]));
        let mut sink = Sink::default();

        receive_blob(&mut reader, &mut sink, &deps, &gate, &emitter).await?;

        assert!(sink.sent.is_empty());
        assert_eq!(sink.closes, 1);
        let events = match emitter.0.lock() {
            Ok(events) => events,
            Err(poisoned) => poisoned.into_inner(),
        };
        assert_eq!(
            events.as_slice(),
            [(
                "admission_saturated".to_owned(),
                json!({
                    "reason": "relay_admission_saturated",
                    "count": 1,
                })
            )]
        );
        drop(events);
        gate.release_sender(&sender);
        Ok(())
    }

    #[tokio::test]
    async fn permitted_short_read_sends_only_ready_and_releases_sender_slot()
    -> Result<(), Box<dyn Error>> {
        let journal = TestJournal::create()?;
        journal.write_ledger(&browser_entry(Some("observer-handle")))?;
        let ledger = AuthorizedClientLedger::new(&journal.root);
        let private_key = recipient_private_key()?;
        let ingestor = UnusedIngest;
        let deps = BlobDeps::new(&ledger, &private_key, [0_u8; 16], &ingestor);
        let gate = BlobAdmissionGate::new(2, 1);
        let emitter = Emitter::default();
        let mut reader = BufferedWsReader::new(Frames::with_frames([Bytes::from(
            valid_offer_header(1).to_vec(),
        )]));
        let mut sink = Sink::default();

        receive_blob(&mut reader, &mut sink, &deps, &gate, &emitter).await?;

        assert_eq!(sink.sent, [Bytes::from_static(b"SBR1\x01\x00")]);
        assert_eq!(sink.closes, 1);
        assert_eq!(gate.sender_count(&sender_key_for_test()), 0);
        Ok(())
    }

    #[tokio::test]
    async fn authenticated_ingest_statuses_send_exact_acks_and_close() -> Result<(), Box<dyn Error>>
    {
        let recipient_private_key = recipient_private_key()?;
        let transfer = authenticated_transfer(&recipient_private_key)?;

        for (ingest_status, acknowledgement_status) in [
            (BlobIngestStatus::Ok, 0x00),
            (BlobIngestStatus::Collision, 0x00),
            (BlobIngestStatus::Duplicate, 0x01),
        ] {
            let journal = TestJournal::create()?;
            journal.write_ledger(&browser_entry_with_sender_key(
                Some("observer-handle"),
                &transfer.sender_public_key_hex,
            ))?;
            let ledger = AuthorizedClientLedger::new(&journal.root);
            let ingestor = StatusIngest {
                status: ingest_status,
            };
            let deps = BlobDeps::new(&ledger, &recipient_private_key, [0_u8; 16], &ingestor);
            let gate = BlobAdmissionGate::new(2, 1);
            let emitter = Emitter::default();
            let expected_acknowledgement = ack(
                &transfer.offer.blob_id,
                acknowledgement_status,
                &transfer.acknowledgement_key,
            )?;
            let mut reader = BufferedWsReader::new(Frames::with_frames([
                Bytes::copy_from_slice(&transfer.offer.header),
                Bytes::copy_from_slice(&transfer.encapsulated_key),
                Bytes::copy_from_slice(&transfer.ciphertext),
            ]));
            let mut sink = Sink::default();

            receive_blob(&mut reader, &mut sink, &deps, &gate, &emitter).await?;

            assert_eq!(
                sink.sent,
                [
                    Bytes::from_static(b"SBR1\x01\x00"),
                    Bytes::copy_from_slice(&expected_acknowledgement),
                ]
            );
            assert_eq!(sink.closes, 1);
            assert_eq!(gate.sender_count(&sender_key_for_test()), 0);
        }
        Ok(())
    }

    fn recipient_private_key() -> Result<P256Secret, Box<dyn Error>> {
        P256Secret::from_slice(&[0x42; 32]).map_err(Into::into)
    }

    fn valid_offer_header(ciphertext_len: u64) -> [u8; 67] {
        let mut header = [0_u8; 67];
        header[..11].copy_from_slice(&[
            b'S', b'B', b'O', b'1', 0x01, 0x00, 0x10, 0x00, 0x01, 0x00, 0x02,
        ]);
        header[11..43].fill(0xa5);
        header[43..59].fill(0x44);
        header[59..67].copy_from_slice(&ciphertext_len.to_be_bytes());
        header
    }

    fn browser_entry(observer_handle: Option<&str>) -> String {
        browser_entry_with_sender_key(observer_handle, "3000")
    }

    fn browser_entry_with_sender_key(
        observer_handle: Option<&str>,
        sender_public_key_hex: &str,
    ) -> String {
        let observer_handle = match observer_handle {
            Some(value) => format!("\"{value}\""),
            None => "null".to_owned(),
        };
        format!(
            "[{{\"fingerprint\":\"{}\",\"kind\":\"browser\",\"pubkey_spki\":\"{sender_public_key_hex}\",\"observer_handle\":{observer_handle}}}]",
            sender_key_for_test(),
        )
    }

    fn sender_key_for_test() -> String {
        format!("sha256:{}", "a5".repeat(32))
    }

    fn authenticated_transfer(
        recipient_private_key: &P256Secret,
    ) -> Result<AuthenticatedTransfer, Box<dyn Error>> {
        let sender_private_key = P256Secret::from_slice(&[0x43; 32])?;
        let sender_public_key_der = sender_private_key
            .public_key()
            .to_public_key_der()?
            .as_bytes()
            .to_vec();
        let archive = valid_archive()?;
        let ciphertext_len = u64::try_from(archive.len())?
            .checked_add(16)
            .ok_or_else(|| std::io::Error::other("test ciphertext length overflow"))?;
        let offer = parse_offer(&valid_offer_header(ciphertext_len))?;
        let mut info = Vec::with_capacity(b"spl-blob-v1".len() + 16 + 32);
        info.extend_from_slice(b"spl-blob-v1");
        info.extend_from_slice(&[0_u8; 16]);
        info.extend_from_slice(&offer.sender_fingerprint);

        let recipient_public_key = hpke_public_key(recipient_private_key)?;
        let sender_hpke_private_key = hpke_private_key(&sender_private_key)?;
        let sender_hpke_public_key = hpke_public_key_from_private_key(&sender_hpke_private_key)?;
        let (encapsulated_key, mut sender_context) = setup_sender::<AesGcm256, HkdfSha256, SuiteKem>(
            &OpModeS::Auth((sender_hpke_private_key, sender_hpke_public_key)),
            &recipient_public_key,
            &info,
        )?;
        let ciphertext = sender_context.seal(&archive, &offer.header)?;
        let mut acknowledgement_key = [0_u8; 32];
        sender_context.export(b"spl-blob-ack-v1", &mut acknowledgement_key)?;

        Ok(AuthenticatedTransfer {
            offer,
            encapsulated_key: encapsulated_key.to_bytes().to_vec(),
            ciphertext,
            acknowledgement_key,
            sender_public_key_hex: hex_encode(&sender_public_key_der),
        })
    }

    fn valid_archive() -> Result<Vec<u8>, Box<dyn Error>> {
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
        header.set_size(u64::try_from(bytes.len()).map_err(std::io::Error::other)?);
        header.set_mode(0o600);
        header.set_cksum();
        builder.append_data(&mut header, name, Cursor::new(bytes))
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

    fn hex_encode(bytes: &[u8]) -> String {
        bytes.iter().map(|byte| format!("{byte:02x}")).collect()
    }
}
