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
    Activity,
    Observation,
    Copresence,
    EventLegacy,
}

#[derive(Debug, Clone, Copy, PartialEq, Eq)]
pub(crate) struct EdgeSourcePattern {
    pub pattern: &'static str,
    pub root: EdgePatternRoot,
    pub kind: EdgeSourceKind,
}

pub(crate) const EDGE_SOURCE_PATTERNS: &[EdgeSourcePattern] = &[
    EdgeSourcePattern {
        pattern: "facets/*/activities/*.jsonl",
        root: EdgePatternRoot::Structural,
        kind: EdgeSourceKind::Activity,
    },
    EdgeSourcePattern {
        pattern: "facets/*/entities/*/observations.jsonl",
        root: EdgePatternRoot::Structural,
        kind: EdgeSourceKind::Observation,
    },
    EdgeSourcePattern {
        pattern: "facets/*/entities/*.jsonl",
        root: EdgePatternRoot::Structural,
        kind: EdgeSourceKind::Copresence,
    },
    EdgeSourcePattern {
        pattern: "facets/*/events/*.jsonl",
        root: EdgePatternRoot::Structural,
        kind: EdgeSourceKind::EventLegacy,
    },
];

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

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn edge_source_patterns_match_exact_structural_shapes() {
        assert_eq!(
            edge_source_for_rel("facets/work/activities/20260430.jsonl"),
            Ok(Some(EdgeSourceKind::Activity))
        );
        assert_eq!(
            edge_source_for_rel("facets/work/entities/alice/observations.jsonl"),
            Ok(Some(EdgeSourceKind::Observation))
        );
        assert_eq!(
            edge_source_for_rel("facets/work/entities/20260430.jsonl"),
            Ok(Some(EdgeSourceKind::Copresence))
        );
        assert_eq!(
            edge_source_for_rel("facets/work/events/20260430.jsonl"),
            Ok(Some(EdgeSourceKind::EventLegacy))
        );
        assert_eq!(
            edge_source_for_rel("facets/work/entities/alice/extra/observations.jsonl"),
            Ok(None)
        );
    }
}
