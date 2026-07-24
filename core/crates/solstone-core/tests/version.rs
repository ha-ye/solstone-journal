// SPDX-License-Identifier: AGPL-3.0-only
// Copyright (c) 2026 sol pbc

use std::process::Command;
use std::time::{SystemTime, UNIX_EPOCH};
use std::{env, fs, path::PathBuf};

fn bin() -> &'static str {
    env!("CARGO_BIN_EXE_solstone-core")
}

fn sol_bin() -> &'static str {
    env!("CARGO_BIN_EXE_sol")
}

fn solstone_bin() -> &'static str {
    env!("CARGO_BIN_EXE_solstone")
}

fn temp_path(name: &str) -> PathBuf {
    let stamp = SystemTime::now()
        .duration_since(UNIX_EPOCH)
        .expect("time should be available")
        .as_nanos();
    env::temp_dir().join(format!("solstone-core-{name}-{stamp}"))
}

#[test]
fn version_writes_stdout_and_exits_zero() {
    let output = Command::new(bin())
        .arg("--version")
        .output()
        .expect("solstone-core should execute");

    assert_eq!(output.status.code(), Some(0));
    assert_eq!(
        String::from_utf8(output.stdout).expect("stdout should be utf-8"),
        format!("solstone-core {}\n", env!("CARGO_PKG_VERSION"))
    );
    assert_eq!(
        String::from_utf8(output.stderr).expect("stderr should be utf-8"),
        ""
    );
}

#[test]
fn usage_error_writes_stderr_and_exits_64() {
    let output = Command::new(bin())
        .arg("--unknown")
        .output()
        .expect("solstone-core should execute");

    assert_eq!(output.status.code(), Some(64));
    assert_eq!(
        String::from_utf8(output.stdout).expect("stdout should be utf-8"),
        ""
    );
    assert_eq!(
        String::from_utf8(output.stderr).expect("stderr should be utf-8"),
        solstone_core_cli::USAGE
    );
}

#[test]
fn journal_path_override_prints_cli_label_without_creating() {
    let target = temp_path("override-no-create");
    let output = Command::new(bin())
        .arg("journal-path")
        .arg("--journal")
        .arg(&target)
        .output()
        .expect("solstone-core should execute");

    assert_eq!(output.status.code(), Some(0));
    assert_eq!(
        String::from_utf8(output.stdout).expect("stdout should be utf-8"),
        format!("cli\t{}\n", target.display())
    );
    assert_eq!(
        String::from_utf8(output.stderr).expect("stderr should be utf-8"),
        ""
    );
    assert!(!target.exists());
}

#[test]
fn journal_path_override_create_creates_directory() {
    let target = temp_path("override-create");
    let output = Command::new(bin())
        .arg("journal-path")
        .arg("--journal")
        .arg(&target)
        .arg("--create")
        .output()
        .expect("solstone-core should execute");

    assert_eq!(output.status.code(), Some(0));
    assert_eq!(
        String::from_utf8(output.stdout).expect("stdout should be utf-8"),
        format!("cli\t{}\n", target.display())
    );
    assert!(target.is_dir());
    fs::remove_dir_all(target).expect("cleanup created journal");
}

#[test]
fn journal_path_empty_override_prints_but_create_errors() {
    let output = Command::new(bin())
        .arg("journal-path")
        .arg("--journal")
        .arg("")
        .output()
        .expect("solstone-core should execute");
    assert_eq!(output.status.code(), Some(0));
    assert_eq!(
        String::from_utf8(output.stdout).expect("stdout should be utf-8"),
        "cli\t\n"
    );

    let create_output = Command::new(bin())
        .arg("journal-path")
        .arg("--journal")
        .arg("")
        .arg("--create")
        .output()
        .expect("solstone-core should execute");
    assert_eq!(create_output.status.code(), Some(75));
    assert_eq!(
        String::from_utf8(create_output.stdout).expect("stdout should be utf-8"),
        ""
    );
    assert!(
        String::from_utf8(create_output.stderr)
            .expect("stderr should be utf-8")
            .starts_with("could not create journal directory (cli): ")
    );
}

#[test]
fn journal_path_env_spaces_are_unstripped() {
    let output = Command::new(bin())
        .arg("journal-path")
        .env("SOLSTONE_JOURNAL", "   ")
        .output()
        .expect("solstone-core should execute");

    assert_eq!(output.status.code(), Some(0));
    assert_eq!(
        String::from_utf8(output.stdout).expect("stdout should be utf-8"),
        "env\t   \n"
    );
}

#[test]
fn journal_path_config_tilde_is_literal() {
    let home = temp_path("config-tilde-home");
    let config_dir = home.join(".config").join("solstone");
    fs::create_dir_all(&config_dir).expect("create config dir");
    fs::write(config_dir.join("config.toml"), "journal = \"~/journal\"\n").expect("write config");

    let output = Command::new(bin())
        .arg("journal-path")
        .env_remove("SOLSTONE_JOURNAL")
        .env("HOME", &home)
        .output()
        .expect("solstone-core should execute");

    assert_eq!(output.status.code(), Some(0));
    assert_eq!(
        String::from_utf8(output.stdout).expect("stdout should be utf-8"),
        "config\t~/journal\n"
    );
    fs::remove_dir_all(home).expect("cleanup config home");
}

#[test]
fn sol_and_solstone_bins_report_the_same_native_version() {
    let sol = Command::new(sol_bin())
        .arg("--version")
        .output()
        .expect("sol should execute");
    let solstone = Command::new(solstone_bin())
        .arg("--version")
        .output()
        .expect("solstone should execute");

    assert_eq!(sol.status.code(), Some(0));
    assert_eq!(solstone.status.code(), Some(0));
    assert_eq!(sol.stdout, solstone.stdout);
    assert_eq!(sol.stderr, solstone.stderr);
    assert_eq!(
        String::from_utf8(sol.stdout).expect("stdout should be utf-8"),
        format!("sol (solstone) {}\n", env!("CARGO_PKG_VERSION"))
    );
    assert_eq!(
        String::from_utf8(solstone.stderr).expect("stderr should be utf-8"),
        ""
    );
}

#[test]
fn sol_root_installed_layout_is_independent_of_cwd() {
    let env_root = temp_path("sol-root-installed-layout");
    let bin_dir = env_root.join("bin");
    let site_packages = env_root
        .join("lib")
        .join("python3.13")
        .join("site-packages");
    fs::create_dir_all(&bin_dir).expect("create fake bin dir");
    fs::create_dir_all(site_packages.join("solstone")).expect("create fake package dir");
    fs::write(site_packages.join("solstone").join("__init__.py"), "").expect("write init");
    let fake_sol = bin_dir.join("sol");
    fs::copy(sol_bin(), &fake_sol).expect("copy sol binary into fake install layout");
    #[cfg(unix)]
    {
        use std::os::unix::fs::PermissionsExt;
        let mut permissions = fs::metadata(&fake_sol)
            .expect("fake sol metadata")
            .permissions();
        permissions.set_mode(0o755);
        fs::set_permissions(&fake_sol, permissions).expect("make fake sol executable");
    }

    let unrelated = env_root.join("unrelated");
    fs::create_dir_all(&unrelated).expect("create unrelated cwd");
    let manifest = PathBuf::from(env!("CARGO_MANIFEST_DIR"));
    let source_checkout = manifest
        .parent()
        .and_then(|path| path.parent())
        .and_then(|path| path.parent())
        .expect("workspace checkout root");

    for cwd in [&unrelated, source_checkout] {
        let output = Command::new(&fake_sol)
            .arg("root")
            .current_dir(cwd)
            .output()
            .expect("fake sol should execute");
        assert_eq!(output.status.code(), Some(0));
        assert_eq!(
            String::from_utf8(output.stdout).expect("stdout should be utf-8"),
            format!("{}\n", site_packages.display())
        );
        assert_eq!(
            String::from_utf8(output.stderr).expect("stderr should be utf-8"),
            ""
        );
    }
    fs::remove_dir_all(env_root).expect("cleanup fake install layout");
}

#[cfg(unix)]
#[test]
fn sol_root_installed_layout_canonicalizes_lib64_alias_independent_of_cwd() {
    use std::os::unix::fs::{PermissionsExt, symlink};

    let env_root = temp_path("sol-root-installed-lib64-layout");
    let bin_dir = env_root.join("bin");
    let site_packages = env_root
        .join("lib")
        .join("python3.13")
        .join("site-packages");
    fs::create_dir_all(&bin_dir).expect("create fake bin dir");
    fs::create_dir_all(site_packages.join("solstone")).expect("create fake package dir");
    fs::write(site_packages.join("solstone").join("__init__.py"), "").expect("write init");
    symlink("lib", env_root.join("lib64")).expect("create lib64 symlink");
    let canonical_site_packages =
        fs::canonicalize(&site_packages).expect("canonical fake site-packages");
    let fake_sol = bin_dir.join("sol");
    fs::copy(sol_bin(), &fake_sol).expect("copy sol binary into fake install layout");
    let mut permissions = fs::metadata(&fake_sol)
        .expect("fake sol metadata")
        .permissions();
    permissions.set_mode(0o755);
    fs::set_permissions(&fake_sol, permissions).expect("make fake sol executable");

    let unrelated = env_root.join("unrelated");
    fs::create_dir_all(&unrelated).expect("create unrelated cwd");
    let manifest = PathBuf::from(env!("CARGO_MANIFEST_DIR"));
    let source_checkout = manifest
        .parent()
        .and_then(|path| path.parent())
        .and_then(|path| path.parent())
        .expect("workspace checkout root");

    for cwd in [&unrelated, source_checkout] {
        let output = Command::new(&fake_sol)
            .arg("root")
            .current_dir(cwd)
            .output()
            .expect("fake sol should execute");
        assert_eq!(output.status.code(), Some(0));
        assert_eq!(
            String::from_utf8(output.stdout).expect("stdout should be utf-8"),
            format!("{}\n", canonical_site_packages.display())
        );
        assert_eq!(
            String::from_utf8(output.stderr).expect("stderr should be utf-8"),
            ""
        );
    }
    fs::remove_dir_all(env_root).expect("cleanup fake install layout");
}

#[cfg(unix)]
#[test]
fn sol_and_solstone_bins_forward_compat_with_public_argv0_identity() {
    use std::io::Write;
    use std::os::unix::fs::PermissionsExt;
    use std::process::Stdio;

    let helper = PathBuf::from(sol_bin()).with_file_name("solstone-python-compat");
    let previous = fs::read(&helper).ok();
    let previous_mode = fs::metadata(&helper)
        .ok()
        .map(|metadata| metadata.permissions().mode());
    fs::write(
        &helper,
        "#!/bin/sh\n".to_string()
            + "printf 'sentinel=%s\\n' \"$SOLSTONE_NATIVE_COMPAT_ACTIVE\"\n"
            + "printf 'marker=%s\\n' \"$1\"\n"
            + "shift\n"
            + "printf 'args='\n"
            + "for arg in \"$@\"; do printf '<%s>' \"$arg\"; done\n"
            + "printf '\\nstdin='\n"
            + "cat\n"
            + "printf 'compat stderr\\n' >&2\n"
            + "exit 23\n",
    )
    .expect("write fake compatibility helper");
    fs::set_permissions(&helper, fs::Permissions::from_mode(0o755))
        .expect("make fake compatibility helper executable");

    for (bin_path, expected_marker) in [
        (sol_bin(), "__solstone_native_argv0=sol"),
        (solstone_bin(), "__solstone_native_argv0=solstone"),
    ] {
        let mut child = Command::new(bin_path)
            .args(["notify", "message"])
            .stdin(Stdio::piped())
            .stdout(Stdio::piped())
            .stderr(Stdio::piped())
            .spawn()
            .expect("native bin should spawn");
        child
            .stdin
            .as_mut()
            .expect("stdin should be piped")
            .write_all(b"payload")
            .expect("write stdin");
        let output = child.wait_with_output().expect("wait for native bin");

        assert_eq!(output.status.code(), Some(23));
        assert_eq!(
            String::from_utf8(output.stdout).expect("stdout should be utf-8"),
            format!(
                "sentinel=armed\nmarker={expected_marker}\nargs=<notify><message>\nstdin=payload"
            )
        );
        assert_eq!(
            String::from_utf8(output.stderr).expect("stderr should be utf-8"),
            "compat stderr\n"
        );
    }

    if let Some(content) = previous {
        fs::write(&helper, content).expect("restore previous helper");
        if let Some(mode) = previous_mode {
            fs::set_permissions(&helper, fs::Permissions::from_mode(mode))
                .expect("restore previous helper mode");
        }
    } else {
        fs::remove_file(&helper).expect("remove fake compatibility helper");
    }
}
