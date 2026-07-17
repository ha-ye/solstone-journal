// SPDX-License-Identifier: AGPL-3.0-only
// Copyright (c) 2026 sol pbc

use std::process::Command;

fn bin() -> &'static str {
    env!("CARGO_BIN_EXE_solstone-core")
}

#[test]
fn version_writes_stdout_and_exits_zero() {
    let output = Command::new(bin())
        .arg("--version")
        .output()
        .expect("solstone-core should execute");

    assert_eq!(output.status.code(), Some(0));
    assert_eq!(
        String::from_utf8(output.stdout).expect("stdout should be utf-8"),
        format!("solstone-core {}\n", env!("CARGO_PKG_VERSION"))
    );
    assert_eq!(
        String::from_utf8(output.stderr).expect("stderr should be utf-8"),
        ""
    );
}

#[test]
fn usage_error_writes_stderr_and_exits_64() {
    let output = Command::new(bin())
        .arg("--unknown")
        .output()
        .expect("solstone-core should execute");

    assert_eq!(output.status.code(), Some(64));
    assert_eq!(
        String::from_utf8(output.stdout).expect("stdout should be utf-8"),
        ""
    );
    assert_eq!(
        String::from_utf8(output.stderr).expect("stderr should be utf-8"),
        solstone_core_cli::USAGE
    );
}
