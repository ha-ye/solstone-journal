// SPDX-License-Identifier: AGPL-3.0-only
// Copyright (c) 2026 sol pbc

use std::ffi::{OsStr, OsString};

pub const USAGE: &str =
    "Usage:\n  solstone-core --version\n  solstone-core journal-path [--journal PATH] [--create]\n";

#[derive(Debug, Clone, PartialEq, Eq)]
pub enum Command {
    Version,
    JournalPath(JournalPathOptions),
}

#[derive(Debug, Clone, PartialEq, Eq)]
pub struct JournalPathOptions {
    pub journal_override: Option<OsString>,
    pub create: bool,
}

#[derive(Debug, Clone, Copy, PartialEq, Eq)]
pub struct UsageError;

pub fn evaluate_args(args: &[OsString]) -> Result<Command, UsageError> {
    match args {
        [flag] if flag == OsStr::new("--version") => Ok(Command::Version),
        [command, rest @ ..] if command == OsStr::new("journal-path") => {
            parse_journal_path(rest).map(Command::JournalPath)
        }
        _ => Err(UsageError),
    }
}

fn parse_journal_path(args: &[OsString]) -> Result<JournalPathOptions, UsageError> {
    let mut journal_override = None;
    let mut create = false;
    let mut index = 0;
    while index < args.len() {
        let arg = args[index].as_os_str();
        if arg == OsStr::new("--create") {
            if create {
                return Err(UsageError);
            }
            create = true;
            index += 1;
            continue;
        }
        if arg == OsStr::new("--journal") {
            if journal_override.is_some() {
                return Err(UsageError);
            }
            let value = args.get(index + 1).ok_or(UsageError)?;
            if value == OsStr::new("--create") || value == OsStr::new("--journal") {
                return Err(UsageError);
            }
            journal_override = Some(value.clone());
            index += 2;
            continue;
        }
        return Err(UsageError);
    }
    Ok(JournalPathOptions {
        journal_override,
        create,
    })
}

pub fn version_line(version: &str) -> String {
    format!("solstone-core {version}\n")
}

#[cfg(test)]
mod tests {
    use super::*;

    fn args(values: &[&str]) -> Vec<OsString> {
        values.iter().map(OsString::from).collect()
    }

    #[test]
    fn accepts_version_flag() {
        assert_eq!(evaluate_args(&args(&["--version"])), Ok(Command::Version));
    }

    #[test]
    fn rejects_empty_args() {
        assert_eq!(evaluate_args(&args(&[])), Err(UsageError));
    }

    #[test]
    fn rejects_unknown_args() {
        assert_eq!(evaluate_args(&args(&["--unknown"])), Err(UsageError));
    }

    #[test]
    fn rejects_extra_args() {
        assert_eq!(
            evaluate_args(&args(&["--version", "extra"])),
            Err(UsageError)
        );
    }

    #[test]
    fn accepts_journal_path() {
        assert_eq!(
            evaluate_args(&args(&["journal-path"])),
            Ok(Command::JournalPath(JournalPathOptions {
                journal_override: None,
                create: false,
            }))
        );
    }

    #[test]
    fn accepts_journal_path_create() {
        assert_eq!(
            evaluate_args(&args(&["journal-path", "--create"])),
            Ok(Command::JournalPath(JournalPathOptions {
                journal_override: None,
                create: true,
            }))
        );
    }

    #[test]
    fn accepts_journal_path_override() {
        assert_eq!(
            evaluate_args(&args(&["journal-path", "--journal", "/tmp/journal"])),
            Ok(Command::JournalPath(JournalPathOptions {
                journal_override: Some(OsString::from("/tmp/journal")),
                create: false,
            }))
        );
    }

    #[test]
    fn accepts_journal_path_override_create() {
        assert_eq!(
            evaluate_args(&args(&[
                "journal-path",
                "--journal",
                "/tmp/journal",
                "--create",
            ])),
            Ok(Command::JournalPath(JournalPathOptions {
                journal_override: Some(OsString::from("/tmp/journal")),
                create: true,
            }))
        );
    }

    #[test]
    fn accepts_journal_path_create_override() {
        assert_eq!(
            evaluate_args(&args(&[
                "journal-path",
                "--create",
                "--journal",
                "/tmp/journal",
            ])),
            Ok(Command::JournalPath(JournalPathOptions {
                journal_override: Some(OsString::from("/tmp/journal")),
                create: true,
            }))
        );
    }

    #[test]
    fn rejects_journal_missing_value() {
        assert_eq!(
            evaluate_args(&args(&["journal-path", "--journal"])),
            Err(UsageError)
        );
        assert_eq!(
            evaluate_args(&args(&["journal-path", "--journal", "--create"])),
            Err(UsageError)
        );
    }

    #[test]
    fn rejects_journal_path_unknown_flags() {
        assert_eq!(
            evaluate_args(&args(&["journal-path", "--unknown"])),
            Err(UsageError)
        );
    }

    #[test]
    fn rejects_journal_path_duplicate_flags() {
        assert_eq!(
            evaluate_args(&args(&["journal-path", "--create", "--create"])),
            Err(UsageError)
        );
        assert_eq!(
            evaluate_args(&args(&[
                "journal-path",
                "--journal",
                "/a",
                "--journal",
                "/b",
            ])),
            Err(UsageError)
        );
    }

    #[test]
    fn formats_version_line() {
        assert_eq!(version_line("1.2.3"), "solstone-core 1.2.3\n");
    }

    #[test]
    fn usage_lists_supported_commands() {
        assert_eq!(
            USAGE,
            "Usage:\n  solstone-core --version\n  solstone-core journal-path [--journal PATH] [--create]\n"
        );
    }
}
