# SPDX-License-Identifier: AGPL-3.0-only
# Copyright (c) 2026 sol pbc

from __future__ import annotations

import importlib
import json
from pathlib import Path

import pytest

mod = importlib.import_module(
    "solstone.apps.sol.maint.008_migrate_provider_check_schedule"
)

JOURNAL = "jour" + "nal"
SOL = "s" + "ol"
PROVIDERS = "providers"
CHECK = "check"
BRAIN_CMD = [JOURNAL, "brain", "refresh"]
LEGACY_HEALTH_FILE = "talents" + ".json"


@pytest.fixture(autouse=True)
def _use_tmp_journal(tmp_path, monkeypatch):
    monkeypatch.setenv("SOLSTONE_JOURNAL", str(tmp_path))


def _write_schedules(journal: Path, data: object) -> Path:
    config_dir = journal / "config"
    config_dir.mkdir(parents=True, exist_ok=True)
    schedules_path = config_dir / "schedules.json"
    schedules_path.write_text(json.dumps(data, indent=2), encoding="utf-8")
    return schedules_path


def _read_schedules(path: Path) -> dict:
    return json.loads(path.read_text(encoding="utf-8"))


def _old_cmd(surface: str = JOURNAL, *extra: str) -> list[str]:
    return [surface, PROVIDERS, CHECK, *extra]


def _seed_cleanup_artifacts(journal: Path) -> Path:
    health = journal / "health"
    health.mkdir(parents=True, exist_ok=True)
    (health / LEGACY_HEALTH_FILE).write_text("{}", encoding="utf-8")
    (health / f"{LEGACY_HEALTH_FILE}.lock").write_text("", encoding="utf-8")
    (health / "recheck.lock").write_text("", encoding="utf-8")
    (health / f".{LEGACY_HEALTH_FILE}.123.tmp").write_text("", encoding="utf-8")
    day_log = journal / "chronicle" / "20260410" / "health" / "providers.log"
    day_log.parent.mkdir(parents=True, exist_ok=True)
    day_log.write_text("preserve\n", encoding="utf-8")
    (health / "providers.log").symlink_to(day_log)
    return day_log


def _assert_cleanup_artifacts_intact(journal: Path, day_log: Path) -> None:
    health = journal / "health"
    assert (health / LEGACY_HEALTH_FILE).exists()
    assert (health / f"{LEGACY_HEALTH_FILE}.lock").exists()
    assert (health / "recheck.lock").exists()
    assert (health / f".{LEGACY_HEALTH_FILE}.123.tmp").exists()
    assert (health / "providers.log").is_symlink()
    assert (health / "providers.log").exists()
    assert day_log.exists()


def test_migrates_matches_coalesces_and_cleans(tmp_path):
    schedules_path = _write_schedules(
        tmp_path,
        {
            "daily_time": "03:17",
            "providers": {
                "cmd": _old_cmd(JOURNAL, "--targeted"),
                "every": "hourly",
                "enabled": True,
                "max_runtime": "99m",
            },
            "providers-check": {
                "cmd": ["custom"],
                "every": "weekly",
                "enabled": False,
            },
            "custom-old": {
                "cmd": _old_cmd(SOL, "--targeted"),
                "every": "5m",
                "enabled": True,
            },
            "unrelated": {
                "cmd": [JOURNAL, "heartbeat"],
                "every": "daily",
                "enabled": True,
                "extra": "kept",
            },
        },
    )
    day_log = _seed_cleanup_artifacts(tmp_path)

    summary = mod.run_migration(dry_run=False)
    data = _read_schedules(schedules_path)

    assert summary.matched == 3
    assert summary.installed_brain is True
    assert summary.removed == 3
    assert summary.cleanup_deleted == 5
    assert summary.errors == 0
    assert data["brain"] == {
        "cmd": BRAIN_CMD,
        "every": "hourly",
        "enabled": False,
        "max_runtime": "5m",
    }
    assert "providers" not in data
    assert "providers-check" not in data
    assert "custom-old" not in data
    assert data["unrelated"] == {
        "cmd": [JOURNAL, "heartbeat"],
        "every": "daily",
        "enabled": True,
        "extra": "kept",
    }
    assert day_log.read_text(encoding="utf-8") == "preserve\n"
    assert not (tmp_path / "health" / "providers.log").exists()


def test_existing_brain_is_preserved_while_old_matches_are_removed(tmp_path):
    existing_brain = {
        "cmd": BRAIN_CMD,
        "every": "hourly",
        "enabled": False,
        "max_runtime": "99m",
        "extra": "kept",
    }
    schedules_path = _write_schedules(
        tmp_path,
        {
            "brain": existing_brain,
            "providers": {
                "cmd": _old_cmd(JOURNAL),
                "every": "daily",
                "enabled": True,
            },
        },
    )

    summary = mod.run_migration(dry_run=False)
    data = _read_schedules(schedules_path)

    assert summary.matched == 1
    assert summary.preserved_brain is True
    assert summary.installed_brain is False
    assert summary.removed == 1
    assert data == {"brain": existing_brain}


def test_second_run_is_noop_after_first_completion(tmp_path):
    schedules_path = _write_schedules(
        tmp_path,
        {
            "providers": {
                "cmd": _old_cmd(JOURNAL),
                "every": "daily",
                "enabled": True,
            },
        },
    )

    first = mod.run_migration(dry_run=False)
    after_first = schedules_path.read_bytes()
    second = mod.run_migration(dry_run=False)

    assert first.installed_brain is True
    assert second.matched == 0
    assert second.preserved_brain is True
    assert second.removed == 0
    assert second.cleanup_deleted == 0
    assert schedules_path.read_bytes() == after_first


def test_cleanup_failure_is_fatal_and_retryable_without_duplicate_brain(tmp_path):
    schedules_path = _write_schedules(
        tmp_path,
        {
            "providers": {
                "cmd": _old_cmd(JOURNAL),
                "every": "daily",
                "enabled": True,
            },
        },
    )
    health = tmp_path / "health"
    health.mkdir(parents=True, exist_ok=True)
    (health / LEGACY_HEALTH_FILE).mkdir()

    summary = mod.run_migration(dry_run=False)
    data = _read_schedules(schedules_path)

    assert summary.errors == 1
    assert summary.cleanup_errors
    assert data == {
        "brain": {
            "cmd": BRAIN_CMD,
            "every": "daily",
            "enabled": True,
            "max_runtime": "5m",
        }
    }
    assert (health / LEGACY_HEALTH_FILE).is_dir()


def test_unreadable_schedules_file_does_not_cleanup_before_schedule_commit(tmp_path):
    day_log = _seed_cleanup_artifacts(tmp_path)
    config_dir = tmp_path / "config"
    config_dir.mkdir(parents=True, exist_ok=True)
    schedules_path = config_dir / "schedules.json"
    schedules_path.mkdir()

    summary = mod.run_migration(dry_run=False)

    assert summary.errors == 1
    assert summary.cleanup_deleted == 0
    assert summary.installed_brain is False
    assert summary.preserved_brain is False
    assert schedules_path.is_dir()
    _assert_cleanup_artifacts_intact(tmp_path, day_log)


def test_malformed_schedules_file_does_not_cleanup_before_schedule_commit(tmp_path):
    day_log = _seed_cleanup_artifacts(tmp_path)
    config_dir = tmp_path / "config"
    config_dir.mkdir(parents=True, exist_ok=True)
    schedules_path = config_dir / "schedules.json"
    schedules_path.write_text("{", encoding="utf-8")

    summary = mod.run_migration(dry_run=False)

    assert summary.errors == 1
    assert summary.cleanup_deleted == 0
    assert summary.installed_brain is False
    assert summary.preserved_brain is False
    assert schedules_path.read_text(encoding="utf-8") == "{"
    _assert_cleanup_artifacts_intact(tmp_path, day_log)
