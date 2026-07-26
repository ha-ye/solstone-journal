# SPDX-License-Identifier: AGPL-3.0-only
# Copyright (c) 2026 sol pbc

from __future__ import annotations

import shutil
import subprocess
from pathlib import Path

import pytest


def _repo_root() -> Path:
    return Path(__file__).resolve().parent.parent


def _assert_inside_repo(path: Path, repo_root: Path) -> None:
    resolved = path.resolve()
    assert resolved.is_relative_to(repo_root)


def _tracked_symlinks(*roots: str) -> list[Path]:
    repo_root = _repo_root()
    result = subprocess.run(
        ["git", "ls-files", *roots],
        cwd=repo_root,
        check=True,
        capture_output=True,
        text=True,
    )
    return [
        repo_root / line
        for line in result.stdout.splitlines()
        if line and (repo_root / line).is_symlink()
    ]


def test_journal_skill_references_exist_and_linked():
    repo_root = _repo_root()
    skill_path = repo_root / "solstone" / "talent" / "journal" / "SKILL.md"
    skill_text = skill_path.read_text(encoding="utf-8")
    references = [
        "references/cli.md",
        "references/config.md",
        "references/facets.md",
        "references/captures.md",
        "references/logs.md",
        "references/storage.md",
        "references/commands.md",
    ]

    for rel_path in references:
        ref_path = skill_path.parent / rel_path
        assert ref_path.exists()
        assert ref_path.read_text(encoding="utf-8").strip()
        assert rel_path in skill_text


def test_journal_template_symlinks_resolve_inside_repo():
    repo_root = _repo_root()
    for path in _tracked_symlinks("journal", "tests/fixtures/journal"):
        _assert_inside_repo(path, repo_root)


@pytest.mark.timeout(30)
def test_make_skills_idempotent(tmp_path):
    """The native project installer is idempotent for make skills' target shape."""
    repo_root = _repo_root()
    temp_root = tmp_path / "repo"
    temp_root.mkdir()

    (temp_root / ".git").mkdir()
    shutil.copy2(repo_root / "pyproject.toml", temp_root / "pyproject.toml")
    bin_dir = temp_root / "bin"
    bin_dir.mkdir()
    native = bin_dir / "solstone-core"
    shutil.copy2(repo_root / ".venv" / "bin" / "solstone-core", native)
    (temp_root / "solstone").mkdir()
    shutil.copytree(
        repo_root / "solstone" / "talent",
        temp_root / "solstone" / "talent",
        symlinks=True,
    )
    shutil.copytree(
        repo_root / "solstone" / "apps",
        temp_root / "solstone" / "apps",
        symlinks=True,
    )

    def link_state(root: Path) -> dict[str, tuple[str, int]]:
        return {
            path.relative_to(root).as_posix(): (
                path.readlink().as_posix(),
                path.lstat().st_mtime_ns,
            )
            for path in sorted(root.rglob("*"))
            if path.is_symlink()
        }

    env = {"HOME": str(tmp_path / "home")}

    def install() -> subprocess.CompletedProcess[str]:
        # Deliberately not check=True: a CalledProcessError reports only the
        # exit status in pytest's summary and swallows the binary's own
        # explanation, which is the whole diagnosis. Assert instead, and put
        # stderr in the failure message.
        run = subprocess.run(
            [
                str(native),
                "__solstone_identity=sol",
                "skills",
                "install",
                "--project",
                str(temp_root),
                "--agent",
                "all",
            ],
            cwd=temp_root,
            env=env,
            capture_output=True,
            text=True,
        )
        assert run.returncode == 0, (
            f"sol skills install exited {run.returncode}: {run.stderr.strip()!r}"
        )
        return run

    first_run = install()
    assert first_run.stderr == ""

    first = link_state(temp_root)

    second_run = install()
    assert second_run.stderr == ""

    second = link_state(temp_root)
    assert first == second
    assert (
        temp_root / ".claude" / "skills" / "journal"
    ).readlink().as_posix() == "../../solstone/talent/journal"

    # Skill-discovery contract: claude code looks at <cwd>/.claude/skills/, so
    # after project skill installation the cwd path must resolve to a real
    # SKILL.md whose content starts with frontmatter. Verifying it here against
    # the tmp tree means the test is hermetic — it doesn't depend on the dev box
    # having previously run `make install` or `make skills`.
    discovered = temp_root / ".claude" / "skills" / "journal" / "SKILL.md"
    assert discovered.is_file()
    assert discovered.read_text(encoding="utf-8").startswith("---")
