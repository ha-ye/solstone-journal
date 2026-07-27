# SPDX-License-Identifier: AGPL-3.0-only
# Copyright (c) 2026 sol pbc

"""Local restic boundary proof for the SPB probe contract.

This test drives the real run_restic subprocess boundary against the installed
restic binary and a tmp_path local repository. On this host, that may be restic
0.18.1; this does not prove restic 0.19.0 runtime behavior. It proves that the
boundary and probe validators accept genuine output from the installed restic.
No broker, rclone, network backend, or production state is contacted.
"""

from __future__ import annotations

import shutil
from pathlib import Path

import pytest

from solstone.think.backup.hosted import HostedBinding
from solstone.think.backup.runner import ResticResult, run_restic
from solstone.think.sandbox_profile import spb_backup_probe as probe

pytestmark = pytest.mark.integration

RESTIC_BIN = shutil.which("restic")


@pytest.mark.skipif(RESTIC_BIN is None, reason="restic is not installed")
def test_run_restic_boundary_accepts_real_local_restic_output(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    home = tmp_path / "home"
    xdg_cache_home = tmp_path / "xdg-cache"
    tmp_dir = tmp_path / "tmp"
    for path in (home, xdg_cache_home, tmp_dir):
        path.mkdir()
    monkeypatch.setenv("HOME", str(home))
    monkeypatch.setenv("XDG_CACHE_HOME", str(xdg_cache_home))
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
        scrub_values=(),
    )

    repository = f"local:{tmp_path / 'repo'}"

    def run_phase(
        args: list[str],
        *,
        json: bool,
        stdin_bytes: bytes | None = None,
    ) -> ResticResult:
        result = run_restic(
            ["--no-cache", *args],
            repository=repository,
            password=preflight.daily_key,
            restic_path=preflight.restic_path,
            json=json,
            timeout=probe.RESTIC_CHILD_TIMEOUT_S,
            process_group=True,
            stdin_bytes=stdin_bytes,
            scrub_values=(repository,),
            terminate_grace_s=probe.TERM_GRACE_S,
            kill_grace_s=probe.KILL_GRACE_S,
        )
        assert "--no-cache" in result.argv
        return result

    init_result = run_phase(["init"], json=False)
    probe._check_restic_result(init_result, expect_json=False)

    backup_result = run_phase(
        ["backup", "--stdin", "--stdin-filename", probe.LOGICAL_SOURCE_PATH],
        json=True,
        stdin_bytes=probe.SPB_SYNTHETIC_FIXTURE_BYTES,
    )
    probe._check_restic_result(backup_result, expect_json=True)
    snapshot_id = probe._validate_backup_records(
        probe._parse_json_records(backup_result.stdout)
    )

    ls_result = run_phase(["ls", "--long", snapshot_id], json=True)
    probe._check_restic_result(ls_result, expect_json=True)
    probe._validate_ls_records(
        probe._parse_json_records(ls_result.stdout),
        snapshot_id,
        preflight,
    )

    restore_result = run_phase(
        ["restore", snapshot_id, "--target", str(preflight.restore_target)],
        json=True,
    )
    probe._check_restic_result(restore_result, expect_json=True)
    probe._validate_restore_records(probe._parse_json_records(restore_result.stdout))
    probe._verify_restore_tree(preflight)

    assert not (home / ".cache" / "restic").exists()
    assert not (xdg_cache_home / "restic").exists()
