# SPDX-License-Identifier: AGPL-3.0-only
# Copyright (c) 2026 sol pbc

from __future__ import annotations

import os
import subprocess
from collections.abc import Sequence
from datetime import UTC, datetime
from pathlib import Path

import pytest

import scripts.check_rust_release_manifest as checker
import scripts.release_advisory_policy as policy

DB_COMMIT = "a" * 40
DB_ARCHIVE = "b" * 64
SNAPSHOT = "advisory-db-1234567890abcdef"
POLICY_TIME = datetime(2026, 7, 20, 12, 0, tzinfo=UTC)
FETCH_TIME = datetime(2026, 7, 20, 11, 30, tzinfo=UTC)
COMMIT_TIME = "2026-07-17T15:52:38Z"

MALFORMED_DB_COMMIT_CASES = (
    ("short-39", "a" * 39),
    ("short-63", "a" * 63),
    ("long-41", "a" * 41),
    ("long-65", "a" * 65),
    ("uppercase", "A" * 40),
    ("non-hex", "g" * 40),
    ("empty", ""),
    ("whitespace", " " + "a" * 40),
    ("extra-line", "a" * 40 + "\nunexpected\n"),
)
MALFORMED_ARCHIVE_DIGEST_CASES = (
    ("short", "b" * 63),
    ("uppercase", "B" * 64),
    ("non-hex", "g" * 64),
    ("empty", ""),
    ("extra-line", "b" * 64 + "\nunexpected"),
)
MALFORMED_DB_COMMITS = tuple(
    pytest.param(value, id=name) for name, value in MALFORMED_DB_COMMIT_CASES
)
MALFORMED_ARCHIVE_DIGESTS = tuple(
    pytest.param(value, id=name) for name, value in MALFORMED_ARCHIVE_DIGEST_CASES
)


class FakeRunner:
    def __init__(
        self,
        snapshot: Path,
        *,
        status: str | Sequence[str] = "",
        fail_check: bool = False,
        commit_stdout: str = DB_COMMIT + "\n",
        commit_timestamp: str = COMMIT_TIME + "\n",
        top_level: Path | None = None,
        scanned_path: Path | None = None,
        debug_stderr: str | None = None,
    ) -> None:
        self.snapshot = snapshot
        self.status_outputs = list(status) if not isinstance(status, str) else [status]
        self.fail_check = fail_check
        self.commit_stdout = commit_stdout
        self.commit_timestamp = commit_timestamp
        self.top_level = top_level
        self.scanned_path = scanned_path or snapshot
        self.debug_stderr = debug_stderr
        self.events: list[str] = []
        self.config_bytes: bytes | None = None
        self.cargo_cwds: list[Path | None] = []
        self.cargo_argvs: list[list[str]] = []

    def __call__(self, argv, **kwargs) -> subprocess.CompletedProcess[str]:
        command = list(argv)
        if command[0] == "cargo-deny":
            if command[-2:] == ["fetch", "db"]:
                raise AssertionError("cargo-deny fetch subcommand should be gone")
            if command[-2:] == ["check", "advisories"]:
                self.events.append("check")
                self.cargo_cwds.append(kwargs.get("cwd"))
                self.cargo_argvs.append(command)
                self.config_bytes = Path(
                    command[command.index("--config") + 1]
                ).read_bytes()
                if self.fail_check:
                    return subprocess.CompletedProcess(command, 1, "", "denied")
                stderr = self.debug_stderr
                if stderr is None:
                    stderr = (
                        "2026-07-21 14:40:36 [DEBUG] "
                        f"Opening advisory database at '{self.scanned_path}'\n"
                    )
                return subprocess.CompletedProcess(command, 0, "", stderr)
        if command[:2] == ["git", "-C"]:
            git_root = Path(command[2])
            subcommand = command[3:]
            if subcommand == ["rev-parse", "--show-toplevel"]:
                self.events.append("show-toplevel")
                top_level = self.top_level or git_root
                return subprocess.CompletedProcess(command, 0, f"{top_level}\n", "")
            if subcommand[:1] == ["status"]:
                self.events.append("status")
                output = self.status_outputs.pop(0)
                if not self.status_outputs:
                    self.status_outputs.append(output)
                return subprocess.CompletedProcess(command, 0, output, "")
            if subcommand[:2] == ["rev-parse", "--verify"]:
                self.events.append("rev-parse")
                return subprocess.CompletedProcess(command, 0, self.commit_stdout, "")
            if subcommand == ["show", "-s", "--format=%cI", "HEAD"]:
                self.events.append("commit-timestamp")
                return subprocess.CompletedProcess(
                    command, 0, self.commit_timestamp, ""
                )
        raise AssertionError(f"unexpected command: {command}")


def _repo(tmp_path: Path, deny_text: str | None = None) -> Path:
    root = tmp_path / "repo"
    (root / "core").mkdir(parents=True, exist_ok=True)
    (root / "core" / "deny.toml").write_text(
        deny_text or '[licenses]\nallow = ["MIT"]\n',
        encoding="utf-8",
    )
    return root


def _clock(events: list[str] | None = None, value: datetime = POLICY_TIME):
    def now() -> datetime:
        if events is not None:
            events.append("policy-clock")
        return value

    return now


def _write_snapshot(
    db_root: Path,
    *,
    name: str = SNAPSHOT,
    advisory_count: int = 1,
    fetch_time: datetime | None = FETCH_TIME,
) -> Path:
    snapshot = db_root / name
    (snapshot / ".git").mkdir(parents=True)
    for index in range(advisory_count):
        advisory = (
            snapshot / "crates" / f"probe{index}" / f"RUSTSEC-2026-{index:04d}.md"
        )
        advisory.parent.mkdir(parents=True, exist_ok=True)
        advisory.write_text(
            "```toml\n"
            "[advisory]\n"
            f'id = "RUSTSEC-2026-{index:04d}"\n'
            f'package = "probe{index}"\n'
            'date = "2026-01-01"\n'
            'url = "https://example.invalid/RUSTSEC-2026-0001"\n'
            'categories = ["unmaintained"]\n'
            "keywords = []\n\n"
            "[versions]\n"
            "patched = []\n"
            "```\n",
            encoding="utf-8",
        )
    if fetch_time is not None:
        fetch_head = snapshot / ".git" / "FETCH_HEAD"
        fetch_head.write_text("", encoding="utf-8")
        timestamp = fetch_time.timestamp()
        os.utime(fetch_head, (timestamp, timestamp))
    return snapshot


def _db_root(
    tmp_path: Path,
    *,
    advisory_count: int = 1,
    fetch_time: datetime | None = FETCH_TIME,
) -> tuple[Path, Path]:
    root = tmp_path / "db-root"
    root.mkdir()
    (root / "db.lock").write_text("", encoding="utf-8")
    return root, _write_snapshot(
        root,
        advisory_count=advisory_count,
        fetch_time=fetch_time,
    )


def _prepare(
    tmp_path: Path,
    *,
    runner: FakeRunner | None = None,
    db_root: Path | None = None,
    snapshot: Path | None = None,
    clock=None,
    archive: str = DB_ARCHIVE,
    cleanup_unlink=policy._unlink_path,
    cleanup_rmdir=policy._remove_dir,
) -> tuple[policy.PolicyRun, FakeRunner, Path]:
    repo = _repo(tmp_path)
    if db_root is None or snapshot is None:
        db_root, snapshot = _db_root(tmp_path)
    runner = runner or FakeRunner(snapshot)
    result = policy.prepare_policy_run(
        repo,
        advisory_source_id="internal",
        db_urls=("ssh://example.test/db.git",),
        db_root=db_root,
        runner=runner,
        temp_path_factory=lambda label: tmp_path / label,
        clock=clock or _clock(runner.events),
        archive_hasher=lambda observed: (
            runner.events.append(f"archive:{observed.name}") or archive
        ),
        cleanup_unlink=cleanup_unlink,
        cleanup_rmdir=cleanup_rmdir,
    )
    return result, runner, repo


def test_materialized_config_and_advisory_check_argv(tmp_path: Path) -> None:
    result, runner, repo = _prepare(tmp_path)

    assert result.result == "pass"
    assert result.db_snapshot_basename == SNAPSHOT
    assert result.advisory_count == 1
    assert result.advisory_acquired_at == "2026-07-20T11:30:00Z"
    assert result.db_commit_timestamp == COMMIT_TIME
    assert runner.config_bytes is not None
    text = runner.config_bytes.decode("utf-8")
    assert text.startswith('[licenses]\nallow = ["MIT"]\n\n[advisories]\n')
    assert 'db-urls = ["ssh://example.test/db.git"]' in text
    assert "git-fetch-with-cli = true" in text
    assert 'maximum-db-staleness = "PT24H"' in text
    assert "24 hours" not in text

    argv = runner.cargo_argvs[0]
    assert argv == [
        "cargo-deny",
        "--config",
        argv[2],
        "--manifest-path",
        str(repo / "core" / "Cargo.toml"),
        "-L",
        "debug",
        "--locked",
        "--offline",
        "check",
        "advisories",
    ]
    assert "fetch" not in runner.events
    assert runner.events == [
        "show-toplevel",
        "status",
        "rev-parse",
        f"archive:{SNAPSHOT}",
        "commit-timestamp",
        "check",
        "status",
        "policy-clock",
    ]
    assert runner.cargo_cwds == [repo]


def test_core_deny_toml_advisories_table_fails_loudly(tmp_path: Path) -> None:
    db_root, _snapshot = _db_root(tmp_path)
    with pytest.raises(policy.ReleasePolicyError) as exc:
        policy.prepare_policy_run(
            _repo(tmp_path, '[advisories]\ndb-path = "x"\n'),
            advisory_source_id="internal-feed",
            db_urls=("ssh://example.test/advisory-db.git",),
            db_root=db_root,
            runner=FakeRunner(db_root / SNAPSHOT),
            temp_path_factory=lambda label: tmp_path / label,
        )

    assert exc.value.failures[0].error == "core deny.toml already defines advisories"


@pytest.mark.parametrize(
    "source_id,db_urls,error",
    [
        ("", ("ssh://example.test/db.git",), "advisory source id is not a public slug"),
        ("internal", (), "advisory db source is empty"),
        (
            "internal",
            ("https://github.com/RustSec/advisory-db",),
            "advisory db url points at GitHub",
        ),
        (
            "internal",
            ("ssh://mirror.github.com/RustSec/db",),
            "advisory db url points at GitHub",
        ),
    ],
)
def test_empty_and_github_advisory_sources_are_rejected(
    tmp_path: Path,
    source_id: str,
    db_urls: tuple[str, ...],
    error: str,
) -> None:
    with pytest.raises(policy.ReleasePolicyError) as exc:
        policy.prepare_policy_run(
            _repo(tmp_path),
            advisory_source_id=source_id,
            db_urls=db_urls,
            db_root=tmp_path / "db",
            runner=FakeRunner(tmp_path / "db" / SNAPSHOT),
            temp_path_factory=lambda label: tmp_path / label,
        )

    assert any(failure.error == error for failure in exc.value.failures)


def test_snapshot_count_must_be_exactly_one(tmp_path: Path) -> None:
    db_root = tmp_path / "empty-db"
    db_root.mkdir()

    with pytest.raises(policy.ReleasePolicyError) as exc:
        policy.prepare_policy_run(
            _repo(tmp_path),
            advisory_source_id="internal",
            db_urls=("ssh://example.test/db.git",),
            db_root=db_root,
            runner=FakeRunner(db_root / SNAPSHOT),
            temp_path_factory=lambda label: tmp_path / label,
        )
    assert exc.value.failures[0].error == "advisory db snapshot count is invalid"
    assert exc.value.failures[0].actual == "0"

    db_root = tmp_path / "multi-db"
    db_root.mkdir()
    first = _write_snapshot(db_root, name="advisory-db-one")
    _write_snapshot(db_root, name="advisory-db-two")
    with pytest.raises(policy.ReleasePolicyError) as exc:
        policy.prepare_policy_run(
            _repo(tmp_path),
            advisory_source_id="internal",
            db_urls=("ssh://example.test/db.git",),
            db_root=db_root,
            runner=FakeRunner(first),
            temp_path_factory=lambda label: tmp_path / f"multi-{label}",
        )
    assert exc.value.failures[0].error == "advisory db snapshot count is invalid"
    assert exc.value.failures[0].actual == "2"


def test_non_top_level_snapshot_fails_walk_up_check(tmp_path: Path) -> None:
    db_root, snapshot = _db_root(tmp_path)
    runner = FakeRunner(snapshot, top_level=tmp_path)

    with pytest.raises(policy.ReleasePolicyError) as exc:
        _prepare(tmp_path, db_root=db_root, snapshot=snapshot, runner=runner)

    assert (
        exc.value.failures[0].error
        == "advisory db snapshot is not an isolated git checkout"
    )
    assert "check" not in runner.events


def test_scanned_advisory_db_must_match_measured_snapshot(tmp_path: Path) -> None:
    db_root, snapshot = _db_root(tmp_path)
    runner = FakeRunner(snapshot, scanned_path=tmp_path / "other-db")

    with pytest.raises(policy.ReleasePolicyError) as exc:
        _prepare(tmp_path, db_root=db_root, snapshot=snapshot, runner=runner)

    assert (
        exc.value.failures[0].error
        == "cargo-deny scanned a different advisory database"
    )


def test_missing_debug_line_fails_closed(tmp_path: Path) -> None:
    db_root, snapshot = _db_root(tmp_path)
    runner = FakeRunner(snapshot, debug_stderr="debug without database path\n")

    with pytest.raises(policy.ReleasePolicyError) as exc:
        _prepare(tmp_path, db_root=db_root, snapshot=snapshot, runner=runner)

    assert (
        exc.value.failures[0].error
        == "cargo-deny advisory database debug line is missing"
    )


def test_absent_fetch_head_fails_closed(tmp_path: Path) -> None:
    db_root, snapshot = _db_root(tmp_path, fetch_time=None)
    runner = FakeRunner(snapshot)

    with pytest.raises(policy.ReleasePolicyError) as exc:
        _prepare(tmp_path, db_root=db_root, snapshot=snapshot, runner=runner)

    assert exc.value.failures[0].error == "advisory db FETCH_HEAD is missing"


def test_stale_fetch_head_fails(tmp_path: Path) -> None:
    db_root, snapshot = _db_root(
        tmp_path,
        fetch_time=datetime(2026, 7, 18, 11, 59, 59, tzinfo=UTC),
    )

    with pytest.raises(policy.ReleasePolicyError) as exc:
        _prepare(tmp_path, db_root=db_root, snapshot=snapshot)

    assert exc.value.failures[0].error == "advisory fetch time is stale"


def test_over_age_content_fails(tmp_path: Path) -> None:
    db_root, snapshot = _db_root(tmp_path)
    runner = FakeRunner(snapshot, commit_timestamp="2026-07-05T11:59:59Z\n")

    with pytest.raises(policy.ReleasePolicyError) as exc:
        _prepare(tmp_path, db_root=db_root, snapshot=snapshot, runner=runner)

    assert exc.value.failures[0].error == "advisory db content is stale"


def test_zero_advisory_count_fails(tmp_path: Path) -> None:
    db_root, snapshot = _db_root(tmp_path, advisory_count=0)
    runner = FakeRunner(snapshot)

    with pytest.raises(policy.ReleasePolicyError) as exc:
        _prepare(tmp_path, db_root=db_root, snapshot=snapshot, runner=runner)

    assert exc.value.failures[0].error == "advisory db snapshot contains no advisories"
    assert "check" not in runner.events


def test_post_run_dirty_snapshot_fails(tmp_path: Path) -> None:
    db_root, snapshot = _db_root(tmp_path)
    runner = FakeRunner(snapshot, status=("", "?? late-file\n"))

    with pytest.raises(policy.ReleasePolicyError) as exc:
        _prepare(tmp_path, db_root=db_root, snapshot=snapshot, runner=runner)

    assert (
        exc.value.failures[0].error
        == "advisory db snapshot has uncommitted or ignored material"
    )
    assert runner.events.count("status") == 2


def test_fresh_fetch_and_four_day_content_pass(tmp_path: Path) -> None:
    db_root, snapshot = _db_root(tmp_path)
    runner = FakeRunner(snapshot, commit_timestamp="2026-07-16T12:00:00Z\n")

    result, _runner, _repo_path = _prepare(
        tmp_path,
        db_root=db_root,
        snapshot=snapshot,
        runner=runner,
    )

    assert result.advisory_acquired_at == "2026-07-20T11:30:00Z"
    assert result.db_commit_timestamp == "2026-07-16T12:00:00Z"


def test_prepare_policy_run_accepts_sha256_db_commit(tmp_path: Path) -> None:
    db_root, snapshot = _db_root(tmp_path)
    result, _runner, _repo_path = _prepare(
        tmp_path,
        db_root=db_root,
        snapshot=snapshot,
        runner=FakeRunner(snapshot, commit_stdout="a" * 64 + "\n"),
    )

    assert result.db_commit == "a" * 64


@pytest.mark.parametrize("commit_stdout", MALFORMED_DB_COMMITS)
def test_prepare_policy_run_rejects_malformed_db_commit_observation(
    tmp_path: Path,
    commit_stdout: str,
) -> None:
    db_root, snapshot = _db_root(tmp_path)
    runner = FakeRunner(snapshot, commit_stdout=commit_stdout)

    with pytest.raises(policy.ReleasePolicyError) as exc:
        _prepare(tmp_path, db_root=db_root, snapshot=snapshot, runner=runner)

    assert exc.value.failures[0].error == "advisory_snapshot.db_commit is invalid"
    assert exc.value.failures[0].expected == "exactly 40 or 64 lowercase hex characters"
    assert "check" not in runner.events
    if commit_stdout.startswith("A"):
        assert exc.value.failures[0].actual == commit_stdout.removesuffix("\n")


@pytest.mark.parametrize("digest", MALFORMED_ARCHIVE_DIGESTS)
def test_prepare_policy_run_rejects_malformed_archive_digest_observation(
    tmp_path: Path,
    digest: str,
) -> None:
    db_root, snapshot = _db_root(tmp_path)
    runner = FakeRunner(snapshot)

    with pytest.raises(policy.ReleasePolicyError) as exc:
        _prepare(
            tmp_path,
            db_root=db_root,
            snapshot=snapshot,
            runner=runner,
            archive=digest,
        )

    assert (
        exc.value.failures[0].error == "advisory_snapshot.db_archive_sha256 is invalid"
    )
    assert exc.value.failures[0].expected == "exactly 64 lowercase hex characters"
    assert "check" not in runner.events
    if digest.startswith("B"):
        assert exc.value.failures[0].actual == digest


def test_policy_run_constructor_accepts_sha256_db_commit() -> None:
    result = policy.PolicyRun(
        advisory_source_id="internal",
        db_snapshot_basename=SNAPSHOT,
        db_commit="a" * 64,
        db_archive_sha256=DB_ARCHIVE,
        advisory_count=1,
        advisory_acquired_at="2026-07-20T11:30:00Z",
        db_commit_timestamp=COMMIT_TIME,
        policy_checked_at="2026-07-20T12:00:00Z",
        result="pass",
    )

    assert result.db_commit == "a" * 64


@pytest.mark.parametrize(
    ("field", "value", "error"),
    [
        *(
            pytest.param(
                "db_commit",
                value,
                "policy_run.db_commit is invalid",
                id=f"commit-{name}",
            )
            for name, value in MALFORMED_DB_COMMIT_CASES
        ),
        *(
            pytest.param(
                "db_archive_sha256",
                value,
                "policy_run.db_archive_sha256 is invalid",
                id=f"archive-{name}",
            )
            for name, value in MALFORMED_ARCHIVE_DIGEST_CASES
        ),
        pytest.param(
            "db_snapshot_basename",
            "../db",
            "policy_run.db_snapshot_basename is invalid",
            id="snapshot-path",
        ),
        pytest.param(
            "db_snapshot_basename",
            "",
            "policy_run.db_snapshot_basename is invalid",
            id="snapshot-empty",
        ),
        pytest.param(
            "advisory_count",
            0,
            "policy_run.advisory_count is invalid",
            id="count-zero",
        ),
        pytest.param(
            "advisory_count",
            True,
            "policy_run.advisory_count is invalid",
            id="count-bool",
        ),
        pytest.param(
            "db_commit_timestamp",
            "2026-07-19T12:00:00-06:00",
            "policy_run.db_commit_timestamp is invalid",
            id="commit-time-not-normalized",
        ),
    ],
)
def test_policy_run_constructor_rejects_malformed_receipt_identity(
    field: str,
    value: object,
    error: str,
) -> None:
    kwargs = {
        "advisory_source_id": "internal",
        "db_snapshot_basename": SNAPSHOT,
        "db_commit": DB_COMMIT,
        "db_archive_sha256": DB_ARCHIVE,
        "advisory_count": 1,
        "advisory_acquired_at": "2026-07-20T11:30:00Z",
        "db_commit_timestamp": COMMIT_TIME,
        "policy_checked_at": "2026-07-20T12:00:00Z",
        "result": "pass",
    }
    kwargs[field] = value

    with pytest.raises(policy.ReleasePolicyError) as exc:
        policy.PolicyRun(**kwargs)

    assert exc.value.failures[0].error == error
    if isinstance(value, str) and value and value[0].isupper():
        assert exc.value.failures[0].actual == value


def _cleanup_failure(_path: Path) -> None:
    raise OSError(5, "cleanup failed", "/private/tmp/release-advisory-secret")


@pytest.mark.parametrize("primary_failure", (False, True), ids=("success", "primary"))
@pytest.mark.parametrize(
    ("cleanup_name", "cleanup_kwargs"),
    [
        ("unlink", {"cleanup_unlink": _cleanup_failure}),
        ("rmdir", {"cleanup_rmdir": _cleanup_failure}),
    ],
)
def test_cleanup_failures_surface_without_masking_primary_errors(
    tmp_path: Path,
    cleanup_name: str,
    cleanup_kwargs: dict,
    primary_failure: bool,
) -> None:
    db_root, snapshot = _db_root(tmp_path)
    runner = FakeRunner(snapshot, status="?? dirty\n" if primary_failure else "")

    with pytest.raises(policy.ReleasePolicyError) as exc:
        _prepare(
            tmp_path,
            db_root=db_root,
            snapshot=snapshot,
            runner=runner,
            **cleanup_kwargs,
        )

    errors = [failure.error for failure in exc.value.failures]
    if primary_failure:
        assert "advisory db snapshot has uncommitted or ignored material" in errors
    else:
        assert errors == [
            "release advisory cleanup failed during materialized config removal"
        ]
    assert any(error.startswith("release advisory cleanup failed") for error in errors)
    assert (
        checker.validate_public_evidence_text(
            "release advisory cleanup", str(exc.value)
        )
        == []
    )


def _caller_db_inventory(root: Path) -> list[tuple[str, str, bytes]]:
    items: list[tuple[str, str, bytes]] = []
    for path in root.rglob("*"):
        relative = path.relative_to(root).as_posix()
        if path.is_dir():
            items.append((relative, "dir", b""))
        elif path.is_file():
            items.append((relative, "file", path.read_bytes()))
    return sorted(items)


@pytest.mark.parametrize(
    ("primary_failure", "cleanup_kwargs"),
    [
        (False, {}),
        (True, {}),
        (False, {"cleanup_unlink": _cleanup_failure}),
    ],
    ids=("success", "primary-failure", "cleanup-failure"),
)
def test_caller_owned_db_root_is_preserved(
    tmp_path: Path,
    primary_failure: bool,
    cleanup_kwargs: dict,
) -> None:
    db_root, snapshot = _db_root(tmp_path)
    before = _caller_db_inventory(db_root)
    runner = FakeRunner(snapshot, status="?? dirty\n" if primary_failure else "")

    if primary_failure or cleanup_kwargs:
        with pytest.raises(policy.ReleasePolicyError):
            _prepare(
                tmp_path,
                db_root=db_root,
                snapshot=snapshot,
                runner=runner,
                **cleanup_kwargs,
            )
    else:
        _prepare(tmp_path, db_root=db_root, snapshot=snapshot, runner=runner)

    assert _caller_db_inventory(db_root) == before


def test_policy_temps_are_cleaned_without_removing_caller_db(tmp_path: Path) -> None:
    db_root, snapshot = _db_root(tmp_path)
    temp_root = tmp_path / "policy-temp"
    _prepare(
        tmp_path,
        db_root=db_root,
        snapshot=snapshot,
        runner=FakeRunner(snapshot),
        clock=_clock([]),
    )
    assert db_root.is_dir()
    assert not (tmp_path / "advisory-policy").exists()

    with pytest.raises(policy.ReleasePolicyError):
        policy.prepare_policy_run(
            _repo(tmp_path),
            advisory_source_id="internal",
            db_urls=("ssh://example.test/db.git",),
            db_root=db_root,
            runner=FakeRunner(snapshot, status="?? dirty\n"),
            temp_path_factory=lambda _label: temp_root,
            clock=_clock([]),
            archive_hasher=lambda _db: DB_ARCHIVE,
        )
    assert db_root.is_dir()
    assert not temp_root.exists()
