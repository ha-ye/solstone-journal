// SPDX-License-Identifier: AGPL-3.0-only
// Copyright (c) 2026 sol pbc

use std::process::ExitCode;
use std::{env, ffi::OsStr, path::PathBuf};

use solstone_core_journal::{
    ConfigError, HomeError, Source, discover_home, read_config_journal, resolve_journal_path,
};
use solstone_core_sol_client::port::read_convey_port;
use solstone_core_sol_client_cli::evaluate_args;

const EXIT_TEMPFAIL: u8 = 75;

fn main() -> ExitCode {
    let args: Vec<_> = env::args_os().skip(1).collect();
    let outcome = evaluate_args(&args);
    let journal = match resolve_process_journal_path() {
        Ok(line) => line,
        Err(error) => {
            eprintln!("native sol journal resolution failed: {error}");
            return ExitCode::from(EXIT_TEMPFAIL);
        }
    };
    let port = read_convey_port(&journal.path);
    println!(
        "native sol client stub\t{:?}\t{}\t{}",
        outcome,
        journal.path.display(),
        port
    );
    ExitCode::SUCCESS
}

struct JournalPathLine {
    path: PathBuf,
}

#[derive(Debug)]
enum JournalPathError {
    Config,
    Home,
}

impl std::fmt::Display for JournalPathError {
    fn fmt(&self, formatter: &mut std::fmt::Formatter<'_>) -> std::fmt::Result {
        match self {
            JournalPathError::Config => formatter.write_str("config decode failed"),
            JournalPathError::Home => formatter.write_str("home unavailable"),
        }
    }
}

impl std::error::Error for JournalPathError {}

fn resolve_process_journal_path() -> Result<JournalPathLine, JournalPathError> {
    let env_journal = env::var_os("SOLSTONE_JOURNAL");
    if let Some(path) = env_journal
        .as_deref()
        .filter(|value| *value != OsStr::new(""))
    {
        return Ok(JournalPathLine {
            path: PathBuf::from(path),
        });
    }

    let home = discover_home(env::var_os("HOME").as_deref(), None)
        .map_err(|HomeError::Unavailable| JournalPathError::Home)?;
    let config_journal =
        read_config_journal(&home).map_err(|ConfigError::Decode| JournalPathError::Config)?;
    let resolved = resolve_journal_path(
        env_journal.as_deref(),
        config_journal.as_deref(),
        None,
        &home,
    );
    match resolved.source {
        Source::Env | Source::Config | Source::Source | Source::Default => {}
    }
    Ok(JournalPathLine {
        path: resolved.path,
    })
}
