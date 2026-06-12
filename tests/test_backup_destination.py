# SPDX-License-Identifier: AGPL-3.0-only
# Copyright (c) 2026 sol pbc

from __future__ import annotations

import json
import logging
from pathlib import Path
from typing import Any

import pytest

from solstone.think.backup import destination
from solstone.think.backup.destination import (
    Destination,
    assemble_backend_env,
    validate_destination,
)
from solstone.think.backup.runner import ResticResult


def test_assemble_backend_env_s3() -> None:
    dest = Destination(
        repository="s3:s3.us-east-1.amazonaws.com/bucket/path",
        backend="s3",
        credentials={
            "access_key_id": "access-key",
            "secret_access_key": "secret-key",
        },
    )

    assert assemble_backend_env(dest) == {
        "AWS_ACCESS_KEY_ID": "access-key",
        "AWS_SECRET_ACCESS_KEY": "secret-key",
    }


def test_assemble_backend_env_b2() -> None:
    dest = Destination(
        repository="b2:bucket:path",
        backend="b2",
        credentials={
            "account_id": "account-id",
            "account_key": "account-key",
        },
    )

    assert assemble_backend_env(dest) == {
        "B2_ACCOUNT_ID": "account-id",
        "B2_ACCOUNT_KEY": "account-key",
    }


def test_assemble_backend_env_rejects_unknown_backend() -> None:
    dest = Destination(repository="repo", backend="unknown", credentials={})

    with pytest.raises(ValueError, match="unsupported backup backend"):
        assemble_backend_env(dest)


def test_assemble_backend_env_rejects_missing_credentials() -> None:
    dest = Destination(
        repository="s3:s3.us-east-1.amazonaws.com/bucket/path",
        backend="s3",
        credentials={"access_key_id": "access-key"},
    )

    with pytest.raises(KeyError, match="secret_access_key"):
        assemble_backend_env(dest)


@pytest.mark.parametrize(
    ("returncode", "reason_code", "reachable", "repo_exists", "message"),
    [
        (0, "repo_exists", True, True, "backup repository is reachable"),
        (
            10,
            "repo_missing",
            True,
            False,
            "backup destination is reachable and needs setup",
        ),
        (11, "locked", True, True, "repository is locked; try again shortly"),
        (12, "auth_failed", True, True, "repository password was rejected"),
        (124, "timeout", False, False, "could not reach the backup destination"),
        (77, "unreachable", False, False, "could not reach the backup destination"),
    ],
)
def test_validate_destination_maps_sanitized_status(
    monkeypatch: pytest.MonkeyPatch,
    caplog: pytest.LogCaptureFixture,
    returncode: int,
    reason_code: str,
    reachable: bool,
    repo_exists: bool,
    message: str,
) -> None:
    raw_secret = "presigned-url-signature"

    def fake_run_restic(args: list[str], **kwargs: Any) -> ResticResult:
        return ResticResult(
            returncode=returncode,
            stdout=f"raw stdout {raw_secret}",
            stderr=f"raw stderr {raw_secret}",
            json=None,
            argv=("restic", *args),
        )

    monkeypatch.setattr(destination, "run_restic", fake_run_restic)
    caplog.set_level(logging.DEBUG, logger="solstone.backup.destination")
    dest = Destination(
        repository="s3:safe-bucket/path",
        backend="s3",
        credentials={
            "access_key_id": "access-key",
            "secret_access_key": "secret-key",
        },
    )

    status = validate_destination(
        dest,
        "repo-password",
        restic_path=Path("/usr/bin/restic"),
    )

    assert status.reason_code == reason_code
    assert status.reachable is reachable
    assert status.repo_exists is repo_exists
    assert status.message == message
    serialized = json.dumps(status.__dict__)
    assert raw_secret not in serialized
    assert raw_secret not in caplog.text


def test_validate_destination_passes_repo_and_creds_separately(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    captured: dict[str, Any] = {}

    def fake_run_restic(args: list[str], **kwargs: Any) -> ResticResult:
        captured["args"] = args
        captured.update(kwargs)
        return ResticResult(
            returncode=0,
            stdout="",
            stderr="",
            json=None,
            argv=("restic", *args),
        )

    monkeypatch.setattr(destination, "run_restic", fake_run_restic)
    dest = Destination(
        repository="s3:https://account.r2.cloudflarestorage.com/bucket/path",
        backend="s3",
        credentials={
            "access_key_id": "access-key",
            "secret_access_key": "secret-key",
        },
    )

    validate_destination(dest, "repo-password", restic_path=Path("/usr/bin/restic"))

    assert captured["args"] == ["cat", "config"]
    assert captured["repository"] == dest.repository
    assert "access-key" not in captured["repository"]
    assert "secret-key" not in captured["repository"]
    assert captured["backend_env"] == {
        "AWS_ACCESS_KEY_ID": "access-key",
        "AWS_SECRET_ACCESS_KEY": "secret-key",
    }
