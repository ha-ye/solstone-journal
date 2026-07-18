// SPDX-License-Identifier: AGPL-3.0-only
// Copyright (c) 2026 sol pbc

pub mod candidates;
mod copresence;
pub mod discovery;
pub mod registry;

use std::fmt;
use std::fs;
use std::path::Path;

use chrono::{Datelike, NaiveDate};
use serde_json::{Map, Value};

use crate::edges::candidates::EdgeResolver;
use crate::edges::registry::{EdgeSourceKind, edge_source_for_rel};
use crate::metadata::extract_path_metadata;
use crate::segment::{segment_key, segment_parse};

type JsonObject = Map<String, Value>;

// Source of truth: solstone/think/indexer/edges.py KINDS at lines 36-56.
pub const KINDS: &[&str] = &[
    "attended-with",
    "co-present",
    "spoke-with",
    "mentioned",
    "committed-to",
    "works-with",
    "works-at",
    "reports-to",
    "family-of",
    "knows",
    "uses",
    "created",
    "other",
    "decided-with",
    "messaged-with",
    "scheduled-with",
    "party-of",
];

// Source of truth: solstone/think/indexer/edges.py DIRECTED_KINDS at lines 59-61.
pub const DIRECTED_KINDS: &[&str] = &[
    "committed-to",
    "mentioned",
    "works-at",
    "reports-to",
    "uses",
    "created",
];

#[derive(Debug, Clone, PartialEq, Eq)]
pub struct EdgeContext {
    pub path: String,
    pub day: String,
    pub facet: String,
}

#[derive(Debug, Clone, PartialEq, Eq)]
pub struct EdgeRow {
    pub src: String,
    pub dst: String,
    pub kind: String,
    pub src_name: Option<String>,
    pub dst_name: Option<String>,
    pub day: Option<String>,
    pub facet: Option<String>,
    pub source: String,
    pub path: String,
    pub anchor: Option<String>,
    pub label: Option<String>,
    pub ts: Option<i64>,
    pub weight: i64,
}

#[derive(Debug, Clone, PartialEq, Eq)]
pub struct NormalizedEdge {
    pub src: String,
    pub dst: String,
    pub kind: String,
    pub directed: i64,
    pub src_name: Option<String>,
    pub dst_name: Option<String>,
    pub day: Option<String>,
    pub facet: Option<String>,
    pub source: String,
    pub path: String,
    pub anchor: Option<String>,
    pub label: Option<String>,
    pub ts: Option<i64>,
    pub weight: i64,
}

#[derive(Debug, Clone, PartialEq, Eq)]
pub struct EdgeFileRows {
    pub rows: Vec<NormalizedEdge>,
    pub invalid_segment: Option<String>,
}

#[derive(Debug, Clone, PartialEq, Eq)]
pub enum EdgeError {
    InvalidPattern(String),
    Io(String),
    UnknownKind(String),
    MissingSrc,
    MissingDst,
    InvalidDay(String),
}

impl fmt::Display for EdgeError {
    fn fmt(&self, formatter: &mut fmt::Formatter<'_>) -> fmt::Result {
        match self {
            EdgeError::InvalidPattern(error) => write!(formatter, "invalid edge pattern: {error}"),
            EdgeError::Io(error) => write!(formatter, "{error}"),
            EdgeError::UnknownKind(kind) => write!(formatter, "Unknown edge kind: {kind:?}"),
            EdgeError::MissingSrc => formatter.write_str("edge row requires non-empty string src"),
            EdgeError::MissingDst => formatter.write_str("edge row requires non-empty string dst"),
            EdgeError::InvalidDay(day) => write!(formatter, "Invalid edge day: {day:?}"),
        }
    }
}

impl std::error::Error for EdgeError {}

pub fn extract_file_edges(
    rel: &str,
    path: &Path,
    resolver: &mut EdgeResolver,
) -> Result<EdgeFileRows, EdgeError> {
    let Some(kind) = edge_source_for_rel(rel)? else {
        return Ok(EdgeFileRows {
            rows: Vec::new(),
            invalid_segment: None,
        });
    };
    if let Some(segment) = segment_key(rel)
        && segment_parse(&segment).is_none()
    {
        return Ok(EdgeFileRows {
            rows: Vec::new(),
            invalid_segment: Some(segment),
        });
    }

    let metadata = extract_path_metadata(rel);
    let context = EdgeContext {
        path: rel.to_string(),
        day: metadata.day,
        facet: metadata.facet,
    };
    let rows = match kind {
        EdgeSourceKind::Copresence => {
            let entries = read_jsonl_objects(path)?;
            copresence::extract_copresence_edges(&entries, &context, resolver)?
        }
    };
    Ok(EdgeFileRows {
        rows: normalize_edges(rows)?,
        invalid_segment: None,
    })
}

pub fn normalize_edges(rows: Vec<EdgeRow>) -> Result<Vec<NormalizedEdge>, EdgeError> {
    let mut prepared = Vec::new();
    for row in rows {
        if !KINDS.contains(&row.kind.as_str()) {
            return Err(EdgeError::UnknownKind(row.kind));
        }
        if row.src.is_empty() {
            return Err(EdgeError::MissingSrc);
        }
        if row.dst.is_empty() {
            return Err(EdgeError::MissingDst);
        }
        if let Some(day) = row.day.as_deref()
            && !valid_edge_day(day)
        {
            return Err(EdgeError::InvalidDay(day.to_string()));
        }

        let directed = if DIRECTED_KINDS.contains(&row.kind.as_str()) {
            1
        } else {
            0
        };
        let mut src = row.src;
        let mut dst = row.dst;
        let mut src_name = row.src_name;
        let mut dst_name = row.dst_name;
        if directed == 0 && src.as_str() > dst.as_str() {
            std::mem::swap(&mut src, &mut dst);
            std::mem::swap(&mut src_name, &mut dst_name);
        }
        let facet = row.facet.map(|facet| {
            if facet.is_empty() {
                facet
            } else {
                facet.to_lowercase()
            }
        });
        prepared.push(NormalizedEdge {
            src,
            dst,
            kind: row.kind,
            directed,
            src_name,
            dst_name,
            day: row.day,
            facet,
            source: row.source,
            path: row.path,
            anchor: row.anchor,
            label: row.label,
            ts: row.ts,
            weight: row.weight,
        });
    }
    Ok(prepared)
}

pub fn valid_edge_day(day: &str) -> bool {
    if day.len() != 8 || !day.bytes().all(|byte| byte.is_ascii_digit()) {
        return false;
    }
    match NaiveDate::parse_from_str(day, "%Y%m%d") {
        Ok(date) => (1..=9999).contains(&date.year()),
        Err(_error) => false,
    }
}

fn read_jsonl_objects(path: &Path) -> Result<Vec<JsonObject>, EdgeError> {
    let text = fs::read_to_string(path).map_err(|error| {
        EdgeError::Io(format!(
            "edge source read failed for {}: {error}",
            path.display()
        ))
    })?;
    Ok(text
        .lines()
        .filter_map(|line| {
            let trimmed = line.trim();
            if trimmed.is_empty() {
                return None;
            }
            match serde_json::from_str::<Value>(trimmed) {
                Ok(Value::Object(record)) => Some(record),
                Ok(_) | Err(_) => None,
            }
        })
        .collect())
}

#[cfg(test)]
mod tests {
    use super::*;
    use crate::edges::candidates::EdgeResolver;
    use serde_json::json;
    use std::fs;
    use std::path::{Path, PathBuf};
    use std::time::{SystemTime, UNIX_EPOCH};

    fn temp_root(name: &str) -> PathBuf {
        let stamp = SystemTime::now()
            .duration_since(UNIX_EPOCH)
            .expect("time should be available")
            .as_nanos();
        std::env::temp_dir().join(format!("solstone-core-indexer-edges-{name}-{stamp}"))
    }

    fn write_json(root: &Path, rel: &str, value: Value) {
        let path = root.join(rel);
        fs::create_dir_all(path.parent().expect("test path should have parent"))
            .expect("create parent");
        fs::write(path, serde_json::to_string(&value).expect("encode json")).expect("write json");
    }

    fn write_jsonl(root: &Path, rel: &str, values: &[Value]) {
        let path = root.join(rel);
        fs::create_dir_all(path.parent().expect("test path should have parent"))
            .expect("create parent");
        let mut text = String::new();
        for value in values {
            text.push_str(&serde_json::to_string(value).expect("encode jsonl value"));
            text.push('\n');
        }
        fs::write(path, text).expect("write jsonl");
    }

    fn seed_entity(root: &Path, entity_id: &str, name: &str) {
        write_json(
            root,
            &format!("entities/{entity_id}/entity.json"),
            json!({"name": name, "type": "Person"}),
        );
        write_json(
            root,
            &format!("facets/work/entities/{entity_id}/entity.json"),
            json!({}),
        );
    }

    fn row(src: &str, dst: &str, kind: &str, day: Option<&str>) -> EdgeRow {
        EdgeRow {
            src: src.to_string(),
            dst: dst.to_string(),
            kind: kind.to_string(),
            src_name: Some(format!("{src} name")),
            dst_name: Some(format!("{dst} name")),
            day: day.map(str::to_string),
            facet: Some("Work".to_string()),
            source: "test".to_string(),
            path: "synthetic".to_string(),
            anchor: Some("anchor".to_string()),
            label: Some(String::new()),
            ts: Some(0),
            weight: 1,
        }
    }

    #[test]
    fn day_validation_matches_python_date_bounds() {
        assert!(valid_edge_day("20240229"));
        assert!(!valid_edge_day("20230229"));
        assert!(!valid_edge_day("20240230"));
        assert!(!valid_edge_day("20241301"));
        assert!(!valid_edge_day("00000101"));
    }

    #[test]
    fn normalization_validates_whole_batch_and_swaps_undirected_names() {
        let normalized =
            normalize_edges(vec![row("zeta", "alpha", "co-present", Some("20260430"))])
                .expect("normalize valid row");
        assert_eq!(normalized[0].src, "alpha");
        assert_eq!(normalized[0].dst, "zeta");
        assert_eq!(normalized[0].src_name.as_deref(), Some("alpha name"));
        assert_eq!(normalized[0].dst_name.as_deref(), Some("zeta name"));
        assert_eq!(normalized[0].facet.as_deref(), Some("work"));
        assert_eq!(normalized[0].directed, 0);

        let directed = normalize_edges(vec![row("zeta", "alpha", "mentioned", Some("20260430"))])
            .expect("normalize directed row");
        assert_eq!(directed[0].src, "zeta");
        assert_eq!(directed[0].dst, "alpha");
        assert_eq!(directed[0].src_name.as_deref(), Some("zeta name"));
        assert_eq!(directed[0].dst_name.as_deref(), Some("alpha name"));
        assert_eq!(directed[0].directed, 1);

        let error = normalize_edges(vec![
            row("alpha", "zeta", "co-present", Some("20260430")),
            row("alpha", "zeta", "not-a-kind", Some("20260430")),
        ])
        .expect_err("bad kind should abort batch");
        assert_eq!(error, EdgeError::UnknownKind("not-a-kind".to_string()));
    }

    #[test]
    fn normalization_rejects_bad_src_dst_and_days() {
        assert_eq!(
            normalize_edges(vec![row("", "zeta", "co-present", Some("20260430"))])
                .expect_err("missing src"),
            EdgeError::MissingSrc
        );
        assert_eq!(
            normalize_edges(vec![row("alpha", "", "co-present", Some("20260430"))])
                .expect_err("missing dst"),
            EdgeError::MissingDst
        );
        assert_eq!(
            normalize_edges(vec![row("alpha", "zeta", "co-present", Some("20240230"))])
                .expect_err("invalid day"),
            EdgeError::InvalidDay("20240230".to_string())
        );
        assert!(normalize_edges(vec![row("alpha", "zeta", "co-present", None)]).is_ok());
    }

    #[test]
    fn copresence_extracts_pairs_without_deduping_resolved_entries() {
        let root = temp_root("copresence");
        seed_entity(&root, "alice", "Alice Edge");
        seed_entity(&root, "bob", "Bob Edge");
        let rel = "facets/work/entities/20260304.jsonl";
        write_jsonl(
            &root,
            rel,
            &[
                json!({"name":" Alice Edge ","segments":["s1","s2"]}),
                json!({"name":"Alice Edge","segments":["s1"]}),
                json!({"name":"Bob Edge","segments":["s1","s2"]}),
            ],
        );

        let mut resolver = EdgeResolver::new(&root);
        resolver.begin_file();
        let extracted =
            extract_file_edges(rel, &root.join(rel), &mut resolver).expect("extract edges");
        assert_eq!(resolver.drops(), 0);
        assert_eq!(extracted.rows.len(), 2);
        assert_eq!(extracted.rows[0].src, "alice");
        assert_eq!(extracted.rows[0].dst, "bob");
        assert_eq!(extracted.rows[0].anchor.as_deref(), Some("s1"));
        assert_eq!(extracted.rows[0].weight, 2);
        assert_eq!(extracted.rows[0].src_name.as_deref(), Some("Alice Edge"));
        assert_eq!(extracted.rows[0].label.as_deref(), Some(""));
        assert_eq!(extracted.rows[0].ts, Some(0));
        assert_eq!(extracted.rows[1].src, "alice");
        assert_eq!(extracted.rows[1].dst, "bob");
        assert_eq!(extracted.rows[1].weight, 1);
        fs::remove_dir_all(root).expect("cleanup copresence root");
    }

    #[test]
    fn copresence_emits_no_edges_without_shared_segments() {
        let root = temp_root("copresence-no-shared");
        seed_entity(&root, "alice", "Alice Edge");
        seed_entity(&root, "bob", "Bob Edge");
        let rel = "facets/work/entities/20260304.jsonl";
        write_jsonl(
            &root,
            rel,
            &[
                json!({"name":"Alice Edge","segments":["s1"]}),
                json!({"name":"Bob Edge","segments":["s2"]}),
            ],
        );

        let mut resolver = EdgeResolver::new(&root);
        resolver.begin_file();
        let extracted =
            extract_file_edges(rel, &root.join(rel), &mut resolver).expect("extract edges");
        assert_eq!(resolver.drops(), 0);
        assert!(extracted.rows.is_empty());
        fs::remove_dir_all(root).expect("cleanup no shared root");
    }

    #[test]
    fn copresence_filters_inputs_and_counts_resolution_drops() {
        let root = temp_root("copresence-drops");
        seed_entity(&root, "alice", "Alice Edge");
        let rel = "facets/work/entities/20260304.jsonl";
        write_jsonl(
            &root,
            rel,
            &[
                json!({"name":"","segments":["s1"]}),
                json!({"name":"No Match","segments":["s1"]}),
                json!({"name":"Alice Edge","segments":[]}),
                json!({"name":"Alice Edge","segments":["s1"]}),
            ],
        );

        let mut resolver = EdgeResolver::new(&root);
        resolver.begin_file();
        let extracted =
            extract_file_edges(rel, &root.join(rel), &mut resolver).expect("extract edges");
        assert_eq!(resolver.drops(), 1);
        assert!(extracted.rows.is_empty());
        fs::remove_dir_all(root).expect("cleanup copresence drops root");
    }

    #[test]
    fn invalid_segment_guard_skips_before_reading() {
        let root = temp_root("invalid-segment");
        let rel = "facets/999999_300/entities/20260304.jsonl";
        let mut resolver = EdgeResolver::new(&root);
        resolver.begin_file();
        let extracted = extract_file_edges(rel, &root.join(rel), &mut resolver)
            .expect("invalid segment is a skipped result");
        assert_eq!(extracted.invalid_segment.as_deref(), Some("999999_300"));
        assert!(extracted.rows.is_empty());
        assert_eq!(resolver.drops(), 0);
        let _ = fs::remove_dir_all(root);
    }
}
