# SPDX-License-Identifier: AGPL-3.0-only
# Copyright (c) 2026 sol pbc

from __future__ import annotations

import json
import shutil
import subprocess
from pathlib import Path
from typing import Any

import pytest

from solstone.think.backup import runner


def test_run_restic_builds_safe_argv_and_minimal_env(
    monkeypatch: pytest.MonkeyPatch,
):
    captured: dict[str, Any] = {}

    def fake_run(argv: list[str], **kwargs: Any) -> subprocess.CompletedProcess[str]:
        captured["argv"] = argv
        captured["env"] = kwargs["env"]
        captured["pass_fds"] = kwargs["pass_fds"]
        return subprocess.CompletedProcess(argv, 0, stdout="{}", stderr="")

    monkeypatch.setenv("PATH", "/bin")
    monkeypatch.setenv("HOME", "/home/test")
    monkeypatch.setenv("TMPDIR", "/tmp/test")
    monkeypatch.setenv("UNRELATED_SECRET", "do-not-copy")
    monkeypatch.setattr(runner.subprocess, "run", fake_run)

    result = runner.run_restic(
        ["snapshots"],
        repository="s3:safe-bucket/path",
        password="repo-password",
        restic_path=Path("/usr/bin/restic"),
        backend_env={
            "AWS_ACCESS_KEY_ID": "access-key",
            "AWS_SECRET_ACCESS_KEY": "backend-secret",
        },
        json=True,
        max_repack_size="1G",
    )

    assert result.argv == (
        "/usr/bin/restic",
        "snapshots",
        "--json",
        "--max-repack-size",
        "1G",
    )
    assert "--insecure-tls" not in result.argv
    assert all("repo-password" not in token for token in result.argv)
    assert all("backend-secret" not in token for token in result.argv)
    assert captured["env"] == {
        "PATH": "/bin",
        "HOME": "/home/test",
        "TMPDIR": "/tmp/test",
        "RESTIC_REPOSITORY": "s3:safe-bucket/path",
        "RESTIC_PASSWORD": "repo-password",
        "AWS_ACCESS_KEY_ID": "access-key",
        "AWS_SECRET_ACCESS_KEY": "backend-secret",
    }
    assert captured["pass_fds"] == ()


def test_run_restic_threads_pass_fds(monkeypatch: pytest.MonkeyPatch):
    captured: dict[str, Any] = {}

    def fake_run(argv: list[str], **kwargs: Any) -> subprocess.CompletedProcess[str]:
        captured["argv"] = argv
        captured["pass_fds"] = kwargs["pass_fds"]
        return subprocess.CompletedProcess(argv, 0, stdout="", stderr="")

    monkeypatch.setattr(runner.subprocess, "run", fake_run)

    result = runner.run_restic(
        ["key", "add", "--new-password-file", "/dev/fd/17"],
        repository="s3:safe-bucket/path",
        password="repo-password",
        restic_path=Path("/usr/bin/restic"),
        pass_fds=(17,),
    )

    assert result.returncode == 0
    assert captured["argv"] == [
        "/usr/bin/restic",
        "key",
        "add",
        "--new-password-file",
        "/dev/fd/17",
    ]
    assert captured["pass_fds"] == (17,)


def test_run_restic_scrubs_success_output_and_json(
    monkeypatch: pytest.MonkeyPatch,
):
    secrets = ("repo-password", "backend-secret", "access-key")

    def fake_run(argv: list[str], **kwargs: Any) -> subprocess.CompletedProcess[str]:
        stdout = json.dumps(
            {"message": ("repo-password backend-secret access-key should be hidden")}
        )
        stderr = "stderr has repo-password and backend-secret"
        return subprocess.CompletedProcess(argv, 0, stdout=stdout, stderr=stderr)

    monkeypatch.setattr(runner.subprocess, "run", fake_run)

    result = runner.run_restic(
        ["snapshots"],
        repository="s3:safe-bucket/path",
        password="repo-password",
        restic_path=Path("/usr/bin/restic"),
        backend_env={
            "AWS_ACCESS_KEY_ID": "access-key",
            "AWS_SECRET_ACCESS_KEY": "backend-secret",
        },
        json=True,
    )

    json_text = json.dumps(result.json)
    for secret in secrets:
        assert secret not in result.stdout
        assert secret not in result.stderr
        assert secret not in json_text
        assert all(secret not in token for token in result.argv)
    assert result.json == {
        "message": "[redacted] [redacted] [redacted] should be hidden"
    }


def test_run_restic_returns_scrubbed_nonzero_result(
    monkeypatch: pytest.MonkeyPatch,
):
    secrets = ("repo-password", "backend-secret")

    def fake_run(argv: list[str], **kwargs: Any) -> subprocess.CompletedProcess[str]:
        return subprocess.CompletedProcess(
            argv,
            42,
            stdout="",
            stderr="failed with repo-password and backend-secret",
        )

    monkeypatch.setattr(runner.subprocess, "run", fake_run)

    result = runner.run_restic(
        ["backup", "/tmp/data"],
        repository="s3:safe-bucket/path",
        password="repo-password",
        restic_path=Path("/usr/bin/restic"),
        backend_env={"AWS_SECRET_ACCESS_KEY": "backend-secret"},
    )

    assert result.returncode == 42
    json_text = json.dumps(result.json)
    for secret in secrets:
        assert secret not in result.stdout
        assert secret not in result.stderr
        assert secret not in json_text
        assert all(secret not in token for token in result.argv)
    assert "[redacted]" in result.stderr


def test_empty_backend_values_are_not_scrubbed(
    monkeypatch: pytest.MonkeyPatch,
):
    def fake_run(argv: list[str], **kwargs: Any) -> subprocess.CompletedProcess[str]:
        assert kwargs["env"]["EMPTY"] == ""
        assert "NONE" not in kwargs["env"]
        return subprocess.CompletedProcess(argv, 0, stdout="abc", stderr="")

    monkeypatch.setattr(runner.subprocess, "run", fake_run)

    result = runner.run_restic(
        ["snapshots"],
        repository="s3:safe-bucket/path",
        password="repo-password",
        restic_path=Path("/usr/bin/restic"),
        backend_env={"EMPTY": "", "NONE": None},
    )

    assert result.stdout == "abc"


def test_run_restic_rejects_insecure_tls(monkeypatch: pytest.MonkeyPatch):
    def fail_run(argv: list[str], **kwargs: Any) -> subprocess.CompletedProcess[str]:
        raise AssertionError("subprocess should not be called")

    monkeypatch.setattr(runner.subprocess, "run", fail_run)

    with pytest.raises(RuntimeError, match="--insecure-tls"):
        runner.run_restic(
            ["backup", "--insecure-tls", "/tmp/data"],
            repository="s3:safe-bucket/path",
            password="repo-password",
            restic_path=Path("/usr/bin/restic"),
        )


def test_run_restic_rejects_secret_in_argv(monkeypatch: pytest.MonkeyPatch):
    def fail_run(argv: list[str], **kwargs: Any) -> subprocess.CompletedProcess[str]:
        raise AssertionError("subprocess should not be called")

    monkeypatch.setattr(runner.subprocess, "run", fail_run)

    with pytest.raises(RuntimeError, match="argv contains a secret"):
        runner.run_restic(
            ["backup", "/tmp/repo-password/data"],
            repository="s3:safe-bucket/path",
            password="repo-password",
            restic_path=Path("/usr/bin/restic"),
        )


def test_run_restic_timeout_returns_scrubbed_result(
    monkeypatch: pytest.MonkeyPatch,
):
    def fake_run(argv: list[str], **kwargs: Any) -> subprocess.CompletedProcess[str]:
        raise subprocess.TimeoutExpired(
            argv,
            timeout=1,
            output=b"stdout repo-password backend-secret",
            stderr=b"stderr repo-password backend-secret",
        )

    monkeypatch.setattr(runner.subprocess, "run", fake_run)

    result = runner.run_restic(
        ["backup", "/tmp/data"],
        repository="s3:safe-bucket/path",
        password="repo-password",
        restic_path=Path("/usr/bin/restic"),
        backend_env={"AWS_SECRET_ACCESS_KEY": "backend-secret"},
        timeout=1,
    )

    assert result.returncode == 124
    assert "repo-password" not in result.stdout
    assert "backend-secret" not in result.stdout
    assert "repo-password" not in result.stderr
    assert "backend-secret" not in result.stderr
    assert result.json is None


def test_parse_json_lines_from_scrubbed_stdout(
    monkeypatch: pytest.MonkeyPatch,
):
    def fake_run(argv: list[str], **kwargs: Any) -> subprocess.CompletedProcess[str]:
        stdout = '{"message":"backend-secret"}\n{"message":"ok"}\n'
        return subprocess.CompletedProcess(argv, 0, stdout=stdout, stderr="")

    monkeypatch.setattr(runner.subprocess, "run", fake_run)

    result = runner.run_restic(
        ["snapshots"],
        repository="s3:safe-bucket/path",
        password="repo-password",
        restic_path=Path("/usr/bin/restic"),
        backend_env={"AWS_SECRET_ACCESS_KEY": "backend-secret"},
        json=True,
    )

    assert result.json == [{"message": "[redacted]"}, {"message": "ok"}]


@pytest.mark.parametrize(
    ("returncode", "reason"),
    [
        (3, "incomplete"),
        (10, "repo_missing"),
        (11, "locked"),
        (12, "auth_failed"),
        (124, "timeout"),
        (77, "failed"),
    ],
)
def test_reason_for_returncode(returncode: int, reason: str) -> None:
    assert runner.reason_for_returncode(returncode) == reason


def test_select_summary_from_dict_or_json_lines() -> None:
    assert runner.select_summary({"message_type": "summary", "snapshot_id": "one"}) == {
        "message_type": "summary",
        "snapshot_id": "one",
    }
    assert runner.select_summary(
        [
            {"message_type": "status", "percent_done": 50},
            {"message_type": "summary", "snapshot_id": "two"},
        ]
    ) == {"message_type": "summary", "snapshot_id": "two"}
    assert runner.select_summary({"message_type": "status"}) is None


RESTIC_BIN = shutil.which("restic")


@pytest.mark.skipif(RESTIC_BIN is None, reason="restic is not installed")
def test_run_restic_local_repository_integration(tmp_path: Path):
    restic_path = Path(RESTIC_BIN or "")
    repo = tmp_path / "repo"
    data = tmp_path / "data.txt"
    data.write_text("backup me", encoding="utf-8")

    init_result = runner.run_restic(
        ["init"],
        repository=f"local:{repo}",
        password="test-password",
        restic_path=restic_path,
        timeout=15,
    )
    assert init_result.returncode == 0, init_result.stderr

    backup_result = runner.run_restic(
        ["backup", str(data)],
        repository=f"local:{repo}",
        password="test-password",
        restic_path=restic_path,
        timeout=30,
    )
    assert backup_result.returncode == 0, backup_result.stderr

    snapshots_result = runner.run_restic(
        ["snapshots"],
        repository=f"local:{repo}",
        password="test-password",
        restic_path=restic_path,
        json=True,
        timeout=15,
    )
    assert snapshots_result.returncode == 0, snapshots_result.stderr
    assert snapshots_result.json is not None
