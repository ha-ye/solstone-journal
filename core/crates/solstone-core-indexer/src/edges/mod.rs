// SPDX-License-Identifier: AGPL-3.0-only
// Copyright (c) 2026 sol pbc

mod activity;
pub mod candidates;
mod copresence;
pub mod discovery;
mod event;
mod observation;
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

// Native edge extraction mirrors Python's source-row normalization, with one
// deliberate divergence: container-valued passthrough edge fields fail during
// extraction. Python can leak earlier rows when sqlite `executemany` later hits
// a bind error; Rust keeps the store savepoint all-or-nothing and rejects those
// values before insertion.

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

#[derive(Debug, Clone, PartialEq)]
pub enum EdgeValue {
    Null,
    Text(String),
    Int(i64),
    Float(f64),
}

#[derive(Debug, Clone, PartialEq)]
pub struct EdgeRow {
    pub src: String,
    pub dst: String,
    pub kind: String,
    pub src_name: EdgeValue,
    pub dst_name: EdgeValue,
    pub day: Option<String>,
    pub facet: Option<String>,
    pub source: String,
    pub path: String,
    pub anchor: Option<String>,
    pub label: EdgeValue,
    pub ts: EdgeValue,
    pub weight: i64,
}

#[derive(Debug, Clone, PartialEq)]
pub struct NormalizedEdge {
    pub src: String,
    pub dst: String,
    pub kind: String,
    pub directed: i64,
    pub src_name: EdgeValue,
    pub dst_name: EdgeValue,
    pub day: Option<String>,
    pub facet: Option<String>,
    pub source: String,
    pub path: String,
    pub anchor: Option<String>,
    pub label: EdgeValue,
    pub ts: EdgeValue,
    pub weight: i64,
}

#[derive(Debug, Clone, PartialEq)]
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
    InvalidObservationPath(String),
    MissingObservationRelationKind,
    InvalidObservationTargetEntityId,
    InvalidEdgeKindType {
        source: &'static str,
        value_type: &'static str,
    },
    UnsupportedEdgeValue {
        field: &'static str,
        value_type: &'static str,
    },
    UnsupportedPythonStringValue {
        field: &'static str,
        value_type: &'static str,
    },
    EdgeIntegerOutOfRange {
        field: &'static str,
    },
    EventTimestampOutOfRange,
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
            EdgeError::InvalidObservationPath(path) => {
                write!(formatter, "invalid observations path: {path}")
            }
            EdgeError::MissingObservationRelationKind => {
                formatter.write_str("observation relation is missing kind")
            }
            EdgeError::InvalidObservationTargetEntityId => formatter
                .write_str("observation relation target_entity_id must be a non-empty string"),
            EdgeError::InvalidEdgeKindType { source, value_type } => {
                write!(
                    formatter,
                    "edge kind for {source} must be a string, got {value_type}"
                )
            }
            EdgeError::UnsupportedEdgeValue { field, value_type } => {
                write!(
                    formatter,
                    "edge field {field} does not support {value_type}"
                )
            }
            EdgeError::UnsupportedPythonStringValue { field, value_type } => {
                write!(
                    formatter,
                    "cannot reproduce Python str() for {field} value of type {value_type}"
                )
            }
            EdgeError::EdgeIntegerOutOfRange { field } => {
                write!(formatter, "edge integer field {field} is outside i64 range")
            }
            EdgeError::EventTimestampOutOfRange => {
                formatter.write_str("event timestamp is outside i64 range")
            }
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
        EdgeSourceKind::Activity => {
            let entries = read_jsonl_objects(path)?;
            activity::extract_activity_edges(&entries, &context)?
        }
        EdgeSourceKind::Observation => {
            let entries = read_jsonl_objects(path)?;
            observation::extract_observation_edges(&entries, &context, resolver.drops_mut())?
        }
        EdgeSourceKind::Copresence => {
            let entries = read_jsonl_objects(path)?;
            copresence::extract_copresence_edges(&entries, &context, resolver)?
        }
        EdgeSourceKind::EventLegacy => {
            let entries = read_jsonl_objects(path)?;
            event::extract_event_edges(&entries, &context, resolver)?
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

pub(crate) enum PythonIntParse {
    Value(i64),
    Invalid,
    OutOfRange,
}

pub(crate) fn json_truthy(value: Option<&Value>) -> bool {
    match value {
        None | Some(Value::Null) => false,
        Some(Value::Bool(value)) => *value,
        Some(Value::Number(value)) => value.as_f64() != Some(0.0),
        Some(Value::String(value)) => !value.is_empty(),
        Some(Value::Array(value)) => !value.is_empty(),
        Some(Value::Object(value)) => !value.is_empty(),
    }
}

pub(crate) fn python_strip(value: &str) -> &str {
    let start = value
        .char_indices()
        .find_map(|(index, ch)| (!is_python_whitespace(ch)).then_some(index))
        .unwrap_or(value.len());
    let end = value
        .char_indices()
        .rev()
        .find_map(|(index, ch)| (!is_python_whitespace(ch)).then_some(index + ch.len_utf8()))
        .unwrap_or(start);
    &value[start..end]
}

fn is_python_whitespace(ch: char) -> bool {
    ch.is_whitespace() || matches!(ch, '\u{001c}'..='\u{001f}')
}

pub(crate) fn python_str(value: &Value, field: &'static str) -> Result<String, EdgeError> {
    match value {
        Value::String(value) => Ok(value.clone()),
        Value::Bool(value) => Ok(if *value { "True" } else { "False" }.to_string()),
        Value::Number(value) => {
            if let Some(value) = value.as_i64() {
                return Ok(value.to_string());
            }
            if let Some(value) = value.as_u64() {
                return Ok(value.to_string());
            }
            Err(EdgeError::UnsupportedPythonStringValue {
                field,
                value_type: "float",
            })
        }
        Value::Null | Value::Array(_) | Value::Object(_) => {
            Err(EdgeError::UnsupportedPythonStringValue {
                field,
                value_type: json_type_name(value),
            })
        }
    }
}

pub(crate) fn edge_str(value: Option<&Value>, field: &'static str) -> Result<String, EdgeError> {
    if !json_truthy(value) {
        return Ok(String::new());
    }
    let Some(value) = value else {
        return Ok(String::new());
    };
    python_str(value, field).map(|value| python_strip(&value).to_string())
}

pub(crate) fn python_title(value: &str) -> String {
    let mut titled = String::new();
    let mut prev_is_cased = false;
    for ch in value.replace('_', " ").chars() {
        if char_is_cased(ch) {
            if prev_is_cased {
                titled.extend(ch.to_lowercase());
            } else {
                titled.extend(ch.to_uppercase());
            }
            prev_is_cased = true;
        } else {
            titled.push(ch);
            prev_is_cased = false;
        }
    }
    titled
}

fn char_is_cased(ch: char) -> bool {
    ch.to_lowercase().to_string() != ch.to_uppercase().to_string()
}

pub(crate) fn edge_int(value: Option<&Value>, field: &'static str) -> Result<i64, EdgeError> {
    match value {
        None | Some(Value::Null) => Ok(0),
        Some(Value::Bool(value)) => Ok(i64::from(*value)),
        Some(Value::Number(value)) => number_to_edge_int(value, field),
        Some(Value::String(value)) => {
            if value.is_empty() {
                return Ok(0);
            }
            match parse_python_int_literal(value) {
                PythonIntParse::Value(value) => Ok(value),
                PythonIntParse::Invalid => Ok(0),
                PythonIntParse::OutOfRange => Err(EdgeError::EdgeIntegerOutOfRange { field }),
            }
        }
        Some(Value::Array(_)) | Some(Value::Object(_)) => Ok(0),
    }
}

fn number_to_edge_int(value: &serde_json::Number, field: &'static str) -> Result<i64, EdgeError> {
    if let Some(value) = value.as_i64() {
        return Ok(value);
    }
    if let Some(value) = value.as_u64() {
        return i64::try_from(value).map_err(|_error| EdgeError::EdgeIntegerOutOfRange { field });
    }
    let Some(value) = value.as_f64() else {
        return Err(EdgeError::EdgeIntegerOutOfRange { field });
    };
    if value == 0.0 {
        return Ok(0);
    }
    let truncated = value.trunc();
    if truncated < i64::MIN as f64 || truncated >= 9_223_372_036_854_776_000.0 {
        return Err(EdgeError::EdgeIntegerOutOfRange { field });
    }
    Ok(truncated as i64)
}

pub(crate) fn parse_python_int_literal(value: &str) -> PythonIntParse {
    let trimmed = python_strip(value);
    if trimmed.is_empty() {
        return PythonIntParse::Invalid;
    }
    let digits = trimmed
        .strip_prefix(['+', '-'])
        .filter(|rest| !rest.is_empty())
        .unwrap_or(trimmed);
    if !digits.bytes().all(|byte| byte.is_ascii_digit()) {
        return PythonIntParse::Invalid;
    }
    match trimmed.parse::<i128>() {
        Ok(value) => match i64::try_from(value) {
            Ok(value) => PythonIntParse::Value(value),
            Err(_error) => PythonIntParse::OutOfRange,
        },
        Err(_error) => PythonIntParse::OutOfRange,
    }
}

pub(crate) fn edge_value_for_text(
    value: Option<&Value>,
    field: &'static str,
) -> Result<EdgeValue, EdgeError> {
    match value {
        None | Some(Value::Null) => Ok(EdgeValue::Null),
        Some(Value::String(value)) => Ok(EdgeValue::Text(value.clone())),
        Some(Value::Bool(value)) => Ok(EdgeValue::Text(if *value { "1" } else { "0" }.to_string())),
        Some(Value::Number(value)) => edge_value_number(value, field),
        Some(Value::Array(_)) => Err(EdgeError::UnsupportedEdgeValue {
            field,
            value_type: "array",
        }),
        Some(Value::Object(_)) => Err(EdgeError::UnsupportedEdgeValue {
            field,
            value_type: "object",
        }),
    }
}

pub(crate) fn edge_value_for_ts(
    value: Option<&Value>,
    field: &'static str,
) -> Result<EdgeValue, EdgeError> {
    match value {
        Some(Value::Bool(value)) => Ok(EdgeValue::Int(i64::from(*value))),
        _ => edge_value_for_text(value, field),
    }
}

fn edge_value_number(
    value: &serde_json::Number,
    field: &'static str,
) -> Result<EdgeValue, EdgeError> {
    if let Some(value) = value.as_i64() {
        return Ok(EdgeValue::Int(value));
    }
    if let Some(value) = value.as_u64() {
        return i64::try_from(value)
            .map(EdgeValue::Int)
            .map_err(|_error| EdgeError::EdgeIntegerOutOfRange { field });
    }
    let Some(value) = value.as_f64() else {
        return Err(EdgeError::EdgeIntegerOutOfRange { field });
    };
    Ok(EdgeValue::Float(value))
}

pub(crate) fn json_type_name(value: &Value) -> &'static str {
    match value {
        Value::Null => "null",
        Value::Bool(_) => "bool",
        Value::Number(value) if value.as_i64().is_some() || value.as_u64().is_some() => "int",
        Value::Number(_) => "float",
        Value::String(_) => "string",
        Value::Array(_) => "array",
        Value::Object(_) => "object",
    }
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
            src_name: EdgeValue::Text(format!("{src} name")),
            dst_name: EdgeValue::Text(format!("{dst} name")),
            day: day.map(str::to_string),
            facet: Some("Work".to_string()),
            source: "test".to_string(),
            path: "synthetic".to_string(),
            anchor: Some("anchor".to_string()),
            label: EdgeValue::Text(String::new()),
            ts: EdgeValue::Int(0),
            weight: 1,
        }
    }

    fn edge_context(rel: &str, day: &str, facet: &str) -> EdgeContext {
        EdgeContext {
            path: rel.to_string(),
            day: day.to_string(),
            facet: facet.to_string(),
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
        assert_eq!(
            normalized[0].src_name,
            EdgeValue::Text("alpha name".to_string())
        );
        assert_eq!(
            normalized[0].dst_name,
            EdgeValue::Text("zeta name".to_string())
        );
        assert_eq!(normalized[0].facet.as_deref(), Some("work"));
        assert_eq!(normalized[0].directed, 0);

        let observation = normalize_edges(vec![EdgeRow {
            src: "z_source".to_string(),
            dst: "a_target".to_string(),
            kind: "works-with".to_string(),
            src_name: EdgeValue::Null,
            dst_name: EdgeValue::Text("Target Name".to_string()),
            day: Some("20260430".to_string()),
            facet: Some("Work".to_string()),
            source: "observation".to_string(),
            path: "synthetic".to_string(),
            anchor: Some("1".to_string()),
            label: EdgeValue::Null,
            ts: EdgeValue::Int(1),
            weight: 1,
        }])
        .expect("normalize observation");
        assert_eq!(observation[0].src, "a_target");
        assert_eq!(observation[0].dst, "z_source");
        assert_eq!(
            observation[0].src_name,
            EdgeValue::Text("Target Name".to_string())
        );
        assert_eq!(observation[0].dst_name, EdgeValue::Null);

        let directed = normalize_edges(vec![row("zeta", "alpha", "mentioned", Some("20260430"))])
            .expect("normalize directed row");
        assert_eq!(directed[0].src, "zeta");
        assert_eq!(directed[0].dst, "alpha");
        assert_eq!(
            directed[0].src_name,
            EdgeValue::Text("zeta name".to_string())
        );
        assert_eq!(
            directed[0].dst_name,
            EdgeValue::Text("alpha name".to_string())
        );
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
    fn edge_int_coercion_matches_python_table_and_overflows_loudly() {
        let cases = [
            (Value::Null, 0),
            (json!(0), 0),
            (json!(""), 0),
            (json!(false), 0),
            (json!(true), 1),
            (json!(12), 12),
            (json!(-12), -12),
            (json!(1.9), 1),
            (json!(-1.9), -1),
            (json!("12"), 12),
            (json!(" 12 "), 12),
            (json!("12.5"), 0),
            (json!("0x10"), 0),
            (json!([]), 0),
            (json!({}), 0),
            (json!("abc"), 0),
        ];
        for (value, expected) in cases {
            assert_eq!(
                edge_int(Some(&value), "created_at"),
                Ok(expected),
                "{value}"
            );
        }
        assert_eq!(
            edge_int(Some(&json!(1e19)), "created_at"),
            Err(EdgeError::EdgeIntegerOutOfRange {
                field: "created_at"
            })
        );
        assert_eq!(
            edge_int(Some(&json!("999999999999999999999")), "created_at"),
            Err(EdgeError::EdgeIntegerOutOfRange {
                field: "created_at"
            })
        );
    }

    #[test]
    fn python_stringification_and_edge_str_keep_falsy_first_semantics() {
        assert_eq!(python_str(&json!(true), "title"), Ok("True".to_string()));
        assert_eq!(
            python_str(&json!(123), "observed_at"),
            Ok("123".to_string())
        );
        assert_eq!(edge_str(Some(&json!(false)), "title"), Ok(String::new()));
        assert_eq!(edge_str(Some(&json!(0)), "title"), Ok(String::new()));
        assert_eq!(edge_str(Some(&json!(0.0)), "title"), Ok(String::new()));
        assert_eq!(edge_str(Some(&json!([])), "title"), Ok(String::new()));
        assert_eq!(edge_str(Some(&json!({})), "title"), Ok(String::new()));
        assert_eq!(
            edge_str(Some(&json!("  named  ")), "title"),
            Ok("named".to_string())
        );
        assert_eq!(
            edge_str(Some(&json!(1.5)), "title"),
            Err(EdgeError::UnsupportedPythonStringValue {
                field: "title",
                value_type: "float"
            })
        );
        assert_eq!(
            edge_str(Some(&json!([1])), "title"),
            Err(EdgeError::UnsupportedPythonStringValue {
                field: "title",
                value_type: "array"
            })
        );
        assert_eq!(
            edge_str(Some(&json!({"a": 1})), "title"),
            Err(EdgeError::UnsupportedPythonStringValue {
                field: "title",
                value_type: "object"
            })
        );
    }

    #[test]
    fn python_title_matches_cased_boundary_behavior() {
        assert_eq!(python_title("edge_sync_review"), "Edge Sync Review");
        assert_eq!(python_title("don't_ship"), "Don'T Ship");
        assert_eq!(python_title("q3-plan"), "Q3-Plan");
        assert_eq!(python_title("a1b2"), "A1B2");
        assert_eq!(python_title("ünicode_thing"), "Ünicode Thing");
    }

    #[test]
    fn observation_edges_preserve_skip_drop_and_passthrough_swap() {
        let root = temp_root("observation");
        let rel = "facets/work/entities/z_source/observations.jsonl";
        write_jsonl(
            &root,
            rel,
            &[
                json!({"observed_at":1777556000000_i64,"source_day":"20260430","relation":{"kind":"works-with","target_entity_id":"a_target","target_name":"Target Name","note":"Plans"}}),
                json!({"observed_at":1777556100000_i64,"source_day":"20260430","relation":{"kind":"knows","target_entity_id":null,"target_name":"Nobody","note":"drop"}}),
                json!({"observed_at":1777556150000_i64,"source_day":"20260430","relation":{"kind":"knows","target_entity_id":"","target_name":"Blank","note":"drop"}}),
                json!({"observed_at":1777556200000_i64,"source_day":"20260430","relation":{"kind":"knows","target_entity_id":"z_source","target_name":"Self","note":"skip"}}),
                json!({"observed_at":true,"source_day":"not-a-day","relation":{"kind":"reports-to","target_entity_id":"b_directed","target_name":false,"note":true}}),
            ],
        );
        let mut resolver = EdgeResolver::new(&root);
        resolver.begin_file();
        let extracted = extract_file_edges(rel, &root.join(rel), &mut resolver)
            .expect("extract observation edges");
        assert_eq!(resolver.drops(), 2);
        assert_eq!(extracted.rows.len(), 2);
        let row = &extracted.rows[0];
        assert_eq!(row.src, "a_target");
        assert_eq!(row.dst, "z_source");
        assert_eq!(row.src_name, EdgeValue::Text("Target Name".to_string()));
        assert_eq!(row.dst_name, EdgeValue::Null);
        assert_eq!(row.anchor.as_deref(), Some("1777556000000"));
        assert_eq!(row.label, EdgeValue::Text("Plans".to_string()));
        assert_eq!(row.ts, EdgeValue::Int(1777556000000));
        let directed = &extracted.rows[1];
        assert_eq!(directed.src, "z_source");
        assert_eq!(directed.dst, "b_directed");
        assert_eq!(directed.src_name, EdgeValue::Null);
        assert_eq!(directed.dst_name, EdgeValue::Text("0".to_string()));
        assert_eq!(directed.anchor.as_deref(), Some("True"));
        assert_eq!(directed.label, EdgeValue::Text("1".to_string()));
        assert_eq!(directed.ts, EdgeValue::Int(1));
        assert_eq!(directed.day, None);
        fs::remove_dir_all(root).expect("cleanup observation root");
    }

    #[test]
    fn observation_failures_are_typed_before_insert() {
        let context = edge_context(
            "facets/work/entities/z_source/observations.jsonl",
            "",
            "work",
        );
        let mut drops = crate::edges::candidates::EdgeDropCounter::default();
        let missing_kind = vec![
            json!({"relation":{"target_entity_id":"a_target"}})
                .as_object()
                .expect("object")
                .clone(),
        ];
        assert_eq!(
            observation::extract_observation_edges(&missing_kind, &context, &mut drops),
            Err(EdgeError::MissingObservationRelationKind)
        );

        let non_string_target = vec![
            json!({"relation":{"kind":"works-with","target_entity_id":123}})
                .as_object()
                .expect("object")
                .clone(),
        ];
        assert_eq!(
            observation::extract_observation_edges(&non_string_target, &context, &mut drops),
            Err(EdgeError::InvalidObservationTargetEntityId)
        );

        let container_label = vec![
            json!({"observed_at":1,"relation":{"kind":"works-with","target_entity_id":"a_target","note":{"bad":true}}})
                .as_object()
                .expect("object")
                .clone(),
        ];
        assert!(matches!(
            observation::extract_observation_edges(&container_label, &context, &mut drops),
            Err(EdgeError::UnsupportedEdgeValue {
                field: "label",
                value_type: "object"
            })
        ));
    }

    #[test]
    fn activity_edges_cover_participation_story_relations_and_titles() {
        let rel = "facets/work/activities/20260430.jsonl";
        let context = edge_context(rel, "20260430", "work");
        let rows = activity::extract_activity_edges(
            &[json!({
                "id":"story-commitments-1",
                "created_at":1777554000000_i64,
                "activity":"don't_ship",
                "participation":[
                    {"role":"attendee","entity_id":"edge_mina"},
                    {"role":"attendee","entity_id":"edge_ravi"},
                    {"role":"attendee","entity_id":"edge_ravi"},
                    {"role":"mentioned","entity_id":"edge_tessa"}
                ],
                "commitments":[{"action":"Send the proposal","owner_entity_id":"edge_mina","counterparty_entity_id":"edge_ravi"}],
                "closures":[{"action":"Confirm the handoff","owner_entity_id":"edge_tessa","counterparty_entity_id":"edge_mina"}],
                "decisions":[
                    {"action":"Skip self","owner_entity_id":"edge_mina","counterparty_entity_id":"edge_mina"},
                    {"action":"Use the stable plan together","owner_entity_id":"edge_mina","counterparty_entity_id":"edge_ravi"}
                ],
                "relations":[{"from":"Mina Edge","to":"Ravi Edge","from_entity_id":"edge_mina","to_entity_id":"edge_ravi","kind":"works-with","note":"Runs planning together","quote":"Let's pair on this"}]
            })
            .as_object()
            .expect("object")
            .clone()],
            &context,
        )
        .expect("activity edges");
        assert_eq!(rows.len(), 5);
        assert_eq!(rows[0].source, "participation");
        assert_eq!(rows[0].label, EdgeValue::Text("Don'T Ship".to_string()));
        assert!(rows.iter().any(|row| {
            row.source == "commitment"
                && row.label == EdgeValue::Text("Send the proposal".to_string())
        }));
        assert!(rows.iter().any(|row| {
            row.source == "closure" && row.src == "edge_tessa" && row.dst == "edge_mina"
        }));
        assert!(rows.iter().any(|row| {
            row.source == "decision"
                && row.label == EdgeValue::Text("Use the stable plan together".to_string())
        }));
        assert!(rows.iter().any(|row| {
            row.source == "relation"
                && row.kind == "works-with"
                && row.label
                    == EdgeValue::Text(
                        "Runs planning together — \"Let's pair on this\"".to_string(),
                    )
        }));

        let untitled = activity::extract_activity_edges(
            &[json!({
                "id":"edge_sync_review",
                "activity":"   ",
                "participation":[
                    {"role":"attendee","entity_id":"a"},
                    {"role":"attendee","entity_id":"b"}
                ]
            })
            .as_object()
            .expect("object")
            .clone()],
            &context,
        )
        .expect("untitled activity edges");
        assert_eq!(
            untitled[0].label,
            EdgeValue::Text("untitled activity".to_string())
        );
    }

    #[test]
    fn event_edges_use_shared_title_stringification_and_surface_errors() {
        let root = temp_root("event");
        seed_entity(&root, "evan", "Evan Edge");
        seed_entity(&root, "fay", "Fay Edge");
        let context = edge_context("facets/work/events/20260430.jsonl", "20260430", "work");
        let mut resolver = EdgeResolver::new(&root);
        resolver.begin_file();

        let rows = event::extract_event_edges(
            &[
                json!({"title":123,"start":"01:02:03","participants":[" Evan Edge ","Fay Edge","Fay Edge","Unknown"]})
                    .as_object()
                    .expect("object")
                    .clone(),
                json!({"title":"","participants":["Evan Edge","Fay Edge"]})
                    .as_object()
                    .expect("object")
                    .clone(),
            ],
            &context,
            &mut resolver,
        )
        .expect("event edges");
        assert_eq!(rows.len(), 1);
        assert_eq!(rows[0].source, "event-legacy");
        assert_eq!(rows[0].anchor.as_deref(), Some(""));
        assert_eq!(rows[0].label, EdgeValue::Text("123".to_string()));
        assert_eq!(rows[0].src_name, EdgeValue::Text("Evan Edge".to_string()));
        assert_eq!(rows[0].dst_name, EdgeValue::Text("Fay Edge".to_string()));

        let float_title = [json!({"title":1.5,"participants":["Evan Edge","Fay Edge"]})
            .as_object()
            .expect("object")
            .clone()];
        assert_eq!(
            event::extract_event_edges(&float_title, &context, &mut resolver),
            Err(EdgeError::UnsupportedPythonStringValue {
                field: "title",
                value_type: "float"
            })
        );

        let container_title = [json!({"title":[1],"participants":["Evan Edge","Fay Edge"]})
            .as_object()
            .expect("object")
            .clone()];
        assert_eq!(
            event::extract_event_edges(&container_title, &context, &mut resolver),
            Err(EdgeError::UnsupportedPythonStringValue {
                field: "title",
                value_type: "array"
            })
        );
        fs::remove_dir_all(root).expect("cleanup event root");
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
        assert_eq!(
            extracted.rows[0].src_name,
            EdgeValue::Text("Alice Edge".to_string())
        );
        assert_eq!(extracted.rows[0].label, EdgeValue::Text(String::new()));
        assert_eq!(extracted.rows[0].ts, EdgeValue::Int(0));
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
