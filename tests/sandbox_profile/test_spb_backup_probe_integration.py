# SPDX-License-Identifier: AGPL-3.0-only
# Copyright (c) 2026 sol pbc

"""Local restic boundary proof for the SPB probe contract.

This test drives the real restic subprocess boundary against pinned restic
0.19.0 and a tmp_path local repository, including parser-mode JSON phases. No
broker, rclone, network backend, or production state is contacted.
"""

from __future__ import annotations

import shutil
import subprocess
from pathlib import Path

import pytest

from solstone.think.backup.hosted import HostedBinding
from solstone.think.backup.runner import (
    ResticJsonRecordsResult,
    ResticResult,
    run_restic,
    run_restic_json_records,
)
from solstone.think.sandbox_profile import spb_backup_probe as probe

pytestmark = pytest.mark.integration

RESTIC_BIN = shutil.which("restic")


@pytest.mark.skipif(RESTIC_BIN is None, reason="restic is not installed")
def test_run_restic_boundary_accepts_real_local_restic_output(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    assert RESTIC_BIN is not None
    version = subprocess.run(
        [RESTIC_BIN, "version"],
        check=True,
        capture_output=True,
        text=True,
        timeout=5,
    )
    assert version.stderr == ""
    assert version.stdout.split()[:2] == ["restic", "0.19.0"]

    home = tmp_path / "home"
    tmp_dir = tmp_path / "tmp"
    for path in (home, tmp_dir):
        path.mkdir()
    monkeypatch.setenv("HOME", str(home))
    monkeypatch.setenv("TMPDIR", str(tmp_dir))

    attempt_dir = tmp_path / "attempt"
    spb_root = attempt_dir / probe.SPB_DIR_NAME
    fixture_path = spb_root / probe.SOURCE_FILE_NAME
    restore_target = spb_root / probe.RESTORE_DIR_NAME
    spb_root.mkdir(parents=True)
    fixture_path.write_bytes(probe.SPB_SYNTHETIC_FIXTURE_BYTES)

    binding = HostedBinding(
        broker_endpoint="https://unused.invalid",
        account_id="unused-account",
        instance_id="unused-instance",
        bucket="unused-bucket",
        prefix="unused-prefix",
        broker_token="unused-token",
    )
    # Hosted/rclone fields are unused by this direct local-repository path.
    preflight = probe._Preflight(
        attempt_dir=attempt_dir,
        spb_root=spb_root,
        restore_target=restore_target,
        fixture_path=fixture_path,
        fixture=probe._fixture_identity(fixture_path),
        binding=binding,
        proof_binding=binding,
        daily_key="synthetic-local-restic-password",
        restic_path=Path(RESTIC_BIN),
        rclone_path=tmp_path / "unused-rclone",
        scrub_values=probe._scrub_values(
            binding=binding,
            proof_binding=binding,
            attempt_dir=attempt_dir,
            spb_root=spb_root,
            restore_target=restore_target,
        ),
    )

    repository = f"local:{tmp_path / 'repo'}"

    def assert_visible_surface_safe(
        result: ResticResult | ResticJsonRecordsResult,
        secrets: tuple[str, ...],
    ) -> None:
        rendered = f"{result!r} {result.stdout} {result.stderr} {' '.join(result.argv)}"
        for secret in secrets:
            assert secret not in rendered

    def run_init_phase(
        args: list[str],
    ) -> ResticResult:
        result = run_restic(
            ["--no-cache", *args],
            repository=repository,
            password=preflight.daily_key,
            restic_path=preflight.restic_path,
            json=False,
            timeout=probe.RESTIC_CHILD_TIMEOUT_S,
            process_group=True,
            scrub_values=(*preflight.scrub_values, repository),
            terminate_grace_s=probe.TERM_GRACE_S,
            kill_grace_s=probe.KILL_GRACE_S,
        )
        assert "--no-cache" in result.argv
        assert_visible_surface_safe(result, (*preflight.scrub_values, repository))
        return result

    def run_records_phase(
        args: list[str],
        *,
        stdin_bytes: bytes | None = None,
        extra_scrub_values: tuple[str, ...] = (),
    ) -> tuple[list[object], ResticJsonRecordsResult]:
        scrub_values = (*preflight.scrub_values, repository, *extra_scrub_values)
        result = run_restic_json_records(
            ["--no-cache", *args],
            repository=repository,
            password=preflight.daily_key,
            restic_path=preflight.restic_path,
            timeout=probe.RESTIC_CHILD_TIMEOUT_S,
            stdin_bytes=stdin_bytes,
            scrub_values=scrub_values,
            terminate_grace_s=probe.TERM_GRACE_S,
            kill_grace_s=probe.KILL_GRACE_S,
        )
        assert "--no-cache" in result.argv
        assert result.stdout
        assert_visible_surface_safe(result, scrub_values)
        probe._check_restic_result(result)
        assert result.has_records
        records = list(result.consume_records())
        assert not result.has_records
        with pytest.raises(TypeError):
            result.consume_records()
        return records, result

    init_result = run_init_phase(["init"])
    probe._check_restic_result(init_result)

    backup_records, backup_result = run_records_phase(
        ["backup", "--stdin", "--stdin-filename", probe.LOGICAL_SOURCE_PATH],
        stdin_bytes=probe.SPB_SYNTHETIC_FIXTURE_BYTES,
    )
    snapshot_id = probe._validate_backup_records(backup_records)
    assert snapshot_id not in backup_result.stdout

    ls_records, _ls_result = run_records_phase(
        ["ls", "--long", snapshot_id],
        extra_scrub_values=(snapshot_id,),
    )
    probe._validate_ls_records(ls_records, snapshot_id, preflight)

    restore_records, _restore_result = run_records_phase(
        ["restore", snapshot_id, "--target", str(preflight.restore_target)],
        extra_scrub_values=(snapshot_id,),
    )
    probe._validate_restore_records(restore_records)
    probe._verify_restore_tree(preflight)

    assert not (home / ".cache" / "restic").exists()
