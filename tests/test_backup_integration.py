# SPDX-License-Identifier: AGPL-3.0-only
# Copyright (c) 2026 sol pbc

from __future__ import annotations

import hashlib
import json
import shlex
import shutil
from pathlib import Path
from typing import Any

import pytest

from solstone.think.backup import restore, rotation, teardown
from solstone.think.backup.destination import Destination, validate_destination
from solstone.think.backup.engine import BACKUP_EXCLUDES
from solstone.think.backup.install import ensure_restic
from solstone.think.backup.readiness import (
    RESTIC_SCHEMA_VERSION,
    RESTIC_TOOL,
    RESTIC_VERSION,
    _binary_path,
    _platform_info,
    _sentinel_path,
    _tool_dir,
)
from solstone.think.backup.repo import init_repository
from solstone.think.backup.runner import run_restic
from solstone.think.backup.state import BACKUP_DEFAULTS, get_destination

RESTIC_BIN = shutil.which("restic")


def _config_path(journal: Path) -> Path:
    return journal / "config" / "journal.json"


def _write_json(path: Path, payload: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(payload, indent=2) + "\n", encoding="utf-8")


def _read_json(path: Path) -> dict[str, Any]:
    return json.loads(path.read_text(encoding="utf-8"))


def _destination(repo: Path) -> Destination:
    return Destination(
        repository=f"local:{repo}",
        backend="s3",
        credentials={
            "access_key_id": "test-access-key",
            "secret_access_key": "test-secret-key",
        },
    )


def _write_journal_config(
    journal: Path,
    *,
    destination: Destination,
    daily_key: str,
    recovery_key: str,
) -> None:
    _write_json(
        _config_path(journal),
        {
            "backup": {
                "enabled": True,
                "destination": {
                    "repository": destination.repository,
                    "backend": destination.backend,
                    "credentials": destination.credentials,
                },
                "daily_key": daily_key,
                "recovery_key": recovery_key,
                "confirmed_recovery_key": True,
            }
        },
    )


def _install_restic_wrapper(restic_bin: str) -> Path:
    os_name, arch = _platform_info()
    tool_dir = _tool_dir(os_name)
    tool_dir.mkdir(parents=True, exist_ok=True)
    binary_path = _binary_path(tool_dir)
    binary_path.write_text(
        "#!/usr/bin/env sh\n"
        'if [ "$1" = "version" ]; then\n'
        f"  echo 'restic {RESTIC_VERSION} test wrapper'\n"
        "  exit 0\n"
        "fi\n"
        f'exec {shlex.quote(restic_bin)} "$@"\n',
        encoding="utf-8",
    )
    binary_path.chmod(0o755)
    digest = hashlib.sha256(binary_path.read_bytes()).hexdigest()
    _write_json(
        _sentinel_path(tool_dir),
        {
            "schema_version": RESTIC_SCHEMA_VERSION,
            "tool": RESTIC_TOOL,
            "version": RESTIC_VERSION,
            "sha256": digest,
            "platform": {"os": os_name, "arch": arch},
            "binary_path": str(binary_path),
        },
    )
    return binary_path


def _backup_journal(
    *,
    journal: Path,
    destination: Destination,
    daily_key: str,
    recovery_key: str,
    restic_path: Path,
) -> None:
    init_repository(
        destination,
        daily_key=daily_key,
        recovery_key=recovery_key,
        restic_path=restic_path,
        timeout=30,
    )
    args = ["backup", str(journal)]
    for pattern in BACKUP_EXCLUDES:
        args.extend(["--exclude", pattern])
    result = run_restic(
        args,
        repository=destination.repository,
        password=daily_key,
        restic_path=restic_path,
        timeout=60,
    )
    assert result.returncode == 0, result.stderr


@pytest.mark.skipif(RESTIC_BIN is None, reason="restic is not installed")
@pytest.mark.timeout(120)
def test_backup_restore_rotation_teardown_real_local_round_trip(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    home = tmp_path / "home"
    home.mkdir()
    monkeypatch.setenv("HOME", str(home))
    restic_path = _install_restic_wrapper(RESTIC_BIN or "")

    repo = tmp_path / "repo"
    source_journal = tmp_path / "source-journal"
    restored_journal = tmp_path / "restored-journal"
    source_journal.mkdir()
    restored_journal.mkdir()
    destination = _destination(repo)
    daily_key = "daily-password"
    recovery_key = ("0" * 32) + ("1" * 32)
    entered_recovery_key = ("O" * 32) + ("I" * 32)

    _write_journal_config(
        source_journal,
        destination=destination,
        daily_key=daily_key,
        recovery_key=recovery_key,
    )
    (source_journal / "chronicle" / "20260612").mkdir(parents=True)
    (source_journal / "chronicle" / "20260612" / "note.txt").write_text(
        "journal content\n",
        encoding="utf-8",
    )

    monkeypatch.setenv("SOLSTONE_JOURNAL", str(source_journal))
    assert ensure_restic() == restic_path
    _backup_journal(
        journal=source_journal,
        destination=destination,
        daily_key=daily_key,
        recovery_key=recovery_key,
        restic_path=restic_path,
    )

    scan_calls: list[tuple[str, dict[str, Any]]] = []

    def fake_scan_journal(journal: str, **kwargs: Any) -> bool:
        scan_calls.append((journal, kwargs))
        return True

    monkeypatch.setattr(restore, "scan_journal", fake_scan_journal)
    monkeypatch.setenv("SOLSTONE_JOURNAL", str(restored_journal))

    restore_result = restore.restore_journal(destination, entered_recovery_key)

    assert restore_result.status == "ok"
    assert restore_result.integrity_ok is True
    assert restore_result.resumable is True
    assert isinstance(restore_result.bytes_restored, int)
    assert restore_result.bytes_restored > 0
    assert (restored_journal / "chronicle" / "20260612" / "note.txt").read_text(
        encoding="utf-8"
    ) == "journal content\n"
    restored_config = _read_json(_config_path(restored_journal))
    assert restored_config["backup"]["daily_key"] == daily_key
    assert restored_config["backup"]["destination"] == {
        "repository": destination.repository,
        "backend": destination.backend,
        "credentials": destination.credentials,
    }
    assert restored_config["backup"]["recovery_key"] == recovery_key
    assert restored_config["backup"]["confirmed_recovery_key"] is True
    assert scan_calls == [(str(restored_journal), {"full": True})]

    rotation_result = rotation.rotate_recovery_key()

    assert rotation_result.status == "ok"
    assert rotation_result.recovery_key is not None
    assert rotation_result.recovery_key != recovery_key
    active_config = _read_json(_config_path(restored_journal))
    assert active_config["backup"]["daily_key"] == daily_key
    assert active_config["backup"]["recovery_key"] == rotation_result.recovery_key
    assert active_config["backup"]["confirmed_recovery_key"] is False

    active_destination = get_destination()
    assert active_destination == destination
    new_status = validate_destination(
        active_destination,
        rotation_result.recovery_key,
        restic_path=restic_path,
        timeout=30,
    )
    old_status = validate_destination(
        active_destination,
        recovery_key,
        restic_path=restic_path,
        timeout=30,
    )
    assert new_status.reason_code == "repo_exists"
    assert old_status.reason_code == "auth_failed"

    teardown_result = teardown.teardown_backup()

    assert teardown_result.status == "ok"
    snapshots = run_restic(
        ["snapshots"],
        repository=destination.repository,
        password=daily_key,
        restic_path=restic_path,
        json=True,
        timeout=30,
    )
    assert snapshots.returncode == 0, snapshots.stderr
    assert snapshots.json == []
    assert _read_json(_config_path(restored_journal))["backup"] == BACKUP_DEFAULTS
