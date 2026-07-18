// SPDX-License-Identifier: AGPL-3.0-only
// Copyright (c) 2026 sol pbc

mod action_logs;
mod activities;
mod ai_chat;
mod browser;
mod chat;
mod day_accumulator;
mod events;
mod imports;

use std::path::Path;

use glob::{MatchOptions, Pattern};
use serde_json::{Map, Value};

use crate::chunker::chunk_markdown;

#[derive(Debug, Clone, Copy, PartialEq, Eq)]
pub enum Family {
    Markdown,
    Event,
    Activity,
    ActionLog,
    StructuredImport,
    AiChat,
    Chat,
    Browser,
    DayAccumulator,
}

#[derive(Debug, Clone, PartialEq, Eq)]
pub struct IndexChunk {
    pub content: String,
}

#[derive(Debug, Clone, PartialEq, Eq)]
pub struct ProducedChunks {
    pub chunks: Vec<IndexChunk>,
    pub agent_override: Option<String>,
}

#[derive(Debug, Clone, Copy, PartialEq, Eq)]
pub(crate) enum PatternRoot {
    Structural,
    DayRooted,
}

#[derive(Debug, Clone, Copy, PartialEq, Eq)]
pub(crate) struct FamilyPattern {
    pub pattern: &'static str,
    pub family: Family,
    pub root: PatternRoot,
}

pub(crate) const INDEX_FAMILY_PATTERNS: &[FamilyPattern] = &[
    FamilyPattern {
        pattern: "*/talents/*.md",
        family: Family::Markdown,
        root: PatternRoot::DayRooted,
    },
    FamilyPattern {
        pattern: "*/talents/*.jsonl",
        family: Family::DayAccumulator,
        root: PatternRoot::DayRooted,
    },
    FamilyPattern {
        pattern: "*/*/*/talents/*.md",
        family: Family::Markdown,
        root: PatternRoot::DayRooted,
    },
    FamilyPattern {
        pattern: "*/*/*/talents/*/*.md",
        family: Family::Markdown,
        root: PatternRoot::DayRooted,
    },
    FamilyPattern {
        pattern: "*/import.*/*/*_transcript.md",
        family: Family::Markdown,
        root: PatternRoot::DayRooted,
    },
    FamilyPattern {
        pattern: "*/import.*/*/imported.md",
        family: Family::Markdown,
        root: PatternRoot::DayRooted,
    },
    FamilyPattern {
        pattern: "*/chat/*/chat.jsonl",
        family: Family::Chat,
        root: PatternRoot::DayRooted,
    },
    FamilyPattern {
        pattern: "*/*/*/browser_*.jsonl",
        family: Family::Browser,
        root: PatternRoot::DayRooted,
    },
    FamilyPattern {
        pattern: "*/import.*/imported.jsonl",
        family: Family::StructuredImport,
        root: PatternRoot::DayRooted,
    },
    FamilyPattern {
        pattern: "*/import.chatgpt/*/conversation_transcript.jsonl",
        family: Family::AiChat,
        root: PatternRoot::DayRooted,
    },
    FamilyPattern {
        pattern: "*/import.claude/*/conversation_transcript.jsonl",
        family: Family::AiChat,
        root: PatternRoot::DayRooted,
    },
    FamilyPattern {
        pattern: "*/import.gemini/*/conversation_transcript.jsonl",
        family: Family::AiChat,
        root: PatternRoot::DayRooted,
    },
    FamilyPattern {
        pattern: "*/import.chatgpt/*/imported_audio.jsonl",
        family: Family::AiChat,
        root: PatternRoot::DayRooted,
    },
    FamilyPattern {
        pattern: "*/import.claude/*/imported_audio.jsonl",
        family: Family::AiChat,
        root: PatternRoot::DayRooted,
    },
    FamilyPattern {
        pattern: "*/import.gemini/*/imported_audio.jsonl",
        family: Family::AiChat,
        root: PatternRoot::DayRooted,
    },
    FamilyPattern {
        pattern: "config/actions/*.jsonl",
        family: Family::ActionLog,
        root: PatternRoot::Structural,
    },
    FamilyPattern {
        pattern: "facets/*/events/*.jsonl",
        family: Family::Event,
        root: PatternRoot::Structural,
    },
    FamilyPattern {
        pattern: "facets/*/activities/*.jsonl",
        family: Family::Activity,
        root: PatternRoot::Structural,
    },
    FamilyPattern {
        pattern: "facets/*/logs/*.jsonl",
        family: Family::ActionLog,
        root: PatternRoot::Structural,
    },
    FamilyPattern {
        pattern: "facets/*/activities/*/*/*.md",
        family: Family::Markdown,
        root: PatternRoot::Structural,
    },
    FamilyPattern {
        pattern: "facets/*/news/*.md",
        family: Family::Markdown,
        root: PatternRoot::Structural,
    },
    FamilyPattern {
        pattern: "reflections/weekly/*.md",
        family: Family::Markdown,
        root: PatternRoot::Structural,
    },
    FamilyPattern {
        pattern: "imports/*/summary.md",
        family: Family::Markdown,
        root: PatternRoot::Structural,
    },
    FamilyPattern {
        pattern: "apps/*/talents/*.md",
        family: Family::Markdown,
        root: PatternRoot::Structural,
    },
];

pub fn classify(rel: &str) -> Option<Family> {
    let options = MatchOptions {
        case_sensitive: true,
        require_literal_separator: true,
        require_literal_leading_dot: false,
    };
    let rel_path = Path::new(rel);
    for spec in INDEX_FAMILY_PATTERNS {
        let pattern = Pattern::new(spec.pattern).expect("index family pattern should be valid");
        if pattern.matches_path_with(rel_path, options) {
            return Some(spec.family);
        }
    }
    None
}

pub(crate) fn patterns_for_root(root: PatternRoot) -> impl Iterator<Item = &'static FamilyPattern> {
    INDEX_FAMILY_PATTERNS
        .iter()
        .filter(move |spec| spec.root == root)
}

pub fn produce_chunks(family: Family, rel: &str, text: &str) -> ProducedChunks {
    match family {
        Family::Markdown => ProducedChunks {
            chunks: chunk_markdown(text)
                .into_iter()
                .map(|chunk| IndexChunk {
                    content: chunk.markdown,
                })
                .collect(),
            agent_override: None,
        },
        Family::Event => ProducedChunks {
            chunks: events::render(&parse_jsonl_objects(text)),
            agent_override: Some("event".to_string()),
        },
        Family::Activity => ProducedChunks {
            chunks: activities::render(&parse_jsonl_objects(text)),
            agent_override: Some("activity".to_string()),
        },
        Family::ActionLog => ProducedChunks {
            chunks: action_logs::render(&parse_jsonl_objects(text)),
            agent_override: Some("action".to_string()),
        },
        Family::StructuredImport => imports::render(&parse_jsonl_objects(text)),
        Family::AiChat => ai_chat::render(rel, &parse_jsonl_objects(text)),
        Family::Chat => chat::render(&parse_jsonl_objects(text)),
        Family::Browser => browser::render(&parse_jsonl_objects(text)),
        Family::DayAccumulator => day_accumulator::render(rel, &parse_jsonl_objects(text)),
    }
}

type JsonObject = Map<String, Value>;

fn parse_jsonl_objects(text: &str) -> Vec<JsonObject> {
    text.lines()
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
        .collect()
}

fn json_falsy(value: Option<&Value>) -> bool {
    match value {
        None | Some(Value::Null) => true,
        Some(Value::Bool(value)) => !value,
        Some(Value::Number(value)) => value.as_f64() == Some(0.0),
        Some(Value::String(value)) => value.is_empty(),
        Some(Value::Array(value)) => value.is_empty(),
        Some(Value::Object(value)) => value.is_empty(),
    }
}

fn json_truthy(value: Option<&Value>) -> bool {
    !json_falsy(value)
}

fn display_value(value: &Value) -> String {
    match value {
        Value::Null => String::new(),
        Value::Bool(value) => value.to_string(),
        Value::Number(value) => value.to_string(),
        Value::String(value) => value.clone(),
        Value::Array(_) | Value::Object(_) => serde_json::to_string(value).unwrap_or_default(),
    }
}

fn truthy_display(record: &JsonObject, key: &str) -> Option<String> {
    let value = record.get(key)?;
    if json_falsy(Some(value)) {
        None
    } else {
        Some(display_value(value))
    }
}

fn stripped_truthy_display(record: &JsonObject, key: &str) -> Option<String> {
    let value = record.get(key)?;
    if json_falsy(Some(value)) {
        return None;
    }
    let stripped = display_value(value).trim().to_string();
    if stripped.is_empty() {
        None
    } else {
        Some(stripped)
    }
}

fn display_or_default(record: &JsonObject, key: &str, default: &str) -> String {
    truthy_display(record, key).unwrap_or_else(|| default.to_string())
}

fn capitalize(value: &str) -> String {
    let mut chars = value.chars();
    let Some(first) = chars.next() else {
        return String::new();
    };
    let mut result = String::new();
    result.extend(first.to_uppercase());
    result.push_str(&chars.as_str().to_lowercase());
    result
}

fn titleize(value: &str) -> String {
    value
        .replace('_', " ")
        .split_whitespace()
        .map(capitalize)
        .collect::<Vec<_>>()
        .join(" ")
}

fn truncate_string(value: &str, max_chars: usize) -> String {
    if value.chars().count() <= max_chars {
        return value.to_string();
    }
    let mut truncated: String = value.chars().take(max_chars).collect();
    truncated.push_str("...");
    truncated
}

#[cfg(test)]
mod tests {
    use super::*;
    use crate::chunker::chunk_markdown;

    #[test]
    fn classifies_indexable_families() {
        assert_eq!(classify("20240101/talents/flow.md"), Some(Family::Markdown));
        assert_eq!(
            classify("20260304/talents/pulse.jsonl"),
            Some(Family::DayAccumulator)
        );
        assert_ne!(
            classify("20260304/talents/pulse.md"),
            Some(Family::DayAccumulator)
        );
        assert_eq!(
            classify("20260304/default/090000_300/talents/sense.jsonl"),
            None
        );
        assert_eq!(classify("20260304/talents/morning_briefing.json"), None);
        assert_eq!(
            classify("20240101/default/123456_300/talents/audio.md"),
            Some(Family::Markdown)
        );
        assert_eq!(
            classify("20240101/default/123456_300/talents/work/audio.md"),
            Some(Family::Markdown)
        );
        assert_eq!(
            classify("20260101/import.ics/090000_300/event_transcript.md"),
            Some(Family::Markdown)
        );
        assert_eq!(
            classify("20260101/import.ics/090000_300/imported.md"),
            Some(Family::Markdown)
        );
        assert_eq!(
            classify("20260101/import.ics/imported.jsonl"),
            Some(Family::StructuredImport)
        );
        assert_ne!(
            classify("20260101/import.ics/imported.jsonl"),
            Some(Family::AiChat)
        );
        assert_eq!(
            classify("20260101/import.claude/thread_a/conversation_transcript.jsonl"),
            Some(Family::AiChat)
        );
        assert_ne!(
            classify("20260101/import.claude/thread_a/conversation_transcript.jsonl"),
            Some(Family::StructuredImport)
        );
        assert_eq!(
            classify("20260101/import.chatgpt/conv_b/imported_audio.jsonl"),
            Some(Family::AiChat)
        );
        assert_eq!(
            classify("20260508/chat/120000_300/chat.jsonl"),
            Some(Family::Chat)
        );
        assert_ne!(
            classify("20260508/chat/120000_300/chat.jsonl"),
            Some(Family::Browser)
        );
        assert_eq!(
            classify("20260703/suze.browser/000141_317/browser_mail-google-com.jsonl"),
            Some(Family::Browser)
        );
        assert_ne!(
            classify("20260703/suze.browser/000141_317/browser_mail-google-com.jsonl"),
            Some(Family::AiChat)
        );
        assert_ne!(
            classify("20260703/suze.browser/000141_317/browser_mail-google-com.jsonl"),
            Some(Family::Chat)
        );
        assert_eq!(
            classify("facets/work/news/20240101.md"),
            Some(Family::Markdown)
        );
        assert_eq!(
            classify("imports/20260101_120000/summary.md"),
            Some(Family::Markdown)
        );
        assert_eq!(
            classify("apps/todos/talents/digest.md"),
            Some(Family::Markdown)
        );
        assert_eq!(
            classify("config/actions/20240101.jsonl"),
            Some(Family::ActionLog)
        );
        assert_eq!(
            classify("facets/work/events/20240101.jsonl"),
            Some(Family::Event)
        );
        assert_eq!(
            classify("facets/work/activities/20240101.jsonl"),
            Some(Family::Activity)
        );
        assert_eq!(
            classify("facets/work/logs/20240101.jsonl"),
            Some(Family::ActionLog)
        );
        assert_eq!(classify("notes/foo.txt"), None);
        assert_eq!(classify("facets/work/entities/foo.jsonl"), None);
        assert_eq!(classify("20240101/default/123456_300/audio.jsonl"), None);
    }

    #[test]
    fn markdown_producer_wraps_chunker_without_content_changes() {
        let text = "# Title\n\nIntro\n\n## Section\n\nBody";
        let expected: Vec<String> = chunk_markdown(text)
            .into_iter()
            .map(|chunk| chunk.markdown)
            .collect();
        let produced = produce_chunks(Family::Markdown, "20240101/talents/flow.md", text);
        let got: Vec<String> = produced
            .chunks
            .into_iter()
            .map(|chunk| chunk.content)
            .collect();
        assert_eq!(got, expected);
        assert_eq!(produced.agent_override, None);
    }

    #[test]
    fn jsonl_parser_skips_malformed_and_non_object_lines_for_all_jsonl_families() {
        let text = r#"
{"title":"Planning","type":"meeting"}
42
["not", "object"]
not json
{"title":"Review","type":"task"}
"#;
        let produced = produce_chunks(Family::Event, "facets/work/events/20240101.jsonl", text);
        assert_eq!(produced.chunks.len(), 2);
        assert!(produced.chunks[0].content.contains("Meeting: Planning"));
        assert!(produced.chunks[1].content.contains("Task: Review"));

        let produced = produce_chunks(
            Family::ActionLog,
            "config/actions/20240101.jsonl",
            r#"
42
not json
{"action":"identity_update","actor":"settings"}
"#,
        );
        assert_eq!(produced.chunks.len(), 1);
        assert!(
            produced.chunks[0]
                .content
                .contains("Identity Update by settings")
        );

        let produced = produce_chunks(
            Family::Activity,
            "facets/work/activities/20240101.jsonl",
            r#"
42
not json
{"id":"coding_090000_300"}
"#,
        );
        assert_eq!(produced.chunks.len(), 1);
        assert!(produced.chunks[0].content.contains("### Coding 090000 300"));
    }

    #[test]
    fn structured_import_agent_preserves_source_case_until_merge() {
        let produced = produce_chunks(
            Family::StructuredImport,
            "20260101/import.ics/imported.jsonl",
            "",
        );
        assert_eq!(produced.agent_override, None);
        assert_eq!(produced.chunks.len(), 0);

        let produced = produce_chunks(
            Family::StructuredImport,
            "20260101/import.ics/imported.jsonl",
            r#"{"import":{"source":"ics"}}"#,
        );
        assert_eq!(produced.agent_override.as_deref(), Some("import.ics"));

        let produced = produce_chunks(
            Family::StructuredImport,
            "20260101/import.ics/imported.jsonl",
            r#"{"import":{"source":"ICS"}}"#,
        );
        assert_eq!(produced.agent_override.as_deref(), Some("import.ICS"));

        let produced = produce_chunks(
            Family::StructuredImport,
            "20260101/import.ics/imported.jsonl",
            r#"{"entry_count":1}"#,
        );
        assert_eq!(produced.agent_override.as_deref(), Some("import.unknown"));
    }

    #[test]
    fn structured_import_skips_header_and_empty_generic_entries() {
        let produced = produce_chunks(
            Family::StructuredImport,
            "20260101/import.ics/imported.jsonl",
            r#"{"import":{"source":"ics"},"title":"Header"}
{"type":"generic"}"#,
        );
        assert_eq!(produced.chunks.len(), 0);

        let produced = produce_chunks(
            Family::StructuredImport,
            "20260101/import.ics/imported.jsonl",
            r#"{"import":{"source":"ics"}}
{"type":"calendar_event","title":"Quarterly Planning","ts":"2026-01-01T09:30:00-07:00"}"#,
        );
        assert_eq!(produced.chunks.len(), 1);
        assert!(produced.chunks[0].content.contains("Quarterly Planning"));
    }

    #[test]
    fn ai_chat_agent_comes_from_path_or_fallback() {
        let produced = produce_chunks(
            Family::AiChat,
            "20260101/import.claude/thread_a/conversation_transcript.jsonl",
            r#"{"model":"claude-3"}"#,
        );
        assert_eq!(produced.agent_override.as_deref(), Some("import.claude"));

        let produced = produce_chunks(
            Family::AiChat,
            "20260101/misc/thread_a/conversation_transcript.jsonl",
            r#"{"model":"claude-3"}"#,
        );
        assert_eq!(produced.agent_override.as_deref(), Some("import.ai_chat"));
    }

    #[test]
    fn ai_chat_indexes_only_non_empty_start_bearing_turns() {
        let produced = produce_chunks(
            Family::AiChat,
            "20260101/import.claude/thread_a/conversation_transcript.jsonl",
            r#"{"model":"claude-3","imported":{"facet":"work"}}
{"start":"00:00:01","speaker":"User","text":"Hello"}
{"start":"00:00:02","speaker":"Assistant","text":""}
{"start":"00:00:03","speaker":"Assistant","text":"Hi there"}
{"speaker":"System","text":"metadata-like"}"#,
        );
        let contents: Vec<&str> = produced
            .chunks
            .iter()
            .map(|chunk| chunk.content.as_str())
            .collect();
        assert_eq!(contents, vec!["**User:** Hello", "**Assistant:** Hi there"]);

        let produced = produce_chunks(
            Family::AiChat,
            "20260101/import.claude/thread_a/conversation_transcript.jsonl",
            r#"{"model":"claude-3"}
{"start":"00:00:01","speaker":"User","text":""}"#,
        );
        assert_eq!(produced.chunks.len(), 0);
    }

    #[test]
    fn event_skip_predicate_is_title_only() {
        let produced = produce_chunks(
            Family::Event,
            "facets/work/events/20240101.jsonl",
            r#"{"type":"meeting"}
{"title":"","type":"meeting"}
{"title":"Standup","type":"meeting","participants":["Alice","Bob"],"summary":"Daily sync"}"#,
        );
        assert_eq!(produced.agent_override.as_deref(), Some("event"));
        assert_eq!(produced.chunks.len(), 1);
        assert!(produced.chunks[0].content.contains("### Meeting: Standup"));
        assert!(
            produced.chunks[0]
                .content
                .contains("**Participants:** Alice, Bob")
        );
        assert!(produced.chunks[0].content.contains("Daily sync"));
    }

    #[test]
    fn action_log_skip_predicate_is_action_only() {
        let produced = produce_chunks(
            Family::ActionLog,
            "config/actions/20240101.jsonl",
            r#"{"actor":"settings"}
{"action":"","actor":"settings"}
{"action":"identity_update","actor":"settings","source":"app","timestamp":"2025-12-16T07:33:05.135587+00:00","use_id":"123","params":{"name":"Alice"}}"#,
        );
        assert_eq!(produced.agent_override.as_deref(), Some("action"));
        assert_eq!(produced.chunks.len(), 1);
        assert!(
            produced.chunks[0]
                .content
                .contains("### Identity Update by settings")
        );
        assert!(
            produced.chunks[0]
                .content
                .contains("**Source:** app | **Time:** 07:33:05")
        );
        assert!(
            produced.chunks[0]
                .content
                .contains("**Talent:** [123](/app/sol/123)")
        );
        assert!(produced.chunks[0].content.contains("- name: Alice"));
    }

    #[test]
    fn activity_objects_always_produce_chunks() {
        let produced = produce_chunks(
            Family::Activity,
            "facets/work/activities/20240101.jsonl",
            r#"{}
{"id":"x"}
{"title":"Launch sync","activity":"meeting","facet":"work","day":"20260418","segments":["090000_300"],"level_avg":0.5,"description":"Team sync","details":"Assigned owners","participation":[{"name":"Mina"}],"story":{"body":"Aligned on launch.","topics":["launch","owners"]},"hidden":true}"#,
        );
        assert_eq!(produced.agent_override.as_deref(), Some("activity"));
        assert_eq!(produced.chunks.len(), 3);
        assert!(produced.chunks[0].content.contains("### Untitled activity"));
        assert!(produced.chunks[1].content.contains("### X"));
        assert!(produced.chunks[2].content.contains("### Launch sync"));
        assert!(produced.chunks[2].content.contains("- Time: 09:00-09:05"));
        assert!(produced.chunks[2].content.contains("- Participation: Mina"));
        assert!(produced.chunks[2].content.contains("Aligned on launch."));
        assert!(
            produced.chunks[2]
                .content
                .contains("Topics: launch, owners")
        );
        assert!(produced.chunks[2].content.contains("- Hidden: yes"));
    }
}
