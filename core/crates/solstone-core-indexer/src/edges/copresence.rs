// SPDX-License-Identifier: AGPL-3.0-only
// Copyright (c) 2026 sol pbc

use std::collections::BTreeSet;

use serde_json::{Map, Value};

use super::candidates::EdgeResolver;
use super::{EdgeContext, EdgeError, EdgeRow, EdgeValue};

type JsonObject = Map<String, Value>;

struct ResolvedPresence {
    entity_id: String,
    name: String,
    segments: BTreeSet<String>,
}

pub(crate) fn extract_copresence_edges(
    entries: &[JsonObject],
    context: &EdgeContext,
    resolver: &mut EdgeResolver,
) -> Result<Vec<EdgeRow>, EdgeError> {
    let mut resolved = Vec::new();
    for entry in entries {
        let Some(Value::String(name)) = entry.get("name") else {
            continue;
        };
        if name.trim().is_empty() {
            continue;
        }
        let Some(Value::Array(segments)) = entry.get("segments") else {
            continue;
        };
        let segment_ids: BTreeSet<String> = segments
            .iter()
            .filter_map(|segment| match segment {
                Value::String(segment) if !segment.trim().is_empty() => Some(segment.clone()),
                _ => None,
            })
            .collect();
        if segment_ids.is_empty() {
            continue;
        }
        let Some(entity_id) = resolver.resolve(context, name)? else {
            continue;
        };
        resolved.push(ResolvedPresence {
            entity_id,
            name: name.trim().to_string(),
            segments: segment_ids,
        });
    }

    let mut rows = Vec::new();
    for left_index in 0..resolved.len() {
        for right in resolved.iter().skip(left_index + 1) {
            let left = &resolved[left_index];
            if left.entity_id == right.entity_id {
                continue;
            }
            let shared: Vec<String> = left
                .segments
                .intersection(&right.segments)
                .cloned()
                .collect();
            let Some(anchor) = shared.first() else {
                continue;
            };
            rows.push(EdgeRow {
                src: left.entity_id.clone(),
                dst: right.entity_id.clone(),
                kind: "co-present".to_string(),
                src_name: EdgeValue::Text(left.name.clone()),
                dst_name: EdgeValue::Text(right.name.clone()),
                day: Some(context.day.clone()),
                facet: Some(context.facet.clone()),
                source: "co-presence".to_string(),
                path: context.path.clone(),
                anchor: Some(anchor.clone()),
                label: EdgeValue::Text(String::new()),
                ts: EdgeValue::Int(0),
                weight: shared.len() as i64,
            });
        }
    }
    Ok(rows)
}
