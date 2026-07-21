#!/usr/bin/env python3
# SPDX-License-Identifier: AGPL-3.0-only
# Copyright (c) 2026 sol pbc

"""Hermetic liveness gate for the release advisory policy rail."""

from __future__ import annotations

import os
import shutil
import subprocess
import sys
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parent.parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from scripts.check_release_preflight import check_cargo_deny  # noqa: E402
from scripts.check_rust_release_manifest import Failure  # noqa: E402
from scripts.release_advisory_policy import (  # noqa: E402
    ReleasePolicyError,
    advisory_check_argv,
    locate_advisory_snapshot,
    materialized_config_bytes,
    prepare_policy_run,
)

FIXTURE_ROOT = ROOT / "tests" / "fixtures" / "release_advisory_db"
WORK_ROOT = ROOT / "target" / "check-release-advisory-liveness"
LOGICAL_DB_URL = "https://fixture.invalid/advisory-db"
GIT_USER_NAME = "solstone release fixture"
GIT_USER_EMAIL = "release-fixture@example.invalid"


class GateError(RuntimeError):
    def __init__(self, failures: Sequence[Failure]) -> None:
        self.failures = tuple(failures)
        super().__init__("; ".join(failure.error for failure in self.failures))


@dataclass(frozen=True)
class MaterializedDb:
    case_root: Path
    source_repo: Path
    db_root: Path
    config_path: Path
    snapshot: Path


def _failure(error: str, *, expected: str, actual: str, repair: str) -> Failure:
    return Failure(error=error, expected=expected, actual=actual, repair=repair)


def _format_failures(failures: Sequence[Any]) -> None:
    for failure in failures:
        print(f"ERROR: {failure.error}", file=sys.stderr)
        print(f"  expected: {failure.expected}", file=sys.stderr)
        print(f"  actual: {failure.actual}", file=sys.stderr)
        print(f"  repair command: {failure.repair}", file=sys.stderr)


def _run(
    argv: Sequence[str],
    *,
    cwd: Path = ROOT,
    env: Mapping[str, str] | None = None,
    check: bool = True,
) -> subprocess.CompletedProcess[str]:
    result = subprocess.run(
        list(argv),
        cwd=cwd,
        env=dict(env) if env is not None else None,
        capture_output=True,
        text=True,
        check=False,
    )
    if check and result.returncode != 0:
        raise GateError(
            [
                _failure(
                    "release advisory liveness command failed",
                    expected="exit 0",
                    actual=result.stderr.strip()
                    or result.stdout.strip()
                    or f"exit {result.returncode}",
                    repair="make check-release-advisory-liveness",
                )
            ]
        )
    return result


def _expect_failure(
    label: str,
    result: subprocess.CompletedProcess[str],
    *,
    contains: str,
) -> None:
    output = "\n".join(part for part in (result.stdout, result.stderr) if part)
    if result.returncode == 0 or contains not in output:
        raise GateError(
            [
                _failure(
                    f"{label} did not fail closed",
                    expected=f"non-zero exit mentioning {contains!r}",
                    actual=output.strip() or f"exit {result.returncode}",
                    repair="make check-release-advisory-liveness",
                )
            ]
        )


def _copy_fixture(input_dir: Path, output_dir: Path) -> None:
    output_dir.mkdir(parents=True, exist_ok=True)
    if input_dir.exists():
        shutil.copytree(input_dir, output_dir, dirs_exist_ok=True)


def _git_env(commit_date: str) -> dict[str, str]:
    env = os.environ.copy()
    env.update(
        {
            "GIT_AUTHOR_DATE": commit_date,
            "GIT_COMMITTER_DATE": commit_date,
        }
    )
    return env


def _rewrite_env(source_repo: Path, global_config: Path) -> dict[str, str]:
    env = os.environ.copy()
    env.update(
        {
            "GIT_CONFIG_NOSYSTEM": "1",
            "GIT_CONFIG_GLOBAL": str(global_config),
            "GIT_CONFIG_COUNT": "1",
            "GIT_CONFIG_KEY_0": (
                f"url.file://{source_repo.resolve().as_posix()}/.insteadOf"
            ),
            "GIT_CONFIG_VALUE_0": LOGICAL_DB_URL,
        }
    )
    return env


def _init_fixture_repo(source_repo: Path, *, commit_date: str) -> None:
    _run(["git", "-c", "init.defaultBranch=main", "init"], cwd=source_repo)
    _commit_all(
        source_repo,
        message="fixture advisory db",
        commit_date=commit_date,
    )


def _commit_all(repo: Path, *, message: str, commit_date: str) -> None:
    _run(
        [
            "git",
            "-c",
            f"user.name={GIT_USER_NAME}",
            "-c",
            f"user.email={GIT_USER_EMAIL}",
            "-c",
            "commit.gpgsign=false",
            "add",
            "-A",
        ],
        cwd=repo,
    )
    _run(
        [
            "git",
            "-c",
            f"user.name={GIT_USER_NAME}",
            "-c",
            f"user.email={GIT_USER_EMAIL}",
            "-c",
            "commit.gpgsign=false",
            "commit",
            "--allow-empty",
            "-m",
            message,
        ],
        cwd=repo,
        env=_git_env(commit_date),
    )


def _write_config(config_path: Path, db_root: Path) -> None:
    config_path.write_bytes(
        materialized_config_bytes(
            (ROOT / "core" / "deny.toml").read_bytes(),
            db_root=db_root,
            db_urls=(LOGICAL_DB_URL,),
        )
    )


def _touch(path: Path, date_expression: str) -> None:
    _run(["touch", "-d", date_expression, str(path)])


def _materialize(
    case_name: str,
    *,
    fixture_name: str | None,
    commit_date: str,
) -> MaterializedDb:
    case_root = WORK_ROOT / case_name
    source_repo = case_root / "source-db"
    db_root = case_root / "db-root"
    config_path = case_root / "deny.toml"
    global_config = case_root / "empty-global-gitconfig"
    source_repo.mkdir(parents=True)
    db_root.mkdir(parents=True)
    global_config.write_text("", encoding="utf-8")
    if fixture_name is not None:
        _copy_fixture(FIXTURE_ROOT / fixture_name, source_repo)
    _init_fixture_repo(source_repo, commit_date=commit_date)
    _write_config(config_path, db_root)
    env = _rewrite_env(source_repo, global_config)
    # Layer (a): these cargo-deny fetches prove the rail-rendered config parses
    # under the pinned binary. The second fetch is the cargo-deny update path that
    # writes FETCH_HEAD.
    for _attempt in range(2):
        _run(
            [
                "cargo-deny",
                "--config",
                str(config_path),
                "--manifest-path",
                str(ROOT / "core" / "Cargo.toml"),
                "fetch",
                "db",
            ],
            env=env,
        )
    snapshot = locate_advisory_snapshot(db_root)
    return MaterializedDb(
        case_root=case_root,
        source_repo=source_repo,
        db_root=db_root,
        config_path=config_path,
        snapshot=snapshot,
    )


def _case_empty_snapshot_fails(commit_date: str) -> None:
    db = _materialize("empty-snapshot", fixture_name="valid", commit_date=commit_date)
    shutil.rmtree(db.snapshot / "crates")
    _commit_all(
        db.snapshot,
        message="empty advisory db",
        commit_date=commit_date,
    )
    _touch(db.snapshot / ".git" / "FETCH_HEAD", "1 hour ago")
    try:
        prepare_policy_run(
            ROOT,
            advisory_source_id="fixture",
            db_urls=(LOGICAL_DB_URL,),
            db_root=db.db_root,
            clock=lambda: datetime.now(UTC),
        )
    except ReleasePolicyError as exc:
        if any(
            failure.error == "advisory db snapshot contains no advisories"
            for failure in exc.failures
        ):
            return
        raise
    raise GateError(
        [
            _failure(
                "empty advisory snapshot passed",
                expected="rail rejects zero advisory count",
                actual="pass",
                repair="make check-release-advisory-liveness",
            )
        ]
    )


def _case_malformed_snapshot_fails(commit_date: str) -> None:
    case_root = WORK_ROOT / "malformed-snapshot"
    source_repo = case_root / "source-db"
    db_root = case_root / "db-root"
    config_path = case_root / "deny.toml"
    global_config = case_root / "empty-global-gitconfig"
    source_repo.mkdir(parents=True)
    db_root.mkdir(parents=True)
    global_config.write_text("", encoding="utf-8")
    _copy_fixture(FIXTURE_ROOT / "malformed", source_repo)
    _init_fixture_repo(source_repo, commit_date=commit_date)
    _write_config(config_path, db_root)
    result = _run(
        [
            "cargo-deny",
            "--config",
            str(config_path),
            "--manifest-path",
            str(ROOT / "core" / "Cargo.toml"),
            "fetch",
            "db",
        ],
        env=_rewrite_env(source_repo, global_config),
        check=False,
    )
    _expect_failure("malformed advisory snapshot", result, contains="failed to parse")


def _case_stale_snapshot_fails_binary(commit_date: str) -> None:
    db = _materialize("stale-binary", fixture_name="valid", commit_date=commit_date)
    _touch(db.snapshot / ".git" / "FETCH_HEAD", "20 days ago")
    result = _run(
        advisory_check_argv("cargo-deny", db.config_path, ROOT),
        check=False,
    )
    # Layer (c): this is cargo-deny's own stale check, not the rail's Python check.
    _expect_failure("stale advisory snapshot", result, contains="repository is stale")


def _case_valid_snapshot_passes(commit_date: str) -> None:
    db = _materialize("valid-snapshot", fixture_name="valid", commit_date=commit_date)
    _touch(db.snapshot / ".git" / "FETCH_HEAD", "1 hour ago")
    result = prepare_policy_run(
        ROOT,
        advisory_source_id="fixture",
        db_urls=(LOGICAL_DB_URL,),
        db_root=db.db_root,
        clock=lambda: datetime.now(UTC),
    )
    # Layer (d): cargo-deny parsed the debug-scanned snapshot and the rail measured
    # the advisory count from that same snapshot.
    if result.advisory_count != 1:
        raise GateError(
            [
                _failure(
                    "valid advisory snapshot count is wrong",
                    expected="1 advisory",
                    actual=str(result.advisory_count),
                    repair="make check-release-advisory-liveness",
                )
            ]
        )


def run_gate() -> None:
    if WORK_ROOT.exists():
        shutil.rmtree(WORK_ROOT)
    WORK_ROOT.mkdir(parents=True)
    commit_date = (
        (datetime.now(UTC) - timedelta(days=1))
        .replace(microsecond=0)
        .isoformat()
        .replace("+00:00", "Z")
    )
    _materialize("config-parses", fixture_name="valid", commit_date=commit_date)
    _case_empty_snapshot_fails(commit_date)
    _case_malformed_snapshot_fails(commit_date)
    _case_stale_snapshot_fails_binary(commit_date)
    _case_valid_snapshot_passes(commit_date)


def main() -> int:
    pin_failures = check_cargo_deny()
    if pin_failures:
        _format_failures(pin_failures)
        return 1
    try:
        run_gate()
    except ReleasePolicyError as exc:
        _format_failures(exc.failures)
        return 1
    except GateError as exc:
        _format_failures(exc.failures)
        return 1
    print("release advisory liveness ok")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
