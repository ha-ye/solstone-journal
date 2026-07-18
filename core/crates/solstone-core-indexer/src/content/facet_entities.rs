// SPDX-License-Identifier: AGPL-3.0-only
// Copyright (c) 2026 sol pbc

use serde_json::Value;

use super::{IndexChunk, JsonObject, ProducedChunks, display_value, json_truthy, titleize};

const SKIP_FIELDS: &[&str] = &[
    "id",
    "type",
    "name",
    "description",
    "updated_at",
    "attached_at",
    "last_seen",
    "detached",
    "tags",
    "aka",
];

pub(super) fn render(rel: &str, records: &[JsonObject]) -> ProducedChunks {
    let chunks = records.iter().map(render_record).collect();
    ProducedChunks {
        chunks,
        agent_override: Some(agent_for_rel(rel).to_string()),
    }
}

fn agent_for_rel(rel: &str) -> &'static str {
    let stem = file_stem(rel);
    if !stem.is_empty() && stem.chars().all(|ch| ch.is_ascii_digit()) {
        "entity:detected"
    } else {
        "entity:attached"
    }
}

fn file_stem(rel: &str) -> &str {
    let filename = rel.rsplit(['/', '\\']).next().unwrap_or(rel);
    filename
        .rsplit_once('.')
        .map(|(stem, _extension)| stem)
        .unwrap_or(filename)
}

fn render_record(record: &JsonObject) -> IndexChunk {
    let entity_type = record
        .get("type")
        .map(display_value)
        .unwrap_or_else(|| "Unknown".to_string());
    let name = record
        .get("name")
        .map(display_value)
        .unwrap_or_else(|| "Unnamed".to_string());
    let mut lines = vec![format!("### {entity_type}: {name}"), String::new()];

    if let Some(description) = record
        .get("description")
        .filter(|value| json_truthy(Some(value)))
    {
        lines.push(display_value(description));
    } else {
        lines.push("*(No description available)*".to_string());
    }
    lines.push(String::new());

    append_array_field(record, "tags", "**Tags:**", &mut lines);
    append_array_field(record, "aka", "**Also known as:**", &mut lines);

    for (key, value) in record {
        if SKIP_FIELDS.contains(&key.as_str()) {
            continue;
        }
        let value_display = match value {
            Value::Array(items) => items
                .iter()
                .map(display_value)
                .collect::<Vec<_>>()
                .join(", "),
            _ => display_value(value),
        };
        lines.push(format!("**{}:** {value_display}", titleize(key)));
    }

    lines.push(String::new());
    IndexChunk {
        content: lines.join("\n"),
    }
}

fn append_array_field(record: &JsonObject, key: &str, label: &str, lines: &mut Vec<String>) {
    let Some(value) = record.get(key).filter(|value| json_truthy(Some(value))) else {
        return;
    };
    let Value::Array(items) = value else {
        return;
    };
    let joined = items
        .iter()
        .map(display_value)
        .collect::<Vec<_>>()
        .join(", ");
    lines.push(format!("{label} {joined}"));
}
