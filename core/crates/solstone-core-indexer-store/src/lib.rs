// SPDX-License-Identifier: AGPL-3.0-only
// Copyright (c) 2026 sol pbc

pub mod db;
pub mod scan;

use std::fmt;
use std::io;
use std::path::PathBuf;

#[derive(Debug)]
pub enum StoreError {
    Discovery(solstone_core_indexer::discovery::DiscoveryError),
    Io(io::Error),
    Path(solstone_core_indexer::paths::JournalPathError),
    Sql(rusqlite::Error),
    OutsideJournal(PathBuf),
    NonUtf8Path(PathBuf),
    MissingFile(PathBuf),
}

impl fmt::Display for StoreError {
    fn fmt(&self, formatter: &mut fmt::Formatter<'_>) -> fmt::Result {
        match self {
            StoreError::Discovery(error) => write!(formatter, "{error}"),
            StoreError::Io(error) => write!(formatter, "{error}"),
            StoreError::Path(error) => write!(formatter, "{error}"),
            StoreError::Sql(error) => write!(formatter, "{error}"),
            StoreError::OutsideJournal(path) => {
                write!(
                    formatter,
                    "file is outside journal directory: {}",
                    path.display()
                )
            }
            StoreError::NonUtf8Path(path) => {
                write!(formatter, "path is not valid UTF-8: {}", path.display())
            }
            StoreError::MissingFile(path) => {
                write!(formatter, "file not found: {}", path.display())
            }
        }
    }
}

impl std::error::Error for StoreError {}

impl From<solstone_core_indexer::discovery::DiscoveryError> for StoreError {
    fn from(error: solstone_core_indexer::discovery::DiscoveryError) -> Self {
        StoreError::Discovery(error)
    }
}

impl From<io::Error> for StoreError {
    fn from(error: io::Error) -> Self {
        StoreError::Io(error)
    }
}

impl From<solstone_core_indexer::paths::JournalPathError> for StoreError {
    fn from(error: solstone_core_indexer::paths::JournalPathError) -> Self {
        StoreError::Path(error)
    }
}

impl From<rusqlite::Error> for StoreError {
    fn from(error: rusqlite::Error) -> Self {
        StoreError::Sql(error)
    }
}
