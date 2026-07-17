// SPDX-License-Identifier: AGPL-3.0-only
// Copyright (c) 2026 sol pbc

use std::process::ExitCode;

use solstone_core_cli::{Command, USAGE, evaluate_args, version_line};

const EXIT_USAGE: u8 = 64;

fn main() -> ExitCode {
    let args: Vec<String> = std::env::args().skip(1).collect();
    match evaluate_args(&args) {
        Ok(Command::Version) => {
            print!("{}", version_line(env!("CARGO_PKG_VERSION")));
            ExitCode::SUCCESS
        }
        Err(_) => {
            eprint!("{USAGE}");
            ExitCode::from(EXIT_USAGE)
        }
    }
}
