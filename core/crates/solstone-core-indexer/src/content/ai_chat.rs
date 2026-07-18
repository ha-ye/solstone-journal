// SPDX-License-Identifier: AGPL-3.0-only
// Copyright (c) 2026 sol pbc

use serde_json::Value;

use super::{IndexChunk, JsonObject, ProducedChunks};

pub(super) fn render(rel: &str, records: &[JsonObject]) -> ProducedChunks {
    if records.is_empty() {
        return ProducedChunks {
            chunks: Vec::new(),
            agent_override: None,
        };
    }

    let source_key = rel
        .split('/')
        .find_map(|part| part.strip_prefix("import."))
        .map(str::to_lowercase)
        .unwrap_or_else(|| "ai_chat".to_string());
    let mut chunks = Vec::new();

    for record in records {
        if !record.contains_key("start") {
            continue;
        }
        let speaker = record.get("speaker").and_then(Value::as_str).unwrap_or("");
        let text = record.get("text").and_then(Value::as_str).unwrap_or("");
        if text.is_empty() {
            continue;
        }
        chunks.push(IndexChunk {
            content: format!("**{speaker}:** {text}"),
        });
    }

    ProducedChunks {
        chunks,
        agent_override: Some(format!("import.{source_key}")),
    }
}

#[cfg(test)]
mod tests {
    use super::*;
    use crate::content::parse_jsonl_objects;

    #[test]
    fn renders_only_start_bearing_non_empty_turns() {
        let records = parse_jsonl_objects(
            r#"{"model":"claude-3"}
{"start":"00:00:01","speaker":"User","text":"Hello"}
{"start":"00:00:02","speaker":"Assistant","text":""}
{"speaker":"Narrator","text":"metadata-like"}
"#,
        );
        let produced = render(
            "20260101/import.claude/thread_a/conversation_transcript.jsonl",
            &records,
        );

        assert_eq!(produced.agent_override.as_deref(), Some("import.claude"));
        assert_eq!(produced.chunks.len(), 1);
        assert_eq!(produced.chunks[0].content, "**User:** Hello");
    }
}
