// SPDX-License-Identifier: AGPL-3.0-only
// Copyright (c) 2026 sol pbc

//! Load-only access to the home upload HPKE key.

use std::{
    fs,
    path::{Path, PathBuf},
};

use p256::elliptic_curve::pkcs8::DecodePrivateKey;
use solstone_core_spl_hpke::P256Secret;

use crate::{KeyError, UploadKeySource};

/// Load-only source for `<journal>/link/hpke/upload_private.pem`.
///
/// This mirrors Python's `load_upload_key()`, deliberately not its distinct
/// pairing-only `load_or_generate_upload_key()` helper. It has no write or
/// generation operation, so a missing key cannot mint an incompatible one.
#[derive(Clone, Debug, Eq, PartialEq)]
pub struct JournalUploadKeySource {
    path: PathBuf,
}

impl JournalUploadKeySource {
    /// Creates a source rooted at the journal directory without accessing it.
    #[must_use]
    pub fn new(journal_root: impl AsRef<Path>) -> Self {
        Self {
            path: journal_root
                .as_ref()
                .join("link")
                .join("hpke")
                .join("upload_private.pem"),
        }
    }
}

impl UploadKeySource for JournalUploadKeySource {
    fn private_key(&self) -> Result<P256Secret, KeyError> {
        let pem = fs::read_to_string(&self.path).map_err(|_| KeyError::Unavailable)?;
        P256Secret::from_pkcs8_pem(&pem).map_err(|_| KeyError::Invalid)
    }
}

#[cfg(test)]
mod tests {
    use std::{
        error::Error,
        fs,
        path::PathBuf,
        sync::atomic::{AtomicUsize, Ordering},
        time::{SystemTime, UNIX_EPOCH},
    };

    use p256::elliptic_curve::pkcs8::EncodePrivateKey;
    use solstone_core_spl_hpke::P256Secret;

    use super::JournalUploadKeySource;
    use crate::{KeyError, UploadKeySource};

    static NEXT_TEST_DIRECTORY: AtomicUsize = AtomicUsize::new(0);

    struct TestJournal {
        root: PathBuf,
    }

    impl TestJournal {
        fn create() -> Result<Self, Box<dyn Error>> {
            let sequence = NEXT_TEST_DIRECTORY.fetch_add(1, Ordering::Relaxed);
            let timestamp = SystemTime::now().duration_since(UNIX_EPOCH)?.as_nanos();
            let root = std::env::temp_dir().join(format!(
                "solstone-spl-upload-key-source-{}-{timestamp}-{sequence}",
                std::process::id()
            ));
            Ok(Self { root })
        }

        fn key_path(&self) -> PathBuf {
            self.root
                .join("link")
                .join("hpke")
                .join("upload_private.pem")
        }
    }

    impl Drop for TestJournal {
        fn drop(&mut self) {
            let _ = fs::remove_dir_all(&self.root);
        }
    }

    #[test]
    fn missing_key_fails_without_creating_any_path() -> Result<(), Box<dyn Error>> {
        let journal = TestJournal::create()?;
        let source = JournalUploadKeySource::new(&journal.root);

        assert_eq!(source.private_key(), Err(KeyError::Unavailable));
        assert!(!journal.root.exists());

        Ok(())
    }

    #[test]
    fn loads_only_a_p256_pkcs8_pem_key() -> Result<(), Box<dyn Error>> {
        let journal = TestJournal::create()?;
        let path = journal.key_path();
        let parent = match path.parent() {
            Some(parent) => parent,
            None => return Err("test key path unexpectedly has no parent".into()),
        };
        fs::create_dir_all(parent)?;
        let expected = P256Secret::from_slice(&[0x42; 32])?;
        fs::write(&path, expected.to_pkcs8_pem(Default::default())?.as_bytes())?;

        let loaded = JournalUploadKeySource::new(&journal.root).private_key()?;
        assert_eq!(loaded.to_bytes(), expected.to_bytes());

        fs::write(path, "not a private key")?;
        assert_eq!(
            JournalUploadKeySource::new(&journal.root).private_key(),
            Err(KeyError::Invalid)
        );
        Ok(())
    }
}
