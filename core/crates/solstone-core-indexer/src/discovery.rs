// SPDX-License-Identifier: AGPL-3.0-only
// Copyright (c) 2026 sol pbc

use std::collections::BTreeMap;
use std::fmt;
use std::path::{Path, PathBuf};

use glob::{GlobError, Pattern, PatternError, glob};

use crate::paths::CHRONICLE_DIR;

pub const DAY_ROOTED_PATTERNS: &[&str] = &[
    "*/talents/*.md",
    "*/*/*/talents/*.md",
    "*/*/*/talents/*/*.md",
    "*/import.*/*/*_transcript.md",
    "*/import.*/*/imported.md",
];

pub const STRUCTURAL_PATTERNS: &[&str] = &[
    "facets/*/activities/*/*/*.md",
    "facets/*/news/*.md",
    "reflections/weekly/*.md",
    "imports/*/summary.md",
    "apps/*/talents/*.md",
];

#[derive(Debug)]
pub enum DiscoveryError {
    NonUtf8Root(PathBuf),
    NonUtf8Relative(PathBuf),
    Pattern(PatternError),
    Glob(GlobError),
    StripPrefix { path: PathBuf, root: PathBuf },
}

impl fmt::Display for DiscoveryError {
    fn fmt(&self, formatter: &mut fmt::Formatter<'_>) -> fmt::Result {
        match self {
            DiscoveryError::NonUtf8Root(path) => {
                write!(
                    formatter,
                    "glob root is not valid UTF-8: {}",
                    path.display()
                )
            }
            DiscoveryError::NonUtf8Relative(path) => {
                write!(
                    formatter,
                    "discovered path is not valid UTF-8: {}",
                    path.display()
                )
            }
            DiscoveryError::Pattern(error) => write!(formatter, "invalid glob pattern: {error}"),
            DiscoveryError::Glob(error) => write!(formatter, "glob traversal failed: {error}"),
            DiscoveryError::StripPrefix { path, root } => write!(
                formatter,
                "discovered path {} is not under root {}",
                path.display(),
                root.display()
            ),
        }
    }
}

impl std::error::Error for DiscoveryError {}

impl From<PatternError> for DiscoveryError {
    fn from(error: PatternError) -> Self {
        DiscoveryError::Pattern(error)
    }
}

impl From<GlobError> for DiscoveryError {
    fn from(error: GlobError) -> Self {
        DiscoveryError::Glob(error)
    }
}

pub fn discover_markdown_files(
    journal: &Path,
) -> Result<BTreeMap<String, PathBuf>, DiscoveryError> {
    let mut files = BTreeMap::new();
    for pattern in STRUCTURAL_PATTERNS {
        discover_from_root(journal, journal, pattern, &mut files)?;
    }

    let chronicle = journal.join(CHRONICLE_DIR);
    let day_root = if chronicle.is_dir() {
        chronicle.as_path()
    } else {
        journal
    };
    for pattern in DAY_ROOTED_PATTERNS {
        discover_from_root(day_root, day_root, pattern, &mut files)?;
    }
    Ok(files)
}

fn discover_from_root(
    root: &Path,
    rel_root: &Path,
    pattern: &str,
    files: &mut BTreeMap<String, PathBuf>,
) -> Result<(), DiscoveryError> {
    let full_pattern = rooted_pattern(root, pattern)?;
    for entry in glob(&full_pattern)? {
        let path = entry?;
        if !path.is_file() {
            continue;
        }
        let rel_path =
            path.strip_prefix(rel_root)
                .map_err(|_error| DiscoveryError::StripPrefix {
                    path: path.clone(),
                    root: rel_root.to_path_buf(),
                })?;
        let rel =
            path_to_posix(rel_path).ok_or_else(|| DiscoveryError::NonUtf8Relative(path.clone()))?;
        files.insert(rel, path);
    }
    Ok(())
}

fn rooted_pattern(root: &Path, pattern: &str) -> Result<String, DiscoveryError> {
    let root = root
        .to_str()
        .ok_or_else(|| DiscoveryError::NonUtf8Root(root.to_path_buf()))?;
    let escaped = Pattern::escape(root);
    let separator = if escaped.ends_with('/') { "" } else { "/" };
    Ok(format!("{escaped}{separator}{pattern}"))
}

fn path_to_posix(path: &Path) -> Option<String> {
    let mut parts = Vec::new();
    for part in path.components() {
        parts.push(part.as_os_str().to_str()?);
    }
    Some(parts.join("/"))
}

#[cfg(test)]
mod tests {
    use super::*;
    use std::fs;
    use std::time::{SystemTime, UNIX_EPOCH};

    fn temp_root(name: &str) -> PathBuf {
        let stamp = SystemTime::now()
            .duration_since(UNIX_EPOCH)
            .expect("time should be available")
            .as_nanos();
        std::env::temp_dir().join(format!("solstone-core-indexer-{name}-{stamp}"))
    }

    fn write(root: &Path, rel: &str) {
        let path = root.join(rel);
        fs::create_dir_all(path.parent().expect("test path should have parent"))
            .expect("create parent");
        fs::write(path, "# Title\n\nbody\n").expect("write markdown");
    }

    #[test]
    fn discovers_chronicle_free_markdown_rels() {
        let root = temp_root("discover");
        write(&root, "chronicle/20240101/talents/flow.md");
        write(&root, "chronicle/.hidden/talents/secret.md");
        write(
            &root,
            "chronicle/20240101/default/123456_300/talents/audio.md",
        );
        write(
            &root,
            "chronicle/20260101/import.ics/090000_300/event_transcript.md",
        );
        write(&root, "facets/work/news/20240101.md");
        write(&root, "imports/20260101_120000/summary.md");
        write(&root, "apps/todos/talents/digest.md");
        write(&root, "chronicle/20240101/default/123456_300/audio.jsonl");

        let files = discover_markdown_files(&root).expect("discover files");
        let rels: Vec<_> = files.keys().cloned().collect();
        assert_eq!(
            rels,
            vec![
                ".hidden/talents/secret.md",
                "20240101/default/123456_300/talents/audio.md",
                "20240101/talents/flow.md",
                "20260101/import.ics/090000_300/event_transcript.md",
                "apps/todos/talents/digest.md",
                "facets/work/news/20240101.md",
                "imports/20260101_120000/summary.md",
            ]
        );
        fs::remove_dir_all(root).expect("cleanup discover root");
    }
}
