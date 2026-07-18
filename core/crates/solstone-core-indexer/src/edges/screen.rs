// SPDX-License-Identifier: AGPL-3.0-only
// Copyright (c) 2026 sol pbc

use std::collections::{BTreeMap, BTreeSet};

use serde_json::Value;

use super::candidates::EdgeResolver;
use super::{
    EdgeContext, EdgeError, EdgeRow, EdgeValue, JsonObject, json_type_name, segment_start_ts_ms,
};

#[derive(Clone, PartialEq)]
struct MessageKey {
    app: String,
    thread: String,
    sender: String,
    timestamp: Value,
    subject: String,
    text: String,
}

#[derive(Clone, PartialEq, Eq)]
struct EventKey {
    app: String,
    title: String,
    start: String,
    end: String,
    calendar: String,
}

pub(crate) fn extract_screen_edges(
    entries: &[JsonObject],
    context: &EdgeContext,
    resolver: &mut EdgeResolver,
) -> Result<Vec<EdgeRow>, EdgeError> {
    let (anchor, segment) = segment_ref(&context.path)?;
    let ts = segment_start_ts_ms(&context.day, &segment, resolver.owner_timezone())?;
    let mut rows = messaging_rows(entries, context, resolver, &anchor, ts)?;
    rows.extend(calendar_rows(entries, context, resolver, &anchor, ts)?);
    Ok(rows)
}

fn messaging_rows(
    entries: &[JsonObject],
    context: &EdgeContext,
    resolver: &mut EdgeResolver,
    anchor: &str,
    ts: i64,
) -> Result<Vec<EdgeRow>, EdgeError> {
    let mut groups: BTreeMap<(String, String), Vec<(MessageKey, String)>> = BTreeMap::new();
    for entry in entries {
        let Some(Value::Object(content)) = entry.get("content") else {
            continue;
        };
        let Some(Value::Object(messaging)) = content.get("messaging") else {
            continue;
        };
        if messaging.get("view") != Some(&Value::String("conversation".to_string())) {
            continue;
        }
        let app = string_field(messaging.get("app"));
        let thread = string_field(messaging.get("thread"));
        let Some(Value::Array(messages)) = messaging.get("messages") else {
            continue;
        };
        for message in messages {
            let Value::Object(message) = message else {
                continue;
            };
            let timestamp = message.get("timestamp").cloned().unwrap_or(Value::Null);
            if matches!(timestamp, Value::Array(_) | Value::Object(_)) {
                return Err(EdgeError::UnsupportedEdgeValue {
                    field: "timestamp",
                    value_type: json_type_name(&timestamp),
                });
            }
            let sender = string_field(message.get("sender"));
            let key = MessageKey {
                app: app.clone(),
                thread: thread.clone(),
                sender: sender.clone(),
                timestamp,
                subject: string_field(message.get("subject")),
                text: string_field(message.get("text")),
            };
            let group = groups.entry((app.clone(), thread.clone())).or_default();
            match group.iter_mut().find(|(seen, _sender)| *seen == key) {
                Some((_seen, stored_sender)) => *stored_sender = sender,
                None => group.push((key, sender)),
            }
        }
    }

    let mut rows = Vec::new();
    for ((_app, thread), messages) in groups {
        let mut sender_ids = BTreeMap::new();
        let senders: BTreeSet<String> = messages
            .iter()
            .map(|(_key, sender)| sender.clone())
            .collect();
        for sender in senders {
            if let Some(entity_id) = resolver.resolve(context, &sender)? {
                sender_ids.insert(sender, entity_id);
            }
        }

        let author_ids: Vec<Option<String>> = messages
            .iter()
            .map(|(_key, sender)| sender_ids.get(sender).cloned())
            .collect();
        let resolved_ids: Vec<String> = author_ids
            .iter()
            .filter_map(|entity_id| entity_id.clone())
            .collect::<BTreeSet<_>>()
            .into_iter()
            .collect();
        for left_index in 0..resolved_ids.len() {
            for right_id in resolved_ids.iter().skip(left_index + 1) {
                let left_id = &resolved_ids[left_index];
                let weight = author_ids
                    .iter()
                    .filter(|entity_id| {
                        entity_id
                            .as_ref()
                            .is_some_and(|entity_id| entity_id == left_id || entity_id == right_id)
                    })
                    .count() as i64;
                rows.push(EdgeRow {
                    src: left_id.clone(),
                    dst: right_id.clone(),
                    kind: "messaged-with".to_string(),
                    src_name: EdgeValue::Null,
                    dst_name: EdgeValue::Null,
                    day: Some(context.day.clone()),
                    facet: Some(context.facet.clone()),
                    source: "messaging".to_string(),
                    path: context.path.clone(),
                    anchor: Some(anchor.to_string()),
                    label: EdgeValue::Text(thread.clone()),
                    ts: EdgeValue::Int(ts),
                    weight,
                });
            }
        }
    }
    Ok(rows)
}

fn calendar_rows(
    entries: &[JsonObject],
    context: &EdgeContext,
    resolver: &mut EdgeResolver,
    anchor: &str,
    ts: i64,
) -> Result<Vec<EdgeRow>, EdgeError> {
    let mut events: Vec<(EventKey, JsonObject)> = Vec::new();
    for entry in entries {
        let Some(Value::Object(content)) = entry.get("content") else {
            continue;
        };
        let Some(Value::Object(calendar_block)) = content.get("calendar") else {
            continue;
        };
        let app = string_field(calendar_block.get("app"));
        let Some(Value::Array(calendar_events)) = calendar_block.get("events") else {
            continue;
        };
        for event in calendar_events {
            let Value::Object(event) = event else {
                continue;
            };
            let key = EventKey {
                app: app.clone(),
                title: string_field(event.get("title")),
                start: string_field(event.get("start")),
                end: string_field(event.get("end")),
                calendar: string_field(event.get("calendar")),
            };
            match events.iter_mut().find(|(seen, _event)| *seen == key) {
                Some((_seen, stored_event)) => *stored_event = event.clone(),
                None => events.push((key, event.clone())),
            }
        }
    }

    let mut rows = Vec::new();
    for (_key, event) in events {
        let Some(Value::Array(guests)) = event.get("guests") else {
            continue;
        };
        let mut resolved = BTreeSet::new();
        for guest in guests {
            if let Some(entity_id) = resolver.resolve(context, &string_field(Some(guest)))? {
                resolved.insert(entity_id);
            }
        }
        if resolved.len() < 2 {
            continue;
        }

        let day = match event_day(event.get("start")) {
            Some(day) => day,
            None => context.day.clone(),
        };
        if !super::valid_edge_day(&day) {
            return Err(EdgeError::InvalidDay(day));
        }
        let resolved_ids: Vec<String> = resolved.into_iter().collect();
        for left_index in 0..resolved_ids.len() {
            for right_id in resolved_ids.iter().skip(left_index + 1) {
                rows.push(EdgeRow {
                    src: resolved_ids[left_index].clone(),
                    dst: right_id.clone(),
                    kind: "scheduled-with".to_string(),
                    src_name: EdgeValue::Null,
                    dst_name: EdgeValue::Null,
                    day: Some(day.clone()),
                    facet: Some(context.facet.clone()),
                    source: "calendar".to_string(),
                    path: context.path.clone(),
                    anchor: Some(anchor.to_string()),
                    label: EdgeValue::Text(string_field(event.get("title"))),
                    ts: EdgeValue::Int(ts),
                    weight: 1,
                });
            }
        }
    }
    Ok(rows)
}

fn event_day(value: Option<&Value>) -> Option<String> {
    let Some(Value::String(value)) = value else {
        return None;
    };
    let text = super::python_strip(value);
    if text.len() >= 10
        && text.as_bytes().get(4) == Some(&b'-')
        && text.as_bytes().get(7) == Some(&b'-')
        && text[..4].bytes().all(|byte| byte.is_ascii_digit())
        && text[5..7].bytes().all(|byte| byte.is_ascii_digit())
        && text[8..10].bytes().all(|byte| byte.is_ascii_digit())
    {
        let day = format!("{}{}{}", &text[..4], &text[5..7], &text[8..10]);
        return super::valid_edge_day(&day).then_some(day);
    }
    let bytes = text.as_bytes();
    if bytes.len() >= 8
        && bytes[..8].iter().all(|byte| byte.is_ascii_digit())
        && bytes.get(8).is_none_or(|byte| !byte.is_ascii_digit())
    {
        let day = text[..8].to_string();
        return super::valid_edge_day(&day).then_some(day);
    }
    None
}

fn segment_ref(path: &str) -> Result<(String, String), EdgeError> {
    let normalized = path.replace('\\', "/");
    let parts: Vec<&str> = normalized.split('/').collect();
    if parts.len() < 3 {
        return Err(EdgeError::InvalidSegmentKey(path.to_string()));
    }
    Ok((parts[..3].join("/"), parts[2].to_string()))
}

fn string_field(value: Option<&Value>) -> String {
    match value {
        Some(Value::String(value)) => super::python_strip(value).to_string(),
        _ => String::new(),
    }
}
