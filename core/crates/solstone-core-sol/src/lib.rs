// SPDX-License-Identifier: AGPL-3.0-only
// Copyright (c) 2026 sol pbc

use std::collections::BTreeMap;
use std::ffi::{OsStr, OsString};
use std::io::{IsTerminal, Read, Result as IoResult};
use std::path::{Path, PathBuf};
use std::process::{Command, ExitCode};
use std::sync::{Mutex, mpsc};
use std::thread;
use std::time::{Duration, Instant, SystemTime, UNIX_EPOCH};
use std::{env, fs};

use chrono::Local;
use serde_json::json;
use solstone_core_journal::{
    ConfigError, HomeError, Source, detect_checkout_root, discover_home, read_config_journal,
    resolve_journal_path,
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
    dispatch_sol_import_with_seams, evaluate_args, help,
};

mod generated;

use generated::journal_host_commands::{JOURNAL_HOST_COMMAND_COUNT, JOURNAL_HOST_COMMANDS};

const EXIT_USAGE: u8 = 64;
const EXIT_SOFTWARE: u8 = 70;
const EXIT_CONFIG: u8 = 78;
const EXIT_TEMPFAIL: u8 = 75;
const USAGE: &str = "Usage: sol <command> [args...]\n";
const SERVICE_MOVED_EXIT: i32 = 2;
const SOL_SERVICE_CMD_REMOVED_ERROR_TAIL: &str = "('sol' is the journal-access surface; 'journal' surfaces journal-service commands; see 'journal --help'.)";
const COMPAT_HELPER_NAME: &str = "solstone-python-compat";
const COMPAT_SENTINEL: &str = "SOLSTONE_NATIVE_COMPAT_ACTIVE";
const COMPAT_SENTINEL_ARMED: &str = "armed";
const COMPAT_ARGV0_MARKER_PREFIX: &str = "__solstone_native_argv0=";
const COMPAT_RECURSION_ERROR: &str =
    "sol: compatibility dispatch recursion detected. Reinstall solstone and solstone-core.";
const TOP_LEVEL_COMPAT_COMMANDS: &[&str] =
    &["notify", "doctor", "check", "contract", "skills", "link"];

pub fn run() -> ExitCode {
    let args = env::args_os().skip(1).collect::<Vec<_>>();
    run_with_stdin_provider(args, &RealStdinProvider)
}

fn run_with_stdin_provider(args: Vec<OsString>, stdin_provider: &dyn StdinProvider) -> ExitCode {
    let mut args = args;
    if args.first().is_some_and(|arg| is_verbose_flag(arg)) {
        args.remove(0);
    }
    match args.as_slice() {
        [] => render_output(help_output()),
        [flag] if flag == OsStr::new("--version") || flag == OsStr::new("-V") => {
            render_output(version_output())
        }
        [command]
            if command == OsStr::new("--help")
                || command == OsStr::new("-h")
                || command == OsStr::new("help") =>
        {
            render_output(help_output())
        }
        [command] if command == OsStr::new("root") => run_root(),
        [command] if command == OsStr::new("--path") => run_plain_path(),
        [command] if command == OsStr::new("path") => run_path(),
        [command] if command == OsStr::new("status") => run_status(),
        [command, rest @ ..] if command == OsStr::new("call") => {
            run_call(&args, rest, stdin_provider)
        }
        [command, rest @ ..] if command == OsStr::new("chat") => {
            run_top_level_native(&args, "chat", rest, stdin_provider)
        }
        [command, rest @ ..] if command == OsStr::new("import") => {
            run_top_level_native(&args, "import", rest, stdin_provider)
        }
        [flag, ..] if flag.to_string_lossy().starts_with('-') => {
            render_output(usage_error_output())
        }
        [command, ..] if is_journal_host_command(command) => {
            render_output(service_moved_output(command))
        }
        [command, ..] if is_top_level_compat_command(command) => delegate_to_compat(&args),
        _ => render_output(unsupported_output()),
    }
}

fn version_output() -> CommandOutput {
    CommandOutput::success(format!("sol (solstone) {}\n", env!("CARGO_PKG_VERSION")))
}

fn help_output() -> CommandOutput {
    let status = root_help_status();
    CommandOutput {
        stdout: help::render_root_help(help::RootHelpStatus {
            journal_path: status.journal_path.as_deref(),
            days: status.days,
        }),
        stderr: status.warnings,
        exit: 0,
    }
}

fn usage_error_output() -> CommandOutput {
    CommandOutput::failure(USAGE, i32::from(EXIT_USAGE))
}

fn unsupported_output() -> CommandOutput {
    CommandOutput::failure("Unsupported native sol command.\n", i32::from(EXIT_USAGE))
}

fn service_moved_output(command: &OsStr) -> CommandOutput {
    let command = command.to_string_lossy();
    CommandOutput::failure(
        format!(
            "'{command}' moved to 'journal {command}' — run that instead.\n{SOL_SERVICE_CMD_REMOVED_ERROR_TAIL}\n"
        ),
        SERVICE_MOVED_EXIT,
    )
}

fn is_top_level_compat_command(command: &OsStr) -> bool {
    command
        .to_str()
        .is_some_and(|value| TOP_LEVEL_COMPAT_COMMANDS.contains(&value))
}

fn is_journal_host_command(command: &OsStr) -> bool {
    debug_assert_eq!(JOURNAL_HOST_COMMANDS.len(), JOURNAL_HOST_COMMAND_COUNT);
    command
        .to_str()
        .is_some_and(|value| JOURNAL_HOST_COMMANDS.binary_search(&value).is_ok())
}

fn is_verbose_flag(command: &OsStr) -> bool {
    command == OsStr::new("-v") || command == OsStr::new("--verbose")
}

#[derive(Debug)]
enum ProjectRootError {
    CurrentExe(std::io::Error),
    Unclassified(PathBuf),
}

impl std::fmt::Display for ProjectRootError {
    fn fmt(&self, formatter: &mut std::fmt::Formatter<'_>) -> std::fmt::Result {
        match self {
            ProjectRootError::CurrentExe(error) => write!(
                formatter,
                "native sol project root resolution failed: could not inspect current executable: {error}"
            ),
            ProjectRootError::Unclassified(executable) => write!(
                formatter,
                "native sol project root resolution failed: could not locate source checkout or installed solstone package from {}",
                executable.display()
            ),
        }
    }
}

impl std::error::Error for ProjectRootError {}

fn installed_site_packages_from_executable_dir(executable_dir: &Path) -> Option<PathBuf> {
    let prefix = executable_dir.parent()?;
    let entries = fs::read_dir(prefix).ok()?;
    for lib_entry in entries.flatten() {
        let lib_path = lib_entry.path();
        if !lib_path.is_dir() {
            continue;
        }
        let Some(lib_name) = lib_path.file_name().and_then(OsStr::to_str) else {
            continue;
        };
        if !lib_name.starts_with("lib") {
            continue;
        }
        let Ok(python_entries) = fs::read_dir(&lib_path) else {
            continue;
        };
        for python_entry in python_entries.flatten() {
            let python_path = python_entry.path();
            if !python_path.is_dir() {
                continue;
            }
            let Some(python_name) = python_path.file_name().and_then(OsStr::to_str) else {
                continue;
            };
            if !python_name.starts_with("python") {
                continue;
            }
            for package_dir_name in ["site-packages", "dist-packages"] {
                let package_dir = python_path.join(package_dir_name);
                let init = package_dir.join("solstone").join("__init__.py");
                if fs::metadata(&init).is_ok_and(|metadata| metadata.is_file()) {
                    return Some(package_dir);
                }
            }
        }
    }
    None
}

fn is_solstone_checkout_root(candidate: &Path) -> bool {
    candidate.join("pyproject.toml").is_file()
        && candidate.join(".git").exists()
        && candidate.join("solstone").is_dir()
}

fn run_root() -> ExitCode {
    match resolve_project_root() {
        Ok(root) => render_output(CommandOutput::success(format!("{}\n", root.display()))),
        Err(error) => {
            eprintln!("{error}");
            ExitCode::from(EXIT_CONFIG)
        }
    }
}

fn resolve_project_root() -> Result<PathBuf, ProjectRootError> {
    let executable = env::current_exe().map_err(ProjectRootError::CurrentExe)?;
    resolve_project_root_from_executable(&executable)
}

fn resolve_project_root_from_executable(executable: &Path) -> Result<PathBuf, ProjectRootError> {
    let Some(executable_dir) = executable.parent() else {
        return Err(ProjectRootError::Unclassified(executable.to_path_buf()));
    };
    if let Some(site_packages) = installed_site_packages_from_executable_dir(executable_dir) {
        return Ok(site_packages);
    }
    for candidate in executable_dir.ancestors() {
        if is_solstone_checkout_root(candidate) {
            return Ok(candidate.to_path_buf());
        }
    }
    Err(ProjectRootError::Unclassified(executable.to_path_buf()))
}

fn should_delegate_to_compat_after_native_miss(all_args: &[OsString], outcome: &Outcome) -> bool {
    matches!(outcome, Outcome::Unsupported { .. }) && is_compat_public_args(all_args)
}

fn is_compat_public_args(all_args: &[OsString]) -> bool {
    match all_args {
        [command, ..] if is_top_level_compat_command(command) => true,
        [command, group, ..] if command == OsStr::new("call") && group == OsStr::new("journal") => {
            true
        }
        _ => false,
    }
}

fn run_plain_path() -> ExitCode {
    match resolve_process_journal_path() {
        Ok(line) => render_output(plain_path_output(&line)),
        Err(error) => {
            eprintln!("native sol journal resolution failed: {error}");
            ExitCode::from(EXIT_TEMPFAIL)
        }
    }
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

fn run_call(
    all_args: &[OsString],
    command_args: &[OsString],
    stdin_provider: &dyn StdinProvider,
) -> ExitCode {
    let Some(args) = os_strings_to_strings(command_args) else {
        return render_output(usage_error_output());
    };
    if let Some(output) = help::render_sol_call_help(&args) {
        return render_output(output);
    }
    run_dispatched(all_args, command_args, stdin_provider)
}

fn run_top_level_native(
    all_args: &[OsString],
    command: &str,
    command_args: &[OsString],
    stdin_provider: &dyn StdinProvider,
) -> ExitCode {
    let Some(args) = os_strings_to_strings(command_args) else {
        return render_output(usage_error_output());
    };
    if let Some(output) = help::render_top_level_help(command, &args) {
        return render_output(output);
    }
    run_dispatched(all_args, command_args, stdin_provider)
}

fn plain_path_output(line: &JournalPathLine) -> CommandOutput {
    CommandOutput::success(format!("{}\n", line.path.display()))
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

struct RootHelpStatus {
    journal_path: Option<String>,
    days: Option<usize>,
    warnings: String,
}

fn root_help_status() -> RootHelpStatus {
    let line = match resolve_process_journal_path() {
        Ok(line) => line,
        Err(error) => {
            return RootHelpStatus {
                journal_path: None,
                days: None,
                warnings: format!(
                    "Warning: could not resolve journal status ({error}). Check SOLSTONE_JOURNAL or ~/.config/solstone/config.toml; showing command help without journal metadata.\n"
                ),
            };
        }
    };
    match inspect_journal_days(&line.path) {
        Ok(days) => RootHelpStatus {
            journal_path: Some(line.path.display().to_string()),
            days,
            warnings: String::new(),
        },
        Err(error) => RootHelpStatus {
            journal_path: Some(line.path.display().to_string()),
            days: None,
            warnings: format!(
                "Warning: could not read journal day status for {} ({error}); showing command help without day count.\n",
                line.path.display()
            ),
        },
    }
}

fn inspect_journal_days(journal: &Path) -> Result<Option<usize>, std::io::Error> {
    match fs::metadata(journal) {
        Ok(metadata) => {
            if !metadata.is_dir() {
                return Ok(None);
            }
        }
        Err(error) if error.kind() == std::io::ErrorKind::NotFound => return Ok(None),
        Err(error) => return Err(error),
    }

    let chronicle = journal.join("chronicle");
    match fs::metadata(&chronicle) {
        Ok(metadata) => {
            if !metadata.is_dir() {
                return Ok(Some(0));
            }
        }
        Err(error) if error.kind() == std::io::ErrorKind::NotFound => return Ok(Some(0)),
        Err(error) => return Err(error),
    }

    let mut days = 0;
    for entry in fs::read_dir(&chronicle)? {
        let entry = entry?;
        let name = entry.file_name();
        if !name.to_str().is_some_and(is_day_dir_name) {
            continue;
        }
        match entry.metadata() {
            Ok(metadata) if metadata.is_dir() => days += 1,
            Ok(_) => {}
            Err(error) if error.kind() == std::io::ErrorKind::NotFound => {}
            Err(error) => return Err(error),
        }
    }
    Ok(Some(days))
}

fn is_day_dir_name(value: &str) -> bool {
    value.len() == 8 && value.bytes().all(|byte| byte.is_ascii_digit())
}

fn run_dispatched(
    all_args: &[OsString],
    command_args: &[OsString],
    stdin_provider: &dyn StdinProvider,
) -> ExitCode {
    let outcome = evaluate_args(all_args);
    if should_delegate_to_compat_after_native_miss(all_args, &outcome) {
        return delegate_to_compat(all_args);
    }
    if matches!(outcome, Outcome::Unsupported { .. }) {
        return render_output(unsupported_output());
    }
    let today = Local::now().format("%Y%m%d").to_string();
    let args = match os_strings_to_strings(command_args) {
        Some(args) => args,
        None => {
            eprint!("{USAGE}");
            return ExitCode::from(EXIT_USAGE);
        }
    };
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
    let stdin = match stdin_provider.read_if_piped() {
        Ok(Some(value)) => value,
        Ok(None) => String::new(),
        Err(error) => {
            eprintln!("native sol stdin read failed: {error}");
            return ExitCode::from(EXIT_TEMPFAIL);
        }
    };
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

fn delegate_to_compat(all_args: &[OsString]) -> ExitCode {
    let existing_sentinel = env::var_os(COMPAT_SENTINEL);
    if let Err(output) = compat_env_preflight(existing_sentinel.as_deref()) {
        return render_output(output);
    }
    let executable = env::current_exe().unwrap_or_else(|_| PathBuf::from("sol"));
    let helper = helper_path_for_executable(&executable);
    if !is_executable(&helper) {
        return render_output(missing_helper_output(&helper));
    }
    let public_argv0 = public_argv0_for_executable(&executable);
    let args = compat_exec_args(&public_argv0, all_args);
    exec_compat(&helper, &args)
}

fn helper_path_for_executable(executable: &Path) -> PathBuf {
    executable.with_file_name(COMPAT_HELPER_NAME)
}

fn missing_helper_message(path: &Path) -> String {
    format!(
        "sol: native compatibility helper is missing or not executable: {}. Reinstall solstone and solstone-core.",
        path.display()
    )
}

fn missing_helper_output(path: &Path) -> CommandOutput {
    CommandOutput::failure(
        format!("{}\n", missing_helper_message(path)),
        i32::from(EXIT_CONFIG),
    )
}

fn compat_recursion_output() -> CommandOutput {
    CommandOutput::failure(
        format!("{COMPAT_RECURSION_ERROR}\n"),
        i32::from(EXIT_SOFTWARE),
    )
}

fn compat_env_preflight(existing_sentinel: Option<&OsStr>) -> Result<(), CommandOutput> {
    if existing_sentinel.is_some() {
        Err(compat_recursion_output())
    } else {
        Ok(())
    }
}

fn public_argv0_for_executable(executable: &Path) -> String {
    match executable.file_name().and_then(OsStr::to_str) {
        Some("solstone") => "solstone".to_string(),
        _ => "sol".to_string(),
    }
}

fn compat_exec_args(public_argv0: &str, all_args: &[OsString]) -> Vec<OsString> {
    let mut args = vec![OsString::from(format!(
        "{COMPAT_ARGV0_MARKER_PREFIX}{public_argv0}"
    ))];
    args.extend(all_args.iter().cloned());
    args
}

fn is_executable(path: &Path) -> bool {
    let Ok(metadata) = fs::metadata(path) else {
        return false;
    };
    if !metadata.is_file() {
        return false;
    }
    #[cfg(unix)]
    {
        use std::os::unix::fs::PermissionsExt;
        metadata.permissions().mode() & 0o111 != 0
    }
    #[cfg(not(unix))]
    {
        true
    }
}

#[cfg(unix)]
fn exec_compat(helper: &Path, args: &[OsString]) -> ExitCode {
    use std::os::unix::process::CommandExt;

    let error = Command::new(helper)
        .args(args)
        .env(COMPAT_SENTINEL, COMPAT_SENTINEL_ARMED)
        .exec();
    eprintln!(
        "sol: native compatibility helper failed to execute: {}: {error}. Reinstall solstone and solstone-core.",
        helper.display()
    );
    ExitCode::from(EXIT_CONFIG)
}

#[cfg(not(unix))]
fn exec_compat(helper: &Path, _args: &[OsString]) -> ExitCode {
    eprintln!(
        "sol: native compatibility helper failed to execute: {}: exec is unavailable. Reinstall solstone and solstone-core.",
        helper.display()
    );
    ExitCode::from(EXIT_CONFIG)
}

fn render_output(output: CommandOutput) -> ExitCode {
    print!("{}", output.stdout);
    eprint!("{}", output.stderr);
    let exit = u8::try_from(output.exit).unwrap_or(EXIT_TEMPFAIL);
    ExitCode::from(exit)
}

trait StdinProvider {
    fn read_if_piped(&self) -> IoResult<Option<String>>;
}

struct RealStdinProvider;

impl StdinProvider for RealStdinProvider {
    fn read_if_piped(&self) -> IoResult<Option<String>> {
        let mut stdin = std::io::stdin();
        if stdin.is_terminal() {
            return Ok(None);
        }
        let mut input = String::new();
        stdin.read_to_string(&mut input)?;
        Ok(Some(input))
    }
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
    let checkout_root = detect_process_checkout_root();
    let resolved = resolve_journal_path(
        env_journal.as_deref(),
        config_journal.as_deref(),
        checkout_root.as_deref(),
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

fn detect_process_checkout_root() -> Option<PathBuf> {
    let executable = env::current_exe().ok()?;
    let executable_dir = executable.parent()?;
    if installed_site_packages_from_executable_dir(executable_dir).is_some() {
        return None;
    }
    executable_dir.ancestors().find_map(detect_checkout_root)
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

    struct PanicStdinProvider;

    impl StdinProvider for PanicStdinProvider {
        fn read_if_piped(&self) -> IoResult<Option<String>> {
            panic!("stdin must not be read for this route")
        }
    }

    fn os_args(values: &[&str]) -> Vec<OsString> {
        values.iter().map(OsString::from).collect()
    }

    fn temp_path(name: &str) -> PathBuf {
        let stamp = SystemTime::now()
            .duration_since(UNIX_EPOCH)
            .expect("time should be available")
            .as_nanos();
        env::temp_dir().join(format!("solstone-core-sol-{name}-{stamp}"))
    }

    #[test]
    fn version_output_matches_restored_native_contract() {
        let output = version_output();
        assert_eq!(output.stderr, "");
        assert_eq!(output.exit, 0);
        assert_eq!(
            output.stdout,
            format!("sol (solstone) {}\n", env!("CARGO_PKG_VERSION"))
        );
    }

    #[test]
    fn help_output_matches_restored_root_contract_shape() {
        let output = help_output();
        assert_eq!(output.exit, 0);
        assert!(
            output
                .stdout
                .starts_with("sol - journal access CLI (solstone)\n\n")
        );
        assert!(output.stdout.contains("Usage: sol <command> [args...]\n"));
        assert!(output.stdout.contains("Conversation\n  chat\n"));
        assert!(output.stdout.contains("Apps (sol call <app>):\n"));
        assert!(output.stdout.contains("  call journal\n"));
    }

    #[test]
    fn call_help_lists_native_groups_and_journal_compat() {
        let output = help::render_call_root_help();
        assert!(output.contains("Usage: sol call <app> <verb> [args...]"));
        assert!(output.contains("  activities\n"));
        assert!(output.contains("  journal\n"));
    }

    #[test]
    fn project_root_resolution_returns_an_existing_directory() {
        assert!(
            resolve_project_root()
                .expect("project root should resolve")
                .is_dir()
        );
    }

    #[test]
    fn project_root_prefers_installed_package_layout_over_checkout_ancestor() {
        let root = temp_path("installed-root");
        let checkout = root.join("checkout");
        let bin = checkout.join(".venv").join("bin");
        let site_packages = checkout
            .join(".venv")
            .join("lib")
            .join("python3.13")
            .join("site-packages");
        fs::create_dir_all(checkout.join(".git")).expect("create .git");
        fs::write(checkout.join("pyproject.toml"), "[project]\n").expect("write pyproject");
        fs::create_dir_all(checkout.join("solstone")).expect("create checkout package dir");
        fs::create_dir_all(site_packages.join("solstone")).expect("create installed package");
        fs::write(site_packages.join("solstone").join("__init__.py"), "").expect("write init");
        fs::create_dir_all(&bin).expect("create bin");

        let resolved = resolve_project_root_from_executable(&bin.join("sol"))
            .expect("installed project root should resolve");
        assert_eq!(resolved, site_packages);
        fs::remove_dir_all(root).expect("cleanup temp root");
    }

    #[test]
    fn project_root_uses_executable_checkout_ancestry_without_cwd_fallback() {
        let root = temp_path("checkout-root");
        let checkout = root.join("checkout");
        let bin = checkout.join("core").join("target").join("debug");
        fs::create_dir_all(checkout.join(".git")).expect("create .git");
        fs::write(checkout.join("pyproject.toml"), "[project]\n").expect("write pyproject");
        fs::create_dir_all(checkout.join("solstone")).expect("create package dir");
        fs::create_dir_all(&bin).expect("create bin");

        let resolved = resolve_project_root_from_executable(&bin.join("sol"))
            .expect("checkout should resolve");
        assert_eq!(resolved, checkout);
        fs::remove_dir_all(root).expect("cleanup temp root");
    }

    #[test]
    fn project_root_errors_when_executable_artifact_is_unclassified() {
        let root = temp_path("unclassified-root");
        let bin = root.join("bin");
        fs::create_dir_all(&bin).expect("create bin");
        let error = resolve_project_root_from_executable(&bin.join("sol")).unwrap_err();
        assert!(error.to_string().contains(
            "native sol project root resolution failed: could not locate source checkout or installed solstone package"
        ));
        fs::remove_dir_all(root).expect("cleanup temp root");
    }

    #[test]
    fn path_output_keeps_labeled_native_proof_surface() {
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
    fn plain_path_output_matches_oracle_path_flag() {
        let line = JournalPathLine {
            label: "default",
            path: PathBuf::from("/tmp/journal"),
        };
        assert_eq!(
            plain_path_output(&line),
            CommandOutput::success("/tmp/journal\n")
        );
    }

    #[test]
    fn absent_journal_omits_days_without_warning_condition() {
        let root = temp_path("absent-journal");
        assert_eq!(inspect_journal_days(&root).unwrap(), None);
    }

    #[test]
    fn journal_day_count_counts_valid_chronicle_day_dirs_only() {
        let root = temp_path("day-count");
        let chronicle = root.join("chronicle");
        fs::create_dir_all(chronicle.join("20260722")).expect("create first day");
        fs::create_dir_all(chronicle.join("20260723")).expect("create second day");
        fs::create_dir_all(chronicle.join("not-a-day")).expect("create invalid day");
        fs::write(chronicle.join("20260724"), "").expect("write day-shaped file");
        assert_eq!(inspect_journal_days(&root).unwrap(), Some(2));
        fs::remove_dir_all(root).expect("cleanup day-count journal");
    }

    #[test]
    fn status_output_keeps_structured_native_proof_surface() {
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
    fn service_host_command_moves_to_journal() {
        assert_eq!(
            service_moved_output(OsStr::new("think")),
            CommandOutput::failure(
                "'think' moved to 'journal think' — run that instead.\n('sol' is the journal-access surface; 'journal' surfaces journal-service commands; see 'journal --help'.)\n",
                SERVICE_MOVED_EXIT,
            )
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
        assert_eq!(output.stderr, "Usage: sol <command> [args...]\n");
    }

    #[test]
    fn root_help_and_proof_routes_do_not_read_stdin() {
        let provider = PanicStdinProvider;
        for args in [
            vec![],
            os_args(&["-v"]),
            os_args(&["--help"]),
            os_args(&["-h"]),
            os_args(&["help"]),
            os_args(&["--version"]),
            os_args(&["-V"]),
            os_args(&["--path"]),
            os_args(&["path"]),
            os_args(&["root"]),
            os_args(&["status"]),
            os_args(&["does-not-exist"]),
            os_args(&["think"]),
            os_args(&["call"]),
            os_args(&["call", "--help"]),
            os_args(&["call", "activities", "--help"]),
            os_args(&["call", "activities", "list", "--help"]),
            os_args(&["chat", "--help"]),
            os_args(&["import", "--help"]),
        ] {
            let _ = run_with_stdin_provider(args, &provider);
        }
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

    #[test]
    fn helper_path_replaces_executable_filename() {
        assert_eq!(
            helper_path_for_executable(Path::new("/opt/bin/sol")),
            PathBuf::from("/opt/bin/solstone-python-compat")
        );
    }

    #[test]
    fn public_argv0_preserves_solstone_and_defaults_to_sol() {
        assert_eq!(
            public_argv0_for_executable(Path::new("/opt/bin/solstone")),
            "solstone"
        );
        assert_eq!(
            public_argv0_for_executable(Path::new("/opt/bin/sol")),
            "sol"
        );
        assert_eq!(
            public_argv0_for_executable(Path::new("/opt/bin/solstone-core-sol")),
            "sol"
        );
    }

    #[test]
    fn compat_argv_uses_leading_marker_and_preserves_public_args() {
        let args = os_args(&["call", "journal", "search", "needle"]);
        assert_eq!(
            compat_exec_args("solstone", &args),
            os_args(&[
                "__solstone_native_argv0=solstone",
                "call",
                "journal",
                "search",
                "needle"
            ])
        );
    }

    #[test]
    fn compat_exec_arms_the_sentinel() {
        assert_eq!(COMPAT_SENTINEL, "SOLSTONE_NATIVE_COMPAT_ACTIVE");
        assert_eq!(COMPAT_SENTINEL_ARMED, "armed");
    }

    #[test]
    fn missing_helper_message_is_actionable() {
        let path = PathBuf::from("/opt/bin/solstone-python-compat");
        let message = "sol: native compatibility helper is missing or not executable: /opt/bin/solstone-python-compat. Reinstall solstone and solstone-core.";
        assert_eq!(missing_helper_message(&path), message);
        assert_eq!(
            missing_helper_output(&path),
            CommandOutput::failure(format!("{message}\n"), i32::from(EXIT_CONFIG))
        );
    }

    #[test]
    fn recursion_preflight_exits_seventy() {
        let expected = CommandOutput::failure(
            "sol: compatibility dispatch recursion detected. Reinstall solstone and solstone-core.\n",
            i32::from(EXIT_SOFTWARE),
        );
        assert_eq!(compat_recursion_output(), expected);
        assert_eq!(
            compat_env_preflight(Some(OsStr::new("active"))),
            Err(expected)
        );
        assert_eq!(compat_env_preflight(None), Ok(()));
    }

    #[test]
    fn missing_helper_is_not_executable() {
        let path = env::temp_dir().join("solstone-core-sol-missing-helper");
        let _ = fs::remove_file(&path);
        assert!(!is_executable(&path));
    }

    #[cfg(unix)]
    #[test]
    fn non_executable_helper_is_incoherent() {
        use std::os::unix::fs::PermissionsExt;

        let path = env::temp_dir().join(format!(
            "solstone-core-sol-nonexec-helper-{}",
            SystemTime::now()
                .duration_since(UNIX_EPOCH)
                .expect("time")
                .as_nanos()
        ));
        fs::write(&path, "#!/bin/sh\n").expect("write helper");
        let mut permissions = fs::metadata(&path).expect("metadata").permissions();
        permissions.set_mode(0o644);
        fs::set_permissions(&path, permissions).expect("chmod helper");
        assert!(!is_executable(&path));
        let _ = fs::remove_file(path);
    }

    #[test]
    fn call_journal_delegates_only_after_native_miss() {
        let journal = os_args(&["call", "journal", "search"]);
        let journal_outcome = evaluate_args(&journal);
        assert!(matches!(journal_outcome, Outcome::Unsupported { .. }));
        assert!(should_delegate_to_compat_after_native_miss(
            &journal,
            &journal_outcome
        ));

        let http_leaf = os_args(&["call", "entities", "search"]);
        let http_outcome = evaluate_args(&http_leaf);
        assert!(matches!(http_outcome, Outcome::Migrated { .. }));
        assert!(!should_delegate_to_compat_after_native_miss(
            &http_leaf,
            &http_outcome
        ));

        let unknown = os_args(&["call", "not-real", "list"]);
        let unknown_outcome = evaluate_args(&unknown);
        assert!(matches!(unknown_outcome, Outcome::Unsupported { .. }));
        assert!(!should_delegate_to_compat_after_native_miss(
            &unknown,
            &unknown_outcome
        ));
    }
}
