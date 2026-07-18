// SPDX-License-Identifier: AGPL-3.0-only
// Copyright (c) 2026 sol pbc

use super::{IndexChunk, JsonObject, display_value, json_truthy};

pub(super) fn render(records: &[JsonObject]) -> Vec<IndexChunk> {
    records.iter().map(render_record).collect()
}

fn render_record(record: &JsonObject) -> IndexChunk {
    let content = record.get("content").map(display_value).unwrap_or_default();
    let mut markdown = format!("- {content}");
    if let Some(source_day) = record
        .get("source_day")
        .filter(|value| json_truthy(Some(value)))
    {
        markdown.push_str(" (observed: ");
        markdown.push_str(&display_value(source_day));
        markdown.push(')');
    }
    IndexChunk { content: markdown }
}
