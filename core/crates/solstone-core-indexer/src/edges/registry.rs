// SPDX-License-Identifier: AGPL-3.0-only
// Copyright (c) 2026 sol pbc

use std::path::Path;

use glob::{MatchOptions, Pattern};

use super::EdgeError;

#[derive(Debug, Clone, Copy, PartialEq, Eq)]
pub(crate) enum EdgePatternRoot {
    Structural,
    DayRooted,
}

#[derive(Debug, Clone, Copy, PartialEq, Eq)]
pub enum EdgeSourceKind {
    Copresence,
}

#[derive(Debug, Clone, Copy, PartialEq, Eq)]
pub(crate) struct EdgeSourcePattern {
    pub pattern: &'static str,
    pub root: EdgePatternRoot,
    pub kind: EdgeSourceKind,
}

pub(crate) const EDGE_SOURCE_PATTERNS: &[EdgeSourcePattern] = &[EdgeSourcePattern {
    pattern: "facets/*/entities/*.jsonl",
    root: EdgePatternRoot::Structural,
    kind: EdgeSourceKind::Copresence,
}];

pub(crate) fn patterns_for_root(
    root: EdgePatternRoot,
) -> impl Iterator<Item = &'static EdgeSourcePattern> {
    EDGE_SOURCE_PATTERNS
        .iter()
        .filter(move |spec| spec.root == root)
}

pub fn edge_source_for_rel(rel: &str) -> Result<Option<EdgeSourceKind>, EdgeError> {
    let options = MatchOptions {
        case_sensitive: true,
        require_literal_separator: true,
        require_literal_leading_dot: false,
    };
    let rel_path = Path::new(rel);
    for spec in EDGE_SOURCE_PATTERNS {
        let pattern = Pattern::new(spec.pattern)
            .map_err(|error| EdgeError::InvalidPattern(error.to_string()))?;
        if pattern.matches_path_with(rel_path, options) {
            return Ok(Some(spec.kind));
        }
    }
    Ok(None)
}
