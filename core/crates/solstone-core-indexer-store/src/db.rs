// SPDX-License-Identifier: AGPL-3.0-only
// Copyright (c) 2026 sol pbc

use std::fs;
use std::path::{Path, PathBuf};

use rusqlite::{Connection, params};

use crate::StoreError;

pub const INDEX_DIR: &str = "indexer";
pub const DB_NAME: &str = "journal.sqlite";

const CREATE_FILES: &str = "CREATE TABLE IF NOT EXISTS files(path TEXT PRIMARY KEY, mtime INTEGER)";
const CREATE_CHUNKS: &str = "\
CREATE VIRTUAL TABLE IF NOT EXISTS chunks USING fts5(
content,
path UNINDEXED,
day UNINDEXED,
facet UNINDEXED,
agent UNINDEXED,
stream UNINDEXED,
idx UNINDEXED,
time_bucket UNINDEXED
)";
// Source of truth: solstone/think/indexer/edges.py EDGES_SCHEMA_PATH / EDGES_SCHEMA_VERSION
const EDGES_SCHEMA_PATH: &str = "edges:__schema__";
const EDGES_SCHEMA_VERSION: i64 = 1;
const CREATE_EDGE_FILES: &str =
    "CREATE TABLE IF NOT EXISTS edge_files(path TEXT PRIMARY KEY, mtime INTEGER)";
const CREATE_EDGES: &str = "\
CREATE TABLE IF NOT EXISTS edges(
src TEXT NOT NULL,
dst TEXT NOT NULL,
kind TEXT NOT NULL,
directed INTEGER NOT NULL,
src_name TEXT,
dst_name TEXT,
day TEXT,
facet TEXT,
source TEXT NOT NULL,
path TEXT NOT NULL,
anchor TEXT,
label TEXT,
ts INTEGER,
weight INTEGER NOT NULL
)";
const CREATE_EDGES_PATH_INDEX: &str = "CREATE INDEX IF NOT EXISTS edges_path ON edges(path)";
const CREATE_EDGES_SRC_INDEX: &str =
    "CREATE INDEX IF NOT EXISTS idx_edges_src ON edges(src, kind, day)";
const CREATE_EDGES_DST_INDEX: &str =
    "CREATE INDEX IF NOT EXISTS idx_edges_dst ON edges(dst, kind, day)";

pub fn db_path(journal: &Path) -> PathBuf {
    journal.join(INDEX_DIR).join(DB_NAME)
}

pub fn open_index(journal: &Path) -> Result<Connection, StoreError> {
    let index_dir = journal.join(INDEX_DIR);
    fs::create_dir_all(&index_dir)?;
    let mut conn = Connection::open(db_path(journal))?;
    conn.execute_batch("PRAGMA journal_mode=WAL; PRAGMA synchronous=NORMAL;")?;
    ensure_schema(&mut conn)?;
    Ok(conn)
}

pub fn reset_index(journal: &Path) -> Result<(), StoreError> {
    match fs::remove_file(db_path(journal)) {
        Ok(()) => Ok(()),
        Err(error) if error.kind() == std::io::ErrorKind::NotFound => Ok(()),
        Err(error) => Err(StoreError::Io(error)),
    }
}

fn ensure_schema(conn: &mut Connection) -> Result<(), StoreError> {
    // Native indexer only ever targets a fresh or --reset DB, so the sentinel is
    // always absent on open and the end state is unconditional "create tables +
    // indexes + write sentinel". Python's _ensure_edges_schema has an in-place
    // version-mismatch drop/rebuild branch; porting it here would be dead code -
    // native relies on --reset for any future edge schema change.
    let tx = conn.transaction()?;
    tx.execute(CREATE_FILES, [])?;
    tx.execute(CREATE_CHUNKS, [])?;
    tx.execute(CREATE_EDGE_FILES, [])?;
    tx.execute(CREATE_EDGES, [])?;
    tx.execute(CREATE_EDGES_PATH_INDEX, [])?;
    tx.execute(CREATE_EDGES_SRC_INDEX, [])?;
    tx.execute(CREATE_EDGES_DST_INDEX, [])?;
    tx.execute(
        "REPLACE INTO edge_files(path, mtime) VALUES (?, ?)",
        params![EDGES_SCHEMA_PATH, EDGES_SCHEMA_VERSION],
    )?;
    tx.commit()?;
    Ok(())
}

#[cfg(test)]
mod tests {
    use super::*;
    use serde_json::json;
    use std::time::{SystemTime, UNIX_EPOCH};

    fn temp_root(name: &str) -> PathBuf {
        let stamp = SystemTime::now()
            .duration_since(UNIX_EPOCH)
            .expect("time should be available")
            .as_nanos();
        std::env::temp_dir().join(format!("solstone-core-indexer-store-{name}-{stamp}"))
    }

    fn table_schema_json(conn: &Connection, table: &str) -> serde_json::Value {
        let columns = conn
            .prepare(&format!("PRAGMA table_info({table})"))
            .expect("prepare table_info")
            .query_map([], |row| {
                let name: String = row.get(1)?;
                let column_type: String = row.get(2)?;
                let notnull: i64 = row.get(3)?;
                let pk: i64 = row.get(5)?;
                Ok(json!({
                    "name": name,
                    "type": column_type,
                    "notnull": notnull,
                    "pk": pk,
                }))
            })
            .expect("query table_info")
            .collect::<Result<Vec<_>, _>>()
            .expect("collect table_info");

        let mut indexes = conn
            .prepare(&format!("PRAGMA index_list({table})"))
            .expect("prepare index_list")
            .query_map([], |row| {
                let name: String = row.get(1)?;
                let unique: i64 = row.get(2)?;
                let origin: String = row.get(3)?;
                Ok((name, unique, origin))
            })
            .expect("query index_list")
            .collect::<Result<Vec<_>, _>>()
            .expect("collect index_list")
            .into_iter()
            .map(|(name, unique, origin)| {
                let columns = conn
                    .prepare(&format!("PRAGMA index_info({name})"))
                    .expect("prepare index_info")
                    .query_map([], |row| row.get::<_, String>(2))
                    .expect("query index_info")
                    .collect::<Result<Vec<_>, _>>()
                    .expect("collect index_info");
                json!({
                    "name": name,
                    "unique": unique,
                    "origin": origin,
                    "columns": columns,
                })
            })
            .collect::<Vec<_>>();
        indexes.sort_by(|left, right| {
            left["name"]
                .as_str()
                .expect("left index name")
                .cmp(right["name"].as_str().expect("right index name"))
        });

        json!({
            "columns": columns,
            "indexes": indexes,
        })
    }

    fn edge_sentinel_json(conn: &Connection) -> serde_json::Value {
        conn.query_row("SELECT path, mtime FROM edge_files", [], |row| {
            let path: String = row.get(0)?;
            let mtime: i64 = row.get(1)?;
            Ok(json!({
                "path": path,
                "mtime": mtime,
            }))
        })
        .expect("edge schema sentinel")
    }

    fn table_exists(conn: &Connection, table: &str) -> bool {
        let count: i64 = conn
            .query_row(
                "SELECT count(*) FROM sqlite_master WHERE type='table' AND name=?",
                [table],
                |row| row.get(0),
            )
            .expect("table existence query");
        count == 1
    }

    #[test]
    fn creates_schema_and_pragmas() {
        let root = temp_root("schema");
        let conn = open_index(&root).expect("open index");
        let journal_mode: String = conn
            .query_row("PRAGMA journal_mode", [], |row| row.get(0))
            .expect("journal mode");
        assert_eq!(journal_mode, "wal");
        let synchronous: i64 = conn
            .query_row("PRAGMA synchronous", [], |row| row.get(0))
            .expect("synchronous");
        assert_eq!(synchronous, 1);
        let files_sql: String = conn
            .query_row(
                "SELECT sql FROM sqlite_master WHERE type='table' AND name='files'",
                [],
                |row| row.get(0),
            )
            .expect("files schema");
        assert_eq!(
            files_sql,
            "CREATE TABLE files(path TEXT PRIMARY KEY, mtime INTEGER)"
        );
        let chunk_cols: Vec<String> = conn
            .prepare("PRAGMA table_info(chunks)")
            .expect("prepare table_info")
            .query_map([], |row| row.get::<_, String>(1))
            .expect("query table_info")
            .collect::<Result<_, _>>()
            .expect("collect table_info");
        assert_eq!(
            chunk_cols,
            vec![
                "content",
                "path",
                "day",
                "facet",
                "agent",
                "stream",
                "idx",
                "time_bucket",
            ]
        );
        drop(conn);
        fs::remove_dir_all(root).expect("cleanup schema root");
    }

    #[test]
    fn edge_schema_matches_python_golden() {
        let root = temp_root("edge-golden");
        let conn = open_index(&root).expect("open index");
        let native_tables = json!({
            "edge_files": table_schema_json(&conn, "edge_files"),
            "edges": table_schema_json(&conn, "edges"),
        });
        let native_sentinel = edge_sentinel_json(&conn);
        let native_schema_version = native_sentinel["mtime"].clone();
        let golden: serde_json::Value = serde_json::from_str(include_str!(concat!(
            env!("CARGO_MANIFEST_DIR"),
            "/../../fixtures/edge_schema.json"
        )))
        .expect("parse edge schema golden");

        assert_eq!(native_tables, golden["tables"]);
        assert_eq!(native_sentinel, golden["sentinel"]);
        assert_eq!(native_schema_version, golden["schema_version"]);
        assert_eq!(golden["schema_version"], json!(EDGES_SCHEMA_VERSION));

        drop(conn);
        fs::remove_dir_all(root).expect("cleanup edge golden root");
    }

    #[test]
    fn edge_tables_empty_except_sentinel() {
        assert_eq!(EDGES_SCHEMA_PATH, "edges:__schema__");
        assert_eq!(EDGES_SCHEMA_VERSION, 1);

        let root = temp_root("edge-empty");
        let conn = open_index(&root).expect("open index");
        assert!(table_exists(&conn, "edge_files"));
        assert!(table_exists(&conn, "edges"));
        let edges_count: i64 = conn
            .query_row("SELECT count(*) FROM edges", [], |row| row.get(0))
            .expect("edges count");
        let edge_files_count: i64 = conn
            .query_row("SELECT count(*) FROM edge_files", [], |row| row.get(0))
            .expect("edge files count");
        let sentinel: (String, i64) = conn
            .query_row("SELECT path, mtime FROM edge_files", [], |row| {
                Ok((row.get(0)?, row.get(1)?))
            })
            .expect("edge schema sentinel");

        assert_eq!(edges_count, 0);
        assert_eq!(edge_files_count, 1);
        assert_eq!(
            sentinel,
            (EDGES_SCHEMA_PATH.to_string(), EDGES_SCHEMA_VERSION)
        );

        drop(conn);
        fs::remove_dir_all(root).expect("cleanup edge empty root");
    }

    #[test]
    fn edge_schema_ensure_is_idempotent() {
        let root = temp_root("edge-idempotent");
        let conn = open_index(&root).expect("first open index");
        drop(conn);

        let conn = open_index(&root).expect("second open index");
        let edges_count: i64 = conn
            .query_row("SELECT count(*) FROM edges", [], |row| row.get(0))
            .expect("edges count");
        let edge_files_count: i64 = conn
            .query_row("SELECT count(*) FROM edge_files", [], |row| row.get(0))
            .expect("edge files count");
        let sentinel: (String, i64) = conn
            .query_row("SELECT path, mtime FROM edge_files", [], |row| {
                Ok((row.get(0)?, row.get(1)?))
            })
            .expect("edge schema sentinel");

        assert_eq!(edges_count, 0);
        assert_eq!(edge_files_count, 1);
        assert_eq!(
            sentinel,
            (EDGES_SCHEMA_PATH.to_string(), EDGES_SCHEMA_VERSION)
        );

        drop(conn);
        fs::remove_dir_all(root).expect("cleanup edge idempotent root");
    }

    #[test]
    fn reset_removes_only_main_database_file() {
        let root = temp_root("reset");
        fs::create_dir_all(root.join(INDEX_DIR)).expect("create index dir");
        fs::write(db_path(&root), "db").expect("write db");
        fs::write(root.join(INDEX_DIR).join("journal.sqlite-wal"), "wal").expect("write wal");
        fs::write(root.join(INDEX_DIR).join("journal.sqlite-shm"), "shm").expect("write shm");
        reset_index(&root).expect("reset index");
        assert!(!db_path(&root).exists());
        assert!(root.join(INDEX_DIR).join("journal.sqlite-wal").exists());
        assert!(root.join(INDEX_DIR).join("journal.sqlite-shm").exists());
        fs::remove_dir_all(root).expect("cleanup reset root");
    }
}
