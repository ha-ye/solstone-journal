# SPDX-License-Identifier: AGPL-3.0-only
# Copyright (c) 2026 sol pbc
from __future__ import annotations

import hashlib
import subprocess
import sys
from pathlib import Path

import pytest

import scripts.check_native_sol_python_manifest as manifest


def _git(repo: Path, *args: str) -> str:
    result = subprocess.run(
        ["git", *args],
        cwd=repo,
        check=True,
        capture_output=True,
        text=True,
    )
    return result.stdout.strip()


def _fixture_tree(repo: Path, files: dict[str, str]) -> tuple[str, dict[str, str]]:
    repo.mkdir()
    _git(repo, "init")
    blobs: dict[str, str] = {}
    for rel_path, text in files.items():
        path = repo / rel_path
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(text, encoding="utf-8")
        blobs[rel_path] = _git(repo, "hash-object", str(path))
    _git(repo, "add", *sorted(files))
    tree = _git(repo, "write-tree")
    return tree, blobs


def _digest(blobs: dict[str, str]) -> str:
    return hashlib.sha256(
        "".join(f"{path}\t{blob}\n" for path, blob in sorted(blobs.items())).encode(
            "utf-8"
        )
    ).hexdigest()


def _patch_manifest(
    monkeypatch: pytest.MonkeyPatch,
    *,
    repo: Path,
    tree: str,
    expected_blobs: dict[str, str],
) -> None:
    monkeypatch.setattr(manifest, "REPO_ROOT", repo)
    monkeypatch.setattr(manifest, "PRE_CUTOVER_COMMIT", tree)
    monkeypatch.setattr(manifest, "EXPECTED_BLOBS", dict(expected_blobs))
    monkeypatch.setattr(manifest, "EXPECTED_SHA256", _digest(expected_blobs))


def _run_manifest_main() -> int:
    try:
        return manifest.main()
    except Exception as error:
        print(error, file=sys.stderr)
        return 1


def test_manifest_main_fails_visibly_when_baseline_path_is_missing(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    repo = tmp_path / "repo"
    tree, blobs = _fixture_tree(repo, {"present.py": "print('present')\n"})
    expected_blobs = {**blobs, "missing.py": "0" * 40}
    _patch_manifest(
        monkeypatch,
        repo=repo,
        tree=tree,
        expected_blobs=expected_blobs,
    )

    exit_code = _run_manifest_main()

    captured = capsys.readouterr()
    assert exit_code == 1
    assert "git ls-tree returned 1 entries; missing: missing.py" in captured.err


def test_manifest_main_fails_visibly_when_expected_blob_drifts(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    repo = tmp_path / "repo"
    tree, blobs = _fixture_tree(repo, {"present.py": "print('present')\n"})
    expected_blobs = dict(blobs)
    expected_blobs["present.py"] = "0" * 40
    _patch_manifest(
        monkeypatch,
        repo=repo,
        tree=tree,
        expected_blobs=expected_blobs,
    )

    exit_code = _run_manifest_main()

    captured = capsys.readouterr()
    assert exit_code == 1
    assert "native sol Python manifest blob drifted" in captured.err
    assert (
        "present.py: expected 0000000000000000000000000000000000000000" in captured.err
    )
