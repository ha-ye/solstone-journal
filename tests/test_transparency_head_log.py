from __future__ import annotations

import subprocess
from pathlib import Path

import pytest

from scripts.transparency_core import HEAD_LOG, PRODUCT, canonical_json_bytes
from scripts.transparency_head_log import (
    HeadLogRow,
    append_head_row,
    git_witness_status,
    head_log_path,
)


def _git(repo: Path, *args: str) -> None:
    subprocess.run(
        ["git", *args],
        cwd=repo,
        check=True,
        capture_output=True,
        text=True,
    )


def _init_repo(repo: Path) -> None:
    _git(repo, "init", "-q")
    (repo / "README.md").write_text("fixture\n", encoding="utf-8")
    _git(repo, "add", "README.md")
    _git(
        repo,
        "-c",
        "user.name=solstone-test",
        "-c",
        "user.email=solstone@example.invalid",
        "commit",
        "-qm",
        "initial",
    )


def _row(seq: int, *, entry_sha256: str | None = None) -> HeadLogRow:
    return HeadLogRow(
        product=PRODUCT,
        seq=seq,
        version=f"0.0.{seq}",
        entry_sha256=entry_sha256 or str(seq) * 64,
        published_utc=f"2026-07-2{seq}T00:00:00Z",
    )


def test_append_head_row_preserves_prior_bytes(tmp_path: Path) -> None:
    path = head_log_path(tmp_path)
    prior = (
        b'{"seq":1, "version":"0.0.1", "product":"solstone-journal", '
        b'"published_utc":"2026-07-21T00:00:00Z", "entry_sha256":"'
        + b"a" * 64
        + b'"}\n'
    )
    path.write_bytes(prior)
    new_row = _row(2, entry_sha256="b" * 64)

    assert append_head_row(tmp_path, new_row) is True

    expected_new = canonical_json_bytes(new_row.as_dict(), label=HEAD_LOG)
    assert path.read_bytes() == prior + expected_new


@pytest.mark.release
def test_git_witness_status_committed(tmp_path: Path) -> None:
    _init_repo(tmp_path)
    append_head_row(tmp_path, _row(1))
    _git(tmp_path, "add", HEAD_LOG)
    _git(
        tmp_path,
        "-c",
        "user.name=solstone-test",
        "-c",
        "user.email=solstone@example.invalid",
        "commit",
        "-qm",
        "commit head log",
    )

    status = git_witness_status(tmp_path)

    assert status.state == "written-and-committed"


@pytest.mark.release
def test_git_witness_status_tracked_but_modified(tmp_path: Path) -> None:
    _init_repo(tmp_path)
    append_head_row(tmp_path, _row(1))
    _git(tmp_path, "add", HEAD_LOG)
    _git(
        tmp_path,
        "-c",
        "user.name=solstone-test",
        "-c",
        "user.email=solstone@example.invalid",
        "commit",
        "-qm",
        "commit head log",
    )
    append_head_row(tmp_path, _row(2))

    status = git_witness_status(tmp_path)

    assert status.state == "written-uncommitted"


@pytest.mark.release
def test_git_witness_status_untracked(tmp_path: Path) -> None:
    _init_repo(tmp_path)
    append_head_row(tmp_path, _row(1))

    status = git_witness_status(tmp_path)

    assert status.state == "written-untracked"
