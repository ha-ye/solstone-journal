// SPDX-License-Identifier: AGPL-3.0-only
// Copyright (c) 2026 sol pbc

use serde_json::Value;

use super::{
    IndexChunk, JsonObject, display_value, json_truthy, stripped_truthy_display, titleize,
};

pub(super) fn render(records: &[JsonObject]) -> Vec<IndexChunk> {
    let mut chunks = Vec::new();
    for record in records {
        let mut lines = vec![format!("### {}", fallback_title(record))];

        if let Some(activity) = activity_type(record) {
            lines.push(format!("- Activity: {activity}"));
        }
        if let Some(facet) = stripped_truthy_display(record, "facet") {
            lines.push(format!("- Facet: {facet}"));
        }
        if let Some(day) = stripped_truthy_display(record, "day") {
            lines.push(format!("- Day: {day}"));
        }
        if let Some(time_range) = activity_time_range(record.get("segments")) {
            lines.push(format!("- Time: {time_range}"));
        }
        if let Some(level) = record.get("level_avg") {
            lines.push(format!("- Level: {}", display_value(level)));
        }
        if let Some(description) = stripped_truthy_display(record, "description") {
            lines.push(format!("- Description: {description}"));
        }
        if let Some(details) = stripped_truthy_display(record, "details") {
            lines.push(format!("- Details: {details}"));
        }
        if let Some(participation) = participation(record) {
            lines.push(format!("- Participation: {participation}"));
        }

        if let Some(Value::Object(story)) = record.get("story") {
            if let Some(body) = story.get("body").and_then(Value::as_str) {
                let stripped = body.trim();
                if !stripped.is_empty() {
                    lines.push(String::new());
                    lines.push(stripped.to_string());
                }
            }
            if let Some(Value::Array(topics)) = story.get("topics") {
                let topic_values: Vec<String> = topics
                    .iter()
                    .filter_map(Value::as_str)
                    .map(str::trim)
                    .filter(|topic| !topic.is_empty())
                    .map(str::to_string)
                    .collect();
                if !topic_values.is_empty() {
                    lines.push(format!("Topics: {}", topic_values.join(", ")));
                }
            }
        }

        if json_truthy(record.get("hidden")) {
            lines.push("- Hidden: yes".to_string());
        }

        chunks.push(IndexChunk {
            content: lines.join("\n"),
        });
    }
    chunks
}

fn fallback_title(record: &JsonObject) -> String {
    if let Some(title) = stripped_truthy_display(record, "title") {
        return title;
    }
    if let Some(description) = stripped_truthy_display(record, "description") {
        return description;
    }
    if let Some(activity) = activity_type(record) {
        return titleize(&activity);
    }
    "Untitled activity".to_string()
}

fn activity_type(record: &JsonObject) -> Option<String> {
    stripped_truthy_display(record, "activity").or_else(|| stripped_truthy_display(record, "id"))
}

fn participation(record: &JsonObject) -> Option<String> {
    let Value::Array(entries) = record.get("participation")? else {
        return None;
    };
    let names: Vec<String> = entries
        .iter()
        .filter_map(Value::as_object)
        .filter_map(|entry| {
            stripped_truthy_display(entry, "name")
                .or_else(|| stripped_truthy_display(entry, "entity_id"))
        })
        .collect();
    if names.is_empty() {
        None
    } else {
        Some(names.join(", "))
    }
}

fn activity_time_range(value: Option<&Value>) -> Option<String> {
    let Value::Array(segments) = value? else {
        return None;
    };
    let first = segments.first()?.as_str()?;
    let last = segments.last()?.as_str()?;
    let (start_hour, start_minute, _, _) = parse_segment(first)?;
    let (_, _, end_second, duration) = parse_segment(last)?;
    let end_second = (end_second + duration).min(23 * 3600 + 59 * 60 + 59);
    Some(format!(
        "{start_hour:02}:{start_minute:02}-{:02}:{:02}",
        end_second / 3600,
        (end_second % 3600) / 60
    ))
}

fn parse_segment(segment: &str) -> Option<(u32, u32, u32, u32)> {
    let (time_part, length_part) = segment.split_once('_')?;
    if time_part.len() != 6
        || !time_part.bytes().all(|byte| byte.is_ascii_digit())
        || length_part.is_empty()
        || !length_part.bytes().all(|byte| byte.is_ascii_digit())
    {
        return None;
    }
    let hour = time_part[0..2].parse::<u32>().ok()?;
    let minute = time_part[2..4].parse::<u32>().ok()?;
    let second = time_part[4..6].parse::<u32>().ok()?;
    if hour > 23 || minute > 59 || second > 59 {
        return None;
    }
    let duration = length_part.parse::<u32>().ok()?;
    Some((hour, minute, hour * 3600 + minute * 60 + second, duration))
}
