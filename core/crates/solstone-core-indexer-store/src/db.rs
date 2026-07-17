// SPDX-License-Identifier: AGPL-3.0-only
// Copyright (c) 2026 sol pbc

use std::fs;
use std::path::{Path, PathBuf};

use rusqlite::Connection;

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

pub fn db_path(journal: &Path) -> PathBuf {
    journal.join(INDEX_DIR).join(DB_NAME)
}

pub fn open_index(journal: &Path) -> Result<Connection, StoreError> {
    let index_dir = journal.join(INDEX_DIR);
    fs::create_dir_all(&index_dir)?;
    let conn = Connection::open(db_path(journal))?;
    conn.execute_batch("PRAGMA journal_mode=WAL; PRAGMA synchronous=NORMAL;")?;
    ensure_schema(&conn)?;
    Ok(conn)
}

pub fn reset_index(journal: &Path) -> Result<(), StoreError> {
    match fs::remove_file(db_path(journal)) {
        Ok(()) => Ok(()),
        Err(error) if error.kind() == std::io::ErrorKind::NotFound => Ok(()),
        Err(error) => Err(StoreError::Io(error)),
    }
}

fn ensure_schema(conn: &Connection) -> Result<(), StoreError> {
    conn.execute(CREATE_FILES, [])?;
    conn.execute(CREATE_CHUNKS, [])?;
    Ok(())
}

#[cfg(test)]
mod tests {
    use super::*;
    use std::time::{SystemTime, UNIX_EPOCH};

    fn temp_root(name: &str) -> PathBuf {
        let stamp = SystemTime::now()
            .duration_since(UNIX_EPOCH)
            .expect("time should be available")
            .as_nanos();
        std::env::temp_dir().join(format!("solstone-core-indexer-store-{name}-{stamp}"))
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
