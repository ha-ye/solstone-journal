// SPDX-License-Identifier: AGPL-3.0-only
// Copyright (c) 2026 sol pbc

use std::path::Path;

pub const DEFAULT_CONVEY_PORT: i64 = 5015;

#[must_use]
pub fn read_convey_port(journal: impl AsRef<Path>) -> i64 {
    read_convey_port_from_file(journal.as_ref().join("health").join("convey.port"))
}

fn read_convey_port_from_file(path: impl AsRef<Path>) -> i64 {
    let text = match std::fs::read_to_string(path) {
        Ok(text) => text,
        Err(_) => return DEFAULT_CONVEY_PORT,
    };
    let stripped = text.trim();
    if stripped.is_empty() {
        return DEFAULT_CONVEY_PORT;
    }
    stripped.parse::<i64>().unwrap_or(DEFAULT_CONVEY_PORT)
}

#[cfg(test)]
mod tests {
    use std::fs;
    use std::sync::atomic::{AtomicU64, Ordering};
    use std::time::{SystemTime, UNIX_EPOCH};

    use super::*;

    static NEXT_ID: AtomicU64 = AtomicU64::new(0);

    fn temp_journal(name: &str) -> std::path::PathBuf {
        let id = SystemTime::now()
            .duration_since(UNIX_EPOCH)
            .expect("system time before unix epoch")
            .as_nanos();
        let sequence = NEXT_ID.fetch_add(1, Ordering::Relaxed);
        let path = std::env::temp_dir().join(format!("solstone-port-test-{name}-{id}-{sequence}"));
        fs::create_dir_all(path.join("health")).expect("create temp journal");
        path
    }

    #[test]
    fn reads_configured_port() {
        let journal = temp_journal("configured");
        fs::write(journal.join("health").join("convey.port"), "6017\n").expect("write port");
        assert_eq!(read_convey_port(&journal), 6017);
        fs::remove_dir_all(journal).expect("remove temp journal");
    }

    #[test]
    fn defaults_when_port_file_is_missing() {
        let journal = temp_journal("missing");
        assert_eq!(read_convey_port(&journal), DEFAULT_CONVEY_PORT);
        fs::remove_dir_all(journal).expect("remove temp journal");
    }

    #[test]
    fn defaults_when_port_file_is_unreadable() {
        let journal = temp_journal("unreadable");
        fs::create_dir(journal.join("health").join("convey.port")).expect("create unreadable path");
        assert_eq!(read_convey_port(&journal), DEFAULT_CONVEY_PORT);
        fs::remove_dir_all(journal).expect("remove temp journal");
    }

    #[test]
    fn defaults_when_port_file_is_empty() {
        let journal = temp_journal("empty");
        fs::write(journal.join("health").join("convey.port"), "").expect("write port");
        assert_eq!(read_convey_port(&journal), DEFAULT_CONVEY_PORT);
        fs::remove_dir_all(journal).expect("remove temp journal");
    }

    #[test]
    fn defaults_when_port_file_is_whitespace_only() {
        let journal = temp_journal("whitespace");
        fs::write(journal.join("health").join("convey.port"), " \n\t").expect("write port");
        assert_eq!(read_convey_port(&journal), DEFAULT_CONVEY_PORT);
        fs::remove_dir_all(journal).expect("remove temp journal");
    }

    #[test]
    fn defaults_when_port_file_is_malformed() {
        let journal = temp_journal("malformed");
        fs::write(journal.join("health").join("convey.port"), "not-a-port").expect("write port");
        assert_eq!(read_convey_port(&journal), DEFAULT_CONVEY_PORT);
        fs::remove_dir_all(journal).expect("remove temp journal");
    }

    #[test]
    fn defaults_when_journal_does_not_exist() {
        let journal = std::env::temp_dir().join("solstone-port-test-nonexistent-journal");
        let _ = fs::remove_dir_all(&journal);
        assert_eq!(read_convey_port(&journal), DEFAULT_CONVEY_PORT);
    }

    #[test]
    fn reads_port_from_path_with_spaces() {
        let journal = temp_journal("path with spaces");
        fs::write(journal.join("health").join("convey.port"), "6020").expect("write port");
        assert_eq!(read_convey_port(&journal), 6020);
        fs::remove_dir_all(journal).expect("remove temp journal");
    }

    #[test]
    fn reads_python_int_style_port() {
        let journal = temp_journal("python-int");
        fs::write(journal.join("health").join("convey.port"), " 70000\n").expect("write port");
        assert_eq!(read_convey_port(&journal), 70000);
        fs::remove_dir_all(journal).expect("remove temp journal");
    }
}
