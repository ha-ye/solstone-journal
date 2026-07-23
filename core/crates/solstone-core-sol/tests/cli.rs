// SPDX-License-Identifier: AGPL-3.0-only
// Copyright (c) 2026 sol pbc

use std::process::{Command, Output};
use std::time::{SystemTime, UNIX_EPOCH};
use std::{env, fs};

fn run(args: &[&str]) -> Output {
    let home = temp_home();
    let output = Command::new(env!("CARGO_BIN_EXE_solstone-core-sol"))
        .args(args)
        .env("HOME", &home)
        .env_remove("SOLSTONE_JOURNAL")
        .output()
        .expect("run solstone-core-sol");
    let _ = fs::remove_dir_all(home);
    output
}

fn temp_home() -> std::path::PathBuf {
    let nanos = SystemTime::now()
        .duration_since(UNIX_EPOCH)
        .expect("time")
        .as_nanos();
    let path = env::temp_dir().join(format!("solstone-core-sol-test-{nanos}"));
    fs::create_dir_all(&path).expect("create temp home");
    path
}

fn stdout(output: &Output) -> String {
    String::from_utf8(output.stdout.clone()).expect("stdout utf-8")
}

fn stderr(output: &Output) -> String {
    String::from_utf8(output.stderr.clone()).expect("stderr utf-8")
}

#[test]
fn prints_version() {
    let output = run(&["--version"]);
    assert!(output.status.success());
    assert!(stdout(&output).starts_with("solstone-core-sol "));
}

#[test]
fn prints_help_for_no_args_and_help() {
    let no_args = run(&[]);
    let help = run(&["help"]);
    assert!(no_args.status.success());
    assert!(help.status.success());
    assert!(stdout(&no_args).contains("solstone-core-sol call <app> <verb>"));
    assert_eq!(stdout(&no_args), stdout(&help));
}

#[test]
fn prints_journal_path() {
    let output = run(&["path"]);
    assert!(output.status.success());
    assert!(stdout(&output).starts_with("default\t"));
}

#[test]
fn prints_status_with_port_default() {
    let output = run(&["status"]);
    assert!(output.status.success());
    let text = stdout(&output);
    assert!(text.contains("journal\t"));
    assert!(text.contains("convey_port\t5015"));
}

#[test]
fn unknown_command_is_explicitly_unsupported() {
    let output = run(&["call", "transcripts", "list"]);
    assert_eq!(output.status.code(), Some(64));
    assert_eq!(stderr(&output), "Unsupported native sol command.\n");
}

#[test]
fn invalid_flag_prints_usage() {
    let output = run(&["--bogus"]);
    assert_eq!(output.status.code(), Some(64));
    assert!(stderr(&output).starts_with("Usage:\n"));
}

#[test]
fn moved_stub_dispatches_and_exits_two() {
    let output = run(&["call", "identity", "--unknown", "extra"]);
    assert_eq!(output.status.code(), Some(2));
    assert_eq!(
        stderr(&output),
        "Moved to `journal identity` — run that instead.\n"
    );
}
