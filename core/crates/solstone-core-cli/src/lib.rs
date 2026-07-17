// SPDX-License-Identifier: AGPL-3.0-only
// Copyright (c) 2026 sol pbc

pub const USAGE: &str = "Usage: solstone-core --version\n";

#[derive(Debug, Clone, Copy, PartialEq, Eq)]
pub enum Command {
    Version,
}

#[derive(Debug, Clone, Copy, PartialEq, Eq)]
pub struct UsageError;

pub fn evaluate_args(args: &[String]) -> Result<Command, UsageError> {
    match args {
        [flag] if flag == "--version" => Ok(Command::Version),
        _ => Err(UsageError),
    }
}

pub fn version_line(version: &str) -> String {
    format!("solstone-core {version}\n")
}

#[cfg(test)]
mod tests {
    use super::*;

    fn args(values: &[&str]) -> Vec<String> {
        values.iter().map(|value| (*value).to_string()).collect()
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
    fn formats_version_line() {
        assert_eq!(version_line("1.2.3"), "solstone-core 1.2.3\n");
    }

    #[test]
    fn usage_is_one_line() {
        assert_eq!(USAGE, "Usage: solstone-core --version\n");
    }
}
