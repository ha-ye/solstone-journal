// SPDX-License-Identifier: AGPL-3.0-only
// Copyright (c) 2026 sol pbc

use std::process::Command;
use std::time::{SystemTime, UNIX_EPOCH};
use std::{env, fs, path::Path};

fn bin() -> &'static str {
    env!("CARGO_BIN_EXE_solstone-core")
}

fn temp_path(name: &str) -> std::path::PathBuf {
    let stamp = SystemTime::now()
        .duration_since(UNIX_EPOCH)
        .expect("time should be available")
        .as_nanos();
    env::temp_dir().join(format!("solstone-core-indexer-{name}-{stamp}"))
}

fn write(root: &Path, rel: &str, text: &str) {
    let path = root.join(rel);
    fs::create_dir_all(path.parent().expect("test path should have parent"))
        .expect("create parent");
    fs::write(path, text).expect("write test file");
}

#[test]
fn indexer_without_operation_prints_usage_to_stdout_and_exits_zero() {
    let output = Command::new(bin())
        .arg("indexer")
        .env_remove("SOLSTONE_JOURNAL")
        .output()
        .expect("solstone-core should execute");

    assert_eq!(output.status.code(), Some(0));
    assert_eq!(
        String::from_utf8(output.stdout).expect("stdout should be utf-8"),
        solstone_core_cli::USAGE
    );
    assert_eq!(
        String::from_utf8(output.stderr).expect("stderr should be utf-8"),
        ""
    );
}

#[test]
fn indexer_rescan_full_succeeds_for_tiny_journal() {
    let root = temp_path("success");
    write(
        &root,
        "chronicle/20260717/talents/flow.md",
        "# Flow\n\nindexed",
    );

    let output = Command::new(bin())
        .arg("indexer")
        .arg("--journal")
        .arg(&root)
        .arg("--rescan-full")
        .output()
        .expect("solstone-core should execute");

    assert_eq!(output.status.code(), Some(0));
    assert_eq!(
        String::from_utf8(output.stderr).expect("stderr should be utf-8"),
        ""
    );
    assert!(root.join("indexer/journal.sqlite").is_file());
    fs::remove_dir_all(root).expect("cleanup success root");
}

#[test]
fn indexer_write_failure_exits_tempfail() {
    let root = temp_path("write-failure");
    fs::write(&root, "not a dir").expect("write file journal path");

    let output = Command::new(bin())
        .arg("indexer")
        .arg("--journal")
        .arg(&root)
        .arg("--rescan-full")
        .output()
        .expect("solstone-core should execute");

    assert_eq!(output.status.code(), Some(75));
    assert!(
        String::from_utf8(output.stderr)
            .expect("stderr should be utf-8")
            .starts_with("indexer scan failed: ")
    );
    fs::remove_file(root).expect("cleanup write failure path");
}

#[test]
fn indexer_rescan_file_conflict_exits_usage() {
    let output = Command::new(bin())
        .arg("indexer")
        .arg("--rescan-file")
        .arg("20260717/talents/flow.md")
        .arg("--rescan")
        .output()
        .expect("solstone-core should execute");

    assert_eq!(output.status.code(), Some(64));
    assert_eq!(
        String::from_utf8(output.stderr).expect("stderr should be utf-8"),
        solstone_core_cli::USAGE
    );
}

#[test]
fn indexer_unsupported_rescan_file_exits_declined() {
    let root = temp_path("declined");
    write(&root, "facets/work/events/20240101.jsonl", "{}\n");

    let output = Command::new(bin())
        .arg("indexer")
        .arg("--journal")
        .arg(&root)
        .arg("--rescan-file")
        .arg("facets/work/events/20240101.jsonl")
        .output()
        .expect("solstone-core should execute");

    assert_eq!(output.status.code(), Some(69));
    assert_eq!(
        String::from_utf8(output.stdout).expect("stdout should be utf-8"),
        ""
    );
    assert_eq!(
        String::from_utf8(output.stderr).expect("stderr should be utf-8"),
        "indexer declined unsupported file\n"
    );
    assert!(!root.join("indexer/journal.sqlite").exists());
    fs::remove_dir_all(root).expect("cleanup declined root");
}
