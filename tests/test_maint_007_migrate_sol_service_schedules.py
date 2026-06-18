import importlib
import json
from pathlib import Path

import pytest

mod = importlib.import_module(
    "solstone.apps.sol.maint.007_migrate_sol_service_schedules"
)


@pytest.fixture(autouse=True)
def _use_tmp_journal(tmp_path, monkeypatch):
    monkeypatch.setenv("SOLSTONE_JOURNAL", str(tmp_path))


def _write_schedules(journal: Path, data: object) -> Path:
    config_dir = journal / "config"
    config_dir.mkdir(parents=True, exist_ok=True)
    schedules_path = config_dir / "schedules.json"
    schedules_path.write_text(json.dumps(data, indent=2), encoding="utf-8")
    return schedules_path


def test_happy_path_rewrites_all_stale_forms(tmp_path):
    schedules_path = _write_schedules(
        tmp_path,
        {
            "daily_time": "03:17",
            "weekly-agents": {
                "cmd": ["sol", "think", "--weekly", "-v"],
                "every": "weekly",
                "enabled": True,
            },
            "heartbeat": {
                "cmd": ["sol", "heartbeat"],
                "every": "daily",
                "enabled": True,
            },
            "providers-check": {
                "cmd": ["sol", "providers", "check"],
                "every": "daily",
                "enabled": True,
            },
            "sync:plaud": {
                "cmd": ["sol", "import", "--sync", "plaud", "--save"],
                "every": "hourly",
                "enabled": True,
                "max_runtime": 600,
            },
        },
    )

    summary = mod.run_migration(dry_run=False)

    assert summary.discovered == 4
    assert summary.rewritten == 4
    assert summary.preserved == 1
    assert summary.errors == 0
    data = json.loads(schedules_path.read_text(encoding="utf-8"))
    assert data["daily_time"] == "03:17"
    assert data["weekly-agents"]["cmd"] == ["journal", "think", "--weekly", "-v"]
    assert data["weekly-agents"]["every"] == "weekly"
    assert data["weekly-agents"]["enabled"] is True
    assert data["heartbeat"]["cmd"] == ["journal", "heartbeat"]
    assert data["heartbeat"]["every"] == "daily"
    assert data["heartbeat"]["enabled"] is True
    assert data["providers-check"]["cmd"] == ["journal", "providers", "check"]
    assert data["providers-check"]["every"] == "daily"
    assert data["providers-check"]["enabled"] is True
    assert data["sync:plaud"]["cmd"] == [
        "journal",
        "importer",
        "--sync",
        "plaud",
        "--save",
    ]
    assert data["sync:plaud"]["every"] == "hourly"
    assert data["sync:plaud"]["enabled"] is True
    assert data["sync:plaud"]["max_runtime"] == 600


def test_access_and_universal_preserved(tmp_path):
    initial = {
        "import-file": {
            "cmd": ["sol", "import", "plaud.m4a"],
            "every": "daily",
            "enabled": True,
        },
        "entities-list": {
            "cmd": ["sol", "call", "entities", "list"],
            "every": "daily",
            "enabled": True,
        },
        "doctor": {
            "cmd": ["sol", "doctor"],
            "every": "daily",
            "enabled": True,
        },
    }
    schedules_path = _write_schedules(tmp_path, initial)

    summary = mod.run_migration(dry_run=False)

    assert summary.discovered == 0
    assert summary.rewritten == 0
    assert summary.preserved == 3
    assert summary.errors == 0
    assert json.loads(schedules_path.read_text(encoding="utf-8")) == initial


def test_non_sol_entries_preserved(tmp_path):
    initial = {
        "facet-candidates": {
            "cmd": ["journal", "facet-candidates"],
            "every": "daily",
            "enabled": True,
        },
        "backup-prune": {
            "cmd": ["journal", "maintenance", "run", "backup:prune"],
            "every": "daily",
            "enabled": True,
        },
    }
    schedules_path = _write_schedules(tmp_path, initial)

    summary = mod.run_migration(dry_run=False)

    assert summary.discovered == 0
    assert summary.rewritten == 0
    assert summary.preserved == 2
    assert summary.errors == 0
    assert json.loads(schedules_path.read_text(encoding="utf-8")) == initial


def test_reserved_metadata_keys_untouched(tmp_path):
    schedules_path = _write_schedules(
        tmp_path,
        {
            "daily_time": "03:17",
            "weekly_day": "sunday",
            "weekly_time": "04:21",
            "weekly-agents": {
                "cmd": ["sol", "think"],
                "every": "weekly",
                "enabled": True,
            },
        },
    )

    summary = mod.run_migration(dry_run=False)

    assert summary.discovered == 1
    assert summary.rewritten == 1
    assert summary.preserved == 3
    assert summary.errors == 0
    data = json.loads(schedules_path.read_text(encoding="utf-8"))
    assert data["daily_time"] == "03:17"
    assert data["weekly_day"] == "sunday"
    assert data["weekly_time"] == "04:21"
    assert data["weekly-agents"]["cmd"] == ["journal", "think"]


def test_special_case_generalizes_across_backends(tmp_path):
    schedules_path = _write_schedules(
        tmp_path,
        {
            "sync:granola": {
                "cmd": ["sol", "import", "--sync", "granola", "--save"],
                "every": "hourly",
                "enabled": True,
            },
            "sync:obsidian": {
                "cmd": ["sol", "import", "--sync", "obsidian", "--save"],
                "every": "hourly",
                "enabled": True,
            },
        },
    )

    summary = mod.run_migration(dry_run=False)

    assert summary.discovered == 2
    assert summary.rewritten == 2
    assert summary.preserved == 0
    assert summary.errors == 0
    data = json.loads(schedules_path.read_text(encoding="utf-8"))
    assert data["sync:granola"]["cmd"] == [
        "journal",
        "importer",
        "--sync",
        "granola",
        "--save",
    ]
    assert data["sync:obsidian"]["cmd"] == [
        "journal",
        "importer",
        "--sync",
        "obsidian",
        "--save",
    ]


def test_idempotent_rerun(tmp_path):
    initial = {
        "weekly-agents": {
            "cmd": ["journal", "think", "--weekly", "-v"],
            "every": "weekly",
            "enabled": True,
        }
    }
    schedules_path = _write_schedules(tmp_path, initial)
    before_bytes = schedules_path.read_bytes()
    before_mtime_ns = schedules_path.stat().st_mtime_ns

    summary = mod.run_migration(dry_run=False)

    assert summary.discovered == 0
    assert summary.rewritten == 0
    assert summary.errors == 0
    assert summary.skipped_reason is None
    assert schedules_path.read_bytes() == before_bytes
    assert schedules_path.stat().st_mtime_ns == before_mtime_ns


def test_missing_file(tmp_path):
    config_dir = tmp_path / "config"
    config_dir.mkdir(parents=True, exist_ok=True)

    summary = mod.run_migration(dry_run=False)

    assert summary.skipped_reason == "no file"
    assert summary.discovered == 0
    assert summary.errors == 0
    assert not (config_dir / "schedules.json").exists()


def test_empty_file(tmp_path):
    config_dir = tmp_path / "config"
    config_dir.mkdir(parents=True, exist_ok=True)
    schedules_path = config_dir / "schedules.json"
    schedules_path.write_text("", encoding="utf-8")

    summary = mod.run_migration(dry_run=False)

    assert summary.skipped_reason == "empty file"
    assert summary.errors == 0
    assert schedules_path.read_text(encoding="utf-8") == ""


def test_malformed_json(tmp_path):
    config_dir = tmp_path / "config"
    config_dir.mkdir(parents=True, exist_ok=True)
    schedules_path = config_dir / "schedules.json"
    schedules_path.write_text("{not json", encoding="utf-8")

    summary = mod.run_migration(dry_run=False)

    assert summary.skipped_reason == "unparseable"
    assert summary.errors == 0
    assert schedules_path.read_text(encoding="utf-8") == "{not json"


def test_non_dict_root(tmp_path):
    config_dir = tmp_path / "config"
    config_dir.mkdir(parents=True, exist_ok=True)
    schedules_path = config_dir / "schedules.json"
    schedules_path.write_text("[]", encoding="utf-8")

    summary = mod.run_migration(dry_run=False)

    assert summary.skipped_reason == "unparseable"
    assert summary.errors == 0
    assert schedules_path.read_text(encoding="utf-8") == "[]"


def test_dry_run_does_not_write(tmp_path, monkeypatch):
    schedules_path = _write_schedules(
        tmp_path,
        {
            "weekly-agents": {
                "cmd": ["sol", "think", "--weekly", "-v"],
                "every": "weekly",
                "enabled": True,
            }
        },
    )
    before_bytes = schedules_path.read_bytes()

    def fail_if_called(*_args, **_kwargs):
        raise AssertionError("dry-run wrote schedules")

    monkeypatch.setattr(mod, "set_schedule_entries", fail_if_called)

    summary = mod.run_migration(dry_run=True)

    assert summary.discovered == 1
    assert summary.rewritten == 1
    assert summary.errors == 0
    assert schedules_path.read_bytes() == before_bytes


def test_owner_write_failure_preserves_existing_bytes(tmp_path, monkeypatch):
    schedules_path = _write_schedules(
        tmp_path,
        {
            "weekly-agents": {
                "cmd": ["sol", "think", "--weekly", "-v"],
                "every": "weekly",
                "enabled": True,
            }
        },
    )
    before_bytes = schedules_path.read_bytes()

    def _boom(_entries):
        raise OSError("boom")

    monkeypatch.setattr(mod, "set_schedule_entries", _boom)

    summary = mod.run_migration(dry_run=False)

    assert summary.errors >= 1
    assert schedules_path.read_bytes() == before_bytes
