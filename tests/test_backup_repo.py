# SPDX-License-Identifier: AGPL-3.0-only
# Copyright (c) 2026 sol pbc

from __future__ import annotations

import shutil
from pathlib import Path
from typing import Any

import pytest

from solstone.think.backup import repo
from solstone.think.backup.destination import (
    Destination,
    DestinationStatus,
    assemble_backend_env,
    validate_destination,
)
from solstone.think.backup.runner import ResticResult, run_restic

RESTIC_BIN = shutil.which("restic")


def _destination(repository: str = "s3:safe-bucket/path") -> Destination:
    return Destination(
        repository=repository,
        backend="s3",
        credentials={
            "access_key_id": "access-key",
            "secret_access_key": "secret-key",
        },
    )


def _status(reason_code: str) -> DestinationStatus:
    if reason_code == "repo_exists":
        return DestinationStatus(True, True, "repo_exists", "exists")
    if reason_code == "repo_missing":
        return DestinationStatus(True, False, "repo_missing", "missing")
    if reason_code == "auth_failed":
        return DestinationStatus(True, True, "auth_failed", "bad password")
    if reason_code == "locked":
        return DestinationStatus(True, True, "locked", "repository is locked")
    return DestinationStatus(False, False, reason_code, "could not reach")


def test_init_repository_missing_repo_initializes_adds_and_verifies(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    statuses = iter([_status("repo_missing"), _status("repo_exists")])
    calls: list[tuple[str, Any]] = []

    def fake_validate(destination, password, **kwargs):
        calls.append(("validate", password))
        return next(statuses)

    def fake_run_restic(args: list[str], **kwargs: Any) -> ResticResult:
        calls.append(("run", tuple(args), kwargs.get("pass_fds", ())))
        return ResticResult(0, "", "", None, ("restic", *args))

    monkeypatch.setattr(repo, "validate_destination", fake_validate)
    monkeypatch.setattr(repo, "run_restic", fake_run_restic)

    repo.init_repository(
        _destination(),
        daily_key="daily",
        recovery_key="A" * 64,
        restic_path=Path("/usr/bin/restic"),
    )

    assert calls[0] == ("validate", "daily")
    assert calls[1] == ("run", ("init",), ())
    assert calls[2][0] == "run"
    assert calls[2][1][:3] == ("key", "add", "--new-password-file")
    assert calls[2][2]
    assert calls[3] == ("validate", "A" * 64)


def test_init_repository_existing_repo_with_recovery_key_noops(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    statuses = iter([_status("repo_exists"), _status("repo_exists")])
    calls: list[tuple[str, Any]] = []

    def fake_validate(destination, password, **kwargs):
        calls.append(("validate", password))
        return next(statuses)

    def fail_run_restic(args: list[str], **kwargs: Any) -> ResticResult:
        raise AssertionError("restic should not be called")

    monkeypatch.setattr(repo, "validate_destination", fake_validate)
    monkeypatch.setattr(repo, "run_restic", fail_run_restic)

    repo.init_repository(
        _destination(),
        daily_key="daily",
        recovery_key="recovery",
        restic_path=Path("/usr/bin/restic"),
    )

    assert calls == [("validate", "daily"), ("validate", "recovery")]


def test_init_repository_existing_repo_adds_missing_recovery_key(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    statuses = iter(
        [_status("repo_exists"), _status("auth_failed"), _status("repo_exists")]
    )
    calls: list[tuple[str, Any]] = []

    def fake_validate(destination, password, **kwargs):
        calls.append(("validate", password))
        return next(statuses)

    def fake_run_restic(args: list[str], **kwargs: Any) -> ResticResult:
        calls.append(("run", tuple(args)))
        return ResticResult(0, "", "", None, ("restic", *args))

    monkeypatch.setattr(repo, "validate_destination", fake_validate)
    monkeypatch.setattr(repo, "run_restic", fake_run_restic)

    repo.init_repository(
        _destination(),
        daily_key="daily",
        recovery_key="recovery",
        restic_path=Path("/usr/bin/restic"),
    )

    assert calls[0] == ("validate", "daily")
    assert calls[1] == ("validate", "recovery")
    assert calls[2][0] == "run"
    assert calls[2][1][:3] == ("key", "add", "--new-password-file")
    assert calls[3] == ("validate", "recovery")


def test_init_repository_daily_auth_failed_raises_conflict(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(
        repo,
        "validate_destination",
        lambda *args, **kwargs: _status("auth_failed"),
    )

    def fail_run_restic(args: list[str], **kwargs: Any) -> ResticResult:
        raise AssertionError("restic should not be called")

    monkeypatch.setattr(repo, "run_restic", fail_run_restic)

    with pytest.raises(RuntimeError, match="configured daily key"):
        repo.init_repository(
            _destination(),
            daily_key="daily",
            recovery_key="recovery",
            restic_path=Path("/usr/bin/restic"),
        )


@pytest.mark.parametrize("reason_code", ["locked", "timeout", "unreachable"])
def test_init_repository_unavailable_status_raises_without_restic(
    monkeypatch: pytest.MonkeyPatch,
    reason_code: str,
) -> None:
    monkeypatch.setattr(
        repo,
        "validate_destination",
        lambda *args, **kwargs: _status(reason_code),
    )

    def fail_run_restic(args: list[str], **kwargs: Any) -> ResticResult:
        raise AssertionError("restic should not be called")

    monkeypatch.setattr(repo, "run_restic", fail_run_restic)

    with pytest.raises(RuntimeError):
        repo.init_repository(
            _destination(),
            daily_key="daily",
            recovery_key="recovery",
            restic_path=Path("/usr/bin/restic"),
        )


def test_add_recovery_key_raises_sanitized_error(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    def fake_run_restic(args: list[str], **kwargs: Any) -> ResticResult:
        return ResticResult(
            42,
            "",
            "presigned-url-signature",
            None,
            ("restic", *args),
        )

    monkeypatch.setattr(repo, "run_restic", fake_run_restic)

    with pytest.raises(RuntimeError) as exc_info:
        repo._add_recovery_key(
            _destination(),
            daily_key="daily",
            recovery_key="recovery",
            restic_path=Path("/usr/bin/restic"),
        )

    assert str(exc_info.value) == "restic key add failed with returncode 42"


@pytest.mark.skipif(RESTIC_BIN is None, reason="restic is not installed")
def test_init_and_add_recovery_key_local_repository_integration(tmp_path: Path) -> None:
    restic_path = Path(RESTIC_BIN or "")
    destination = _destination(f"local:{tmp_path / 'repo'}")

    init_result = run_restic(
        ["init"],
        repository=destination.repository,
        password="daily-password",
        restic_path=restic_path,
        backend_env=assemble_backend_env(destination),
        timeout=15,
    )
    assert init_result.returncode == 0, init_result.stderr

    repo._add_recovery_key(
        destination,
        daily_key="daily-password",
        recovery_key="recovery-password",
        restic_path=restic_path,
        timeout=15,
    )

    daily_status = validate_destination(
        destination,
        "daily-password",
        restic_path=restic_path,
        timeout=15,
    )
    recovery_status = validate_destination(
        destination,
        "recovery-password",
        restic_path=restic_path,
        timeout=15,
    )

    assert daily_status.reason_code == "repo_exists"
    assert recovery_status.reason_code == "repo_exists"
