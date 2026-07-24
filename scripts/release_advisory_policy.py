#!/usr/bin/env python3
# SPDX-License-Identifier: AGPL-3.0-only
# Copyright (c) 2026 sol pbc

"""Advisory snapshot binding for release candidates.

Release candidates consume an operator-provisioned advisory database root. The root
is a plain, non-git parent directory containing exactly one cargo-deny snapshot
directory plus cargo-deny's parent-level ``db.lock``. The release rail measures git
identity inside that snapshot only: the snapshot's git top level must resolve to the
snapshot itself, and the receipt records the snapshot basename, HEAD commit, archive
digest, advisory count, FETCH_HEAD mtime, HEAD commit timestamp, check time, and pass
result. Absolute paths are never recorded.

Freshness has two independent bounds. Fetch recency is the measured
``.git/FETCH_HEAD`` mtime and must be within 24 hours. Content age is the measured
HEAD commit timestamp and must be within 14 days. FETCH_HEAD mtime is mutable
filesystem metadata; the commit timestamp is derived from the commit object named by
``db_commit``.

To acquire a conforming snapshot, write a cargo-deny config that sets the same
``db-path`` and non-GitHub ``db-urls`` supplied to the release rail, then run
``cargo-deny --config <cfg> --manifest-path core/Cargo.toml fetch db`` twice. The
second run is intentional: cargo-deny 0.20.2 does not write ``.git/FETCH_HEAD`` on the
first clone into an empty db root, but it does on subsequent fetches. Do not run
manual ``git fetch`` or ``git reset``. ``make audit`` is not this acquisition
operation. ``make audit`` is the separate signed-packet mirror-bound advisory
audit implemented by ``scripts/advisory_mirror_audit.py``; this module remains
the release-candidate advisory acquisition and receipt path.
"""

from __future__ import annotations

import hashlib
import json
import re
import subprocess
import tempfile
from collections.abc import Callable, Sequence
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from pathlib import Path
from urllib.parse import urlparse

from scripts.check_rust_release_manifest import (
    RFC3339_UTC_RE,
    SHA256_RE,
    SOURCE_COMMIT_RE,
    Failure,
)
from scripts.release_tool_pins import CARGO_DENY_PIN

Runner = Callable[..., subprocess.CompletedProcess[str]]
TempPathFactory = Callable[[str], Path]
Clock = Callable[[], datetime]
ArchiveHasher = Callable[[Path], str]
PathRemover = Callable[[Path], None]

ADVISORY_TABLE_RE = re.compile(r"(?m)^\s*\[\s*advisories\s*\]\s*(?:#.*)?$")
ADVISORY_DB_DEBUG_RE = re.compile(r"Opening advisory database at '(?P<path>[^']+)'")
SOURCE_ID_RE = re.compile(r"^[a-z0-9][a-z0-9_-]*$")
MAXIMUM_DB_FETCH_STALENESS_DELTA = timedelta(hours=24)
MAXIMUM_DB_CONTENT_AGE_DELTA = timedelta(days=14)
ARCHIVE_PREFIX = "advisory-db/"
ADVISORY_DB_FETCH_REPAIR = (
    "reacquire RELEASE_ADVISORY_DB_ROOT with cargo-deny --config <cfg> "
    "--manifest-path core/Cargo.toml fetch db twice"
)
ADVISORY_CLOCK_REPAIR = f"check the system clock, then {ADVISORY_DB_FETCH_REPAIR}"


@dataclass(frozen=True)
class PolicyRun:
    advisory_source_id: str
    db_snapshot_basename: str
    db_commit: str
    db_archive_sha256: str
    advisory_count: int
    advisory_acquired_at: str
    db_commit_timestamp: str
    policy_checked_at: str
    result: str

    def __post_init__(self) -> None:
        failures = [
            *validate_snapshot_identity(
                "policy_run",
                db_commit=self.db_commit,
                db_archive_sha256=self.db_archive_sha256,
            ),
            *_validate_policy_run_receipt(self),
        ]
        if failures:
            raise ReleasePolicyError(failures)

    def manifest_dependency_policy(self) -> dict[str, str]:
        return {
            "cargo_deny_version": CARGO_DENY_PIN,
            "deterministic_gate": "pass",
            "advisory_checked_at": self.policy_checked_at,
        }


def _validate_policy_run_receipt(policy_run: PolicyRun) -> list[Failure]:
    failures: list[Failure] = []
    if not _safe_snapshot_basename(policy_run.db_snapshot_basename):
        failures.append(
            _failure(
                "policy_run.db_snapshot_basename is invalid",
                expected="safe snapshot directory basename",
                actual=repr(policy_run.db_snapshot_basename),
                repair="python3 scripts/check_rust_release_manifest.py",
            )
        )
    if type(policy_run.advisory_count) is not int or policy_run.advisory_count <= 0:
        failures.append(
            _failure(
                "policy_run.advisory_count is invalid",
                expected="positive integer advisory count",
                actual=repr(policy_run.advisory_count),
                repair="python3 scripts/check_rust_release_manifest.py",
            )
        )
    for key in (
        "advisory_acquired_at",
        "db_commit_timestamp",
        "policy_checked_at",
    ):
        value = getattr(policy_run, key)
        if not is_normalized_utc_timestamp(value):
            failures.append(
                _failure(
                    f"policy_run.{key} is invalid",
                    expected="RFC3339 UTC timestamp normalized with Z",
                    actual=repr(value),
                    repair="python3 scripts/check_rust_release_manifest.py",
                )
            )
    if policy_run.result != "pass":
        failures.append(
            _failure(
                "policy_run.result is invalid",
                expected="pass",
                actual=repr(policy_run.result),
                repair="python3 scripts/check_rust_release_manifest.py",
            )
        )
    return failures


def _safe_snapshot_basename(value: object) -> bool:
    return (
        isinstance(value, str)
        and bool(value)
        and value not in {".", ".."}
        and Path(value).name == value
        and "/" not in value
        and "\\" not in value
    )


class ReleasePolicyError(RuntimeError):
    def __init__(self, failures: Sequence[Failure]) -> None:
        self.failures = tuple(failures)
        super().__init__("; ".join(failure.error for failure in self.failures))


def _failure(error: str, *, expected: str, actual: str, repair: str) -> Failure:
    return Failure(error=error, expected=expected, actual=actual, repair=repair)


def _actual_identity_value(value: object) -> str:
    if isinstance(value, str) and value:
        return value
    if value == "":
        return "<empty>"
    return str(value)


def validate_snapshot_identity(
    label: str,
    *,
    db_commit: object,
    db_archive_sha256: object,
) -> list[Failure]:
    failures: list[Failure] = []
    if not isinstance(db_commit, str) or not SOURCE_COMMIT_RE.fullmatch(db_commit):
        failures.append(
            _failure(
                f"{label}.db_commit is invalid",
                expected="exactly 40 or 64 lowercase hex characters",
                actual=_actual_identity_value(db_commit),
                repair="python3 scripts/check_rust_release_manifest.py",
            )
        )
    if not isinstance(db_archive_sha256, str) or not SHA256_RE.fullmatch(
        db_archive_sha256
    ):
        failures.append(
            _failure(
                f"{label}.db_archive_sha256 is invalid",
                expected="exactly 64 lowercase hex characters",
                actual=_actual_identity_value(db_archive_sha256),
                repair="python3 scripts/check_rust_release_manifest.py",
            )
        )
    return failures


def _default_temp_path_factory(label: str) -> Path:
    return Path(tempfile.mkdtemp(prefix=f"solstone-{label}-"))


def _utc_now() -> datetime:
    return datetime.now(UTC)


def _unlink_path(path: Path) -> None:
    path.unlink()


def _remove_dir(path: Path) -> None:
    path.rmdir()


def _toml_string(value: str) -> str:
    return json.dumps(value, ensure_ascii=False)


def _cargo_deny_duration(delta: timedelta) -> str:
    total_seconds = delta.total_seconds()
    if total_seconds <= 0 or not float(total_seconds).is_integer():
        raise AssertionError("cargo-deny duration must be a positive whole second")
    seconds = int(total_seconds)
    if seconds % 3600 == 0:
        return f"PT{seconds // 3600}H"
    if seconds % 60 == 0:
        return f"PT{seconds // 60}M"
    return f"PT{seconds}S"


def _duration_label(delta: timedelta) -> str:
    seconds = int(delta.total_seconds())
    if seconds % 86400 == 0:
        return f"{seconds // 86400}d"
    if seconds % 3600 == 0:
        return f"{seconds // 3600}h"
    return f"{seconds}s"


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


def materialized_config_bytes(
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
        "maximum-db-staleness = "
        f"{_toml_string(_cargo_deny_duration(MAXIMUM_DB_FETCH_STALENESS_DELTA))}\n"
    )
    return prefix + block.encode("utf-8")


def _write_materialized_config(
    root: Path,
    temp_root: Path,
    *,
    db_root: Path,
    db_urls: Sequence[str],
) -> Path:
    materialized = materialized_config_bytes(
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


def _git_stdout(runner: Runner, git_root: Path, args: Sequence[str]) -> str:
    return _run(runner, ["git", "-C", str(git_root), *args]).stdout.strip()


def _realpath(path: Path) -> Path:
    return path.resolve(strict=False)


def locate_advisory_snapshot(db_root: Path) -> Path:
    try:
        entries = sorted(db_root.iterdir(), key=lambda path: path.name)
    except OSError as exc:
        raise ReleasePolicyError(
            [
                _failure(
                    "advisory db root could not be inspected",
                    expected="existing advisory db root containing one snapshot",
                    actual=type(exc).__name__,
                    repair="provision RELEASE_ADVISORY_DB_ROOT with cargo-deny fetch db",
                )
            ]
        ) from None
    visible_entries = [path for path in entries if path.name != "db.lock"]
    unexpected = [
        path.name for path in visible_entries if path.is_symlink() or not path.is_dir()
    ]
    if unexpected:
        raise ReleasePolicyError(
            [
                _failure(
                    "advisory db root contains unexpected entries",
                    expected="one snapshot directory plus db.lock",
                    actual=", ".join(unexpected),
                    repair="provision a clean RELEASE_ADVISORY_DB_ROOT with cargo-deny fetch db",
                )
            ]
        )
    snapshots = [path for path in visible_entries if path.is_dir()]
    if len(snapshots) != 1:
        raise ReleasePolicyError(
            [
                _failure(
                    "advisory db snapshot count is invalid",
                    expected="exactly one snapshot directory under RELEASE_ADVISORY_DB_ROOT",
                    actual=str(len(snapshots)),
                    repair="provision a clean RELEASE_ADVISORY_DB_ROOT with cargo-deny fetch db",
                )
            ]
        )
    return snapshots[0]


def _assert_snapshot_git_top_level(runner: Runner, snapshot: Path) -> None:
    try:
        top_level = _git_stdout(runner, snapshot, ["rev-parse", "--show-toplevel"])
    except ReleasePolicyError as exc:
        raise ReleasePolicyError(
            [
                _failure(
                    "advisory db snapshot git root could not be resolved",
                    expected="snapshot directory is a git checkout root",
                    actual="git rev-parse failed",
                    repair="reacquire RELEASE_ADVISORY_DB_ROOT with cargo-deny fetch db",
                )
            ]
        ) from exc
    if _realpath(Path(top_level)) != _realpath(snapshot):
        raise ReleasePolicyError(
            [
                _failure(
                    "advisory db snapshot is not an isolated git checkout",
                    expected=str(_realpath(snapshot)),
                    actual=str(_realpath(Path(top_level))),
                    repair="set RELEASE_ADVISORY_DB_ROOT to cargo-deny's non-git parent directory",
                )
            ]
        )


def _assert_snapshot_clean(runner: Runner, snapshot: Path) -> None:
    clean = _git_stdout(
        runner,
        snapshot,
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
                    "advisory db snapshot has uncommitted or ignored material",
                    expected="empty git status including ignored and untracked files",
                    actual=clean,
                    repair=(
                        "git -C <advisory-db-snapshot> status --porcelain=v1 "
                        "--untracked-files=all --ignored=matching"
                    ),
                )
            ]
        )


def _count_advisories(snapshot: Path) -> int:
    return sum(
        1
        for path in snapshot.glob("crates/**/RUSTSEC-*.md")
        if path.is_file() and not path.is_symlink()
    )


def _validate_advisory_count(count: int) -> None:
    if count <= 0:
        raise ReleasePolicyError(
            [
                _failure(
                    "advisory db snapshot contains no advisories",
                    expected="at least one crates/**/RUSTSEC-*.md advisory",
                    actual="0",
                    repair="reacquire RELEASE_ADVISORY_DB_ROOT from a populated advisory mirror",
                )
            ]
        )


def _fetch_head_mtime(snapshot: Path) -> datetime:
    fetch_head = snapshot / ".git" / "FETCH_HEAD"
    if fetch_head.is_symlink() or not fetch_head.is_file():
        raise ReleasePolicyError(
            [
                _failure(
                    "advisory db FETCH_HEAD is missing",
                    expected="snapshot .git/FETCH_HEAD written by cargo-deny fetch db",
                    actual="<missing>",
                    repair="run cargo-deny --config <cfg> --manifest-path core/Cargo.toml fetch db twice",
                )
            ]
        )
    return datetime.fromtimestamp(fetch_head.stat().st_mtime, UTC)


def _strip_one_trailing_newline(value: str) -> str:
    return value[:-1] if value.endswith("\n") else value


def _git_db_commit(runner: Runner, snapshot: Path) -> str:
    value = _strip_one_trailing_newline(
        _run(
            runner,
            ["git", "-C", str(snapshot), "rev-parse", "--verify", "HEAD^{commit}"],
        ).stdout
    )
    failures = validate_snapshot_identity(
        "advisory_snapshot",
        db_commit=value,
        db_archive_sha256="0" * 64,
    )
    if failures:
        raise ReleasePolicyError(failures)
    return value


def _git_db_commit_timestamp(runner: Runner, snapshot: Path) -> datetime:
    value = _git_stdout(runner, snapshot, ["show", "-s", "--format=%cI", "HEAD"])
    return _parse_utc(value, label="advisory db commit timestamp")


def advisory_check_argv(cargo_deny: str, config_path: Path, root: Path) -> list[str]:
    return [
        cargo_deny,
        "--config",
        str(config_path),
        "--manifest-path",
        str(root / "core" / "Cargo.toml"),
        "-L",
        "debug",
        "--locked",
        "--offline",
        "check",
        "advisories",
    ]


def _scanned_advisory_db(stderr: str) -> Path:
    matches = [match.group("path") for match in ADVISORY_DB_DEBUG_RE.finditer(stderr)]
    if len(matches) != 1:
        raise ReleasePolicyError(
            [
                _failure(
                    "cargo-deny advisory database debug line is missing",
                    expected="exactly one Opening advisory database at '<path>' debug line",
                    actual=str(len(matches)),
                    repair="run the pinned cargo-deny with -L debug and inspect stderr",
                )
            ]
        )
    return Path(matches[0])


def _assert_scanned_snapshot(stderr: str, snapshot: Path) -> None:
    scanned = _scanned_advisory_db(stderr)
    if _realpath(scanned) != _realpath(snapshot):
        raise ReleasePolicyError(
            [
                _failure(
                    "cargo-deny scanned a different advisory database",
                    expected=str(_realpath(snapshot)),
                    actual=str(_realpath(scanned)),
                    repair="provision RELEASE_ADVISORY_DB_ROOT with exactly one cargo-deny snapshot",
                )
            ]
        )


def _default_archive_hasher(snapshot: Path) -> str:
    result = subprocess.run(
        [
            "git",
            "-C",
            str(snapshot),
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


def _parse_utc(value: str, *, label: str = "advisory acquisition time") -> datetime:
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except (AttributeError, ValueError) as exc:
        raise ReleasePolicyError(
            [
                _failure(
                    f"{label} is not RFC3339",
                    expected="RFC3339 timestamp with UTC offset",
                    actual=str(value) if value else "<empty>",
                    repair="python3 scripts/check_rust_release_manifest.py",
                )
            ]
        ) from exc
    if parsed.tzinfo is None:
        raise ReleasePolicyError(
            [
                _failure(
                    f"{label} is missing an offset",
                    expected="RFC3339 timestamp with UTC offset",
                    actual=str(value),
                    repair="python3 scripts/check_rust_release_manifest.py",
                )
            ]
        )
    return parsed.astimezone(UTC)


def _format_utc(value: datetime) -> str:
    return (
        value.astimezone(UTC).replace(microsecond=0).isoformat().replace("+00:00", "Z")
    )


def is_normalized_utc_timestamp(value: object) -> bool:
    if not isinstance(value, str) or not RFC3339_UTC_RE.fullmatch(value):
        return False
    try:
        return _format_utc(_parse_utc(value)) == value
    except ReleasePolicyError:
        return False


def _cleanup_temp(
    temp_root: Path,
    config_path: Path | None,
    *,
    unlink_path: PathRemover = _unlink_path,
    remove_dir: PathRemover = _remove_dir,
) -> None:
    try:
        if config_path is not None and config_path.exists():
            unlink_path(config_path)
        if temp_root.exists():
            remove_dir(temp_root)
    except OSError as exc:
        raise ReleasePolicyError(
            [
                _failure(
                    "release advisory cleanup failed during materialized config removal",
                    expected="owned release advisory temporary files removed",
                    actual=type(exc).__name__,
                    repair="python3 scripts/check_rust_release_manifest.py",
                )
            ]
        ) from None


def _combined_release_policy_error(
    primary_error: ReleasePolicyError | None,
    cleanup_error: ReleasePolicyError | None,
) -> ReleasePolicyError | None:
    if primary_error is not None and cleanup_error is not None:
        return ReleasePolicyError([*primary_error.failures, *cleanup_error.failures])
    return primary_error or cleanup_error


def _validate_acquisition_freshness(
    *,
    advisory_acquired_at: str,
    fetch_acquired: datetime,
    db_commit_timestamp: str,
    db_commit_time: datetime,
    policy_time: datetime,
) -> None:
    failures: list[Failure] = []
    policy_utc = policy_time.astimezone(UTC)
    if fetch_acquired > policy_utc:
        failures.append(
            _failure(
                "advisory fetch time is in the future",
                expected="FETCH_HEAD mtime at or before policy check time",
                actual=advisory_acquired_at,
                repair=ADVISORY_CLOCK_REPAIR,
            )
        )
    elif policy_utc - fetch_acquired > MAXIMUM_DB_FETCH_STALENESS_DELTA:
        failures.append(
            _failure(
                "advisory fetch time is stale",
                expected=(
                    "FETCH_HEAD mtime within "
                    f"{_duration_label(MAXIMUM_DB_FETCH_STALENESS_DELTA)}"
                ),
                actual=advisory_acquired_at,
                repair=ADVISORY_DB_FETCH_REPAIR,
            )
        )
    if db_commit_time > policy_utc:
        failures.append(
            _failure(
                "advisory db commit timestamp is in the future",
                expected="HEAD commit timestamp at or before policy check time",
                actual=db_commit_timestamp,
                repair=ADVISORY_CLOCK_REPAIR,
            )
        )
    elif policy_utc - db_commit_time > MAXIMUM_DB_CONTENT_AGE_DELTA:
        failures.append(
            _failure(
                "advisory db content is stale",
                expected=(
                    "HEAD commit timestamp within "
                    f"{_duration_label(MAXIMUM_DB_CONTENT_AGE_DELTA)}"
                ),
                actual=db_commit_timestamp,
                repair="reacquire RELEASE_ADVISORY_DB_ROOT from a current advisory mirror",
            )
        )
    if failures:
        raise ReleasePolicyError(failures)


def prepare_policy_run(
    root: Path,
    *,
    advisory_source_id: str,
    db_urls: Sequence[str],
    db_root: Path,
    cargo_deny: str = "cargo-deny",
    runner: Runner = subprocess.run,
    temp_path_factory: TempPathFactory = _default_temp_path_factory,
    clock: Clock = _utc_now,
    archive_hasher: ArchiveHasher = _default_archive_hasher,
    cleanup_unlink: PathRemover = _unlink_path,
    cleanup_rmdir: PathRemover = _remove_dir,
) -> PolicyRun:
    failures = _validate_source(advisory_source_id, db_urls)
    if failures:
        raise ReleasePolicyError(failures)
    if db_root is None:
        raise ReleasePolicyError(
            [
                _failure(
                    "release advisory db root is missing",
                    expected="RELEASE_ADVISORY_DB_ROOT containing one cargo-deny snapshot",
                    actual="<missing>",
                    repair="provision RELEASE_ADVISORY_DB_ROOT with cargo-deny fetch db",
                )
            ]
        )

    temp_root = temp_path_factory("advisory-policy")
    config_path: Path | None = None
    result: PolicyRun | None = None
    primary_error: ReleasePolicyError | None = None
    try:
        config_path = _write_materialized_config(
            root,
            temp_root,
            db_root=db_root,
            db_urls=db_urls,
        )

        snapshot = locate_advisory_snapshot(db_root)
        _assert_snapshot_git_top_level(runner, snapshot)
        _assert_snapshot_clean(runner, snapshot)
        db_commit = _git_db_commit(runner, snapshot)
        db_archive_sha256 = archive_hasher(snapshot)
        failures = validate_snapshot_identity(
            "advisory_snapshot",
            db_commit=db_commit,
            db_archive_sha256=db_archive_sha256,
        )
        if failures:
            raise ReleasePolicyError(failures)
        db_commit_time = _git_db_commit_timestamp(runner, snapshot)
        db_commit_timestamp_text = _format_utc(db_commit_time)
        advisory_count = _count_advisories(snapshot)
        _validate_advisory_count(advisory_count)
        check_result = _run(
            runner,
            advisory_check_argv(cargo_deny, config_path, root),
            cwd=root,
        )
        _assert_scanned_snapshot(check_result.stderr, snapshot)
        _assert_snapshot_clean(runner, snapshot)

        policy_time = clock()
        fetch_acquired = _fetch_head_mtime(snapshot)
        acquisition_text = _format_utc(fetch_acquired)
        _validate_acquisition_freshness(
            advisory_acquired_at=acquisition_text,
            fetch_acquired=fetch_acquired,
            db_commit_timestamp=db_commit_timestamp_text,
            db_commit_time=db_commit_time,
            policy_time=policy_time,
        )
        result = PolicyRun(
            advisory_source_id=advisory_source_id,
            db_snapshot_basename=snapshot.name,
            db_commit=db_commit,
            db_archive_sha256=db_archive_sha256,
            advisory_count=advisory_count,
            advisory_acquired_at=acquisition_text,
            db_commit_timestamp=db_commit_timestamp_text,
            policy_checked_at=_format_utc(policy_time),
            result="pass",
        )
    except ReleasePolicyError as exc:
        primary_error = exc
    finally:
        cleanup_error: ReleasePolicyError | None = None
        try:
            _cleanup_temp(
                temp_root,
                config_path,
                unlink_path=cleanup_unlink,
                remove_dir=cleanup_rmdir,
            )
        except ReleasePolicyError as exc:
            cleanup_error = exc
        combined_error = _combined_release_policy_error(primary_error, cleanup_error)
        if combined_error is not None:
            raise combined_error
    if result is None:
        raise AssertionError("release advisory policy run did not produce a result")
    return result
