// SPDX-License-Identifier: AGPL-3.0-only
// Copyright (c) 2026 sol pbc

//! Validation for the self-contained archive carried by an SPL blob offer.

use std::io::{Cursor, Read};

use flate2::read::GzDecoder;
use regex::Regex;
use serde_json::{Map, Value};
use tar::{Archive, EntryType};
use thiserror::Error;

const MAX_ENTRIES: usize = 64;
const MAX_ENTRY_BYTES: u64 = 16 * 1024 * 1024;
const MAX_TOTAL_BYTES: u64 = 64 * 1024 * 1024;
const METADATA_NAME: &str = "blob.json";

/// A regular payload file that passed archive validation.
#[derive(Clone, Debug, Eq, PartialEq)]
pub struct BlobArchiveEntry {
    /// The single-component name stored in the archive.
    pub name: String,
    /// The complete in-memory content of the validated file.
    pub bytes: Vec<u8>,
}

/// Validated metadata from `blob.json`.
#[derive(Clone, Debug, PartialEq)]
pub struct BlobArchiveMetadata {
    /// Archive format version, always `1` for a parsed archive.
    pub version: u64,
    /// Eight-digit archive day.
    pub day: String,
    /// Segment identifier in `HHMMSS_N` form.
    pub segment: String,
    /// The archive's nonempty source host.
    pub host: String,
    /// Caller-defined metadata, constrained to a JSON object.
    pub meta: Map<String, Value>,
}

/// A fully validated gzip-compressed tar archive.
#[derive(Clone, Debug, PartialEq)]
pub struct ValidatedBlobArchive {
    /// Parsed metadata formerly held in `blob.json`.
    pub metadata: BlobArchiveMetadata,
    /// Every regular file other than `blob.json`.
    pub entries: Vec<BlobArchiveEntry>,
}

/// Reasons an archive cannot safely be made available to an SPL receiver.
#[derive(Debug, Error)]
pub enum BlobArchiveError {
    /// The gzip stream or tar reader returned an I/O error.
    #[error("archive stream error: {0}")]
    Stream(#[from] std::io::Error),

    /// More than the permitted number of tar entries was present.
    #[error("archive has more than {MAX_ENTRIES} entries")]
    TooManyEntries,

    /// A tar entry was not a regular file.
    #[error("archive entry {name:?} has unsupported type {entry_type:?}")]
    UnsupportedEntryType {
        /// The path reported by the tar header, when it was representable.
        name: String,
        /// The tar entry type reported by the header.
        entry_type: EntryType,
    },

    /// A tar name was not valid UTF-8.
    #[error("archive entry name is not valid UTF-8")]
    NonUtf8EntryName,

    /// A tar name violated the single-file-name contract.
    #[error("archive entry name {name:?} is not allowed")]
    InvalidEntryName {
        /// The rejected archive name.
        name: String,
    },

    /// One file exceeded the per-file size limit.
    #[error("archive entry {name:?} exceeds {MAX_ENTRY_BYTES} bytes")]
    EntryTooLarge {
        /// The oversized entry's name.
        name: String,
        /// The size declared by the tar header.
        size: u64,
    },

    /// The declared regular-file sizes exceeded the total size limit.
    #[error("archive regular files exceed {MAX_TOTAL_BYTES} bytes")]
    TotalTooLarge,

    /// The archive included more than one metadata file.
    #[error("archive has more than one {METADATA_NAME}")]
    DuplicateMetadata,

    /// The archive did not include its required metadata file.
    #[error("archive is missing {METADATA_NAME}")]
    MissingMetadata,

    /// The archive has no regular payload file besides its metadata.
    #[error("archive has no payload files")]
    MissingPayload,

    /// `blob.json` was not valid JSON.
    #[error("{METADATA_NAME} is not valid JSON: {0}")]
    InvalidMetadataJson(serde_json::Error),

    /// `blob.json` was not a JSON object.
    #[error("{METADATA_NAME} must be a JSON object")]
    MetadataNotObject,

    /// The metadata version was absent or not numeric version one.
    #[error("{METADATA_NAME}.v must equal 1")]
    InvalidVersion,

    /// The metadata day was absent or did not use eight ASCII digits.
    #[error("{METADATA_NAME}.day must contain exactly eight digits")]
    InvalidDay,

    /// The metadata segment was absent or did not use `HHMMSS_N` form.
    #[error("{METADATA_NAME}.segment must use HHMMSS_N form")]
    InvalidSegment,

    /// The metadata host was absent, not a string, or empty.
    #[error("{METADATA_NAME}.host must be a nonempty string")]
    InvalidHost,

    /// The metadata meta field was absent or not a JSON object.
    #[error("{METADATA_NAME}.meta must be a JSON object")]
    InvalidMeta,
}

/// Parse and validate an SPL blob archive without writing any file to disk.
pub fn parse_blob_archive(input: &[u8]) -> Result<ValidatedBlobArchive, BlobArchiveError> {
    let decoder = GzDecoder::new(Cursor::new(input));
    let mut archive = Archive::new(decoder);
    let mut entry_count = 0_usize;
    let mut total_bytes = 0_u64;
    let mut metadata_bytes = None;
    let mut payload_entries = Vec::new();

    for entry_result in archive.entries()? {
        entry_count = entry_count
            .checked_add(1)
            .ok_or(BlobArchiveError::TooManyEntries)?;
        if entry_count > MAX_ENTRIES {
            return Err(BlobArchiveError::TooManyEntries);
        }

        let mut entry = entry_result?;
        let path = entry.path()?;
        let name = path
            .to_str()
            .ok_or(BlobArchiveError::NonUtf8EntryName)?
            .to_owned();
        validate_entry_name(&name)?;

        let entry_type = entry.header().entry_type();
        if !entry_type.is_file() {
            return Err(BlobArchiveError::UnsupportedEntryType { name, entry_type });
        }

        let size = entry.size();
        if size > MAX_ENTRY_BYTES {
            return Err(BlobArchiveError::EntryTooLarge { name, size });
        }
        total_bytes = total_bytes
            .checked_add(size)
            .filter(|total| *total <= MAX_TOTAL_BYTES)
            .ok_or(BlobArchiveError::TotalTooLarge)?;

        let mut bytes = Vec::with_capacity(size as usize);
        entry.read_to_end(&mut bytes)?;
        if name == METADATA_NAME {
            if metadata_bytes.replace(bytes).is_some() {
                return Err(BlobArchiveError::DuplicateMetadata);
            }
        } else {
            payload_entries.push(BlobArchiveEntry { name, bytes });
        }
    }

    let metadata_bytes = metadata_bytes.ok_or(BlobArchiveError::MissingMetadata)?;
    if payload_entries.is_empty() {
        return Err(BlobArchiveError::MissingPayload);
    }

    let metadata = parse_metadata(&metadata_bytes)?;
    Ok(ValidatedBlobArchive {
        metadata,
        entries: payload_entries,
    })
}

fn validate_entry_name(name: &str) -> Result<(), BlobArchiveError> {
    if name.is_empty()
        || matches!(name, "." | "..")
        || name.starts_with('/')
        || name.contains('\\')
        || name.contains('/')
    {
        return Err(BlobArchiveError::InvalidEntryName {
            name: name.to_owned(),
        });
    }
    Ok(())
}

fn parse_metadata(bytes: &[u8]) -> Result<BlobArchiveMetadata, BlobArchiveError> {
    let value: Value =
        serde_json::from_slice(bytes).map_err(BlobArchiveError::InvalidMetadataJson)?;
    let object = value
        .as_object()
        .ok_or(BlobArchiveError::MetadataNotObject)?;

    let version = object
        .get("v")
        .and_then(Value::as_f64)
        .filter(|version| *version == 1.0)
        .map(|_| 1)
        .ok_or(BlobArchiveError::InvalidVersion)?;
    let day = object
        .get("day")
        .and_then(Value::as_str)
        .filter(|day| matches_day(day))
        .ok_or(BlobArchiveError::InvalidDay)?
        .to_owned();
    let segment = object
        .get("segment")
        .and_then(Value::as_str)
        .filter(|segment| matches_segment(segment))
        .ok_or(BlobArchiveError::InvalidSegment)?
        .to_owned();
    let host = object
        .get("host")
        .and_then(Value::as_str)
        .filter(|host| !host.is_empty())
        .ok_or(BlobArchiveError::InvalidHost)?
        .to_owned();
    let meta = object
        .get("meta")
        .and_then(Value::as_object)
        .ok_or(BlobArchiveError::InvalidMeta)?
        .clone();

    Ok(BlobArchiveMetadata {
        version,
        day,
        segment,
        host,
        meta,
    })
}

fn matches_day(value: &str) -> bool {
    matches_pattern(r"^\d{8}$", value)
}

fn matches_segment(value: &str) -> bool {
    matches_pattern(r"^\d{6}_\d+$", value)
}

fn matches_pattern(pattern: &str, value: &str) -> bool {
    Regex::new(pattern).is_ok_and(|regex| regex.is_match(value))
}

#[cfg(test)]
mod tests {
    use std::io::Cursor;

    use flate2::{Compression, write::GzEncoder};
    use tar::{Builder, EntryType, Header};

    use super::{BlobArchiveError, parse_blob_archive, validate_entry_name};

    const METADATA: &str =
        r#"{"v":1,"day":"20260731","segment":"120001_3","host":"home.example","meta":{"a":true}}"#;

    fn regular_archive(entries: &[(&str, &[u8])]) -> Result<Vec<u8>, std::io::Error> {
        let encoder = GzEncoder::new(Vec::new(), Compression::default());
        let mut builder = Builder::new(encoder);
        for (name, bytes) in entries {
            let mut header = Header::new_gnu();
            header.set_size(bytes.len() as u64);
            header.set_mode(0o600);
            header.set_cksum();
            builder.append_data(&mut header, *name, Cursor::new(*bytes))?;
        }
        let encoder = builder.into_inner()?;
        encoder.finish()
    }

    fn valid_archive() -> Result<Vec<u8>, std::io::Error> {
        regular_archive(&[("blob.json", METADATA.as_bytes()), ("entry.bin", b"body")])
    }

    fn archive_with_metadata(metadata: &[u8]) -> Result<Vec<u8>, std::io::Error> {
        regular_archive(&[("blob.json", metadata), ("entry.bin", b"body")])
    }

    #[test]
    fn returns_metadata_and_payload_entries() -> Result<(), Box<dyn std::error::Error>> {
        let parsed = parse_blob_archive(&valid_archive()?)?;

        assert_eq!(parsed.metadata.version, 1);
        assert_eq!(parsed.metadata.day, "20260731");
        assert_eq!(parsed.entries.len(), 1);
        assert_eq!(parsed.entries[0].name, "entry.bin");
        assert_eq!(parsed.entries[0].bytes, b"body");
        Ok(())
    }

    #[test]
    fn accepts_float_one_for_metadata_version() -> Result<(), Box<dyn std::error::Error>> {
        let metadata =
            br#"{"v":1.0,"day":"20260731","segment":"120001_3","host":"home.example","meta":{}}"#;
        let parsed = parse_blob_archive(&archive_with_metadata(metadata)?)?;

        assert_eq!(parsed.metadata.version, 1);
        Ok(())
    }

    #[test]
    fn accepts_unicode_decimal_digits_in_metadata() -> Result<(), Box<dyn std::error::Error>> {
        let metadata =
            r#"{"v":1,"day":"١٢٣٤٥٦٧٨","segment":"١٢٣٤٥٦_٧","host":"home.example","meta":{}}"#;
        let parsed = parse_blob_archive(&archive_with_metadata(metadata.as_bytes())?)?;

        assert_eq!(parsed.metadata.day, "١٢٣٤٥٦٧٨");
        assert_eq!(parsed.metadata.segment, "١٢٣٤٥٦_٧");
        Ok(())
    }

    #[test]
    fn rejects_more_than_64_entries() -> Result<(), Box<dyn std::error::Error>> {
        let mut names = Vec::new();
        names.push(("blob.json".to_owned(), METADATA.as_bytes().to_vec()));
        for number in 0..64 {
            names.push((format!("{number}.bin"), Vec::new()));
        }
        let references: Vec<(&str, &[u8])> = names
            .iter()
            .map(|(name, bytes)| (name.as_str(), bytes.as_slice()))
            .collect();
        let archive = regular_archive(&references)?;

        assert!(matches!(
            parse_blob_archive(&archive),
            Err(BlobArchiveError::TooManyEntries)
        ));
        Ok(())
    }

    #[test]
    fn rejects_non_regular_entries() -> Result<(), Box<dyn std::error::Error>> {
        let encoder = GzEncoder::new(Vec::new(), Compression::default());
        let mut builder = Builder::new(encoder);
        let mut header = Header::new_gnu();
        header.set_entry_type(EntryType::Directory);
        header.set_size(0);
        header.set_mode(0o700);
        header.set_cksum();
        builder.append_data(&mut header, "directory", Cursor::new(Vec::<u8>::new()))?;
        let encoder = builder.into_inner()?;
        let archive = encoder.finish()?;

        assert!(matches!(
            parse_blob_archive(&archive),
            Err(BlobArchiveError::UnsupportedEntryType { .. })
        ));
        Ok(())
    }

    #[test]
    fn rejects_disallowed_names() {
        for name in ["", ".", "..", "/absolute", "back\\slash", "nested/name"] {
            assert!(matches!(
                validate_entry_name(name),
                Err(BlobArchiveError::InvalidEntryName { .. })
            ));
        }
    }

    #[test]
    fn rejects_oversized_regular_file() -> Result<(), Box<dyn std::error::Error>> {
        let large = vec![0_u8; (16 * 1024 * 1024) + 1];
        let archive = regular_archive(&[("blob.json", METADATA.as_bytes()), ("large", &large)])?;

        assert!(matches!(
            parse_blob_archive(&archive),
            Err(BlobArchiveError::EntryTooLarge { .. })
        ));
        Ok(())
    }

    #[test]
    fn rejects_total_oversized_regular_files() -> Result<(), Box<dyn std::error::Error>> {
        let first = vec![0_u8; 16 * 1024 * 1024];
        let second = vec![0_u8; 16 * 1024 * 1024];
        let third = vec![0_u8; 16 * 1024 * 1024];
        let fourth = vec![0_u8; 16 * 1024 * 1024];
        let extra = vec![0_u8; 1];
        let archive = regular_archive(&[
            ("blob.json", METADATA.as_bytes()),
            ("one", &first),
            ("two", &second),
            ("three", &third),
            ("four", &fourth),
            ("five", &extra),
        ])?;

        assert!(matches!(
            parse_blob_archive(&archive),
            Err(BlobArchiveError::TotalTooLarge)
        ));
        Ok(())
    }

    #[test]
    fn requires_exactly_one_metadata_file() -> Result<(), Box<dyn std::error::Error>> {
        let missing = regular_archive(&[("entry.bin", b"body")])?;
        assert!(matches!(
            parse_blob_archive(&missing),
            Err(BlobArchiveError::MissingMetadata)
        ));

        let duplicate = regular_archive(&[
            ("blob.json", METADATA.as_bytes()),
            ("blob.json", METADATA.as_bytes()),
            ("entry.bin", b"body"),
        ])?;
        assert!(matches!(
            parse_blob_archive(&duplicate),
            Err(BlobArchiveError::DuplicateMetadata)
        ));
        Ok(())
    }

    #[test]
    fn requires_payload_file() -> Result<(), Box<dyn std::error::Error>> {
        let archive = regular_archive(&[("blob.json", METADATA.as_bytes())])?;

        assert!(matches!(
            parse_blob_archive(&archive),
            Err(BlobArchiveError::MissingPayload)
        ));
        Ok(())
    }

    #[test]
    fn validates_metadata_shape() -> Result<(), Box<dyn std::error::Error>> {
        let cases = [
            (b"not json".as_slice(), BlobArchiveErrorKind::InvalidJson),
            (b"[]".as_slice(), BlobArchiveErrorKind::NotObject),
            (
                br#"{"v":2,"day":"20260731","segment":"120001_3","host":"home","meta":{}}"#
                    .as_slice(),
                BlobArchiveErrorKind::Version,
            ),
            (
                br#"{"v":1,"day":"bad","segment":"120001_3","host":"home","meta":{}}"#.as_slice(),
                BlobArchiveErrorKind::Day,
            ),
            (
                br#"{"v":1,"day":"20260731","segment":"no","host":"home","meta":{}}"#.as_slice(),
                BlobArchiveErrorKind::Segment,
            ),
            (
                br#"{"v":1,"day":"20260731","segment":"120001_3","host":"","meta":{}}"#.as_slice(),
                BlobArchiveErrorKind::Host,
            ),
            (
                br#"{"v":1,"day":"20260731","segment":"120001_3","host":"home","meta":[]}"#
                    .as_slice(),
                BlobArchiveErrorKind::Meta,
            ),
        ];
        for (metadata, kind) in cases {
            let archive = regular_archive(&[("blob.json", metadata), ("entry.bin", b"body")])?;
            let result = parse_blob_archive(&archive);
            assert!(kind.matches(result));
        }
        Ok(())
    }

    enum BlobArchiveErrorKind {
        InvalidJson,
        NotObject,
        Version,
        Day,
        Segment,
        Host,
        Meta,
    }

    impl BlobArchiveErrorKind {
        fn matches(&self, result: Result<super::ValidatedBlobArchive, BlobArchiveError>) -> bool {
            matches!(
                (self, result),
                (
                    Self::InvalidJson,
                    Err(BlobArchiveError::InvalidMetadataJson(_))
                ) | (Self::NotObject, Err(BlobArchiveError::MetadataNotObject))
                    | (Self::Version, Err(BlobArchiveError::InvalidVersion))
                    | (Self::Day, Err(BlobArchiveError::InvalidDay))
                    | (Self::Segment, Err(BlobArchiveError::InvalidSegment))
                    | (Self::Host, Err(BlobArchiveError::InvalidHost))
                    | (Self::Meta, Err(BlobArchiveError::InvalidMeta))
            )
        }
    }
}
