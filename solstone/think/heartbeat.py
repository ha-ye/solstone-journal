# SPDX-License-Identifier: AGPL-3.0-only
# Copyright (c) 2026 sol pbc

"""CLI command for deterministic health repair passes."""

from __future__ import annotations

import argparse
import dataclasses
import logging
import os
import sys
import time
from datetime import datetime
from pathlib import Path

from solstone.think.steward import append_steward_event, run_recipe_pass
from solstone.think.utils import get_journal, require_solstone, setup_cli

logger = logging.getLogger(__name__)


RECENCY_WINDOW_HOURS = 12


def _last_success_time(health_dir: Path) -> datetime | None:
    """Return the timestamp of the most recent successful heartbeat run."""
    log_file = health_dir / "heartbeat.log"
    if not log_file.exists():
        return None
    try:
        lines = log_file.read_text().strip().splitlines()
    except OSError:
        return None
    for line in reversed(lines):
        if "outcome=success" in line:
            ts_str = line.split()[0]
            try:
                return datetime.fromisoformat(ts_str)
            except ValueError:
                continue
    return None


def main() -> None:
    """Entry point for ``journal heartbeat``."""
    parser = argparse.ArgumentParser(
        description="Run deterministic health repair pass",
    )
    parser.add_argument(
        "--force",
        action="store_true",
        help="Run full check regardless of recency",
    )
    args = setup_cli(parser)
    require_solstone()

    journal = Path(get_journal())
    health_dir = journal / "health"
    health_dir.mkdir(parents=True, exist_ok=True)

    # Recency check: skip if a recent successful run exists
    if not args.force:
        last_success = _last_success_time(health_dir)
        if last_success is not None:
            hours_since = (datetime.now() - last_success).total_seconds() / 3600
            if hours_since < RECENCY_WINDOW_HOURS:
                logger.info(
                    "Heartbeat succeeded %.1f hours ago (within %d-hour window), skipping",
                    hours_since,
                    RECENCY_WINDOW_HOURS,
                )
                sys.exit(0)

    pid_file = health_dir / "heartbeat.pid"

    try:
        # PID file guard
        if pid_file.exists():
            try:
                existing_pid = int(pid_file.read_text().strip())
                os.kill(existing_pid, 0)
                # Process is alive - exit cleanly
                logger.info("Heartbeat already running (PID %d)", existing_pid)
                sys.exit(0)
            except ProcessLookupError:
                # Dead process - stale PID file, remove and continue
                logger.info("Removing stale PID file (PID %d)", existing_pid)
                pid_file.unlink(missing_ok=True)
            except PermissionError:
                # Process alive but different user
                logger.info(
                    "Heartbeat already running (PID %d, different user)", existing_pid
                )
                sys.exit(0)
            except ValueError:
                # Corrupt PID file
                logger.warning("Corrupt PID file, removing")
                pid_file.unlink(missing_ok=True)

        # Write our PID
        pid_file.write_text(str(os.getpid()))
        start_time = time.monotonic()

        try:
            today = datetime.now().strftime("%Y%m%d")
            result = run_recipe_pass(today)
            append_steward_event(
                "pass",
                fired=[dataclasses.asdict(o) for o in result["fired"]],
                escalated_targets=result["escalated_targets"],
                data_source_errors=result["data_source_errors"],
            )
            logger.info("Heartbeat repair pass complete")
            _log_run(health_dir, start_time, "success")
            sys.exit(0)
        except SystemExit:
            raise
        except Exception:
            logger.exception("Heartbeat repair pass failed")
            _log_run(health_dir, start_time, "error")
            sys.exit(1)

    finally:
        pid_file.unlink(missing_ok=True)


def _log_run(health_dir: Path, start_time: float, outcome: str) -> None:
    """Append one line to heartbeat.log."""
    duration = int(time.monotonic() - start_time)
    timestamp = datetime.now().isoformat(timespec="seconds")
    log_file = health_dir / "heartbeat.log"
    with open(log_file, "a") as f:
        f.write(f"{timestamp} duration={duration}s outcome={outcome}\n")
