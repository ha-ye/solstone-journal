// SPDX-License-Identifier: AGPL-3.0-only
// Copyright (c) 2026 sol pbc

use std::fs;
use std::path::{Path, PathBuf};

use glob::{Pattern, glob};

use crate::chunker::chunk_markdown;
use crate::paths::resolve_journal_path;
use crate::segment::time_bucket;
use crate::stream::extract_stream;

#[derive(Debug, Clone, PartialEq, Eq)]
pub struct SegmentAggregateRow {
    pub path: String,
    pub day: String,
    pub facet: String,
    pub agent: String,
    pub stream: Option<String>,
    pub idx: i64,
    pub time_bucket: String,
    pub content: String,
}

#[derive(Debug, Clone, PartialEq, Eq)]
pub struct SegmentAggregate {
    pub rows: Vec<SegmentAggregateRow>,
    pub warnings: Vec<String>,
    pub complete: bool,
}

pub fn build_segment_aggregate(journal: &Path, rel_segment: &str) -> SegmentAggregate {
    let mut warnings = Vec::new();
    let stream_lookup = extract_stream(journal, rel_segment);
    let stream = stream_lookup.stream;
    warnings.extend(stream_lookup.warning);

    let segment_dir = match resolve_journal_path(journal, rel_segment) {
        Ok(path) => path,
        Err(error) => {
            warnings.push(format!(
                "segment aggregate path failed for {rel_segment}: {error}"
            ));
            return SegmentAggregate {
                rows: Vec::new(),
                warnings,
                complete: true,
            };
        }
    };

    let mut talent_files = Vec::new();
    collect_globbed_paths(
        &segment_dir,
        "talents/*.md",
        &mut talent_files,
        &mut warnings,
    );
    collect_globbed_paths(
        &segment_dir,
        "talents/*/*.md",
        &mut talent_files,
        &mut warnings,
    );
    talent_files.sort_by_key(|path| path.to_string_lossy().into_owned());

    let mut contents = Vec::new();
    let mut complete = true;
    for path in talent_files {
        match fs::read_to_string(&path) {
            Ok(text) => contents.push(text),
            Err(error) => {
                complete = false;
                warnings.push(format!(
                    "segment aggregate read failed for {}: {error}",
                    path.display()
                ));
            }
        }
    }

    let content = contents.join("\n\n---\n\n");
    let day = rel_segment
        .split('/')
        .next()
        .unwrap_or_default()
        .to_string();
    let bucket = time_bucket(rel_segment);
    let mut rows = Vec::new();
    for chunk in chunk_markdown(&content) {
        let content = chunk.markdown.trim();
        if content.is_empty() {
            continue;
        }
        rows.push(SegmentAggregateRow {
            path: rel_segment.to_string(),
            day: day.clone(),
            facet: String::new(),
            agent: "segment".to_string(),
            stream: stream.clone(),
            idx: rows.len() as i64,
            time_bucket: bucket.clone(),
            content: content.to_string(),
        });
    }

    SegmentAggregate {
        rows,
        warnings,
        complete,
    }
}

fn collect_globbed_paths(
    root: &Path,
    suffix: &str,
    paths: &mut Vec<PathBuf>,
    warnings: &mut Vec<String>,
) {
    let Some(root_str) = root.to_str() else {
        warnings.push(format!(
            "segment aggregate glob root is not valid UTF-8: {}",
            root.display()
        ));
        return;
    };
    let separator = if root_str.ends_with('/') { "" } else { "/" };
    let pattern = format!("{}{separator}{suffix}", Pattern::escape(root_str));
    let entries = match glob(&pattern) {
        Ok(entries) => entries,
        Err(error) => {
            warnings.push(format!(
                "segment aggregate glob pattern failed for {pattern}: {error}"
            ));
            return;
        }
    };
    for entry in entries {
        match entry {
            Ok(path) => paths.push(path),
            Err(error) => warnings.push(format!("segment aggregate glob failed: {error}")),
        }
    }
}
