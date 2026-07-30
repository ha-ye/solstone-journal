# SPDX-License-Identifier: AGPL-3.0-only
# Copyright (c) 2026 sol pbc

from __future__ import annotations

import hashlib
import json
import os
import subprocess
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
pytestmark = pytest.mark.release
RETAINED_ROOTS = (
    Path("dist") / "release-candidate",
    Path("target") / "release-evidence",
)
EXTERNAL_SEAM_COMMANDS = (
    "ssh",
    "rsync",
    "twine",
    "uvx",
    "gh",
    "git",
    "curl",
    "uv",
    "cargo",
    "codesign",
    "xcrun",
)
PASSTHROUGH_ENV_KEYS = (
    "EXPECTED_RELEASE_COMMIT",
    "RELEASE_MODEL_PACKAGES",
    "RELEASE_ADVISORY_SOURCE_NAME",
    "RELEASE_ADVISORY_DB_URL",
    "RELEASE_ADVISORY_DB_ROOT",
)


def _write_external_sentinels(sentinel_dir: Path, log: Path) -> None:
    sentinel_dir.mkdir()
    for name in EXTERNAL_SEAM_COMMANDS:
        path = sentinel_dir / name
        path.write_text(
            f'#!/bin/sh\necho {name} "$@" >> {log}\nexit 99\n',
            encoding="utf-8",
        )
        path.chmod(0o755)


def _reachability_env(tmp_path: Path) -> tuple[dict[str, str], Path]:
    sentinel_dir = tmp_path / "sentinels"
    log = tmp_path / "sentinel.log"
    _write_external_sentinels(sentinel_dir, log)
    interpreter_dir = Path(sys.executable).parent
    return (
        {
            "PATH": os.pathsep.join(
                (str(sentinel_dir), str(interpreter_dir), "/usr/bin", "/bin")
            ),
            "HOME": str(tmp_path / "home"),
            "PYTEST_CURRENT_TEST": os.environ["PYTEST_CURRENT_TEST"],
            "SOLSTONE_RELEASE_TEST_RAIL": "1",
        },
        log,
    )


def _retained_snapshot(root: Path) -> tuple[tuple[str, str, str], ...]:
    entries: list[tuple[str, str, str]] = []
    for relative_root in RETAINED_ROOTS:
        base = root / relative_root
        root_name = relative_root.as_posix()
        if not base.exists():
            entries.append(("missing-root", root_name, ""))
            continue
        if not base.is_dir() or base.is_symlink():
            entries.append(("other-root", root_name, ""))
            continue
        entries.append(("dir", root_name, ""))
        for path in sorted(base.rglob("*"), key=lambda item: item.relative_to(root)):
            relative = path.relative_to(root).as_posix()
            if path.is_file() and not path.is_symlink():
                entries.append(
                    ("file", relative, hashlib.sha256(path.read_bytes()).hexdigest())
                )
            elif path.is_dir() and not path.is_symlink():
                entries.append(("dir", relative, ""))
            else:
                entries.append(("other", relative, ""))
    return tuple(entries)


def _run_release(
    argv: list[str], env: dict[str, str]
) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        argv,
        cwd=ROOT,
        env=env,
        capture_output=True,
        text=True,
        check=False,
    )


def _assert_no_import_failure(stderr: str) -> None:
    assert "ModuleNotFoundError" not in stderr
    assert "Traceback" not in stderr


def _assert_no_external_seam(log: Path) -> None:
    assert not log.exists() or log.read_text(encoding="utf-8") == ""


def test_candidate_runs_release_checks_before_driver(tmp_path: Path) -> None:
    shim_dir = tmp_path / "shims"
    shim_dir.mkdir()
    make_record = tmp_path / "make-record"
    python_record = tmp_path / "python-record"
    make = shim_dir / "make"
    make.write_text(
        f'#!/bin/sh\nprintf "%s\\n" "$*" > {make_record}\nexit 73\n',
        encoding="utf-8",
    )
    make.chmod(0o755)
    python = shim_dir / "python3"
    python.write_text(
        f"#!/bin/sh\ntouch {python_record}\nexit 99\n",
        encoding="utf-8",
    )
    python.chmod(0o755)

    result = _run_release(
        ["bash", "scripts/release.sh", "--candidate"],
        {
            "PATH": os.pathsep.join((str(shim_dir), "/usr/bin", "/bin")),
            "HOME": str(tmp_path / "home"),
        },
    )

    assert result.returncode == 73
    assert make_record.read_text(encoding="utf-8") == "release-checks\n"
    assert not python_record.exists()


def test_candidate_entrypoint_reaches_driver_build_host_validation(
    tmp_path: Path,
) -> None:
    env, log = _reachability_env(tmp_path)
    before = _retained_snapshot(ROOT)

    result = _run_release(["bash", "scripts/release.sh", "--candidate"], env)

    assert result.returncode == 1
    assert "build-host channel is not configured" in result.stderr
    assert "RELEASE_BUILD_HOST_CHANNEL" in result.stderr
    _assert_no_import_failure(result.stderr)
    _assert_no_external_seam(log)
    assert _retained_snapshot(ROOT) == before


def test_recover_entrypoint_reaches_driver_retained_ledger_validation(
    tmp_path: Path,
) -> None:
    env, log = _reachability_env(tmp_path)
    before = _retained_snapshot(ROOT)

    result = _run_release(
        [
            "bash",
            "scripts/release.sh",
            "--recover",
            "9.9.9",
            "aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa",
        ],
        env,
    )

    assert result.returncode == 1
    assert "retained ledger could not be read for selector" in result.stderr
    _assert_no_import_failure(result.stderr)
    _assert_no_external_seam(log)
    assert _retained_snapshot(ROOT) == before


def test_dry_run_linux_entrypoint_reaches_driver_success(
    tmp_path: Path,
) -> None:
    env, log = _reachability_env(tmp_path)
    env["RELEASE_MODEL_PACKAGES"] = "exclude"
    before = _retained_snapshot(ROOT)

    result = _run_release(["bash", "scripts/release.sh", "--dry-run-linux"], env)

    assert result.returncode == 0
    assert "linux structural dry-run validated" in result.stdout
    _assert_no_import_failure(result.stderr)
    _assert_no_external_seam(log)
    assert _retained_snapshot(ROOT) == before


def _write_python_recorder(shim_dir: Path, record_path: Path) -> None:
    shim_dir.mkdir()
    shim = shim_dir / "python3"
    shim.write_text(
        "\n".join(
            (
                f"#!{sys.executable}",
                "import json",
                "import os",
                "import sys",
                f"record_path = {str(record_path)!r}",
                f"keys = {PASSTHROUGH_ENV_KEYS!r}",
                "record = {",
                '    "sys_argv": sys.argv,',
                '    "driver_argv": sys.argv[1:],',
                '    "env": {key: os.environ[key] for key in keys if key in os.environ},',
                "}",
                "with open(record_path, 'w', encoding='utf-8') as handle:",
                "    json.dump(record, handle, sort_keys=True)",
                "    handle.write('\\n')",
                "raise SystemExit(0)",
                "",
            )
        ),
        encoding="utf-8",
    )
    shim.chmod(0o755)


def _dispatch_env(tmp_path: Path, record_path: Path) -> tuple[dict[str, str], Path]:
    shim_dir = tmp_path / "python-shim"
    sentinel_dir = tmp_path / "sentinels"
    log = tmp_path / "sentinel.log"
    _write_python_recorder(shim_dir, record_path)
    _write_external_sentinels(sentinel_dir, log)
    env = {
        "PATH": os.pathsep.join((str(shim_dir), str(sentinel_dir), "/usr/bin", "/bin")),
        "HOME": str(tmp_path / "home"),
        "EXPECTED_RELEASE_COMMIT": "bbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbb",
        "RELEASE_MODEL_PACKAGES": "include",
        "RELEASE_ADVISORY_SOURCE_NAME": "dispatch-source",
        "RELEASE_ADVISORY_DB_URL": "file:///dispatch-db",
        "RELEASE_ADVISORY_DB_ROOT": str(tmp_path / "advisory-db"),
        "PYTEST_CURRENT_TEST": os.environ["PYTEST_CURRENT_TEST"],
        "SOLSTONE_RELEASE_TEST_RAIL": "1",
    }
    return env, log


def test_release_shell_dispatches_candidate_selector_and_env(
    tmp_path: Path,
) -> None:
    record_path = tmp_path / "python-record.json"
    env, log = _dispatch_env(tmp_path, record_path)

    result = _run_release(["bash", "scripts/release.sh", "--candidate"], env)

    assert result.returncode == 0
    record = json.loads(record_path.read_text(encoding="utf-8"))
    recorded_argv = record["driver_argv"]
    assert recorded_argv[0] == "scripts/release_candidate_driver.py"
    assert recorded_argv[1:] == ["candidate"]
    assert record["env"] == {key: env[key] for key in PASSTHROUGH_ENV_KEYS}
    _assert_no_external_seam(log)


def test_release_shell_dispatches_recover_selector_without_candidate(
    tmp_path: Path,
) -> None:
    record_path = tmp_path / "python-record.json"
    env, log = _dispatch_env(tmp_path, record_path)
    version = "8.8.8"
    source_commit = "cccccccccccccccccccccccccccccccccccccccc"

    result = _run_release(
        ["bash", "scripts/release.sh", "--recover", version, source_commit],
        env,
    )

    assert result.returncode == 0
    record = json.loads(record_path.read_text(encoding="utf-8"))
    recorded_argv = record["driver_argv"]
    assert recorded_argv[0] == "scripts/release_candidate_driver.py"
    assert recorded_argv[1:] == [
        "recover",
        "--version",
        version,
        "--source-commit",
        source_commit,
    ]
    assert "candidate" not in recorded_argv
    _assert_no_external_seam(log)
