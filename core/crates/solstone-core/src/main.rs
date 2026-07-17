// SPDX-License-Identifier: AGPL-3.0-only
// Copyright (c) 2026 sol pbc

use std::process::ExitCode;
use std::{env, ffi::OsStr, path::PathBuf};

use solstone_core_cli::{Command, JournalPathOptions, USAGE, evaluate_args, version_line};
use solstone_core_journal::{
    ConfigError, HomeError, Source, discover_home, ensure_journal_dir_with_label,
    read_config_journal, resolve_journal_path,
};

const EXIT_USAGE: u8 = 64;
const EXIT_TEMPFAIL: u8 = 75;

struct JournalPathLine {
    label: &'static str,
    path: PathBuf,
}

enum JournalPathError {
    Config(ConfigError),
    Home(HomeError),
    Create(solstone_core_journal::EnsureJournalDirError),
}

fn main() -> ExitCode {
    let args: Vec<_> = env::args_os().skip(1).collect();
    match evaluate_args(&args) {
        Ok(Command::Version) => {
            print!("{}", version_line(env!("CARGO_PKG_VERSION")));
            ExitCode::SUCCESS
        }
        Ok(Command::JournalPath(options)) => match run_journal_path(options) {
            Ok(line) => {
                println!("{}\t{}", line.label, line.path.display());
                ExitCode::SUCCESS
            }
            Err(error) => {
                eprint_journal_path_error(error);
                ExitCode::from(EXIT_TEMPFAIL)
            }
        },
        Err(_) => {
            eprint!("{USAGE}");
            ExitCode::from(EXIT_USAGE)
        }
    }
}

fn run_journal_path(options: JournalPathOptions) -> Result<JournalPathLine, JournalPathError> {
    let line = if let Some(path) = options.journal_override {
        JournalPathLine {
            label: "cli",
            path: PathBuf::from(path),
        }
    } else {
        resolve_process_journal_path()?
    };

    if options.create {
        ensure_journal_dir_with_label(&line.path, line.label).map_err(JournalPathError::Create)?;
    }

    Ok(line)
}

fn resolve_process_journal_path() -> Result<JournalPathLine, JournalPathError> {
    let env_journal = env::var_os("SOLSTONE_JOURNAL");
    if let Some(path) = env_journal
        .as_deref()
        .filter(|value| *value != OsStr::new(""))
    {
        return Ok(JournalPathLine {
            label: "env",
            path: PathBuf::from(path),
        });
    }

    let home = discover_binary_home().map_err(JournalPathError::Home)?;
    let config_journal = read_config_journal(&home).map_err(JournalPathError::Config)?;
    let resolved = resolve_journal_path(
        env_journal.as_deref(),
        config_journal.as_deref(),
        None,
        &home,
    );
    Ok(JournalPathLine {
        label: match resolved.source {
            Source::Env => "env",
            Source::Config => "config",
            Source::Source => "source",
            Source::Default => "default",
        },
        path: resolved.path,
    })
}

fn discover_binary_home() -> Result<PathBuf, HomeError> {
    let home_env = env::var_os("HOME");
    if let Some(home) = home_env.as_deref() {
        return discover_home(Some(home), None);
    }
    let fallback = env::home_dir();
    discover_home(None, fallback.as_deref())
}

fn eprint_journal_path_error(error: JournalPathError) {
    match error {
        JournalPathError::Config(ConfigError::Decode) => {
            eprintln!("journal-path failed: config is not valid UTF-8")
        }
        JournalPathError::Home(HomeError::Unavailable) => {
            eprintln!("journal-path failed: could not determine home directory")
        }
        JournalPathError::Create(error) => eprintln!("{error}"),
    }
}
