// SPDX-License-Identifier: AGPL-3.0-only
// Copyright (c) 2026 sol pbc

use std::collections::BTreeMap;
use std::ffi::{OsStr, OsString};
use std::io::{Read, Result as IoResult};
use std::path::{Path, PathBuf};
use std::process::{Command, ExitCode};
use std::sync::{Mutex, mpsc};
use std::thread;
use std::time::{Duration, Instant, SystemTime, UNIX_EPOCH};
use std::{env, fs};

use chrono::Local;
use serde_json::json;
use solstone_core_journal::{
    ConfigError, HomeError, Source, discover_home, read_config_journal, resolve_journal_path,
};
use solstone_core_sol_client::command::CommandOutput;
use solstone_core_sol_client::port::read_convey_port;
use solstone_core_sol_client::seam::{
    BuildIdentityProvider, ChatEventSource, ChatInput, ClientItemIdProvider, Clock, FileProvider,
    HttpTransport, ProcessOutput, ProcessSpawner,
};
use solstone_core_sol_client::sse::SseDecoder;
use solstone_core_sol_client::transport::UreqHttpTransport;
use solstone_core_sol_client_cli::{
    DispatchSeams, Outcome, dispatch_sol_call_with_seams, dispatch_sol_chat_with_seams,
    dispatch_sol_import_with_seams, evaluate_args,
};

const EXIT_USAGE: u8 = 64;
const EXIT_TEMPFAIL: u8 = 75;
const USAGE: &str = "Usage:\n  solstone-core-sol --version\n  solstone-core-sol help\n  solstone-core-sol status\n  solstone-core-sol path\n  solstone-core-sol call <app> <verb> [args...]\n  solstone-core-sol chat [args...]\n  solstone-core-sol import [args...]\n";

pub fn run() -> ExitCode {
    let args = env::args_os().skip(1).collect::<Vec<_>>();
    match args.as_slice() {
        [] => render_output(help_output()),
        [flag] if flag == OsStr::new("--version") || flag == OsStr::new("version") => {
            render_output(version_output())
        }
        [command] if command == OsStr::new("--help") || command == OsStr::new("help") => {
            render_output(help_output())
        }
        [command] if command == OsStr::new("path") => run_path(),
        [command] if command == OsStr::new("status") => run_status(),
        [command, rest @ ..] if command == OsStr::new("call") => run_dispatched(&args, rest),
        [command, rest @ ..] if command == OsStr::new("chat") => run_dispatched(&args, rest),
        [command, rest @ ..] if command == OsStr::new("import") => run_dispatched(&args, rest),
        [flag, ..] if flag.to_string_lossy().starts_with('-') => {
            render_output(usage_error_output())
        }
        _ => render_output(unsupported_output()),
    }
}

fn version_output() -> CommandOutput {
    CommandOutput::success(format!("solstone-core-sol {}\n", env!("CARGO_PKG_VERSION")))
}

fn help_output() -> CommandOutput {
    CommandOutput::success(USAGE)
}

fn usage_error_output() -> CommandOutput {
    CommandOutput::failure(USAGE, i32::from(EXIT_USAGE))
}

fn unsupported_output() -> CommandOutput {
    CommandOutput::failure("Unsupported native sol command.\n", i32::from(EXIT_USAGE))
}

fn run_path() -> ExitCode {
    match resolve_process_journal_path() {
        Ok(line) => render_output(path_output(&line)),
        Err(error) => {
            eprintln!("native sol journal resolution failed: {error}");
            ExitCode::from(EXIT_TEMPFAIL)
        }
    }
}

fn run_status() -> ExitCode {
    match resolve_process_journal_path() {
        Ok(line) => {
            let port = read_convey_port(&line.path);
            render_output(status_output(&line, port))
        }
        Err(error) => {
            eprintln!("native sol journal resolution failed: {error}");
            ExitCode::from(EXIT_TEMPFAIL)
        }
    }
}

fn path_output(line: &JournalPathLine) -> CommandOutput {
    CommandOutput::success(format!("{}\t{}\n", line.label, line.path.display()))
}

fn status_output(line: &JournalPathLine, port: i64) -> CommandOutput {
    CommandOutput::success(format!(
        "journal\t{}\nconvey_port\t{port}\n",
        line.path.display()
    ))
}

fn run_dispatched(all_args: &[OsString], command_args: &[OsString]) -> ExitCode {
    let today = Local::now().format("%Y%m%d").to_string();
    let args = match os_strings_to_strings(command_args) {
        Some(args) => args,
        None => {
            eprint!("{USAGE}");
            return ExitCode::from(EXIT_USAGE);
        }
    };
    let outcome = evaluate_args(all_args);
    let journal = match resolve_process_journal_path() {
        Ok(line) => line,
        Err(error) => {
            eprintln!("native sol journal resolution failed: {error}");
            return ExitCode::from(EXIT_TEMPFAIL);
        }
    };
    let port = read_convey_port(&journal.path);
    let transport = UreqHttpTransport::new(port);
    let env = env::vars().collect::<BTreeMap<_, _>>();
    let stdin = read_stdin();
    let clock = SystemClock::default();
    let files = RealFileProvider;
    let build_identity = RealBuildIdentityProvider;
    let client_item_ids = RealClientItemIdProvider;
    let chat_events = ChannelChatEventSource::default();

    let output = match outcome {
        Outcome::Migrated { .. } | Outcome::MovedStub { .. } => dispatch_sol_call_with_seams(
            &args,
            &env,
            &stdin,
            &today,
            DispatchSeams {
                transport: &transport,
                clock: None,
                chat_events: None,
                files: Some(&files),
                build_identity: Some(&build_identity),
                client_item_ids: Some(&client_item_ids),
            },
        ),
        Outcome::Chat { .. } => dispatch_sol_chat_with_seams(
            &args,
            &env,
            &stdin,
            &today,
            DispatchSeams {
                transport: &transport,
                clock: Some(&clock),
                chat_events: Some(&chat_events),
                files: Some(&files),
                build_identity: Some(&build_identity),
                client_item_ids: Some(&client_item_ids),
            },
        ),
        Outcome::Import { .. } => dispatch_sol_import_with_seams(
            &args,
            &env,
            &stdin,
            &today,
            DispatchSeams {
                transport: &transport,
                clock: None,
                chat_events: None,
                files: Some(&files),
                build_identity: Some(&build_identity),
                client_item_ids: Some(&client_item_ids),
            },
        ),
        Outcome::Unsupported { .. } => unsupported_output(),
    };
    render_output(output)
}

fn render_output(output: CommandOutput) -> ExitCode {
    print!("{}", output.stdout);
    eprint!("{}", output.stderr);
    let exit = u8::try_from(output.exit).unwrap_or(EXIT_TEMPFAIL);
    ExitCode::from(exit)
}

fn read_stdin() -> String {
    let mut input = String::new();
    let _ = std::io::stdin().read_to_string(&mut input);
    input
}

fn os_strings_to_strings(args: &[OsString]) -> Option<Vec<String>> {
    args.iter()
        .map(|arg| arg.to_str().map(str::to_string))
        .collect()
}

struct JournalPathLine {
    label: &'static str,
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
            label: "env",
            path: PathBuf::from(path),
        });
    }

    let home = discover_binary_home().map_err(|HomeError::Unavailable| JournalPathError::Home)?;
    let config_journal =
        read_config_journal(&home).map_err(|ConfigError::Decode| JournalPathError::Config)?;
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

#[derive(Debug)]
struct SystemClock {
    started: Instant,
}

impl Default for SystemClock {
    fn default() -> Self {
        Self {
            started: Instant::now(),
        }
    }
}

impl Clock for SystemClock {
    fn now(&self) -> SystemTime {
        SystemTime::now()
    }

    fn monotonic(&self) -> Duration {
        self.started.elapsed()
    }

    fn sleep(&self, duration: Duration) {
        thread::sleep(duration);
    }
}

#[derive(Debug, Default)]
struct ChannelChatEventSource {
    receiver: Mutex<Option<mpsc::Receiver<ChatInput>>>,
}

impl ChatEventSource for ChannelChatEventSource {
    fn open(
        &self,
        transport: &dyn HttpTransport,
    ) -> Result<(), solstone_core_sol_client::error::ClientError> {
        let mut stream = transport.open_sse(solstone_core_sol_client::transport::SseRequest {
            path: "/sse/events".to_string(),
            policy: solstone_core_sol_client::transport::TimeoutPolicy::SseOpen,
        })?;
        let (sender, receiver) = mpsc::channel();
        *self.receiver.lock().expect("chat receiver lock") = Some(receiver);
        thread::spawn(move || {
            let mut decoder = SseDecoder::default();
            let mut buffer = [0_u8; 8192];
            loop {
                match stream.body.read(&mut buffer) {
                    Ok(0) => break,
                    Ok(count) => {
                        decoder.push_chunk(&buffer[..count]);
                        while let Some(event) = decoder.pop_event() {
                            if sender.send(ChatInput::SseEvent(event)).is_err() {
                                return;
                            }
                        }
                    }
                    Err(_) => break,
                }
            }
            let _ = sender.send(ChatInput::SseEnded);
        });
        Ok(())
    }

    fn next(&self, timeout: Duration, clock: &dyn Clock) -> ChatInput {
        let guard = self.receiver.lock().expect("chat receiver lock");
        let Some(receiver) = guard.as_ref() else {
            clock.sleep(timeout);
            return ChatInput::PollTick;
        };
        match receiver.recv_timeout(timeout) {
            Ok(input) => input,
            Err(mpsc::RecvTimeoutError::Timeout) => ChatInput::PollTick,
            Err(mpsc::RecvTimeoutError::Disconnected) => ChatInput::SseEnded,
        }
    }
}

#[derive(Debug)]
struct RealFileProvider;

impl FileProvider for RealFileProvider {
    fn read(&self, path: &Path) -> IoResult<Vec<u8>> {
        fs::read(path)
    }

    fn read_to_string(&self, path: &Path) -> IoResult<String> {
        fs::read_to_string(path)
    }

    fn exists(&self, path: &Path) -> bool {
        path.exists()
    }

    fn is_file(&self, path: &Path) -> bool {
        path.is_file()
    }

    fn canonicalize(&self, path: &Path) -> IoResult<PathBuf> {
        fs::canonicalize(path)
    }
}

struct RealClientItemIdProvider;

impl ClientItemIdProvider for RealClientItemIdProvider {
    fn client_item_id(&self) -> String {
        let mut bytes = [0_u8; 16];
        let read = fs::File::open("/dev/urandom")
            .and_then(|mut file| file.read_exact(&mut bytes))
            .is_ok();
        if !read {
            let nanos = SystemTime::now()
                .duration_since(UNIX_EPOCH)
                .unwrap_or_default()
                .as_nanos();
            bytes.copy_from_slice(&nanos.to_be_bytes());
        }
        bytes[6] = (bytes[6] & 0x0f) | 0x40;
        bytes[8] = (bytes[8] & 0x3f) | 0x80;
        bytes.iter().map(|byte| format!("{byte:02x}")).collect()
    }
}

#[derive(Debug)]
struct RealProcessSpawner;

impl ProcessSpawner for RealProcessSpawner {
    fn run(&self, program: &str, args: &[String]) -> IoResult<ProcessOutput> {
        let output = Command::new(program).args(args).output()?;
        Ok(ProcessOutput {
            status: output.status.code().unwrap_or(1),
            stdout: output.stdout,
            stderr: output.stderr,
        })
    }
}

#[derive(Debug)]
struct RealBuildIdentityProvider;

impl BuildIdentityProvider for RealBuildIdentityProvider {
    fn build_identity(&self, _journal: &Path) -> Option<serde_json::Value> {
        let spawner = RealProcessSpawner;
        let revision = spawner
            .run(
                "git",
                &[
                    "rev-parse".to_string(),
                    "--short".to_string(),
                    "HEAD".to_string(),
                ],
            )
            .ok()
            .filter(|output| output.status == 0)
            .and_then(|output| String::from_utf8(output.stdout).ok())
            .map(|value| value.trim().to_string())
            .filter(|value| !value.is_empty());
        Some(json!({
            "version": env!("CARGO_PKG_VERSION"),
            "revision": revision,
            "platform": {
                "system": env::consts::OS,
                "release": "",
                "machine": env::consts::ARCH,
                "python": "?"
            }
        }))
    }
}

#[cfg(test)]
mod tests {
    use super::*;

    use solstone_core_sol_client::seam::ScriptedHttpTransport;


    #[test]
    fn version_output_matches_old_binary_test() {
        let output = version_output();
        assert_eq!(output.stderr, "");
        assert_eq!(output.exit, 0);
        assert!(output.stdout.starts_with("solstone-core-sol "));
    }

    #[test]
    fn help_output_matches_old_binary_test() {
        let output = help_output();
        assert_eq!(output.stderr, "");
        assert_eq!(output.exit, 0);
        assert!(
            output
                .stdout
                .contains("solstone-core-sol call <app> <verb>")
        );
    }

    #[test]
    fn path_output_matches_old_binary_test() {
        let line = JournalPathLine {
            label: "default",
            path: PathBuf::from("/tmp/journal"),
        };
        assert_eq!(
            path_output(&line),
            CommandOutput::success("default\t/tmp/journal\n")
        );
    }

    #[test]
    fn status_output_matches_old_binary_test() {
        let line = JournalPathLine {
            label: "default",
            path: PathBuf::from("/tmp/journal"),
        };
        assert_eq!(
            status_output(&line, 5015),
            CommandOutput::success("journal\t/tmp/journal\nconvey_port\t5015\n")
        );
    }

    #[test]
    fn unknown_command_is_explicitly_unsupported() {
        assert_eq!(
            unsupported_output(),
            CommandOutput::failure("Unsupported native sol command.\n", i32::from(EXIT_USAGE))
        );
    }

    #[test]
    fn invalid_flag_prints_usage() {
        let output = usage_error_output();
        assert_eq!(output.stdout, "");
        assert_eq!(output.exit, i32::from(EXIT_USAGE));
        assert!(output.stderr.starts_with("Usage:\n"));
    }

    #[test]
    fn moved_stub_dispatches_and_exits_two() {
        let args = vec![
            "identity".to_string(),
            "--unknown".to_string(),
            "extra".to_string(),
        ];
        let env = BTreeMap::new();
        let transport = ScriptedHttpTransport::new(vec![]);
        let output = dispatch_sol_call_with_seams(
            &args,
            &env,
            "",
            "20260723",
            DispatchSeams {
                transport: &transport,
                clock: None,
                chat_events: None,
                files: None,
                build_identity: None,
                client_item_ids: None,
            },
        );

        assert_eq!(output.exit, 2);
        assert_eq!(
            output.stderr,
            "Moved to `journal identity` — run that instead.\n"
        );
        transport.assert_done();
    }

}
