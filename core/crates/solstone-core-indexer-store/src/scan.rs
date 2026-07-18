// SPDX-License-Identifier: AGPL-3.0-only
// Copyright (c) 2026 sol pbc

use std::collections::{BTreeMap, BTreeSet};
use std::fs;
use std::path::{Path, PathBuf};
use std::time::UNIX_EPOCH;

use rusqlite::{Connection, params};
use solstone_core_indexer::content::{Family, classify, produce_chunks};
use solstone_core_indexer::discovery::discover_indexable_files;
use solstone_core_indexer::metadata::extract_path_metadata;
use solstone_core_indexer::paths::{relative_to_journal, resolve_journal_path};
use solstone_core_indexer::segment::{is_historical_day, time_bucket};
use solstone_core_indexer::stream::extract_stream;

use crate::StoreError;
use crate::db::open_index;

#[derive(Debug, Default, Clone, PartialEq, Eq)]
pub struct ScanReport {
    pub indexed: usize,
    pub removed: usize,
    pub skipped: usize,
    pub warnings: Vec<String>,
}

#[derive(Debug, Clone, PartialEq, Eq)]
pub enum RescanFileStatus {
    Indexed { warnings: Vec<String> },
    Declined,
}

pub fn scan_journal(journal: &Path, full: bool, today: &str) -> Result<ScanReport, StoreError> {
    let conn = open_index(journal)?;
    let mut report = ScanReport::default();
    let mut files = discover_indexable_files(journal)?;
    if !full {
        files.retain(|rel, _path| !is_historical_day(rel, today));
    }

    let db_mtimes = load_file_mtimes(&conn)?;
    let mut to_index = Vec::new();
    for (rel, path) in &files {
        match file_mtime_secs(path) {
            Ok(mtime) => {
                if db_mtimes.get(rel) != Some(&mtime) {
                    to_index.push((rel.clone(), path.clone(), mtime));
                }
            }
            Err(error) => {
                report.skipped += 1;
                report
                    .warnings
                    .push(format!("mtime read failed for {rel}: {error}"));
            }
        }
    }

    for (rel, path, mtime) in &to_index {
        let Some(family) = classify(rel) else {
            report.skipped += 1;
            report
                .warnings
                .push(format!("unclassified discovered file skipped: {rel}"));
            continue;
        };
        conn.execute("DELETE FROM chunks WHERE path=?", [rel])?;
        match index_file(&conn, journal, rel, path, family) {
            Ok(warnings) => {
                conn.execute(
                    "REPLACE INTO files(path, mtime) VALUES (?, ?)",
                    params![rel, mtime],
                )?;
                report.warnings.extend(warnings);
                report.indexed += 1;
            }
            Err(warning) => {
                report.skipped += 1;
                report.warnings.push(warning);
            }
        }
    }

    let discovered: BTreeSet<String> = files.keys().cloned().collect();
    let mut removed = Vec::new();
    for rel in db_mtimes.keys() {
        let in_scope = full || !is_historical_day(rel, today);
        if in_scope && !discovered.contains(rel) {
            removed.push(rel.clone());
        }
    }
    for rel in &removed {
        conn.execute("DELETE FROM chunks WHERE path=?", [rel])?;
        conn.execute("DELETE FROM files WHERE path=?", [rel])?;
    }
    report.removed = removed.len();
    Ok(report)
}

pub fn rescan_file(journal: &Path, input: &Path) -> Result<RescanFileStatus, StoreError> {
    let (rel, path) = resolve_rescan_target(journal, input)?;
    let Some(family) = classify(&rel) else {
        return Ok(RescanFileStatus::Declined);
    };
    if !path.is_file() {
        return Err(StoreError::MissingFile(path));
    }
    let mtime = file_mtime_secs(&path)?;
    let conn = open_index(journal)?;
    conn.execute("DELETE FROM chunks WHERE path=?", [&rel])?;
    match index_file(&conn, journal, &rel, &path, family) {
        Ok(warnings) => {
            conn.execute(
                "REPLACE INTO files(path, mtime) VALUES (?, ?)",
                params![rel, mtime],
            )?;
            Ok(RescanFileStatus::Indexed { warnings })
        }
        Err(warning) => Err(StoreError::Io(std::io::Error::new(
            std::io::ErrorKind::InvalidData,
            warning,
        ))),
    }
}

fn load_file_mtimes(conn: &Connection) -> Result<BTreeMap<String, i64>, StoreError> {
    let mut statement = conn.prepare("SELECT path, mtime FROM files")?;
    let rows = statement.query_map([], |row| {
        Ok((row.get::<_, String>(0)?, row.get::<_, i64>(1)?))
    })?;
    let mut mtimes = BTreeMap::new();
    for row in rows {
        let (path, mtime) = row?;
        mtimes.insert(path, mtime);
    }
    Ok(mtimes)
}

fn index_file(
    conn: &Connection,
    journal: &Path,
    rel: &str,
    path: &Path,
    family: Family,
) -> Result<Vec<String>, String> {
    let text = fs::read_to_string(path)
        .map_err(|error| format!("content read failed for {rel}: {error}"))?;
    let produced = produce_chunks(family, rel, &text);
    let metadata = extract_path_metadata(rel);
    let facet = metadata.facet.to_lowercase();
    let agent = produced
        .agent_override
        .unwrap_or_else(|| metadata.agent.clone())
        .to_lowercase();
    let stream_lookup = extract_stream(journal, rel);
    let stream = stream_lookup.stream;
    let bucket = time_bucket(rel);
    let warnings: Vec<String> = stream_lookup.warning.into_iter().collect();

    for (idx, chunk) in produced.chunks.iter().enumerate() {
        let content = chunk.content.trim();
        if content.is_empty() {
            continue;
        }
        conn.execute(
            "INSERT INTO chunks(content, path, day, facet, agent, stream, idx, time_bucket) VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
            params![
                content,
                rel,
                metadata.day,
                facet,
                agent,
                stream.as_deref(),
                idx as i64,
                bucket,
            ],
        )
        .map_err(|error| format!("chunk insert failed for {rel}: {error}"))?;
    }
    Ok(warnings)
}

fn resolve_rescan_target(journal: &Path, input: &Path) -> Result<(String, PathBuf), StoreError> {
    if input.is_absolute() {
        let journal_abs = fs::canonicalize(journal)?;
        let abs = fs::canonicalize(input)
            .map_err(|_error| StoreError::MissingFile(input.to_path_buf()))?;
        let rel = relative_to_journal(&journal_abs, &abs)
            .ok_or_else(|| StoreError::OutsideJournal(abs.clone()))?;
        Ok((rel, abs))
    } else {
        let rel = input
            .to_str()
            .ok_or_else(|| StoreError::NonUtf8Path(input.to_path_buf()))?;
        let path = resolve_journal_path(journal, rel)?;
        Ok((rel.to_string(), path))
    }
}

fn file_mtime_secs(path: &Path) -> Result<i64, StoreError> {
    let modified = fs::metadata(path)?.modified()?;
    let duration = modified.duration_since(UNIX_EPOCH).map_err(|error| {
        StoreError::Io(std::io::Error::new(std::io::ErrorKind::InvalidData, error))
    })?;
    Ok(duration.as_secs() as i64)
}

#[cfg(test)]
mod tests {
    use super::*;
    use crate::db::{db_path, reset_index};
    use rusqlite::Connection;
    use std::time::{SystemTime, UNIX_EPOCH};

    fn temp_root(name: &str) -> PathBuf {
        let stamp = SystemTime::now()
            .duration_since(UNIX_EPOCH)
            .expect("time should be available")
            .as_nanos();
        std::env::temp_dir().join(format!("solstone-core-indexer-store-scan-{name}-{stamp}"))
    }

    fn write(root: &Path, rel: &str, text: &str) {
        let path = root.join(rel);
        fs::create_dir_all(path.parent().expect("test path should have parent"))
            .expect("create parent");
        fs::write(path, text).expect("write test file");
    }

    fn write_stream(root: &Path, day: &str, stream: &str, segment: &str) {
        let dir = root.join("chronicle").join(day).join(stream).join(segment);
        fs::create_dir_all(&dir).expect("create stream dir");
        fs::write(
            dir.join("stream.json"),
            format!(r#"{{"stream":"{stream}"}}"#),
        )
        .expect("write stream marker");
    }

    fn count(conn: &Connection, sql: &str) -> i64 {
        conn.query_row(sql, [], |row| row.get(0)).expect("count")
    }

    fn chunk_row(
        conn: &Connection,
        path: &str,
    ) -> (String, String, String, Option<String>, String, String) {
        conn.query_row(
            "SELECT day, facet, agent, stream, time_bucket, content FROM chunks WHERE path=? ORDER BY idx LIMIT 1",
            [path],
            |row| {
                Ok((
                    row.get(0)?,
                    row.get(1)?,
                    row.get(2)?,
                    row.get(3)?,
                    row.get(4)?,
                    row.get(5)?,
                ))
            },
        )
        .expect("chunk metadata row")
    }

    #[test]
    fn scan_skips_reindexes_and_deletes_missing() {
        let root = temp_root("mtime");
        write(&root, "chronicle/20260717/talents/flow.md", "# Flow\n\none");
        let report = scan_journal(&root, true, "20260717").expect("first scan");
        assert_eq!(report.indexed, 1);
        let conn = Connection::open(db_path(&root)).expect("open db after first scan");
        let stream: Option<String> = conn
            .query_row(
                "SELECT stream FROM chunks WHERE path='20260717/talents/flow.md'",
                [],
                |row| row.get(0),
            )
            .expect("stream value");
        assert_eq!(stream, None);
        drop(conn);
        let report = scan_journal(&root, true, "20260717").expect("second scan");
        assert_eq!(report.indexed, 0);

        let conn = open_index(&root).expect("open index");
        conn.execute(
            "UPDATE files SET mtime=0 WHERE path='20260717/talents/flow.md'",
            [],
        )
        .expect("force reindex");
        drop(conn);
        write(&root, "chronicle/20260717/talents/flow.md", "# Flow\n\ntwo");
        let report = scan_journal(&root, true, "20260717").expect("third scan");
        assert_eq!(report.indexed, 1);

        fs::remove_file(root.join("chronicle/20260717/talents/flow.md")).expect("remove file");
        let report = scan_journal(&root, true, "20260717").expect("remove scan");
        assert_eq!(report.removed, 1);
        let conn = Connection::open(db_path(&root)).expect("open db");
        assert_eq!(count(&conn, "SELECT count(*) FROM files"), 0);
        fs::remove_dir_all(root).expect("cleanup mtime root");
    }

    #[test]
    fn light_mode_excludes_historical_indexing_and_removal() {
        let root = temp_root("light");
        write(&root, "chronicle/20240101/talents/old.md", "# Old\n\nold");
        write(
            &root,
            "chronicle/20260717/talents/today.md",
            "# Today\n\ntoday",
        );
        let report = scan_journal(&root, false, "20260717").expect("light scan");
        assert_eq!(report.indexed, 1);
        let conn = Connection::open(db_path(&root)).expect("open db");
        assert_eq!(
            count(
                &conn,
                "SELECT count(*) FROM files WHERE path='20240101/talents/old.md'"
            ),
            0
        );
        drop(conn);

        let report = scan_journal(&root, true, "20260717").expect("full scan");
        assert_eq!(report.indexed, 1);
        fs::remove_file(root.join("chronicle/20240101/talents/old.md")).expect("remove old");
        let report = scan_journal(&root, false, "20260717").expect("light removal scan");
        assert_eq!(report.removed, 0);
        let report = scan_journal(&root, true, "20260717").expect("full removal scan");
        assert_eq!(report.removed, 1);
        fs::remove_dir_all(root).expect("cleanup light root");
    }

    #[test]
    fn invalid_markdown_isolated_during_scan() {
        let root = temp_root("invalid");
        write(&root, "chronicle/20260717/talents/flow.md", "# Flow\n\nok");
        let invalid = root.join("chronicle/20260717/talents/bad.md");
        fs::create_dir_all(invalid.parent().expect("invalid parent")).expect("create parent");
        fs::write(invalid, [0xff]).expect("write invalid utf8");
        let report = scan_journal(&root, true, "20260717").expect("scan with invalid");
        assert_eq!(report.indexed, 1);
        assert_eq!(report.skipped, 1);
        assert_eq!(report.warnings.len(), 1);
        fs::remove_dir_all(root).expect("cleanup invalid root");
    }

    #[test]
    fn rescan_file_indexes_classified_families_and_declines_other_paths() {
        let root = temp_root("rescan-file");
        write(
            &root,
            "chronicle/20260717/default/234567_300/talents/audio.md",
            "# Audio\n\nbad time",
        );
        write_stream(&root, "20260717", "default", "234567_300");
        assert_eq!(
            rescan_file(
                &root,
                Path::new("20260717/default/234567_300/talents/audio.md")
            )
            .expect("rescan markdown"),
            RescanFileStatus::Indexed {
                warnings: Vec::new()
            }
        );
        let conn = Connection::open(db_path(&root)).expect("open db");
        let row: (String, String) = conn
            .query_row(
                "SELECT stream, time_bucket FROM chunks WHERE path='20260717/default/234567_300/talents/audio.md'",
                [],
                |row| Ok((row.get(0)?, row.get(1)?)),
            )
            .expect("metadata row");
        assert_eq!(row, ("default".to_string(), String::new()));
        drop(conn);

        write(
            &root,
            "facets/work/events/20240101.jsonl",
            r#"{"type":"meeting","title":"Standup"}
"#,
        );
        assert_eq!(
            rescan_file(&root, Path::new("facets/work/events/20240101.jsonl"))
                .expect("rescan event jsonl"),
            RescanFileStatus::Indexed {
                warnings: Vec::new()
            }
        );
        let conn = Connection::open(db_path(&root)).expect("open db after event rescan");
        let event_agent: String = conn
            .query_row(
                "SELECT agent FROM chunks WHERE path='facets/work/events/20240101.jsonl'",
                [],
                |row| row.get(0),
            )
            .expect("event agent row");
        assert_eq!(event_agent, "event");
        drop(conn);

        write(&root, "notes/foo.txt", "unsupported");
        assert_eq!(
            rescan_file(&root, Path::new("notes/foo.txt")).expect("decline unsupported"),
            RescanFileStatus::Declined
        );
        fs::remove_dir_all(root).expect("cleanup rescan root");
    }

    #[test]
    fn scan_indexes_jsonl_families_with_path_metadata() {
        let root = temp_root("jsonl-families");
        write(
            &root,
            "config/actions/20240101.jsonl",
            r#"{"action":"identity_update","actor":"settings","source":"app","timestamp":"2025-12-16T07:33:05.135587+00:00","params":{"name":"Alice"}}
"#,
        );
        write(
            &root,
            "facets/Work/events/20240101.jsonl",
            r#"{"type":"meeting","title":"Standup","start":"09:00:00","end":"09:30:00","participants":["Alice","Bob"],"summary":"Daily sync"}
"#,
        );
        write(
            &root,
            "facets/work/activities/20240101.jsonl",
            r#"{}
{"id":"coding_090000_300","segments":["090000_300"]}
"#,
        );
        write(
            &root,
            "facets/work/logs/20240101.jsonl",
            r#"{"action":"activity_update","actor":"activities","source":"app","params":{"id":"coding"}}
"#,
        );

        let report = scan_journal(&root, true, "20260717").expect("scan jsonl families");
        assert_eq!(report.indexed, 4);
        let conn = Connection::open(db_path(&root)).expect("open db");
        let config_row: (String, String, String, Option<String>, String, String) = conn
            .query_row(
                "SELECT day, facet, agent, stream, time_bucket, content FROM chunks WHERE path='config/actions/20240101.jsonl'",
                [],
                |row| {
                    Ok((
                        row.get(0)?,
                        row.get(1)?,
                        row.get(2)?,
                        row.get(3)?,
                        row.get(4)?,
                        row.get(5)?,
                    ))
                },
            )
            .expect("config action row");
        assert_eq!(
            config_row,
            (
                "20240101".to_string(),
                String::new(),
                "action".to_string(),
                None,
                String::new(),
                "### Identity Update by settings\n\n**Source:** app | **Time:** 07:33:05\n\n**Parameters:**\n- name: Alice".to_string(),
            )
        );

        let event_row: (String, String, String, String) = conn
            .query_row(
                "SELECT day, facet, agent, content FROM chunks WHERE path='facets/Work/events/20240101.jsonl'",
                [],
                |row| Ok((row.get(0)?, row.get(1)?, row.get(2)?, row.get(3)?)),
            )
            .expect("event row");
        assert_eq!(event_row.0, "20240101");
        assert_eq!(event_row.1, "work");
        assert_eq!(event_row.2, "event");
        assert!(event_row.3.contains("### Meeting: Standup"));
        assert!(event_row.3.contains("**Participants:** Alice, Bob"));

        assert_eq!(
            count(
                &conn,
                "SELECT count(*) FROM chunks WHERE path='facets/work/activities/20240101.jsonl' AND agent='activity'"
            ),
            2
        );
        let activity_content: String = conn
            .query_row(
                "SELECT content FROM chunks WHERE path='facets/work/activities/20240101.jsonl' AND idx=1",
                [],
                |row| row.get(0),
            )
            .expect("activity content");
        assert!(activity_content.contains("### Coding 090000 300"));
        assert!(activity_content.contains("- Time: 09:00-09:05"));

        let action_log_agent: String = conn
            .query_row(
                "SELECT agent FROM chunks WHERE path='facets/work/logs/20240101.jsonl'",
                [],
                |row| row.get(0),
            )
            .expect("facet log row");
        assert_eq!(action_log_agent, "action");
        fs::remove_dir_all(root).expect("cleanup jsonl families root");
    }

    #[test]
    fn scan_indexes_facet_entities_and_observations() {
        let root = temp_root("facet-entities-observations");
        write(
            &root,
            "facets/work/entities/20260304.jsonl",
            r#"{"type":"Person","name":"Romeo Montague","description":"Met Juliet at Denver Tech Summit","tags":["summit"],"aka":["Romeo"],"role":"Engineer"}
"#,
        );
        write(
            &root,
            "facets/work/entities/123.jsonl",
            r#"{"type":"Person","name":"Short Stem","description":"Short digit stem"}
"#,
        );
        write(
            &root,
            "facets/work/entities/99999999.jsonl",
            r#"{"type":"Person","name":"Invalid Day","description":"Invalid calendar day"}
"#,
        );
        write(
            &root,
            "facets/work/entities/some-slug.jsonl",
            r#"{"type":"Project","name":"Attached Shape","description":"Slug-shaped jsonl"}
"#,
        );
        write(
            &root,
            "facets/work/entities/romeo_montague/observations.jsonl",
            r#"{"content":"Prefers morning product reviews","observed_at":1772658000000,"source_day":"20260304"}
"#,
        );
        write(&root, "facets/work/entities/empty.jsonl", "");
        write(
            &root,
            "facets/work/entities/empty_person/observations.jsonl",
            "",
        );

        let report = scan_journal(&root, true, "20260717").expect("scan facet entities");
        assert_eq!(report.indexed, 7);
        let conn = Connection::open(db_path(&root)).expect("open db");

        for (path, expected_agent, expected_day) in [
            (
                "facets/work/entities/20260304.jsonl",
                "entity:detected",
                "20260304",
            ),
            // This fails if the agent predicate is implemented via is_date_key.
            ("facets/work/entities/123.jsonl", "entity:detected", ""),
            (
                "facets/work/entities/99999999.jsonl",
                "entity:detected",
                "99999999",
            ),
            (
                "facets/work/entities/some-slug.jsonl",
                "entity:attached",
                "",
            ),
        ] {
            let row = chunk_row(&conn, path);
            assert_eq!(row.0, expected_day, "{path}");
            assert_eq!(row.1, "work", "{path}");
            assert_eq!(row.2, expected_agent, "{path}");
            assert_eq!(row.3, None, "{path}");
            assert_eq!(row.4, "", "{path}");
        }

        let entity_row = chunk_row(&conn, "facets/work/entities/20260304.jsonl");
        assert!(entity_row.5.contains("### Person: Romeo Montague"));
        assert!(entity_row.5.contains("Met Juliet at Denver Tech Summit"));
        assert!(entity_row.5.contains("**Tags:** summit"));
        assert!(entity_row.5.contains("**Role:** Engineer"));

        let observation_row = chunk_row(
            &conn,
            "facets/work/entities/romeo_montague/observations.jsonl",
        );
        assert_eq!(observation_row.0, "");
        assert_eq!(observation_row.1, "work");
        assert_eq!(observation_row.2, "observation");
        assert_eq!(observation_row.3, None);
        assert_eq!(observation_row.4, "");
        assert!(
            observation_row
                .5
                .contains("- Prefers morning product reviews (observed: 20260304)")
        );

        assert_eq!(
            count(
                &conn,
                "SELECT count(*) FROM chunks WHERE path='facets/work/entities/empty.jsonl'"
            ),
            0
        );
        assert_eq!(
            count(
                &conn,
                "SELECT count(*) FROM files WHERE path='facets/work/entities/empty.jsonl'"
            ),
            1
        );
        assert_eq!(
            count(
                &conn,
                "SELECT count(*) FROM chunks WHERE path='facets/work/entities/empty_person/observations.jsonl'"
            ),
            0
        );
        assert_eq!(
            count(
                &conn,
                "SELECT count(*) FROM files WHERE path='facets/work/entities/empty_person/observations.jsonl'"
            ),
            1
        );

        assert_eq!(count(&conn, "SELECT count(*) FROM edges"), 0);
        let edge_files = conn
            .prepare("SELECT path FROM edge_files ORDER BY path")
            .expect("prepare edge files")
            .query_map([], |row| row.get::<_, String>(0))
            .expect("query edge files")
            .map(|row| row.expect("edge file row"))
            .collect::<Vec<_>>();
        assert_eq!(edge_files, vec!["edges:__schema__".to_string()]);
        fs::remove_dir_all(root).expect("cleanup facet entities root");
    }

    #[test]
    fn scan_indexes_structured_imports_with_formatter_agent() {
        let root = temp_root("structured-import");
        write(
            &root,
            "chronicle/20260101/import.ics/imported.jsonl",
            r#"{"import":{"source":"ICS"},"entry_count":2}
{"type":"calendar_event","title":"Planning Session","ts":"2026-01-01T09:30:00-07:00","duration_minutes":30}
{"type":"generic"}
"#,
        );

        let report = scan_journal(&root, true, "20260717").expect("scan structured import");
        assert_eq!(report.indexed, 1);
        let conn = Connection::open(db_path(&root)).expect("open db");
        assert_eq!(
            count(
                &conn,
                "SELECT count(*) FROM chunks WHERE path='20260101/import.ics/imported.jsonl'"
            ),
            1
        );
        let row: (
            String,
            String,
            String,
            Option<String>,
            String,
            String,
        ) = conn
            .query_row(
                "SELECT day, facet, agent, stream, time_bucket, content FROM chunks WHERE path='20260101/import.ics/imported.jsonl'",
                [],
                |row| {
                    Ok((
                        row.get(0)?,
                        row.get(1)?,
                        row.get(2)?,
                        row.get(3)?,
                        row.get(4)?,
                        row.get(5)?,
                    ))
                },
            )
            .expect("structured import row");
        assert_eq!(row.0, "20260101");
        assert_eq!(row.1, "");
        assert_eq!(row.2, "import.ics");
        assert_eq!(row.3, None);
        assert_eq!(row.4, "");
        assert!(row.5.contains("Planning Session"));
        fs::remove_dir_all(root).expect("cleanup structured import root");
    }

    #[test]
    fn scan_indexes_ai_chat_imports_without_metadata_facet() {
        let root = temp_root("ai-chat-import");
        write(
            &root,
            "chronicle/20260101/import.claude/thread_a/conversation_transcript.jsonl",
            r#"{"model":"claude-3","imported":{"facet":"work"}}
{"start":"00:00:01","speaker":"User","text":"Hello"}
{"start":"00:00:02","speaker":"Assistant","text":"Hi there"}
{"start":"00:00:03","speaker":"Assistant","text":""}
"#,
        );

        let report = scan_journal(&root, true, "20260717").expect("scan ai chat import");
        assert_eq!(report.indexed, 1);
        let conn = Connection::open(db_path(&root)).expect("open db");
        assert_eq!(
            count(
                &conn,
                "SELECT count(*) FROM chunks WHERE path='20260101/import.claude/thread_a/conversation_transcript.jsonl'"
            ),
            2
        );
        let row: (
            String,
            String,
            String,
            Option<String>,
            String,
            String,
        ) = conn
            .query_row(
                "SELECT day, facet, agent, stream, time_bucket, content FROM chunks WHERE path='20260101/import.claude/thread_a/conversation_transcript.jsonl' ORDER BY idx LIMIT 1",
                [],
                |row| {
                    Ok((
                        row.get(0)?,
                        row.get(1)?,
                        row.get(2)?,
                        row.get(3)?,
                        row.get(4)?,
                        row.get(5)?,
                    ))
                },
            )
            .expect("ai chat import row");
        assert_eq!(row.0, "20260101");
        assert_eq!(row.1, "");
        assert_eq!(row.2, "import.claude");
        assert_eq!(row.3, None);
        assert_eq!(row.4, "");
        assert!(row.5.contains("**User:**"));
        fs::remove_dir_all(root).expect("cleanup ai chat import root");
    }

    #[test]
    fn scan_writes_file_row_for_zero_chunk_ai_chat_import() {
        let root = temp_root("zero-ai-chat-import");
        write(
            &root,
            "chronicle/20260101/import.gemini/thread_a/conversation_transcript.jsonl",
            r#"{"model":"gemini"}
{"start":"00:00:01","speaker":"User","text":""}
{"start":"00:00:02","speaker":"Assistant","text":""}
"#,
        );

        let report = scan_journal(&root, true, "20260717").expect("scan zero ai chat import");
        assert_eq!(report.indexed, 1);
        let conn = Connection::open(db_path(&root)).expect("open db");
        assert_eq!(
            count(
                &conn,
                "SELECT count(*) FROM chunks WHERE path='20260101/import.gemini/thread_a/conversation_transcript.jsonl'"
            ),
            0
        );
        assert_eq!(
            count(
                &conn,
                "SELECT count(*) FROM files WHERE path='20260101/import.gemini/thread_a/conversation_transcript.jsonl'"
            ),
            1
        );
        fs::remove_dir_all(root).expect("cleanup zero ai chat import root");
    }

    #[test]
    fn scan_indexes_chat_streams_with_segment_bucket() {
        let root = temp_root("chat-stream");
        write(
            &root,
            "chronicle/20260508/chat/120000_300/chat.jsonl",
            r#"{"kind":"owner_message","ts":1,"text":"Need a diff"}
{"kind":"owner_message","ts":2,"text":"   "}
{"kind":"sol_message","ts":3,"text":"I can do that"}
{"kind":"owner_chat_open","ts":4,"request_id":"req","surface":"convey"}
{"kind":"mystery","ts":5,"text":"skip me"}
"#,
        );

        let report = scan_journal(&root, true, "20260717").expect("scan chat stream");
        assert_eq!(report.indexed, 1);
        let conn = Connection::open(db_path(&root)).expect("open db");
        assert_eq!(
            count(
                &conn,
                "SELECT count(*) FROM chunks WHERE path='20260508/chat/120000_300/chat.jsonl'"
            ),
            3
        );
        assert_eq!(
            count(
                &conn,
                "SELECT count(*) FROM files WHERE path='20260508/chat/120000_300/chat.jsonl'"
            ),
            1
        );
        let row: (String, String, String, Option<String>, String) = conn
            .query_row(
                "SELECT day, facet, agent, stream, time_bucket FROM chunks WHERE path='20260508/chat/120000_300/chat.jsonl' ORDER BY idx LIMIT 1",
                [],
                |row| {
                    Ok((
                        row.get(0)?,
                        row.get(1)?,
                        row.get(2)?,
                        row.get(3)?,
                        row.get(4)?,
                    ))
                },
            )
            .expect("chat metadata row");
        assert_eq!(row.0, "20260508");
        assert_eq!(row.1, "");
        assert_eq!(row.2, "chat");
        assert_eq!(row.3, None);
        assert_eq!(row.4, "afternoon");

        let contents: Vec<String> = conn
            .prepare(
                "SELECT content FROM chunks WHERE path='20260508/chat/120000_300/chat.jsonl' ORDER BY idx",
            )
            .expect("prepare chat contents")
            .query_map([], |row| row.get(0))
            .expect("query chat contents")
            .map(|row| row.expect("chat content row"))
            .collect();
        assert_eq!(
            contents,
            vec![
                "**Owner** Need a diff".to_string(),
                "**Owner**".to_string(),
                "**Sol** I can do that".to_string(),
            ]
        );
        fs::remove_dir_all(root).expect("cleanup chat stream root");
    }

    #[test]
    fn scan_indexes_browser_streams_with_marker_stream() {
        let root = temp_root("browser-stream");
        write_stream(&root, "20260703", "suze.browser", "000141_317");
        write(
            &root,
            "chronicle/20260703/suze.browser/000141_317/browser_mail-google-com.jsonl",
            r#"{"t":"segment_start","ts":1783046501000,"site":"mail.google.com","url":"https://mail.google.com/mail/u/0/#inbox","title":"Inbox - Gmail","adapter":"gmail","blocks":[{"type":"heading","text":"Inbox"},{"type":"row","text":"Ari Patel - Browser stream contract review"},{"type":"link","text":"Open pull request"}]}
{"t":"delta","ts":1783046509120,"op":"add","block":{"type":"row","text":"Status toast: All changes saved"}}
{"t":"segment_start","ts":1783046594000,"url":"https://example.com/fallback","blocks":[{"type":"text","text":"Fallback page text"}]}
{"t":"delta","ts":1783046530100,"op":"remove","block":{"type":"row","text":"Promotions tab collapsed"}}
"#,
        );

        let report = scan_journal(&root, true, "20260717").expect("scan browser stream");
        assert_eq!(report.indexed, 1);
        let conn = Connection::open(db_path(&root)).expect("open db");
        assert_eq!(
            count(
                &conn,
                "SELECT count(*) FROM chunks WHERE path='20260703/suze.browser/000141_317/browser_mail-google-com.jsonl'"
            ),
            3
        );
        assert_eq!(
            count(
                &conn,
                "SELECT count(*) FROM files WHERE path='20260703/suze.browser/000141_317/browser_mail-google-com.jsonl'"
            ),
            1
        );
        let row: (String, String, String, String, String, String) = conn
            .query_row(
                "SELECT day, facet, agent, stream, time_bucket, content FROM chunks WHERE path='20260703/suze.browser/000141_317/browser_mail-google-com.jsonl' ORDER BY idx LIMIT 1",
                [],
                |row| {
                    Ok((
                        row.get(0)?,
                        row.get(1)?,
                        row.get(2)?,
                        row.get(3)?,
                        row.get(4)?,
                        row.get(5)?,
                    ))
                },
            )
            .expect("browser metadata row");
        assert_eq!(row.0, "20260703");
        assert_eq!(row.1, "");
        assert_eq!(row.2, "browser");
        assert_eq!(row.3, "suze.browser");
        assert_eq!(row.4, "night");
        assert!(row.5.contains("Inbox - Gmail"));
        assert!(row.5.contains("Ari Patel - Browser stream contract review"));

        let all_content: String = conn
            .prepare(
                "SELECT content FROM chunks WHERE path='20260703/suze.browser/000141_317/browser_mail-google-com.jsonl' ORDER BY idx",
            )
            .expect("prepare browser contents")
            .query_map([], |row| row.get::<_, String>(0))
            .expect("query browser contents")
            .map(|row| row.expect("browser content row"))
            .collect::<Vec<_>>()
            .join("\n");
        assert!(all_content.contains("https://example.com/fallback"));
        assert!(all_content.contains("Fallback page text"));
        assert!(all_content.contains("Status toast: All changes saved"));
        assert!(!all_content.contains("Promotions tab collapsed"));
        fs::remove_dir_all(root).expect("cleanup browser stream root");
    }

    #[test]
    fn scan_indexes_day_accumulator_records_with_file_stem_agent() {
        let root = temp_root("day-accumulator");
        write(
            &root,
            "chronicle/20260304/talents/pulse.jsonl",
            r#"{"ts":10,"summary":"Clear morning","needs_you":[{"text":"Review proposal"}]}
{"title":"Second pulse","detail":"afternoon check"}
"#,
        );

        let report = scan_journal(&root, true, "20260717").expect("scan day accumulator");
        assert_eq!(report.indexed, 1);
        let conn = Connection::open(db_path(&root)).expect("open db");
        assert_eq!(
            count(
                &conn,
                "SELECT count(*) FROM chunks WHERE path='20260304/talents/pulse.jsonl'"
            ),
            2
        );
        assert_eq!(
            count(
                &conn,
                "SELECT count(*) FROM files WHERE path='20260304/talents/pulse.jsonl'"
            ),
            1
        );
        let row: (String, String, String, Option<String>, String, String) = conn
            .query_row(
                "SELECT day, facet, agent, stream, time_bucket, content FROM chunks WHERE path='20260304/talents/pulse.jsonl' ORDER BY idx LIMIT 1",
                [],
                |row| {
                    Ok((
                        row.get(0)?,
                        row.get(1)?,
                        row.get(2)?,
                        row.get(3)?,
                        row.get(4)?,
                        row.get(5)?,
                    ))
                },
            )
            .expect("day accumulator row");
        assert_eq!(row.0, "20260304");
        assert_eq!(row.1, "");
        assert_eq!(row.2, "pulse");
        assert_eq!(row.3, None);
        assert_eq!(row.4, "");
        assert!(row.5.contains(r#""summary":"Clear morning""#));
        assert!(row.5.contains(r#""needs_you""#));

        let second: String = conn
            .query_row(
                "SELECT content FROM chunks WHERE path='20260304/talents/pulse.jsonl' AND idx=1",
                [],
                |row| row.get(0),
            )
            .expect("second day accumulator row");
        assert!(second.contains(r#""title":"Second pulse""#));
        assert!(second.contains(r#""detail":"afternoon check""#));
        fs::remove_dir_all(root).expect("cleanup day accumulator root");
    }

    #[test]
    fn scan_indexes_talent_json_families_with_static_agents_and_metadata() {
        let root = temp_root("talent-json");
        write_stream(&root, "20260717", "default", "090000_300");
        write(
            &root,
            "chronicle/20260717/default/090000_300/talents/documents.json",
            r#"{"overview":"Trust update.","parties":[{"name":"Priya Shah","role":"trustee"}],"key_provisions":[{"text":"Trustee may distribute assets."}],"assets":[{"name":"Brokerage Account"}],"conditions":[{"trigger":"Settlor's death","effect":"Successor trustee takes office."}],"important_dates":[{"date":"2026-07-17","meaning":"Effective date."}],"summary":"Summary."}"#,
        );
        write(
            &root,
            "chronicle/20260717/default/090000_300/talents/screen.json",
            r#"{"narrative":"Viewed the release dashboard.","entities":[{"type":"Tool","name":"Grafana","context":"Latency dashboard."}]}"#,
        );
        write(
            &root,
            "chronicle/20260717/default/090000_300/talents/sense.json",
            r#"{"content_type":"meeting","emotional_register":"focused","activity_summary":"Reviewed launch status.","entities":[{"type":"Person","name":"Alice"}],"facets":[{"facet":"work","activity":"launch","level":"high"}],"meeting_detected":true,"speakers":["Alice"]}"#,
        );
        write(
            &root,
            "chronicle/20260717/talents/morning_briefing.json",
            r#"{"metadata":{"coverage_preamble":"Daily briefing."},"your_day":[{"time":"09:00","text":"Meet Alice."}],"yesterday":["Shipped."],"needs_attention":[{"text":"Review."}],"forward_look":["Prepare."],"reading":[{"facet":"work","summary":"News."}]}"#,
        );

        let report = scan_journal(&root, true, "20260717").expect("scan talent json");
        assert_eq!(report.indexed, 4);
        let conn = Connection::open(db_path(&root)).expect("open db");

        for (path, agent) in [
            (
                "20260717/default/090000_300/talents/documents.json",
                "documents",
            ),
            ("20260717/default/090000_300/talents/screen.json", "screen"),
            ("20260717/default/090000_300/talents/sense.json", "sense"),
        ] {
            let row: (String, String, String, String, String) = conn
                .query_row(
                    "SELECT day, facet, agent, stream, time_bucket FROM chunks WHERE path=? ORDER BY idx LIMIT 1",
                    [path],
                    |row| {
                        Ok((
                            row.get(0)?,
                            row.get(1)?,
                            row.get(2)?,
                            row.get(3)?,
                            row.get(4)?,
                        ))
                    },
                )
                .expect("segment talent json metadata row");
            assert_eq!(
                row,
                (
                    "20260717".to_string(),
                    String::new(),
                    agent.to_string(),
                    "default".to_string(),
                    "morning".to_string(),
                ),
                "{path}"
            );
            assert_eq!(
                count(
                    &conn,
                    &format!("SELECT count(*) FROM files WHERE path='{path}'")
                ),
                1,
                "{path}"
            );
        }

        let morning_row: (String, String, String, Option<String>, String, String) = conn
            .query_row(
                "SELECT day, facet, agent, stream, time_bucket, content FROM chunks WHERE path='20260717/talents/morning_briefing.json' ORDER BY idx LIMIT 1",
                [],
                |row| {
                    Ok((
                        row.get(0)?,
                        row.get(1)?,
                        row.get(2)?,
                        row.get(3)?,
                        row.get(4)?,
                        row.get(5)?,
                    ))
                },
            )
            .expect("morning briefing row");
        assert_eq!(morning_row.0, "20260717");
        assert_eq!(morning_row.1, "");
        assert_eq!(morning_row.2, "morning_briefing");
        assert_eq!(morning_row.3, None);
        assert_eq!(morning_row.4, "");
        assert!(morning_row.5.contains("## Your Day"));
        fs::remove_dir_all(root).expect("cleanup talent json root");
    }

    #[test]
    fn scan_writes_file_rows_for_invalid_or_non_object_talent_json() {
        let root = temp_root("invalid-talent-json");
        for segment in ["090000_300", "100000_300", "110000_300", "120000_300"] {
            write_stream(&root, "20260717", "default", segment);
        }
        let cases = [
            (
                "chronicle/20260717/default/090000_300/talents/documents.json",
                "{",
            ),
            (
                "chronicle/20260717/default/100000_300/talents/screen.json",
                "null",
            ),
            (
                "chronicle/20260717/default/110000_300/talents/sense.json",
                "42",
            ),
            (
                "chronicle/20260717/default/120000_300/talents/documents.json",
                "[]",
            ),
            ("chronicle/20260717/talents/morning_briefing.json", ""),
        ];
        for (path, text) in cases {
            write(&root, path, text);
        }

        let report = scan_journal(&root, true, "20260717").expect("scan invalid talent json");
        assert_eq!(report.indexed, 5);
        assert_eq!(report.skipped, 0);
        let conn = Connection::open(db_path(&root)).expect("open db");
        for path in [
            "20260717/default/090000_300/talents/documents.json",
            "20260717/default/100000_300/talents/screen.json",
            "20260717/default/110000_300/talents/sense.json",
            "20260717/default/120000_300/talents/documents.json",
            "20260717/talents/morning_briefing.json",
        ] {
            assert_eq!(
                count(
                    &conn,
                    &format!("SELECT count(*) FROM chunks WHERE path='{path}'")
                ),
                0,
                "{path}"
            );
            assert_eq!(
                count(
                    &conn,
                    &format!("SELECT count(*) FROM files WHERE path='{path}'")
                ),
                1,
                "{path}"
            );
        }
        fs::remove_dir_all(root).expect("cleanup invalid talent json root");
    }

    #[test]
    fn scan_preserves_empty_object_chunk_counts_by_talent_json_family() {
        let root = temp_root("empty-talent-json");
        write_stream(&root, "20260717", "default", "090000_300");
        write(
            &root,
            "chronicle/20260717/default/090000_300/talents/documents.json",
            "{}",
        );
        write(
            &root,
            "chronicle/20260717/default/090000_300/talents/screen.json",
            "{}",
        );
        write(
            &root,
            "chronicle/20260717/default/090000_300/talents/sense.json",
            "{}",
        );
        write(
            &root,
            "chronicle/20260717/talents/morning_briefing.json",
            "{}",
        );

        let report = scan_journal(&root, true, "20260717").expect("scan empty talent json");
        assert_eq!(report.indexed, 4);
        let conn = Connection::open(db_path(&root)).expect("open db");
        for (path, chunks) in [
            ("20260717/default/090000_300/talents/documents.json", 1),
            ("20260717/default/090000_300/talents/screen.json", 1),
            ("20260717/default/090000_300/talents/sense.json", 0),
            ("20260717/talents/morning_briefing.json", 1),
        ] {
            assert_eq!(
                count(
                    &conn,
                    &format!("SELECT count(*) FROM chunks WHERE path='{path}'")
                ),
                chunks,
                "{path}"
            );
            assert_eq!(
                count(
                    &conn,
                    &format!("SELECT count(*) FROM files WHERE path='{path}'")
                ),
                1,
                "{path}"
            );
        }
        fs::remove_dir_all(root).expect("cleanup empty talent json root");
    }

    #[test]
    fn scan_writes_file_row_for_zero_chunk_chat_stream() {
        let root = temp_root("zero-chat-stream");
        write(
            &root,
            "chronicle/20260508/chat/130000_300/chat.jsonl",
            r#"{"kind":"owner_chat_open","ts":1,"request_id":"req","surface":"convey"}
{"kind":"mystery","ts":2,"text":"skip me"}
"#,
        );

        let report = scan_journal(&root, true, "20260717").expect("scan zero chat stream");
        assert_eq!(report.indexed, 1);
        let conn = Connection::open(db_path(&root)).expect("open db");
        assert_eq!(
            count(
                &conn,
                "SELECT count(*) FROM chunks WHERE path='20260508/chat/130000_300/chat.jsonl'"
            ),
            0
        );
        assert_eq!(
            count(
                &conn,
                "SELECT count(*) FROM files WHERE path='20260508/chat/130000_300/chat.jsonl'"
            ),
            1
        );
        fs::remove_dir_all(root).expect("cleanup zero chat stream root");
    }

    #[test]
    fn scan_writes_files_rows_for_zero_chunk_jsonl() {
        let root = temp_root("zero-jsonl");
        write(
            &root,
            "facets/work/events/20240101.jsonl",
            r#"{"type":"meeting"}
{"title":""}
"#,
        );
        write(
            &root,
            "facets/work/logs/20240101.jsonl",
            r#"{"actor":"settings"}
{"action":""}
"#,
        );

        let report = scan_journal(&root, true, "20260717").expect("scan zero jsonl");
        assert_eq!(report.indexed, 2);
        let conn = Connection::open(db_path(&root)).expect("open db");
        assert_eq!(
            count(
                &conn,
                "SELECT count(*) FROM chunks WHERE path='facets/work/events/20240101.jsonl'"
            ),
            0
        );
        assert_eq!(
            count(
                &conn,
                "SELECT count(*) FROM chunks WHERE path='facets/work/logs/20240101.jsonl'"
            ),
            0
        );
        assert_eq!(
            count(
                &conn,
                "SELECT count(*) FROM files WHERE path='facets/work/events/20240101.jsonl'"
            ),
            1
        );
        assert_eq!(
            count(
                &conn,
                "SELECT count(*) FROM files WHERE path='facets/work/logs/20240101.jsonl'"
            ),
            1
        );
        fs::remove_dir_all(root).expect("cleanup zero jsonl root");
    }

    #[test]
    fn scan_skips_non_object_jsonl_lines_and_keeps_file_row() {
        let root = temp_root("non-object-jsonl");
        write(
            &root,
            "facets/work/events/20240101.jsonl",
            r#"42
["not", "object"]
not json
{"type":"meeting","title":"Planning"}
"#,
        );

        let report = scan_journal(&root, true, "20260717").expect("scan non-object jsonl");
        assert_eq!(report.indexed, 1);
        let conn = Connection::open(db_path(&root)).expect("open db");
        assert_eq!(
            count(
                &conn,
                "SELECT count(*) FROM chunks WHERE path='facets/work/events/20240101.jsonl'"
            ),
            1
        );
        assert_eq!(
            count(
                &conn,
                "SELECT count(*) FROM files WHERE path='facets/work/events/20240101.jsonl'"
            ),
            1
        );
        let content: String = conn
            .query_row(
                "SELECT content FROM chunks WHERE path='facets/work/events/20240101.jsonl'",
                [],
                |row| row.get(0),
            )
            .expect("event content");
        assert!(content.contains("### Meeting: Planning"));
        fs::remove_dir_all(root).expect("cleanup non-object jsonl root");
    }

    #[test]
    fn short_segment_length_resolves_stream_and_bucket() {
        let root = temp_root("short-segment");
        write(
            &root,
            "chronicle/20260717/default/143022_60/talents/audio.md",
            "# Audio\n\nshort segment",
        );
        write_stream(&root, "20260717", "default", "143022_60");
        let report = scan_journal(&root, true, "20260717").expect("scan short segment");
        assert_eq!(report.indexed, 1);
        let conn = Connection::open(db_path(&root)).expect("open db");
        let row: (String, String) = conn
            .query_row(
                "SELECT stream, time_bucket FROM chunks WHERE path='20260717/default/143022_60/talents/audio.md'",
                [],
                |row| Ok((row.get(0)?, row.get(1)?)),
            )
            .expect("short segment metadata row");
        assert_eq!(row, ("default".to_string(), "afternoon".to_string()));
        fs::remove_dir_all(root).expect("cleanup short segment root");
    }

    #[test]
    fn scan_lowercases_facet_and_agent_at_insert() {
        let root = temp_root("lowercase");
        write(
            &root,
            "apps/MyApp/talents/Digest.md",
            "# Digest\n\napp content",
        );
        write(
            &root,
            "facets/Work/news/20260101.md",
            "# News\n\nfacet content",
        );
        let report = scan_journal(&root, true, "20260717").expect("scan mixed case");
        assert_eq!(report.indexed, 2);
        let conn = Connection::open(db_path(&root)).expect("open db");
        let app_agent: String = conn
            .query_row(
                "SELECT agent FROM chunks WHERE path='apps/MyApp/talents/Digest.md'",
                [],
                |row| row.get(0),
            )
            .expect("app agent row");
        assert_eq!(app_agent, "myapp:digest");
        let news_row: (String, String) = conn
            .query_row(
                "SELECT facet, agent FROM chunks WHERE path='facets/Work/news/20260101.md'",
                [],
                |row| Ok((row.get(0)?, row.get(1)?)),
            )
            .expect("news metadata row");
        assert_eq!(news_row, ("work".to_string(), "news".to_string()));
        fs::remove_dir_all(root).expect("cleanup lowercase root");
    }

    #[test]
    fn reset_semantics_remove_only_main_database() {
        let root = temp_root("reset-scan");
        open_index(&root).expect("open index");
        fs::write(root.join("indexer/journal.sqlite-wal"), "wal").expect("write wal");
        reset_index(&root).expect("reset index");
        assert!(!db_path(&root).exists());
        assert!(root.join("indexer/journal.sqlite-wal").exists());
        fs::remove_dir_all(root).expect("cleanup reset root");
    }
}
