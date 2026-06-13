# SPDX-License-Identifier: AGPL-3.0-only
# Copyright (c) 2026 sol pbc

from __future__ import annotations

import os
import plistlib
from pathlib import Path
from types import SimpleNamespace

import pytest

from solstone.think import install_guard


@pytest.fixture
def doctor():
    from solstone.think import doctor as doctor_module

    return doctor_module


@pytest.fixture
def home_root(monkeypatch, tmp_path):
    home = tmp_path / "home"
    home.mkdir()
    monkeypatch.setattr(Path, "home", classmethod(lambda cls: home))
    return home


def args(doctor):
    return doctor.Args(verbose=False, json=False, jsonl=False, port=5015)


def make_repo(tmp_path: Path, *, worktree: bool = False) -> Path:
    repo = tmp_path / "repo"
    repo.mkdir()
    if worktree:
        (repo / ".git").write_text("gitdir: /tmp/worktree\n", encoding="utf-8")
    else:
        (repo / ".git").mkdir()
    return repo


def make_alias(home_root: Path, binary: str, target: Path | str) -> Path:
    alias = home_root / ".local" / "bin" / binary
    alias.parent.mkdir(parents=True, exist_ok=True)
    alias.symlink_to(target)
    return alias


def make_existing_target(path: Path) -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("", encoding="utf-8")
    return path


def patch_alias_absent(doctor, monkeypatch):
    monkeypatch.setattr(
        doctor,
        "import_install_guard",
        lambda: (install_guard.AliasState, install_guard.check_alias),
    )


def tree_snapshot(root: Path) -> list[tuple[str, str, str]]:
    snapshot: list[tuple[str, str, str]] = []
    for path in sorted(root.rglob("*")):
        rel = path.relative_to(root).as_posix()
        if path.is_symlink():
            snapshot.append((rel, "symlink", os.readlink(path)))
        elif path.is_file():
            snapshot.append((rel, "file", path.read_text(encoding="utf-8")))
        elif path.is_dir():
            snapshot.append((rel, "dir", ""))
    return snapshot


def install_router_skill_links(doctor, journal: Path) -> None:
    sources = doctor.skills_cli.discover_project_sources(doctor.ROOT)
    for rel_dir in [Path(".claude/skills"), Path(".agents/skills")]:
        skills_dir = journal / rel_dir
        skills_dir.mkdir(parents=True)
        for source in sources:
            link = skills_dir / source.name
            link.symlink_to(os.path.relpath(source, skills_dir))


def test_service_running_ok(doctor, monkeypatch):
    monkeypatch.setattr(doctor, "service_is_installed", lambda: True)
    monkeypatch.setattr(doctor, "fetch_supervisor_status", lambda: {"crashed": []})

    result = doctor.service_running_check(args(doctor))

    assert result.status == "ok"
    assert result.detail == "journal service is running"


def test_service_running_stopped_warns(doctor, monkeypatch):
    monkeypatch.setattr(doctor, "service_is_installed", lambda: True)
    monkeypatch.setattr(doctor, "fetch_supervisor_status", lambda: None)
    monkeypatch.setattr(doctor, "service_is_failed", lambda: False)

    result = doctor.service_running_check(args(doctor))

    assert result.status == "warn"
    assert result.detail == "service installed but not running"
    assert result.fix == "run journal service start"


def test_service_running_failed_unit_fails(doctor, monkeypatch):
    monkeypatch.setattr(doctor, "service_is_installed", lambda: True)
    monkeypatch.setattr(doctor, "fetch_supervisor_status", lambda: None)
    monkeypatch.setattr(doctor, "service_is_failed", lambda: True)

    result = doctor.service_running_check(args(doctor))

    assert result.status == "fail"
    assert result.detail == "journal service unit is failed"


def test_service_running_crash_loop_fails(doctor, monkeypatch):
    monkeypatch.setattr(doctor, "service_is_installed", lambda: True)
    monkeypatch.setattr(
        doctor,
        "fetch_supervisor_status",
        lambda: {"crashed": [{"name": "cortex", "restart_attempts": 3}]},
    )

    result = doctor.service_running_check(args(doctor))

    assert result.status == "fail"
    assert result.detail == "crash-loop: cortex (3 restart attempts)"
    assert result.fix == "run journal service logs"


def test_service_identity_not_installed_skips(doctor, monkeypatch):
    monkeypatch.setattr(
        doctor,
        "check_service_target_identity",
        lambda: SimpleNamespace(
            installed=False,
            target="",
            matches_current_install=False,
            detail="service not installed",
        ),
    )

    result = doctor.service_identity_check(args(doctor))

    assert result.status == "skip"
    assert result.detail == "no local journal service"


def test_service_identity_malformed_fails(doctor, monkeypatch):
    monkeypatch.setattr(
        doctor,
        "check_service_target_identity",
        lambda: SimpleNamespace(
            installed=True,
            target="",
            matches_current_install=False,
            detail="service config invalid",
        ),
    )

    result = doctor.service_identity_check(args(doctor))

    assert result.status == "fail"
    assert result.detail == "service config invalid"
    assert result.fix == "run journal setup to reinstall the service"


def test_service_identity_mismatch_fails_with_force_fix(doctor, monkeypatch):
    monkeypatch.setattr(
        doctor,
        "check_service_target_identity",
        lambda: SimpleNamespace(
            installed=True,
            target="/tmp/old/journal",
            matches_current_install=False,
            detail="service target mismatch",
        ),
    )

    result = doctor.service_identity_check(args(doctor))

    assert result.status == "fail"
    assert "journal setup --force" in (result.fix or "")


def test_service_identity_match_ok(doctor, monkeypatch):
    monkeypatch.setattr(
        doctor,
        "check_service_target_identity",
        lambda: SimpleNamespace(
            installed=True,
            target="/tmp/current/journal",
            matches_current_install=True,
            detail="service target matches current install",
        ),
    )

    result = doctor.service_identity_check(args(doctor))

    assert result.status == "ok"
    assert result.detail == "service target matches current install"


def test_role_skip_without_local_journal(doctor, monkeypatch, tmp_path, home_root):
    journal = tmp_path / "missing-journal"
    monkeypatch.setattr(doctor, "get_journal_info", lambda: (str(journal), "env"))
    monkeypatch.setattr(doctor, "service_is_installed", lambda: False)
    monkeypatch.setattr(
        doctor,
        "check_journal_sync",
        lambda: pytest.fail("journal_sync should be role-skipped"),
    )
    patch_alias_absent(doctor, monkeypatch)
    monkeypatch.setattr(doctor, "ROOT", make_repo(tmp_path))

    results = doctor.run_checks(args(doctor), checks=doctor.JOURNAL_CHECKS)
    by_name = {result.name: result for result in results}

    assert by_name["journal_dir_writable"].status == "skip"
    assert by_name["journal_sync"].status == "skip"
    assert by_name["service_identity"].status == "skip"
    assert by_name["service_running"].status == "skip"
    assert by_name["skill_state"].status == "skip"
    assert by_name["disk_space"].status in {"ok", "warn"}
    assert by_name["config_dir_readable"].status == "ok"
    assert by_name["feature:pdf"].status in {"ok", "warn"}
    assert by_name["feature:whisper"].status in {"ok", "warn"}


def test_skill_state_no_local_journal_skips(doctor, monkeypatch, tmp_path):
    journal = tmp_path / "missing-journal"
    monkeypatch.setattr(doctor, "get_journal_info", lambda: (str(journal), "env"))
    monkeypatch.setattr(doctor, "is_packaged_install", lambda: False)

    result = doctor.skill_state_check(args(doctor))

    assert result.status == "skip"
    assert result.detail == "no local journal"


def test_skill_state_current_router_links_ok(doctor, monkeypatch, tmp_path):
    journal = tmp_path / "journal"
    journal.mkdir()
    install_router_skill_links(doctor, journal)
    monkeypatch.setattr(doctor, "get_journal_info", lambda: (str(journal), "env"))
    monkeypatch.setattr(doctor, "is_packaged_install", lambda: False)

    result = doctor.skill_state_check(args(doctor))

    assert result.status == "ok"
    assert result.detail == "router skills sol, journal are installed and current"


def test_skill_state_warns_for_stale_and_missing_links_without_writing(
    doctor, monkeypatch, tmp_path
):
    journal = tmp_path / "journal"
    skills_dir = journal / ".claude" / "skills"
    skills_dir.mkdir(parents=True)
    sources = {
        source.name: source
        for source in doctor.skills_cli.discover_project_sources(doctor.ROOT)
    }
    (skills_dir / "journal").symlink_to(os.path.relpath(sources["journal"], skills_dir))
    (skills_dir / "entities").symlink_to(
        "../../../solstone/apps/entities/talent/entities"
    )
    before = tree_snapshot(journal)
    monkeypatch.setattr(doctor, "get_journal_info", lambda: (str(journal), "env"))
    monkeypatch.setattr(doctor, "is_packaged_install", lambda: False)

    result = doctor.skill_state_check(args(doctor))

    assert result.status == "warn"
    assert f"missing router sol at {skills_dir / 'sol'}" in result.detail
    assert f"stale skill link entities at {skills_dir / 'entities'}" in result.detail
    assert result.fix is not None
    assert "journal setup" in result.fix
    assert f"sol skills install --project {journal} --agent all" in result.fix
    assert tree_snapshot(journal) == before


class TestJournalAlias:
    @pytest.fixture(autouse=True)
    def isolated_legacy_backups(self, doctor, monkeypatch, tmp_path):
        backup_dir = tmp_path / "legacy-backups"
        backup_dir.mkdir()
        monkeypatch.setattr(install_guard, "_legacy_backup_dir", lambda: backup_dir)
        self.backup_dir = backup_dir

    def test_journal_only_absent_ok_even_if_sol_is_foreign(
        self, doctor, monkeypatch, home_root, tmp_path
    ):
        patch_alias_absent(doctor, monkeypatch)
        repo = make_repo(tmp_path)
        sol_target = make_existing_target(tmp_path / "other" / ".venv" / "bin" / "sol")
        make_alias(home_root, "sol", sol_target)
        monkeypatch.setattr(doctor, "ROOT", repo)

        result = doctor.stale_alias_symlink_check(args(doctor), binary="journal")

        assert result.status == "ok"

    def test_journal_uv_tool_reports_only_journal(
        self, doctor, monkeypatch, home_root, tmp_path
    ):
        patch_alias_absent(doctor, monkeypatch)
        repo = make_repo(tmp_path)
        target = make_existing_target(
            home_root
            / ".local"
            / "share"
            / "uv"
            / "tools"
            / "solstone"
            / "bin"
            / "journal"
        )
        alias = make_alias(home_root, "journal", target)
        original_target = alias.readlink()
        monkeypatch.setattr(doctor, "ROOT", repo)

        result = doctor.stale_alias_symlink_check(args(doctor), binary="journal")

        assert result.status == "warn"
        assert "uv-tool" in result.detail
        assert result.fix is not None
        assert "journal setup" in result.fix
        assert alias.is_symlink()
        assert alias.readlink() == original_target
        assert not (home_root / ".local" / "bin" / "sol").exists()
        assert list(self.backup_dir.glob("*.old-symlink-*")) == []


class TestLaunchdStalePlist:
    def test_skip_on_linux(self, doctor, monkeypatch):
        monkeypatch.setattr(doctor, "platform_tag", lambda: "linux")
        result = doctor.launchd_stale_plist_check(args(doctor))
        assert result.status == "skip"

    def test_skip_when_absent(self, doctor, monkeypatch, home_root):
        monkeypatch.setattr(doctor, "platform_tag", lambda: "darwin")
        result = doctor.launchd_stale_plist_check(args(doctor))
        assert result.status == "skip"

    def test_fail_when_target_missing(self, doctor, monkeypatch, home_root):
        monkeypatch.setattr(doctor, "platform_tag", lambda: "darwin")
        plist_path = (
            home_root / "Library" / "LaunchAgents" / "org.solpbc.solstone.plist"
        )
        plist_path.parent.mkdir(parents=True)
        plist_path.write_bytes(
            plistlib.dumps({"ProgramArguments": ["/tmp/missing-sol"]})
        )
        result = doctor.launchd_stale_plist_check(args(doctor))
        assert result.status == "fail"

    def test_ok_when_target_exists(self, doctor, monkeypatch, home_root, tmp_path):
        monkeypatch.setattr(doctor, "platform_tag", lambda: "darwin")
        exe = tmp_path / "sol"
        exe.write_text("", encoding="utf-8")
        plist_path = (
            home_root / "Library" / "LaunchAgents" / "org.solpbc.solstone.plist"
        )
        plist_path.parent.mkdir(parents=True)
        plist_path.write_bytes(plistlib.dumps({"ProgramArguments": [str(exe)]}))
        result = doctor.launchd_stale_plist_check(args(doctor))
        assert result.status == "ok"
