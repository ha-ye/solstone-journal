// SPDX-License-Identifier: AGPL-3.0-only
// Copyright (c) 2026 sol pbc

use std::fmt;
use std::path::{Path, PathBuf};

use glob::{MatchOptions, Pattern, PatternError};

use crate::discovery::{DAY_ROOTED_PATTERNS, STRUCTURAL_PATTERNS};
use crate::segment::is_date_key;

pub const CHRONICLE_DIR: &str = "chronicle";

#[derive(Debug, Clone, PartialEq, Eq)]
pub enum JournalPathError {
    Empty,
    Absolute,
    Backslash,
    InvalidComponent,
}

impl fmt::Display for JournalPathError {
    fn fmt(&self, formatter: &mut fmt::Formatter<'_>) -> fmt::Result {
        formatter.write_str(match self {
            JournalPathError::Empty => "rel must be non-empty",
            JournalPathError::Absolute => "rel must be journal-relative",
            JournalPathError::Backslash => "rel must use POSIX separators",
            JournalPathError::InvalidComponent => {
                "rel must not contain empty, '.', or '..' components"
            }
        })
    }
}

impl std::error::Error for JournalPathError {}

#[derive(Debug, Clone, PartialEq, Eq)]
pub struct PatternMatchError {
    message: String,
}

impl fmt::Display for PatternMatchError {
    fn fmt(&self, formatter: &mut fmt::Formatter<'_>) -> fmt::Result {
        formatter.write_str(&self.message)
    }
}

impl std::error::Error for PatternMatchError {}

impl From<PatternError> for PatternMatchError {
    fn from(error: PatternError) -> Self {
        PatternMatchError {
            message: error.to_string(),
        }
    }
}

pub fn resolve_journal_path(journal: &Path, rel: &str) -> Result<PathBuf, JournalPathError> {
    validate_rel(rel)?;
    let first = rel.split('/').next().unwrap_or("");
    if is_date_key(first) {
        Ok(journal.join(CHRONICLE_DIR).join(rel))
    } else {
        Ok(journal.join(rel))
    }
}

pub fn relative_to_journal(journal: &Path, abs_path: &Path) -> Option<String> {
    let chronicle_root = journal.join(CHRONICLE_DIR);
    if let Ok(rel) = abs_path.strip_prefix(&chronicle_root) {
        return path_to_posix(rel);
    }
    abs_path.strip_prefix(journal).ok().and_then(path_to_posix)
}

pub fn matches_markdown_pattern(rel: &str) -> Result<bool, PatternMatchError> {
    let options = MatchOptions {
        case_sensitive: true,
        require_literal_separator: true,
        require_literal_leading_dot: false,
    };
    let rel_path = Path::new(rel);
    for pattern in DAY_ROOTED_PATTERNS.iter().chain(STRUCTURAL_PATTERNS.iter()) {
        if Pattern::new(pattern)?.matches_path_with(rel_path, options) {
            return Ok(true);
        }
    }
    Ok(false)
}

fn validate_rel(rel: &str) -> Result<(), JournalPathError> {
    if rel.is_empty() {
        return Err(JournalPathError::Empty);
    }
    if Path::new(rel).is_absolute() {
        return Err(JournalPathError::Absolute);
    }
    if rel.contains('\\') {
        return Err(JournalPathError::Backslash);
    }
    if rel
        .split('/')
        .any(|part| part.is_empty() || part == "." || part == "..")
    {
        return Err(JournalPathError::InvalidComponent);
    }
    Ok(())
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

    #[test]
    fn resolves_date_prefixed_rels_under_chronicle() {
        let journal = Path::new("/tmp/journal");
        assert_eq!(
            resolve_journal_path(journal, "20240101/talents/flow.md").unwrap(),
            PathBuf::from("/tmp/journal/chronicle/20240101/talents/flow.md")
        );
        assert_eq!(
            resolve_journal_path(journal, "facets/work/news/20240101.md").unwrap(),
            PathBuf::from("/tmp/journal/facets/work/news/20240101.md")
        );
    }

    #[test]
    fn classifies_only_in_scope_markdown_patterns() {
        assert!(matches_markdown_pattern("20240101/talents/flow.md").unwrap());
        assert!(matches_markdown_pattern("20240101/default/123456_300/talents/audio.md").unwrap());
        assert!(
            matches_markdown_pattern("20240101/default/123456_300/talents/work/audio.md").unwrap()
        );
        assert!(
            matches_markdown_pattern("20260101/import.ics/090000_300/event_transcript.md").unwrap()
        );
        assert!(matches_markdown_pattern("20260101/import.ics/090000_300/imported.md").unwrap());
        assert!(matches_markdown_pattern("facets/work/news/20240101.md").unwrap());
        assert!(matches_markdown_pattern("imports/20260101_120000/summary.md").unwrap());
        assert!(matches_markdown_pattern("apps/todos/talents/digest.md").unwrap());
        assert!(!matches_markdown_pattern("facets/work/events/20240101.jsonl").unwrap());
        assert!(!matches_markdown_pattern("20240101/default/123456_300/audio.jsonl").unwrap());
    }
}
