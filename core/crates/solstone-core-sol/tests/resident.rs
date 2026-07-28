// SPDX-License-Identifier: AGPL-3.0-only
// Copyright (c) 2026 sol pbc

#![cfg(unix)]

use std::io::{BufRead, BufReader, ErrorKind};
use std::net::TcpListener;
use std::os::unix::process::ExitStatusExt;
use std::process::{Child, Command, ExitStatus, Stdio};
use std::sync::mpsc;
use std::thread::{sleep, spawn};
use std::time::{Duration, Instant};

use nix::sys::signal::{Signal, kill};
use nix::unistd::Pid;
use solstone_core_sol_client::aggregate::{self, Handler};
use solstone_core_sol_client::resident::ResidentHandler;

fn bin() -> &'static str {
    env!("CARGO_BIN_EXE_solstone-resident-fixture")
}

fn spawn_fixture() -> Child {
    for _ in 0..100 {
        match Command::new(bin())
            .stdin(Stdio::null())
            .stdout(Stdio::piped())
            .stderr(Stdio::piped())
            .spawn()
        {
            Ok(child) => return child,
            Err(error) if error.kind() == ErrorKind::ExecutableFileBusy => {
                sleep(Duration::from_millis(20));
            }
            Err(error) => panic!("resident fixture should spawn: {error:?}"),
        }
    }
    panic!("resident fixture stayed busy after retries: {}", bin());
}

fn read_startup_line(child: &mut Child) -> String {
    let stdout = child.stdout.take().expect("fixture stdout should be piped");
    let (tx, rx) = mpsc::channel();
    spawn(move || {
        let mut reader = BufReader::new(stdout);
        let mut line = String::new();
        let result = reader.read_line(&mut line).map(|bytes| (bytes, line));
        let _ = tx.send(result);
    });

    match rx.recv_timeout(Duration::from_secs(5)) {
        Ok(Ok((bytes, line))) => {
            assert!(bytes > 0, "fixture exited before writing startup line");
            assert!(line.ends_with('\n'), "startup line must end in newline");
            line
        }
        Ok(Err(error)) => panic!("startup line read failed: {error}"),
        Err(mpsc::RecvTimeoutError::Timeout) => {
            let _ = child.kill();
            let _ = child.wait();
            panic!("startup line did not arrive before timeout");
        }
        Err(mpsc::RecvTimeoutError::Disconnected) => {
            panic!("startup line reader disconnected")
        }
    }
}

fn parse_startup_port(line: &str) -> u16 {
    let trimmed = line
        .strip_suffix('\n')
        .expect("startup line should end in newline");
    let (label, port) = trimmed
        .split_once('\t')
        .expect("startup line should be label-tab-port");
    assert_eq!(label, "resident-fixture");
    port.parse().expect("startup port should parse")
}

fn send_signal(child: &Child, signal: Signal) {
    let pid = i32::try_from(child.id()).expect("child pid should fit i32");
    kill(Pid::from_raw(pid), signal).expect("signal should be delivered");
}

fn wait_for_exit(child: &mut Child) -> ExitStatus {
    let deadline = Instant::now() + Duration::from_secs(5);
    loop {
        match child.try_wait() {
            Ok(Some(status)) => return status,
            Ok(None) if Instant::now() < deadline => sleep(Duration::from_millis(20)),
            Ok(None) => {
                let _ = child.kill();
                let _ = child.wait();
                panic!("resident fixture did not exit before timeout");
            }
            Err(error) => panic!("resident fixture wait failed: {error}"),
        }
    }
}

fn run_fixture_until_signal(signal: Signal) -> (ExitStatus, u16) {
    let mut child = spawn_fixture();
    let startup = read_startup_line(&mut child);
    let port = parse_startup_port(&startup);
    send_signal(&child, signal);
    let status = wait_for_exit(&mut child);
    (status, port)
}

fn assert_graceful_shutdown_and_released_port(status: ExitStatus, port: u16) {
    // Exit status proves graceful handling instead of default signal death.
    assert_eq!(status.code(), Some(0));
    assert_eq!(status.signal(), None);

    // Re-bind proves no live listener owns the port. Re-bind alone does not prove
    // gracefulness because the kernel closes descriptors on any process exit.
    TcpListener::bind(("127.0.0.1", port)).expect("released fixture port should re-bind");
}

fn assert_buffered_handler_slice(_handlers: &'static [Handler]) {}
fn assert_resident_handler_slice(_handlers: &'static [ResidentHandler]) {}

#[test]
fn resident_startup_line_arrives_before_child_exit_unlike_buffered_output() {
    let mut child = spawn_fixture();

    // A buffered implementation can only reach render_output after the handler
    // returns; a resident handler is still running, so this read would time out.
    let startup = read_startup_line(&mut child);
    assert!(parse_startup_port(&startup) > 0);
    assert_eq!(child.try_wait().expect("try_wait should succeed"), None);

    send_signal(&child, Signal::SIGTERM);
    let status = wait_for_exit(&mut child);
    assert_eq!(status.code(), Some(0));
    assert_eq!(status.signal(), None);
}

#[test]
fn resident_sigint_exits_zero_and_releases_listener() {
    let (status, port) = run_fixture_until_signal(Signal::SIGINT);
    assert_graceful_shutdown_and_released_port(status, port);
}

#[test]
fn resident_sigterm_exits_zero_and_releases_listener() {
    let (status, port) = run_fixture_until_signal(Signal::SIGTERM);
    assert_graceful_shutdown_and_released_port(status, port);
}

#[test]
fn resident_fixture_is_absent_from_inventory_and_handlers_are_buffered() {
    assert!(aggregate::handler_for(&["resident-fixture"]).is_none());
    assert!(aggregate::handler_for(&["solstone-resident-fixture"]).is_none());
    assert!(aggregate::handler_for(&["resident", "fixture"]).is_none());

    for entry in aggregate::entries() {
        assert!(
            !entry.path.contains(&"resident-fixture"),
            "fixture must not be routeable through native inventory"
        );
    }

    let handlers = aggregate::handler_bindings();
    let resident_handlers = aggregate::resident_handler_bindings();
    assert_eq!(
        handlers.len() + resident_handlers.len(),
        aggregate::entries().len()
    );
    // The helper's &'static [Handler] parameter is the compile-time assertion
    // that generated inventory bindings stay on the buffered handler lane.
    assert_buffered_handler_slice(handlers);
    assert_resident_handler_slice(resident_handlers);
}
