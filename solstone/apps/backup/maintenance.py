# SPDX-License-Identifier: AGPL-3.0-only
# Copyright (c) 2026 sol pbc

"""App-owned scheduled maintenance routines for solstone backup."""

from __future__ import annotations

import argparse

from solstone.think.backup.engine import (
    BACKUP_MAX_RUNTIME,
    PRUNE_MAX_RUNTIME,
    VERIFY_MAX_RUNTIME,
    run_backup,
    run_prune,
    run_verification,
)
from solstone.think.maintenance import MaintenanceRoutine
from solstone.think.offload import OFFLOAD_MAX_RUNTIME, run_offload
from solstone.think.utils import require_solstone


def run_backup_routine(args: list[str]) -> int:
    require_solstone()
    parser = argparse.ArgumentParser(prog="journal maintenance run backup:run")
    parser.parse_args(args)

    result = run_backup()
    if result.status == "ok":
        print(f"backup: ok snapshot_id={result.snapshot_id}")
    elif result.status == "skipped":
        print("backup: skipped")
    else:
        print(f"backup: error reason={result.error_reason}")
    return 0


def run_prune_routine(args: list[str]) -> int:
    require_solstone()
    parser = argparse.ArgumentParser(prog="journal maintenance run backup:prune")
    parser.parse_args(args)

    result = run_prune()
    if result.status == "ok":
        print("backup prune: ok")
    elif result.status == "skipped":
        print("backup prune: skipped")
    else:
        print(f"backup prune: error reason={result.error_reason}")
    return 0


def run_verification_routine(args: list[str]) -> int:
    require_solstone()
    parser = argparse.ArgumentParser(prog="journal maintenance run backup:verify")
    parser.parse_args(args)

    result = run_verification()
    if result.status == "ok":
        print(f"backup verify: ok subset={result.checked_subset}")
    elif result.status == "skipped":
        print("backup verify: skipped")
    else:
        print(f"backup verify: error reason={result.reason}")
    return 0


def run_offload_routine(args: list[str]) -> int:
    require_solstone()
    parser = argparse.ArgumentParser(prog="journal maintenance run backup:offload")
    parser.add_argument("--dry-run", action="store_true")
    parsed = parser.parse_args(args)

    result = run_offload(dry_run=parsed.dry_run)
    if result.dry_run:
        selected_files = sum(detail.files for detail in result.details)
        selected_bytes = sum(detail.bytes for detail in result.details)
        segments = ",".join(
            f"{detail.day}/{detail.stream}/{detail.segment}:{detail.bytes}"
            for detail in result.details
        )
        if result.status == "stalled":
            print(f"backup offload: stalled reason={result.reason} dry_run=true")
        elif result.status == "skipped":
            print("backup offload: skipped dry_run=true")
        else:
            print(
                "backup offload: ok dry_run=true "
                f"selected_files={selected_files} selected_bytes={selected_bytes} "
                f"ran_out_of_media={result.ran_out_of_media} segments={segments}"
            )
    elif result.status == "ok":
        print(
            "backup offload: ok "
            f"files_offloaded={result.files_offloaded} "
            f"bytes_offloaded={result.bytes_offloaded} "
            f"ran_out_of_media={result.ran_out_of_media}"
        )
    elif result.status == "skipped":
        print("backup offload: skipped")
    else:
        print(
            f"backup offload: stalled reason={result.reason} "
            f"files_offloaded={result.files_offloaded} "
            f"bytes_offloaded={result.bytes_offloaded}"
        )
    return 0


ROUTINES = [
    MaintenanceRoutine(
        name="run",
        description="run encrypted backup.",
        every="hourly",
        run=run_backup_routine,
        max_runtime=BACKUP_MAX_RUNTIME,
    ),
    MaintenanceRoutine(
        name="prune",
        description="apply encrypted backup retention policy.",
        every="daily",
        run=run_prune_routine,
        max_runtime=PRUNE_MAX_RUNTIME,
    ),
    MaintenanceRoutine(
        name="verify",
        description="verify encrypted backup read-back.",
        every="weekly",
        run=run_verification_routine,
        max_runtime=VERIFY_MAX_RUNTIME,
    ),
    MaintenanceRoutine(
        name="offload",
        description="offload verified raw media after backup.",
        every="daily",
        run=run_offload_routine,
        max_runtime=OFFLOAD_MAX_RUNTIME,
    ),
]
