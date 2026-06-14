# SPDX-License-Identifier: AGPL-3.0-only
# Copyright (c) 2026 sol pbc

"""App-owned scheduled maintenance routines for solstone backup."""

from __future__ import annotations

import argparse

from solstone.think.backup.engine import (
    BACKUP_MAX_RUNTIME,
    PRUNE_MAX_RUNTIME,
    run_backup,
    run_prune,
)
from solstone.think.maintenance import MaintenanceRoutine
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


ROUTINES = [
    MaintenanceRoutine(
        name="run",
        description="Run solstone backup.",
        every="hourly",
        run=run_backup_routine,
        max_runtime=BACKUP_MAX_RUNTIME,
    ),
    MaintenanceRoutine(
        name="prune",
        description="Apply solstone backup retention policy.",
        every="daily",
        run=run_prune_routine,
        max_runtime=PRUNE_MAX_RUNTIME,
    ),
]
