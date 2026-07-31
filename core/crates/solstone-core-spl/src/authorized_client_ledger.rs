// SPDX-License-Identifier: AGPL-3.0-only
// Copyright (c) 2026 sol pbc

//! Fresh, fail-closed browser authorization lookups.

use std::fs;
use std::path::{Path, PathBuf};

use crate::{
    AuthorizedClients, BrowserLedger, LedgerError, LedgerRow, LedgerStatus,
    parse_authorized_clients,
};

/// An owned result from a browser authorization lookup.
///
/// This owns the authorization material so a lookup can read a fresh file for
/// each call rather than retaining a last-good ledger in memory.
#[derive(Clone, Debug, Eq, PartialEq)]
pub enum BrowserLedgerLookup {
    /// The ledger could not be statted, read, or parsed as a JSON list.
    LedgerUnavailable,
    /// No browser authorization matched the sender fingerprint.
    NotAuthorized,
    /// A browser entry matched but lacks required upload material.
    Incomplete,
    /// The matching browser entry has the upload material required by SPL.
    Authorized {
        /// Hex-encoded sender SPKI.
        pubkey_spki: String,
        /// Observer-side upload handle.
        observer_handle: String,
    },
}

/// Read-only, fail-closed access to `<journal_root>/link/authorized_clients.json`.
///
/// Delegated U2/C1s contract: every [`Self::lookup_browser`] call stats the
/// ledger and reads it afresh, so a Python writer's completed record is visible
/// in the same process without a restart or sleep. A missing, unreadable,
/// malformed, or non-list file has no retained authorization. This follows the
/// mtime reload and fail-closed behavior in
/// `solstone/think/link/auth.py:87-105,248-293`.
#[derive(Clone, Debug, Eq, PartialEq)]
pub struct AuthorizedClientLedger {
    path: PathBuf,
}

impl AuthorizedClientLedger {
    /// Creates a reader rooted at a journal directory.
    #[must_use]
    pub fn new(journal_root: impl AsRef<Path>) -> Self {
        Self {
            path: journal_root
                .as_ref()
                .join("link")
                .join("authorized_clients.json"),
        }
    }

    /// Resolves a browser sender's authorization from a freshly read ledger.
    #[must_use]
    pub fn lookup_browser(&self, sender_fingerprint: &[u8; 32]) -> BrowserLedgerLookup {
        let fingerprint = fingerprint_key(sender_fingerprint);
        match self.lookup(&fingerprint) {
            Err(_) => BrowserLedgerLookup::LedgerUnavailable,
            Ok(None) => BrowserLedgerLookup::NotAuthorized,
            Ok(Some(row)) => match (
                row.pubkey_spki_hex.filter(|value| !value.is_empty()),
                row.observer_handle.filter(|value| !value.is_empty()),
            ) {
                (Some(pubkey_spki), Some(observer_handle)) => BrowserLedgerLookup::Authorized {
                    pubkey_spki,
                    observer_handle,
                },
                _ => BrowserLedgerLookup::Incomplete,
            },
        }
    }

    fn read_fresh(&self) -> Result<AuthorizedClients, LedgerError> {
        // The Python pairing process is the only writer. Stat before every
        // lookup, then read from disk instead of retaining a last-good cache:
        // a write made between browser offers is visible immediately, and a
        // disappeared or unreadable file revokes authorization fail-closed.
        fs::metadata(&self.path)
            .and_then(|metadata| metadata.modified())
            .map_err(|_| LedgerError::Unavailable)?;
        let bytes = fs::read(&self.path).map_err(|_| LedgerError::Unavailable)?;
        let ledger = parse_authorized_clients(&bytes);
        if ledger.status() == LedgerStatus::Unavailable {
            return Err(LedgerError::Malformed);
        }
        Ok(ledger)
    }
}

impl BrowserLedger for AuthorizedClientLedger {
    fn lookup(&self, fingerprint: &str) -> Result<Option<LedgerRow>, LedgerError> {
        let ledger = self.read_fresh()?;
        let Some(entry) = ledger.entries().get(fingerprint) else {
            return Ok(None);
        };
        if entry.kind != "browser" {
            return Ok(None);
        }

        Ok(Some(LedgerRow {
            pubkey_spki_hex: entry.pubkey_spki.clone(),
            observer_handle: entry.observer_handle.clone(),
        }))
    }
}

fn fingerprint_key(sender_fingerprint: &[u8; 32]) -> String {
    const HEX: &[u8; 16] = b"0123456789abcdef";

    let mut fingerprint = String::with_capacity("sha256:".len() + sender_fingerprint.len() * 2);
    fingerprint.push_str("sha256:");
    for &byte in sender_fingerprint {
        fingerprint.push(char::from(HEX[usize::from(byte >> 4)]));
        fingerprint.push(char::from(HEX[usize::from(byte & 0x0f)]));
    }
    fingerprint
}

#[cfg(test)]
mod tests {
    use std::error::Error;
    use std::fs;
    use std::path::PathBuf;
    use std::sync::atomic::{AtomicUsize, Ordering};
    use std::time::{SystemTime, UNIX_EPOCH};

    use super::{AuthorizedClientLedger, BrowserLedgerLookup};
    use crate::{BrowserLedger, LedgerRow};

    static NEXT_TEST_DIRECTORY: AtomicUsize = AtomicUsize::new(0);

    struct TestJournal {
        root: PathBuf,
    }

    impl TestJournal {
        fn create() -> Result<Self, Box<dyn Error>> {
            let sequence = NEXT_TEST_DIRECTORY.fetch_add(1, Ordering::Relaxed);
            let timestamp = SystemTime::now().duration_since(UNIX_EPOCH)?.as_nanos();
            let root = std::env::temp_dir().join(format!(
                "solstone-spl-authorized-client-ledger-{}-{timestamp}-{sequence}",
                std::process::id()
            ));
            fs::create_dir_all(root.join("link"))?;
            Ok(Self { root })
        }

        fn ledger_path(&self) -> PathBuf {
            self.root.join("link").join("authorized_clients.json")
        }
    }

    impl Drop for TestJournal {
        fn drop(&mut self) {
            let _ = fs::remove_dir_all(&self.root);
        }
    }

    #[test]
    fn existing_reader_observes_completed_python_write_without_sleep() -> Result<(), Box<dyn Error>>
    {
        let journal = TestJournal::create()?;
        let fingerprint = [0xa5; 32];
        let ledger = AuthorizedClientLedger::new(&journal.root);

        fs::write(journal.ledger_path(), browser_entry("browser", "null"))?;
        assert_eq!(
            ledger.lookup_browser(&fingerprint),
            BrowserLedgerLookup::Incomplete
        );
        assert_eq!(
            ledger.lookup(&format!("sha256:{}", "a5".repeat(32)))?,
            Some(LedgerRow {
                pubkey_spki_hex: Some("30aa".to_owned()),
                observer_handle: None,
            })
        );

        fs::write(
            journal.ledger_path(),
            browser_entry("browser", "\"observer-handle\""),
        )?;
        assert_eq!(
            ledger.lookup_browser(&fingerprint),
            BrowserLedgerLookup::Authorized {
                pubkey_spki: "30aa".to_owned(),
                observer_handle: "observer-handle".to_owned(),
            }
        );
        assert_eq!(
            ledger.lookup(&format!("sha256:{}", "a5".repeat(32)))?,
            Some(LedgerRow {
                pubkey_spki_hex: Some("30aa".to_owned()),
                observer_handle: Some("observer-handle".to_owned()),
            })
        );

        Ok(())
    }

    #[test]
    fn a_non_browser_kind_is_not_authorized() -> Result<(), Box<dyn Error>> {
        let journal = TestJournal::create()?;
        let ledger = AuthorizedClientLedger::new(&journal.root);

        fs::write(
            journal.ledger_path(),
            browser_entry("cert", "\"observer-handle\""),
        )?;

        assert_eq!(
            ledger.lookup_browser(&[0xa5; 32]),
            BrowserLedgerLookup::NotAuthorized
        );
        Ok(())
    }

    #[test]
    fn disappearance_and_invalid_contents_do_not_retain_authorization() -> Result<(), Box<dyn Error>>
    {
        let journal = TestJournal::create()?;
        let ledger = AuthorizedClientLedger::new(&journal.root);
        let path = journal.ledger_path();

        fs::write(&path, browser_entry("browser", "\"observer-handle\""))?;
        assert!(matches!(
            ledger.lookup_browser(&[0xa5; 32]),
            BrowserLedgerLookup::Authorized { .. }
        ));

        fs::remove_file(&path)?;
        assert_eq!(
            ledger.lookup_browser(&[0xa5; 32]),
            BrowserLedgerLookup::LedgerUnavailable
        );

        fs::write(&path, "{}")?;
        assert_eq!(
            ledger.lookup_browser(&[0xa5; 32]),
            BrowserLedgerLookup::LedgerUnavailable
        );

        Ok(())
    }

    fn browser_entry(kind: &str, observer_handle: &str) -> String {
        let fingerprint = "a5".repeat(32);
        format!(
            "[{{\"fingerprint\":\"sha256:{fingerprint}\",\"kind\":\"{kind}\",\"pubkey_spki\":\"30aa\",\"observer_handle\":{observer_handle}}}]"
        )
    }
}
