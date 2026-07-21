#!/usr/bin/env python3
# SPDX-License-Identifier: AGPL-3.0-only
# Copyright (c) 2026 sol pbc

"""Advisory snapshot binding for release candidates."""

from __future__ import annotations

import hashlib
import json
import re
import shutil
import subprocess
import tempfile
from collections.abc import Callable, Sequence
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from pathlib import Path
from urllib.parse import urlparse

from scripts.check_rust_release_manifest import Failure
from scripts.release_tool_pins import CARGO_DENY_PIN

PolicyMode = str
Runner = Callable[..., subprocess.CompletedProcess[str]]
TempPathFactory = Callable[[str], Path]
Clock = Callable[[], datetime]
ArchiveHasher = Callable[[Path], str]

ADVISORY_TABLE_RE = re.compile(r"(?m)^\s*\[\s*advisories\s*\]\s*(?:#.*)?$")
SOURCE_ID_RE = re.compile(r"^[a-z0-9][a-z0-9_-]*$")
MAXIMUM_DB_STALENESS = "24 hours"
MAXIMUM_DB_STALENESS_DELTA = timedelta(hours=24)
ARCHIVE_PREFIX = "advisory-db/"


@dataclass(frozen=True)
class PolicyRun:
    advisory_source_id: str
    db_commit: str
    db_archive_sha256: str
    advisory_acquired_at: str
    policy_checked_at: str
    result: str

    def manifest_dependency_policy(self) -> dict[str, str]:
        return {
            "cargo_deny_version": CARGO_DENY_PIN,
            "deterministic_gate": "pass",
            "advisory_checked_at": self.policy_checked_at,
        }


class ReleasePolicyError(RuntimeError):
    def __init__(self, failures: Sequence[Failure]) -> None:
        self.failures = tuple(failures)
        super().__init__("; ".join(failure.error for failure in self.failures))


def _failure(error: str, *, expected: str, actual: str, repair: str) -> Failure:
    return Failure(error=error, expected=expected, actual=actual, repair=repair)


def _default_temp_path_factory(label: str) -> Path:
    return Path(tempfile.mkdtemp(prefix=f"solstone-{label}-"))


def _utc_now() -> datetime:
    return datetime.now(UTC)


def _toml_string(value: str) -> str:
    return json.dumps(value, ensure_ascii=False)


def _advisory_host(value: str) -> str | None:
    parsed = urlparse(value)
    if parsed.scheme and parsed.hostname:
        return parsed.hostname.lower()
    if "://" in value:
        return None
    match = re.fullmatch(r"(?:[^@:/]+@)?(?P<host>[^:/]+):[^:]+", value)
    if match:
        return match.group("host").lower()
    return None


def _validate_source(
    advisory_source_id: str,
    db_urls: Sequence[str],
) -> list[Failure]:
    failures: list[Failure] = []
    if not SOURCE_ID_RE.fullmatch(advisory_source_id):
        failures.append(
            _failure(
                "advisory source id is not a public slug",
                expected="non-empty lowercase slug",
                actual=advisory_source_id or "<empty>",
                repair="python3 scripts/check_rust_release_manifest.py",
            )
        )
    if not db_urls:
        failures.append(
            _failure(
                "advisory db source is empty",
                expected="at least one non-GitHub advisory db url",
                actual="<empty>",
                repair="python3 scripts/check_rust_release_manifest.py",
            )
        )
        return failures
    for url in db_urls:
        host = _advisory_host(url)
        if host is None:
            failures.append(
                _failure(
                    "advisory db url is not a git url with a host",
                    expected="git url with non-GitHub host",
                    actual="redacted",
                    repair="python3 scripts/check_rust_release_manifest.py",
                )
            )
            continue
        if host == "github.com" or host.endswith(".github.com"):
            failures.append(
                _failure(
                    "advisory db url points at GitHub",
                    expected="non-GitHub advisory db host",
                    actual=host,
                    repair="python3 scripts/check_rust_release_manifest.py",
                )
            )
    return failures


def _materialized_config_bytes(
    base_bytes: bytes,
    *,
    db_root: Path,
    db_urls: Sequence[str],
) -> bytes:
    try:
        base_text = base_bytes.decode("utf-8")
    except UnicodeDecodeError as exc:
        raise ReleasePolicyError(
            [
                _failure(
                    "core deny.toml is not UTF-8",
                    expected="UTF-8 TOML",
                    actual=str(exc),
                    repair="python3 scripts/check_rust_release_manifest.py",
                )
            ]
        ) from exc
    if ADVISORY_TABLE_RE.search(base_text):
        raise ReleasePolicyError(
            [
                _failure(
                    "core deny.toml already defines advisories",
                    expected="core/deny.toml without [advisories]",
                    actual="[advisories] present",
                    repair="python3 scripts/check_rust_release_manifest.py",
                )
            ]
        )
    prefix = base_bytes if base_bytes.endswith(b"\n") else base_bytes + b"\n"
    urls = ", ".join(_toml_string(url) for url in db_urls)
    block = (
        "\n"
        "[advisories]\n"
        f"db-path = {_toml_string(str(db_root))}\n"
        f"db-urls = [{urls}]\n"
        "git-fetch-with-cli = true\n"
        f"maximum-db-staleness = {_toml_string(MAXIMUM_DB_STALENESS)}\n"
    )
    return prefix + block.encode("utf-8")


def _write_materialized_config(
    root: Path,
    temp_root: Path,
    *,
    db_root: Path,
    db_urls: Sequence[str],
) -> Path:
    materialized = _materialized_config_bytes(
        (root / "core" / "deny.toml").read_bytes(),
        db_root=db_root,
        db_urls=db_urls,
    )
    temp_root.mkdir(parents=True, exist_ok=True)
    path = temp_root / "deny.release-advisories.toml"
    path.write_bytes(materialized)
    return path


def _run(
    runner: Runner,
    argv: Sequence[str],
    *,
    cwd: Path | None = None,
) -> subprocess.CompletedProcess[str]:
    result = runner(
        list(argv),
        cwd=cwd,
        capture_output=True,
        text=True,
        check=False,
    )
    if result.returncode != 0:
        actual = (
            result.stderr.strip()
            or result.stdout.strip()
            or f"exit {result.returncode}"
        )
        raise ReleasePolicyError(
            [
                _failure(
                    "release advisory command failed",
                    expected="exit 0",
                    actual=actual,
                    repair="python3 scripts/check_rust_release_manifest.py",
                )
            ]
        )
    return result


def _git_stdout(runner: Runner, db_root: Path, args: Sequence[str]) -> str:
    return _run(runner, ["git", "-C", str(db_root), *args]).stdout.strip()


def _default_archive_hasher(db_root: Path) -> str:
    result = subprocess.run(
        [
            "git",
            "-C",
            str(db_root),
            "archive",
            "--format=tar",
            f"--prefix={ARCHIVE_PREFIX}",
            "--mtime=@0",
            "HEAD",
        ],
        capture_output=True,
        check=False,
    )
    if result.returncode != 0:
        raise ReleasePolicyError(
            [
                _failure(
                    "release advisory archive failed",
                    expected="git archive exit 0",
                    actual=result.stderr.decode("utf-8", errors="replace").strip()
                    or f"exit {result.returncode}",
                    repair="python3 scripts/check_rust_release_manifest.py",
                )
            ]
        )
    return hashlib.sha256(result.stdout).hexdigest()


def _parse_utc(value: str) -> datetime:
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError as exc:
        raise ReleasePolicyError(
            [
                _failure(
                    "advisory acquisition time is not RFC3339",
                    expected="RFC3339 timestamp with UTC offset",
                    actual=value or "<empty>",
                    repair="python3 scripts/check_rust_release_manifest.py",
                )
            ]
        ) from exc
    if parsed.tzinfo is None:
        raise ReleasePolicyError(
            [
                _failure(
                    "advisory acquisition time is missing an offset",
                    expected="RFC3339 timestamp with UTC offset",
                    actual=value,
                    repair="python3 scripts/check_rust_release_manifest.py",
                )
            ]
        )
    return parsed.astimezone(UTC)


def _format_utc(value: datetime) -> str:
    return (
        value.astimezone(UTC).replace(microsecond=0).isoformat().replace("+00:00", "Z")
    )


def _cleanup_temp(
    temp_root: Path,
    config_path: Path | None,
    *,
    remove_tree: bool,
) -> None:
    if remove_tree:
        shutil.rmtree(temp_root, ignore_errors=True)
        return
    if config_path is not None:
        config_path.unlink(missing_ok=True)
    try:
        temp_root.rmdir()
    except OSError:
        pass


def _validate_acquisition_freshness(
    *,
    advisory_acquired_at: str,
    acquired: datetime,
    policy_time: datetime,
) -> None:
    failures: list[Failure] = []
    policy_utc = policy_time.astimezone(UTC)
    if acquired > policy_utc:
        failures.append(
            _failure(
                "advisory acquisition time is in the future",
                expected="acquisition time at or before policy check time",
                actual=advisory_acquired_at,
                repair="python3 scripts/check_rust_release_manifest.py",
            )
        )
    elif policy_utc - acquired > MAXIMUM_DB_STALENESS_DELTA:
        failures.append(
            _failure(
                "advisory acquisition time is stale",
                expected=f"acquisition time within {MAXIMUM_DB_STALENESS}",
                actual=advisory_acquired_at,
                repair="python3 scripts/check_rust_release_manifest.py",
            )
        )
    if failures:
        raise ReleasePolicyError(failures)


def prepare_policy_run(
    root: Path,
    *,
    advisory_source_id: str,
    db_urls: Sequence[str],
    mode: PolicyMode,
    advisory_acquired_at: str | None = None,
    db_root: Path | None = None,
    cargo_deny: str = "cargo-deny",
    runner: Runner = subprocess.run,
    temp_path_factory: TempPathFactory = _default_temp_path_factory,
    clock: Clock = _utc_now,
    archive_hasher: ArchiveHasher = _default_archive_hasher,
) -> PolicyRun:
    failures = _validate_source(advisory_source_id, db_urls)
    if failures:
        raise ReleasePolicyError(failures)
    if mode not in {"refresh-once", "caller-provisioned"}:
        raise ReleasePolicyError(
            [
                _failure(
                    "advisory policy mode is unsupported",
                    expected="refresh-once or caller-provisioned",
                    actual=mode,
                    repair="python3 scripts/check_rust_release_manifest.py",
                )
            ]
        )
    if mode == "caller-provisioned" and db_root is None:
        raise ReleasePolicyError(
            [
                _failure(
                    "caller-provisioned advisory mode has no db root",
                    expected="existing advisory db root",
                    actual="<missing>",
                    repair="python3 scripts/check_rust_release_manifest.py",
                )
            ]
        )
    if mode == "caller-provisioned" and advisory_acquired_at is None:
        raise ReleasePolicyError(
            [
                _failure(
                    "caller-provisioned advisory mode has no acquisition time",
                    expected="trusted advisory acquisition RFC3339 timestamp",
                    actual="<missing>",
                    repair="python3 scripts/check_rust_release_manifest.py",
                )
            ]
        )
    if mode == "caller-provisioned" and advisory_acquired_at is not None:
        _parse_utc(advisory_acquired_at)

    temp_root = temp_path_factory("advisory-policy")
    resolved_db_root = db_root or (temp_root / "advisory-db")
    config_path: Path | None = None
    try:
        config_path = _write_materialized_config(
            root,
            temp_root,
            db_root=resolved_db_root,
            db_urls=db_urls,
        )

        if mode == "refresh-once":
            _run(
                runner,
                [cargo_deny, "--config", str(config_path), "fetch", "db"],
                cwd=root,
            )

        clean = _git_stdout(
            runner,
            resolved_db_root,
            [
                "status",
                "--porcelain=v1",
                "--untracked-files=all",
                "--ignored=matching",
            ],
        )
        if clean:
            raise ReleasePolicyError(
                [
                    _failure(
                        "advisory db has uncommitted or ignored material",
                        expected="empty git status including ignored and untracked files",
                        actual=clean,
                        repair="git status --porcelain=v1 --untracked-files=all --ignored=matching",
                    )
                ]
            )
        db_commit = _git_stdout(
            runner, resolved_db_root, ["rev-parse", "--verify", "HEAD^{commit}"]
        )
        db_archive_sha256 = archive_hasher(resolved_db_root)
        if mode == "refresh-once":
            acquisition_text = _format_utc(clock())
        else:
            assert advisory_acquired_at is not None
            acquisition_text = advisory_acquired_at
            _parse_utc(acquisition_text)
        _run(
            runner,
            [
                cargo_deny,
                "--config",
                str(config_path),
                "--locked",
                "--offline",
                "check",
                "advisories",
            ],
            cwd=root,
        )

        policy_time = clock()
        acquired = _parse_utc(acquisition_text)
        _validate_acquisition_freshness(
            advisory_acquired_at=acquisition_text,
            acquired=acquired,
            policy_time=policy_time,
        )
        return PolicyRun(
            advisory_source_id=advisory_source_id,
            db_commit=db_commit,
            db_archive_sha256=db_archive_sha256,
            advisory_acquired_at=acquisition_text,
            policy_checked_at=_format_utc(policy_time),
            result="pass",
        )
    finally:
        _cleanup_temp(temp_root, config_path, remove_tree=mode == "refresh-once")
