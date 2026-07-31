// SPDX-License-Identifier: AGPL-3.0-only
// Copyright (c) 2026 sol pbc

use std::process::ExitCode;
use std::{
    env,
    ffi::OsStr,
    io::{Read, Write},
    path::PathBuf,
};

use solstone_core_cli::{
    Command, IndexerOptions, JournalPathOptions, SplCommand, USAGE, evaluate_args, version_line,
};
use solstone_core_indexer_store::db::reset_index;
use solstone_core_indexer_store::scan::{
    RescanFileStatus, rebuild_edges, rescan_file, scan_journal,
};
use solstone_core_journal::{
    ConfigError, HomeError, Source, discover_home, ensure_journal_dir_with_label,
    read_config_journal, resolve_journal_path,
};
use solstone_core_spl_hpke::{HpkeCliOperation, run_hpke_framed};

const EXIT_USAGE: u8 = 64;
const EXIT_UNAVAILABLE: u8 = 69;
const EXIT_TEMPFAIL: u8 = 75;
const EXIT_HPKE_FAILURE: u8 = 1;
const HPKE_MAX_FIELD_LENGTH: usize = 96 * 1024 * 1024;
const HPKE_MAX_FIELD_COUNT: usize = 5;
const HPKE_MAX_REQUEST_LENGTH: usize = HPKE_MAX_FIELD_COUNT * (HPKE_MAX_FIELD_LENGTH + 4);
const HPKE_READ_CHUNK_LENGTH: usize = 64 * 1024;
const SPL_SERVICE_UNAVAILABLE_LINE: &str = "spl: unavailable\n";
const ZERO_EDGE_HINT: &str = "Zero edges indexed: edges are talent-derived, and the --rescan-full edge phase remains modification-time incremental — run journal indexer --rebuild-edges to force full edge re-extraction.";
const SOL_IDENTITY_TOKEN: &str = "__solstone_identity=sol";
const SOLSTONE_IDENTITY_TOKEN: &str = "__solstone_identity=solstone";

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
    let mut args: Vec<_> = env::args_os().skip(1).collect();
    if let Some(identity) = sol_identity_from_first_arg(&args) {
        args.remove(0);
        return solstone_core_sol::run(identity, args);
    }
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
        Ok(Command::Indexer(options)) => run_indexer(options),
        Ok(Command::Spl(command)) => run_spl_process(command),
        Err(_) => {
            eprint!("{USAGE}");
            ExitCode::from(EXIT_USAGE)
        }
    }
}

fn run_spl_process(command: SplCommand) -> ExitCode {
    let stdin = std::io::stdin();
    let stdout = std::io::stdout();
    let stderr = std::io::stderr();
    let mut input = stdin.lock();
    let mut output = stdout.lock();
    let mut error = stderr.lock();

    run_spl_command(command, &mut input, &mut output, &mut error)
}

fn run_spl_command(
    command: SplCommand,
    input: &mut dyn Read,
    stdout: &mut dyn Write,
    stderr: &mut dyn Write,
) -> ExitCode {
    match command {
        SplCommand::Hpke(command) => run_hpke_process_io(command, input, stdout, stderr),
        // Service composition awaits the separately accepted U4/U5 process unit.
        SplCommand::Service(_) => write_spl_service_unavailable(stderr),
    }
}

fn write_spl_service_unavailable(stderr: &mut dyn Write) -> ExitCode {
    let _ = stderr.write_all(SPL_SERVICE_UNAVAILABLE_LINE.as_bytes());
    ExitCode::from(EXIT_UNAVAILABLE)
}

fn sol_identity_from_first_arg(args: &[std::ffi::OsString]) -> Option<&'static str> {
    match args.first().and_then(|arg| arg.to_str()) {
        Some(SOL_IDENTITY_TOKEN) => Some("sol"),
        Some(SOLSTONE_IDENTITY_TOKEN) => Some("solstone"),
        _ => None,
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

/// Runs one framed HPKE operation using only standard I/O seams.
///
/// This helper is intentionally separate from command dispatch so the HPKE
/// framing contract can be tested without process-global standard streams.
/// It never reads operation values from argv.
fn run_hpke_process_io(
    command: solstone_core_cli::HpkeCommand,
    input: &mut dyn Read,
    stdout: &mut dyn Write,
    stderr: &mut dyn Write,
) -> ExitCode {
    let request = match read_hpke_request(input) {
        Ok(request) => request,
        Err(()) => return write_hpke_error(stderr, "bad-input"),
    };

    let operation = match command {
        solstone_core_cli::HpkeCommand::OpenBase => HpkeCliOperation::OpenBase,
        solstone_core_cli::HpkeCommand::SealBase => HpkeCliOperation::SealBase,
    };

    match run_hpke_framed(operation, &request) {
        Ok(response) => match stdout.write_all(&response) {
            Ok(()) => ExitCode::SUCCESS,
            Err(_) => ExitCode::from(EXIT_TEMPFAIL),
        },
        Err(error) => write_hpke_error(stderr, error.class()),
    }
}

fn read_hpke_request(input: &mut dyn Read) -> Result<Vec<u8>, ()> {
    read_bounded_request(input, HPKE_MAX_REQUEST_LENGTH)
}

fn read_bounded_request(input: &mut dyn Read, maximum_length: usize) -> Result<Vec<u8>, ()> {
    let mut request = Vec::new();
    let mut buffer = [0_u8; HPKE_READ_CHUNK_LENGTH];
    let mut remaining = maximum_length;

    while remaining > 0 {
        let read_length = remaining.min(buffer.len());
        let destination = buffer.get_mut(..read_length).ok_or(())?;
        let read = input.read(destination).map_err(|_| ())?;
        if read == 0 {
            return Ok(request);
        }
        let next_remaining = remaining.checked_sub(read).ok_or(())?;
        request.try_reserve_exact(read).map_err(|_| ())?;
        let received = buffer.get(..read).ok_or(())?;
        request.extend_from_slice(received);
        remaining = next_remaining;
    }

    let mut overflow_byte = [0_u8; 1];
    match input.read(&mut overflow_byte) {
        Ok(0) => Ok(request),
        Ok(_) | Err(_) => Err(()),
    }
}

fn write_hpke_error(stderr: &mut dyn Write, class: &str) -> ExitCode {
    let line = format!("hpke: {class}\n");
    let _ = stderr.write_all(line.as_bytes());
    ExitCode::from(EXIT_HPKE_FAILURE)
}

fn run_indexer(options: IndexerOptions) -> ExitCode {
    if !options.reset
        && !options.rebuild_edges
        && !options.rescan
        && !options.rescan_full
        && options.rescan_file.is_none()
    {
        print!("{USAGE}");
        return ExitCode::SUCCESS;
    }

    let line = if let Some(path) = options.journal_override {
        JournalPathLine {
            label: "cli",
            path: PathBuf::from(path),
        }
    } else {
        match resolve_process_journal_path() {
            Ok(line) => line,
            Err(error) => {
                eprint_journal_path_error(error);
                return ExitCode::from(EXIT_TEMPFAIL);
            }
        }
    };

    if options.reset
        && let Err(error) = reset_index(&line.path)
    {
        eprintln!("indexer reset failed: {error}");
        return ExitCode::from(EXIT_TEMPFAIL);
    }

    if options.rebuild_edges {
        match rebuild_edges(&line.path) {
            Ok(report) => {
                for warning in report.warnings {
                    eprintln!("warning: {warning}");
                }
                if report.failed > 0 {
                    return ExitCode::from(EXIT_TEMPFAIL);
                }
            }
            Err(error) => {
                eprintln!("indexer edge rebuild failed: {error}");
                return ExitCode::from(EXIT_TEMPFAIL);
            }
        }
    }

    if let Some(path) = options.rescan_file {
        match rescan_file(&line.path, &PathBuf::from(path)) {
            Ok(RescanFileStatus::Indexed { warnings }) => {
                for warning in warnings {
                    eprintln!("warning: {warning}");
                }
                return ExitCode::SUCCESS;
            }
            Ok(RescanFileStatus::Declined) => {
                eprintln!("indexer declined unsupported file");
                return ExitCode::from(EXIT_UNAVAILABLE);
            }
            Err(error) => {
                eprintln!("indexer rescan-file failed: {error}");
                return ExitCode::from(EXIT_TEMPFAIL);
            }
        }
    }

    if options.rescan || options.rescan_full {
        let today = chrono::Local::now().format("%Y%m%d").to_string();
        match scan_journal(&line.path, options.rescan_full, &today) {
            Ok(report) => {
                for warning in report.warnings {
                    eprintln!("warning: {warning}");
                }
                let should_emit_zero_edge_hint = options.rescan_full
                    && !options.rebuild_edges
                    && !options.reset
                    && report.edge_rows_inserted == 0;
                if should_emit_zero_edge_hint {
                    println!("{ZERO_EDGE_HINT}");
                }
                return ExitCode::SUCCESS;
            }
            Err(error) => {
                eprintln!("indexer scan failed: {error}");
                return ExitCode::from(EXIT_TEMPFAIL);
            }
        }
    }

    ExitCode::SUCCESS
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

#[cfg(test)]
mod tests {
    use std::{
        error::Error,
        io::{Cursor, Error as IoError, ErrorKind},
    };

    use solstone_core_cli::{HpkeCommand, ServiceOptions, SplCommand};

    use super::{read_bounded_request, run_hpke_process_io, run_spl_command};

    const RECIPIENT_PRIVATE_KEY_DER: &[u8] = &[
        0x30, 0x81, 0x87, 0x02, 0x01, 0x00, 0x30, 0x13, 0x06, 0x07, 0x2a, 0x86, 0x48, 0xce, 0x3d,
        0x02, 0x01, 0x06, 0x08, 0x2a, 0x86, 0x48, 0xce, 0x3d, 0x03, 0x01, 0x07, 0x04, 0x6d, 0x30,
        0x6b, 0x02, 0x01, 0x01, 0x04, 0x20, 0xb2, 0xb6, 0xd8, 0xc8, 0x23, 0x78, 0xe0, 0xfc, 0xb3,
        0xda, 0x20, 0x8f, 0xf4, 0x2d, 0xdd, 0xdf, 0x0a, 0xf9, 0x2b, 0xc5, 0xbc, 0x4d, 0x2e, 0x70,
        0xc3, 0xc5, 0x65, 0xc8, 0xe2, 0xc1, 0x9c, 0x8b, 0xa1, 0x44, 0x03, 0x42, 0x00, 0x04, 0x21,
        0xe5, 0x93, 0x78, 0xef, 0x96, 0x6e, 0xd3, 0x79, 0x18, 0x94, 0x62, 0x23, 0x1f, 0xd3, 0x2a,
        0x5b, 0x85, 0xe1, 0x7a, 0x7a, 0xb2, 0x57, 0xf7, 0x92, 0x40, 0xfb, 0x95, 0x6a, 0x59, 0x2e,
        0xd1, 0x61, 0x58, 0x63, 0x34, 0x16, 0x34, 0xe7, 0x41, 0xda, 0x8d, 0x8a, 0xa8, 0x4f, 0x33,
        0x2e, 0x77, 0xf5, 0x59, 0x08, 0x01, 0x03, 0x18, 0xaf, 0xa7, 0x81, 0xcc, 0x62, 0x16, 0x90,
        0x52, 0x10, 0x80,
    ];
    const RECIPIENT_PUBLIC_KEY_DER: &[u8] = &[
        0x30, 0x59, 0x30, 0x13, 0x06, 0x07, 0x2a, 0x86, 0x48, 0xce, 0x3d, 0x02, 0x01, 0x06, 0x08,
        0x2a, 0x86, 0x48, 0xce, 0x3d, 0x03, 0x01, 0x07, 0x03, 0x42, 0x00, 0x04, 0x21, 0xe5, 0x93,
        0x78, 0xef, 0x96, 0x6e, 0xd3, 0x79, 0x18, 0x94, 0x62, 0x23, 0x1f, 0xd3, 0x2a, 0x5b, 0x85,
        0xe1, 0x7a, 0x7a, 0xb2, 0x57, 0xf7, 0x92, 0x40, 0xfb, 0x95, 0x6a, 0x59, 0x2e, 0xd1, 0x61,
        0x58, 0x63, 0x34, 0x16, 0x34, 0xe7, 0x41, 0xda, 0x8d, 0x8a, 0xa8, 0x4f, 0x33, 0x2e, 0x77,
        0xf5, 0x59, 0x08, 0x01, 0x03, 0x18, 0xaf, 0xa7, 0x81, 0xcc, 0x62, 0x16, 0x90, 0x52, 0x10,
        0x80,
    ];

    #[test]
    fn seal_and_open_write_complete_framed_responses() -> Result<(), Box<dyn Error>> {
        let info = b"0123456789abcdef";
        let plaintext = b"process I/O framing keeps its response whole";
        let aad = b"process-io-aad";
        let seal_request = frame_fields(&[RECIPIENT_PUBLIC_KEY_DER, info, plaintext, aad])?;
        let mut seal_input = Cursor::new(seal_request);
        let mut seal_stdout = Vec::new();
        let mut seal_stderr = Vec::new();

        let seal_exit = run_hpke_process_io(
            HpkeCommand::SealBase,
            &mut seal_input,
            &mut seal_stdout,
            &mut seal_stderr,
        );

        assert_eq!(seal_exit, std::process::ExitCode::SUCCESS);
        assert!(seal_stderr.is_empty());
        let sealed_fields = parse_fields(&seal_stdout)?;
        let [enc, ciphertext] = sealed_fields.as_slice() else {
            return Err(
                IoError::new(ErrorKind::InvalidData, "expected sealed response fields").into(),
            );
        };
        assert_eq!(enc.len(), 65);

        let open_request = frame_fields(&[RECIPIENT_PRIVATE_KEY_DER, info, enc, ciphertext, aad])?;
        let mut open_input = Cursor::new(open_request);
        let mut open_stdout = Vec::new();
        let mut open_stderr = Vec::new();

        let open_exit = run_hpke_process_io(
            HpkeCommand::OpenBase,
            &mut open_input,
            &mut open_stdout,
            &mut open_stderr,
        );

        assert_eq!(open_exit, std::process::ExitCode::SUCCESS);
        assert!(open_stderr.is_empty());
        let opened_fields = parse_fields(&open_stdout)?;
        assert_eq!(opened_fields, vec![plaintext.to_vec()]);
        Ok(())
    }

    #[test]
    fn spl_hpke_command_dispatches_to_the_framed_runner() -> Result<(), Box<dyn Error>> {
        let request = frame_fields(&[
            RECIPIENT_PUBLIC_KEY_DER,
            b"0123456789abcdef",
            b"dispatch payload",
            b"dispatch-aad",
        ])?;
        let mut input = Cursor::new(request);
        let mut stdout = Vec::new();
        let mut stderr = Vec::new();

        let exit = run_spl_command(
            SplCommand::Hpke(HpkeCommand::SealBase),
            &mut input,
            &mut stdout,
            &mut stderr,
        );

        assert_eq!(exit, std::process::ExitCode::SUCCESS);
        assert!(stderr.is_empty());
        assert_eq!(parse_fields(&stdout)?.len(), 2);
        Ok(())
    }

    #[test]
    fn spl_service_is_fixed_class_unavailable_until_service_composition() {
        let mut input = Cursor::new(b"not-used".to_vec());
        let mut stdout = Vec::new();
        let mut stderr = Vec::new();

        let exit = run_spl_command(
            SplCommand::Service(ServiceOptions {
                verbose: false,
                debug: false,
            }),
            &mut input,
            &mut stdout,
            &mut stderr,
        );

        assert_eq!(exit, std::process::ExitCode::from(69));
        assert!(stdout.is_empty());
        assert_eq!(stderr, b"spl: unavailable\n");
    }

    #[test]
    fn malformed_input_has_empty_stdout_and_a_single_error_class() -> Result<(), Box<dyn Error>> {
        let sensitive_marker = b"sensitive-marker-must-not-echo";
        let stated_length = u32::try_from(sensitive_marker.len())?
            .checked_add(1)
            .ok_or_else(|| IoError::new(ErrorKind::InvalidData, "test field too long"))?;
        let mut malformed_request = stated_length.to_be_bytes().to_vec();
        malformed_request.extend_from_slice(sensitive_marker);
        let mut input = Cursor::new(malformed_request);
        let mut stdout = Vec::new();
        let mut stderr = Vec::new();

        let exit = run_hpke_process_io(HpkeCommand::SealBase, &mut input, &mut stdout, &mut stderr);

        assert_eq!(exit, std::process::ExitCode::from(1));
        assert!(stdout.is_empty());
        assert_eq!(stderr, b"hpke: bad-input\n");
        assert!(
            !stderr
                .windows(sensitive_marker.len())
                .any(|window| window == sensitive_marker)
        );
        Ok(())
    }

    #[test]
    fn wrong_field_count_has_empty_stdout_and_a_single_error_class() -> Result<(), Box<dyn Error>> {
        let sensitive_marker = b"wrong-count-marker-must-not-echo";
        let request = frame_fields(&[sensitive_marker])?;
        let mut input = Cursor::new(request);
        let mut stdout = Vec::new();
        let mut stderr = Vec::new();

        let exit = run_hpke_process_io(HpkeCommand::OpenBase, &mut input, &mut stdout, &mut stderr);

        assert_eq!(exit, std::process::ExitCode::from(1));
        assert!(stdout.is_empty());
        assert_eq!(stderr, b"hpke: bad-field-count\n");
        assert!(
            !stderr
                .windows(sensitive_marker.len())
                .any(|window| window == sensitive_marker)
        );
        Ok(())
    }

    #[test]
    fn capped_reader_stops_at_the_first_byte_beyond_the_limit() {
        let mut reader = EndlessReader { bytes_supplied: 0 };

        let result = read_bounded_request(&mut reader, 7);

        assert!(result.is_err());
        assert_eq!(reader.bytes_supplied, 8);
    }

    #[test]
    fn reader_failure_has_empty_stdout_and_a_single_error_class() {
        let mut input = FailingReader;
        let mut stdout = Vec::new();
        let mut stderr = Vec::new();

        let exit = run_hpke_process_io(HpkeCommand::SealBase, &mut input, &mut stdout, &mut stderr);

        assert_eq!(exit, std::process::ExitCode::from(1));
        assert!(stdout.is_empty());
        assert_eq!(stderr, b"hpke: bad-input\n");
    }

    fn frame_fields(fields: &[&[u8]]) -> Result<Vec<u8>, Box<dyn Error>> {
        let mut framed = Vec::new();
        for field in fields {
            let field_length = u32::try_from(field.len())?;
            framed.extend_from_slice(&field_length.to_be_bytes());
            framed.extend_from_slice(field);
        }
        Ok(framed)
    }

    fn parse_fields(input: &[u8]) -> Result<Vec<Vec<u8>>, Box<dyn Error>> {
        let mut fields = Vec::new();
        let mut offset = 0_usize;

        while offset < input.len() {
            let header_end = offset
                .checked_add(std::mem::size_of::<u32>())
                .ok_or_else(|| IoError::new(ErrorKind::InvalidData, "field header overflow"))?;
            let header = input
                .get(offset..header_end)
                .ok_or_else(|| IoError::new(ErrorKind::InvalidData, "missing field header"))?;
            let header: [u8; 4] = header.try_into()?;
            let field_length = usize::try_from(u32::from_be_bytes(header))?;
            let field_end = header_end
                .checked_add(field_length)
                .ok_or_else(|| IoError::new(ErrorKind::InvalidData, "field overflow"))?;
            let field = input
                .get(header_end..field_end)
                .ok_or_else(|| IoError::new(ErrorKind::InvalidData, "missing field body"))?;
            fields.push(field.to_vec());
            offset = field_end;
        }

        Ok(fields)
    }

    struct EndlessReader {
        bytes_supplied: usize,
    }

    impl std::io::Read for EndlessReader {
        fn read(&mut self, buffer: &mut [u8]) -> std::io::Result<usize> {
            buffer.fill(0xa5);
            self.bytes_supplied = self.bytes_supplied.saturating_add(buffer.len());
            Ok(buffer.len())
        }
    }

    struct FailingReader;

    impl std::io::Read for FailingReader {
        fn read(&mut self, _buffer: &mut [u8]) -> std::io::Result<usize> {
            Err(IoError::other("input unavailable"))
        }
    }
}
