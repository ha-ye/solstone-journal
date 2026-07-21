# SPDX-License-Identifier: AGPL-3.0-only
# Copyright (c) 2026 sol pbc

from __future__ import annotations

import subprocess
from datetime import UTC, datetime
from pathlib import Path

import pytest

import scripts.release_advisory_policy as policy


class FakeRunner:
    def __init__(self, *, status: str = "", fail_check: bool = False) -> None:
        self.status = status
        self.fail_check = fail_check
        self.events: list[str] = []
        self.config_bytes: bytes | None = None
        self.cargo_cwds: list[Path | None] = []

    def __call__(self, argv, **kwargs) -> subprocess.CompletedProcess[str]:
        command = list(argv)
        if command[0] == "cargo-deny" and command[-2:] == ["fetch", "db"]:
            self.events.append("fetch")
            self.cargo_cwds.append(kwargs.get("cwd"))
            self.config_bytes = Path(
                command[command.index("--config") + 1]
            ).read_bytes()
            return subprocess.CompletedProcess(command, 0, "", "")
        if command[0] == "cargo-deny" and command[-2:] == ["check", "advisories"]:
            self.events.append("check")
            self.cargo_cwds.append(kwargs.get("cwd"))
            self.config_bytes = Path(
                command[command.index("--config") + 1]
            ).read_bytes()
            assert "--locked" in command
            assert "--offline" in command
            if self.fail_check:
                return subprocess.CompletedProcess(command, 1, "", "denied")
            return subprocess.CompletedProcess(command, 0, "", "")
        if command[:3] == ["git", "-C", command[2]]:
            subcommand = command[3:]
            if subcommand[:1] == ["status"]:
                self.events.append("status")
                return subprocess.CompletedProcess(command, 0, self.status, "")
            if subcommand[:2] == ["rev-parse", "--verify"]:
                self.events.append("rev-parse")
                return subprocess.CompletedProcess(command, 0, "a" * 40 + "\n", "")
        raise AssertionError(f"unexpected command: {command}")


def _repo(tmp_path: Path, deny_text: str | None = None) -> Path:
    root = tmp_path / "repo"
    (root / "core").mkdir(parents=True, exist_ok=True)
    (root / "core" / "deny.toml").write_text(
        deny_text or '[licenses]\nallow = ["MIT"]\n',
        encoding="utf-8",
    )
    return root


class ClockSequence:
    def __init__(self, events: list[str], *values: tuple[str, datetime]) -> None:
        self.events = events
        self.values = list(values)

    def __call__(self) -> datetime:
        label, value = self.values.pop(0)
        self.events.append(label)
        return value


def _clock(events: list[str]):
    def now() -> datetime:
        events.append("policy-clock")
        return datetime(2026, 7, 20, 12, 0, tzinfo=UTC)

    return now


def test_materialized_config_appends_advisories_and_replaces_config(
    tmp_path: Path,
) -> None:
    runner = FakeRunner()
    events = runner.events
    repo = _repo(tmp_path)

    result = policy.prepare_policy_run(
        repo,
        advisory_source_id="internal-feed",
        db_urls=("ssh://example.test/advisory-db.git",),
        mode="refresh-once",
        runner=runner,
        temp_path_factory=lambda label: tmp_path / label,
        clock=ClockSequence(
            events,
            ("acquisition-clock", datetime(2026, 7, 20, 11, 30, tzinfo=UTC)),
            ("policy-clock", datetime(2026, 7, 20, 12, 0, tzinfo=UTC)),
        ),
        archive_hasher=lambda _db: events.append("archive") or "b" * 64,
    )

    assert result.result == "pass"
    assert result.advisory_acquired_at == "2026-07-20T11:30:00Z"
    assert runner.config_bytes is not None
    text = runner.config_bytes.decode("utf-8")
    assert text.startswith('[licenses]\nallow = ["MIT"]\n\n[advisories]\n')
    assert 'db-urls = ["ssh://example.test/advisory-db.git"]' in text
    assert "git-fetch-with-cli = true" in text
    assert 'maximum-db-staleness = "24 hours"' in text
    assert events == [
        "fetch",
        "status",
        "rev-parse",
        "archive",
        "acquisition-clock",
        "check",
        "policy-clock",
    ]
    assert runner.cargo_cwds == [repo, repo]


def test_core_deny_toml_advisories_table_fails_loudly(tmp_path: Path) -> None:
    with pytest.raises(policy.ReleasePolicyError) as exc:
        policy.prepare_policy_run(
            _repo(tmp_path, '[advisories]\ndb-path = "x"\n'),
            advisory_source_id="internal-feed",
            db_urls=("ssh://example.test/advisory-db.git",),
            mode="caller-provisioned",
            advisory_acquired_at="2026-07-20T11:00:00Z",
            db_root=tmp_path / "db",
            runner=FakeRunner(),
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
            mode="caller-provisioned",
            advisory_acquired_at="2026-07-20T11:00:00Z",
            db_root=tmp_path / "db",
            runner=FakeRunner(),
            temp_path_factory=lambda label: tmp_path / label,
        )

    assert any(failure.error == error for failure in exc.value.failures)


def test_caller_provisioned_cache_requires_clean_including_ignored(
    tmp_path: Path,
) -> None:
    with pytest.raises(policy.ReleasePolicyError) as exc:
        policy.prepare_policy_run(
            _repo(tmp_path),
            advisory_source_id="internal",
            db_urls=("ssh://example.test/db.git",),
            mode="caller-provisioned",
            advisory_acquired_at="2026-07-20T11:00:00Z",
            db_root=tmp_path / "db",
            runner=FakeRunner(status="!! ignored\n?? untracked\n"),
            temp_path_factory=lambda label: tmp_path / label,
        )

    assert (
        exc.value.failures[0].error == "advisory db has uncommitted or ignored material"
    )


def test_refresh_clock_order_and_policy_before_acquisition_fails(
    tmp_path: Path,
) -> None:
    runner = FakeRunner()

    result = policy.prepare_policy_run(
        _repo(tmp_path),
        advisory_source_id="internal",
        db_urls=("ssh://example.test/db.git",),
        mode="refresh-once",
        runner=runner,
        temp_path_factory=lambda label: tmp_path / label,
        clock=ClockSequence(
            runner.events,
            ("acquisition-clock", datetime(2026, 7, 20, 12, 0, tzinfo=UTC)),
            ("policy-clock", datetime(2026, 7, 20, 12, 1, tzinfo=UTC)),
        ),
        archive_hasher=lambda _db: runner.events.append("archive") or "b" * 64,
    )

    assert result.advisory_acquired_at == "2026-07-20T12:00:00Z"
    assert result.policy_checked_at == "2026-07-20T12:01:00Z"
    assert runner.events.index("acquisition-clock") < runner.events.index("check")
    assert runner.events.index("check") < runner.events.index("policy-clock")

    with pytest.raises(policy.ReleasePolicyError) as exc:
        policy.prepare_policy_run(
            _repo(tmp_path),
            advisory_source_id="internal",
            db_urls=("ssh://example.test/db.git",),
            mode="refresh-once",
            runner=FakeRunner(),
            temp_path_factory=lambda label: tmp_path / f"early-{label}",
            clock=ClockSequence(
                [],
                ("acquisition-clock", datetime(2026, 7, 20, 12, 0, tzinfo=UTC)),
                ("policy-clock", datetime(2026, 7, 20, 11, 59, tzinfo=UTC)),
            ),
            archive_hasher=lambda _db: "b" * 64,
        )

    assert exc.value.failures[0].error == "advisory acquisition time is in the future"


def test_stale_and_future_caller_acquisition_times_fail(tmp_path: Path) -> None:
    for acquired, expected_error in (
        ("2026-07-18T11:59:59Z", "advisory acquisition time is stale"),
        ("2026-07-20T12:00:01Z", "advisory acquisition time is in the future"),
    ):
        with pytest.raises(policy.ReleasePolicyError) as exc:
            policy.prepare_policy_run(
                _repo(tmp_path),
                advisory_source_id="internal",
                db_urls=("ssh://example.test/db.git",),
                mode="caller-provisioned",
                advisory_acquired_at=acquired,
                db_root=tmp_path / "caller-db",
                runner=FakeRunner(),
                temp_path_factory=lambda label: tmp_path / f"{acquired}-{label}",
                clock=_clock([]),
                archive_hasher=lambda _db: "b" * 64,
            )
        assert exc.value.failures[0].error == expected_error


def test_old_commit_with_fresh_acquisition_passes(tmp_path: Path) -> None:
    runner = FakeRunner()

    result = policy.prepare_policy_run(
        _repo(tmp_path),
        advisory_source_id="internal",
        db_urls=("ssh://example.test/db.git",),
        mode="caller-provisioned",
        advisory_acquired_at="2026-07-20T11:30:00Z",
        db_root=tmp_path / "caller-db",
        runner=runner,
        temp_path_factory=lambda label: tmp_path / label,
        clock=_clock(runner.events),
        archive_hasher=lambda _db: runner.events.append("archive") or "b" * 64,
    )

    assert result.db_commit == "a" * 40
    assert result.advisory_acquired_at == "2026-07-20T11:30:00Z"
    assert "log" not in runner.events


def test_caller_timestamp_is_preserved_and_required(tmp_path: Path) -> None:
    exact = "2026-07-20T11:30:00+00:00"
    result = policy.prepare_policy_run(
        _repo(tmp_path),
        advisory_source_id="internal",
        db_urls=("ssh://example.test/db.git",),
        mode="caller-provisioned",
        advisory_acquired_at=exact,
        db_root=tmp_path / "caller-db",
        runner=FakeRunner(),
        temp_path_factory=lambda label: tmp_path / label,
        clock=_clock([]),
        archive_hasher=lambda _db: "b" * 64,
    )
    assert result.advisory_acquired_at == exact

    for value, error in (
        (None, "caller-provisioned advisory mode has no acquisition time"),
        ("not-a-time", "advisory acquisition time is not RFC3339"),
    ):
        with pytest.raises(policy.ReleasePolicyError) as exc:
            policy.prepare_policy_run(
                _repo(tmp_path),
                advisory_source_id="internal",
                db_urls=("ssh://example.test/db.git",),
                mode="caller-provisioned",
                advisory_acquired_at=value,
                db_root=tmp_path / "caller-db",
                runner=FakeRunner(),
                temp_path_factory=lambda label: tmp_path / f"{value}-{label}",
            )
        assert exc.value.failures[0].error == error


def test_policy_temps_are_cleaned_without_removing_caller_db(tmp_path: Path) -> None:
    refresh_root = tmp_path / "refresh-temp"
    policy.prepare_policy_run(
        _repo(tmp_path),
        advisory_source_id="internal",
        db_urls=("ssh://example.test/db.git",),
        mode="refresh-once",
        runner=FakeRunner(),
        temp_path_factory=lambda _label: refresh_root,
        clock=ClockSequence(
            [],
            ("acquisition-clock", datetime(2026, 7, 20, 11, 30, tzinfo=UTC)),
            ("policy-clock", datetime(2026, 7, 20, 12, 0, tzinfo=UTC)),
        ),
        archive_hasher=lambda _db: "b" * 64,
    )
    assert not refresh_root.exists()

    failed_refresh_root = tmp_path / "failed-refresh-temp"
    with pytest.raises(policy.ReleasePolicyError):
        policy.prepare_policy_run(
            _repo(tmp_path),
            advisory_source_id="internal",
            db_urls=("ssh://example.test/db.git",),
            mode="refresh-once",
            runner=FakeRunner(status="?? dirty\n"),
            temp_path_factory=lambda _label: failed_refresh_root,
            clock=ClockSequence(
                [],
                ("acquisition-clock", datetime(2026, 7, 20, 11, 30, tzinfo=UTC)),
                ("policy-clock", datetime(2026, 7, 20, 12, 0, tzinfo=UTC)),
            ),
            archive_hasher=lambda _db: "b" * 64,
        )
    assert not failed_refresh_root.exists()

    caller_db = tmp_path / "caller-owned-db"
    caller_db.mkdir()
    caller_temp = tmp_path / "caller-temp"
    policy.prepare_policy_run(
        _repo(tmp_path),
        advisory_source_id="internal",
        db_urls=("ssh://example.test/db.git",),
        mode="caller-provisioned",
        advisory_acquired_at="2026-07-20T11:30:00Z",
        db_root=caller_db,
        runner=FakeRunner(),
        temp_path_factory=lambda _label: caller_temp,
        clock=_clock([]),
        archive_hasher=lambda _db: "b" * 64,
    )
    assert caller_db.is_dir()
    assert not caller_temp.exists()

    with pytest.raises(policy.ReleasePolicyError):
        policy.prepare_policy_run(
            _repo(tmp_path),
            advisory_source_id="internal",
            db_urls=("ssh://example.test/db.git",),
            mode="caller-provisioned",
            advisory_acquired_at="2026-07-20T11:30:00Z",
            db_root=caller_db,
            runner=FakeRunner(status="?? dirty\n"),
            temp_path_factory=lambda _label: caller_temp,
            clock=_clock([]),
            archive_hasher=lambda _db: "b" * 64,
        )
    assert caller_db.is_dir()
    assert not caller_temp.exists()
