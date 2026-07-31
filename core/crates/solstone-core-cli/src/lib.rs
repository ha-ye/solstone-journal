// SPDX-License-Identifier: AGPL-3.0-only
// Copyright (c) 2026 sol pbc

use std::ffi::{OsStr, OsString};

pub const USAGE: &str = "Usage:\n  solstone-core --version\n  solstone-core journal-path [--journal PATH] [--create]\n  solstone-core indexer [--journal PATH] [--reset] [--rebuild-edges] [--rescan | --rescan-full | --rescan-file PATH]\n  solstone-core spl service [-v | --verbose] [-d | --debug]\n  solstone-core spl hpke <open-base | seal-base>\n";

#[derive(Debug, Clone, PartialEq, Eq)]
pub enum Command {
    Version,
    JournalPath(JournalPathOptions),
    Indexer(IndexerOptions),
    Spl(SplCommand),
}

#[derive(Debug, Clone, Copy, PartialEq, Eq)]
pub enum SplCommand {
    Service(ServiceOptions),
    Hpke(HpkeCommand),
}

#[derive(Debug, Clone, Copy, PartialEq, Eq)]
pub struct ServiceOptions {
    pub verbose: bool,
    pub debug: bool,
}

#[derive(Debug, Clone, Copy, PartialEq, Eq)]
pub enum HpkeCommand {
    OpenBase,
    SealBase,
}

#[derive(Debug, Clone, PartialEq, Eq)]
pub struct JournalPathOptions {
    pub journal_override: Option<OsString>,
    pub create: bool,
}

#[derive(Debug, Clone, PartialEq, Eq)]
pub struct IndexerOptions {
    pub journal_override: Option<OsString>,
    pub reset: bool,
    pub rebuild_edges: bool,
    pub rescan: bool,
    pub rescan_full: bool,
    pub rescan_file: Option<OsString>,
}

#[derive(Debug, Clone, Copy, PartialEq, Eq)]
pub struct UsageError;

pub fn evaluate_args(args: &[OsString]) -> Result<Command, UsageError> {
    match args {
        [flag] if flag == OsStr::new("--version") => Ok(Command::Version),
        [command, rest @ ..] if command == OsStr::new("journal-path") => {
            parse_journal_path(rest).map(Command::JournalPath)
        }
        [command, rest @ ..] if command == OsStr::new("indexer") => {
            parse_indexer(rest).map(Command::Indexer)
        }
        [command, rest @ ..] if command == OsStr::new("spl") => parse_spl(rest).map(Command::Spl),
        _ => Err(UsageError),
    }
}

fn parse_spl(args: &[OsString]) -> Result<SplCommand, UsageError> {
    match args {
        [command, rest @ ..] if command == OsStr::new("service") => {
            parse_service(rest).map(SplCommand::Service)
        }
        [command, operation]
            if command == OsStr::new("hpke") && operation == OsStr::new("open-base") =>
        {
            Ok(SplCommand::Hpke(HpkeCommand::OpenBase))
        }
        [command, operation]
            if command == OsStr::new("hpke") && operation == OsStr::new("seal-base") =>
        {
            Ok(SplCommand::Hpke(HpkeCommand::SealBase))
        }
        _ => Err(UsageError),
    }
}

fn parse_service(args: &[OsString]) -> Result<ServiceOptions, UsageError> {
    let mut verbose = false;
    let mut debug = false;
    for argument in args {
        let argument = argument.as_os_str();
        if argument == OsStr::new("-v") || argument == OsStr::new("--verbose") {
            if verbose {
                return Err(UsageError);
            }
            verbose = true;
            continue;
        }
        if argument == OsStr::new("-d") || argument == OsStr::new("--debug") {
            if debug {
                return Err(UsageError);
            }
            debug = true;
            continue;
        }
        return Err(UsageError);
    }
    Ok(ServiceOptions { verbose, debug })
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

fn parse_indexer(args: &[OsString]) -> Result<IndexerOptions, UsageError> {
    let mut journal_override = None;
    let mut reset = false;
    let mut rebuild_edges = false;
    let mut rescan = false;
    let mut rescan_full = false;
    let mut rescan_file = None;
    let mut index = 0;
    while index < args.len() {
        let arg = args[index].as_os_str();
        if arg == OsStr::new("--reset") {
            if reset {
                return Err(UsageError);
            }
            reset = true;
            index += 1;
            continue;
        }
        if arg == OsStr::new("--rebuild-edges") {
            if rebuild_edges {
                return Err(UsageError);
            }
            rebuild_edges = true;
            index += 1;
            continue;
        }
        if arg == OsStr::new("--rescan") {
            if rescan {
                return Err(UsageError);
            }
            rescan = true;
            index += 1;
            continue;
        }
        if arg == OsStr::new("--rescan-full") {
            if rescan_full {
                return Err(UsageError);
            }
            rescan_full = true;
            index += 1;
            continue;
        }
        if arg == OsStr::new("--journal") {
            if journal_override.is_some() {
                return Err(UsageError);
            }
            let value = args.get(index + 1).ok_or(UsageError)?;
            if is_indexer_flag(value.as_os_str()) {
                return Err(UsageError);
            }
            journal_override = Some(value.clone());
            index += 2;
            continue;
        }
        if arg == OsStr::new("--rescan-file") {
            if rescan_file.is_some() {
                return Err(UsageError);
            }
            let value = args.get(index + 1).ok_or(UsageError)?;
            if is_indexer_flag(value.as_os_str()) {
                return Err(UsageError);
            }
            rescan_file = Some(value.clone());
            index += 2;
            continue;
        }
        return Err(UsageError);
    }

    if rescan_file.is_some() && (rescan || rescan_full) {
        return Err(UsageError);
    }

    Ok(IndexerOptions {
        journal_override,
        reset,
        rebuild_edges,
        rescan,
        rescan_full,
        rescan_file,
    })
}

fn is_indexer_flag(value: &OsStr) -> bool {
    matches!(
        value.to_str(),
        Some(
            "--journal"
                | "--reset"
                | "--rebuild-edges"
                | "--rescan"
                | "--rescan-full"
                | "--rescan-file",
        )
    )
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
    fn accepts_indexer_without_operation_flags() {
        assert_eq!(
            evaluate_args(&args(&["indexer"])),
            Ok(Command::Indexer(IndexerOptions {
                journal_override: None,
                reset: false,
                rebuild_edges: false,
                rescan: false,
                rescan_full: false,
                rescan_file: None,
            }))
        );
    }

    #[test]
    fn accepts_spl_service() {
        assert_eq!(
            evaluate_args(&args(&["spl", "service"])),
            Ok(Command::Spl(SplCommand::Service(ServiceOptions {
                verbose: false,
                debug: false,
            })))
        );
    }

    #[test]
    fn accepts_spl_service_verbose_and_debug_flags_in_either_order() {
        assert_eq!(
            evaluate_args(&args(&["spl", "service", "-v", "--debug"])),
            Ok(Command::Spl(SplCommand::Service(ServiceOptions {
                verbose: true,
                debug: true,
            })))
        );
        assert_eq!(
            evaluate_args(&args(&["spl", "service", "-d", "--verbose"])),
            Ok(Command::Spl(SplCommand::Service(ServiceOptions {
                verbose: true,
                debug: true,
            })))
        );
    }

    #[test]
    fn accepts_each_spl_service_flag() {
        for (flag, expected) in [
            (
                "-v",
                ServiceOptions {
                    verbose: true,
                    debug: false,
                },
            ),
            (
                "--verbose",
                ServiceOptions {
                    verbose: true,
                    debug: false,
                },
            ),
            (
                "-d",
                ServiceOptions {
                    verbose: false,
                    debug: true,
                },
            ),
            (
                "--debug",
                ServiceOptions {
                    verbose: false,
                    debug: true,
                },
            ),
        ] {
            assert_eq!(
                evaluate_args(&args(&["spl", "service", flag])),
                Ok(Command::Spl(SplCommand::Service(expected))),
                "{flag}"
            );
        }
    }

    #[test]
    fn rejects_duplicate_or_unknown_spl_service_flags() {
        for values in [
            &["spl", "service", "-v", "-v"][..],
            &["spl", "service", "--verbose", "--verbose"][..],
            &["spl", "service", "-v", "--verbose"][..],
            &["spl", "service", "-d", "-d"][..],
            &["spl", "service", "--debug", "--debug"][..],
            &["spl", "service", "-d", "--debug"][..],
            &["spl", "service", "--unknown"][..],
        ] {
            assert_eq!(evaluate_args(&args(values)), Err(UsageError), "{values:?}");
        }
    }

    #[test]
    fn rejects_spl_service_extra_args() {
        for values in [
            &["spl", "service", "extra"][..],
            &["spl", "service", "service"][..],
        ] {
            assert_eq!(evaluate_args(&args(values)), Err(UsageError), "{values:?}");
        }
    }

    #[test]
    fn rejects_spl_hpke_flags() {
        for values in [
            &["spl", "hpke", "open-base", "-v"][..],
            &["spl", "hpke", "open-base", "--verbose"][..],
            &["spl", "hpke", "seal-base", "-d"][..],
            &["spl", "hpke", "seal-base", "--debug"][..],
        ] {
            assert_eq!(evaluate_args(&args(values)), Err(UsageError), "{values:?}");
        }
    }

    #[test]
    fn accepts_spl_hpke_base_operations() {
        assert_eq!(
            evaluate_args(&args(&["spl", "hpke", "open-base"])),
            Ok(Command::Spl(SplCommand::Hpke(HpkeCommand::OpenBase)))
        );
        assert_eq!(
            evaluate_args(&args(&["spl", "hpke", "seal-base"])),
            Ok(Command::Spl(SplCommand::Hpke(HpkeCommand::SealBase)))
        );
    }

    #[test]
    fn rejects_incomplete_unknown_and_extra_spl_args() {
        for values in [
            &["spl"][..],
            &["spl", "hpke"][..],
            &["spl", "hpke", "unknown"][..],
            &["spl", "hpke", "open-base", "extra"][..],
            &["spl", "hpke", "open-base", "open-base"][..],
            &["spl", "hpke", "seal-base", "extra"][..],
            &["spl", "hpke", "seal-base", "seal-base"][..],
            &["spl", "unknown"][..],
        ] {
            assert_eq!(evaluate_args(&args(values)), Err(UsageError), "{values:?}");
        }
    }

    #[test]
    fn accepts_indexer_rescan_full_reset_and_override() {
        assert_eq!(
            evaluate_args(&args(&[
                "indexer",
                "--journal",
                "/tmp/journal",
                "--reset",
                "--rescan-full",
            ])),
            Ok(Command::Indexer(IndexerOptions {
                journal_override: Some(OsString::from("/tmp/journal")),
                reset: true,
                rebuild_edges: false,
                rescan: false,
                rescan_full: true,
                rescan_file: None,
            }))
        );
    }

    #[test]
    fn accepts_indexer_rescan_file() {
        assert_eq!(
            evaluate_args(&args(&[
                "indexer",
                "--rescan-file",
                "20240101/talents/flow.md",
            ])),
            Ok(Command::Indexer(IndexerOptions {
                journal_override: None,
                reset: false,
                rebuild_edges: false,
                rescan: false,
                rescan_full: false,
                rescan_file: Some(OsString::from("20240101/talents/flow.md")),
            }))
        );
    }

    #[test]
    fn accepts_indexer_rebuild_edges_composed_with_rescan() {
        assert_eq!(
            evaluate_args(&args(&["indexer", "--rebuild-edges", "--rescan"])),
            Ok(Command::Indexer(IndexerOptions {
                journal_override: None,
                reset: false,
                rebuild_edges: true,
                rescan: true,
                rescan_full: false,
                rescan_file: None,
            }))
        );
    }

    #[test]
    fn rejects_indexer_conflicts_missing_values_and_duplicates() {
        for values in [
            &["indexer", "--rescan-file"][..],
            &["indexer", "--journal"][..],
            &["indexer", "--rescan-file", "--rescan"][..],
            &["indexer", "--journal", "--reset"][..],
            &["indexer", "--reset", "--reset"][..],
            &["indexer", "--rebuild-edges", "--rebuild-edges"][..],
            &["indexer", "--rescan-file", "a.md", "--rescan"][..],
            &["indexer", "--rescan-file", "a.md", "--rescan-full"][..],
            &["indexer", "--unknown"][..],
        ] {
            assert_eq!(evaluate_args(&args(values)), Err(UsageError), "{values:?}");
        }
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
            "Usage:\n  solstone-core --version\n  solstone-core journal-path [--journal PATH] [--create]\n  solstone-core indexer [--journal PATH] [--reset] [--rebuild-edges] [--rescan | --rescan-full | --rescan-file PATH]\n  solstone-core spl service [-v | --verbose] [-d | --debug]\n  solstone-core spl hpke <open-base | seal-base>\n"
        );
    }
}
