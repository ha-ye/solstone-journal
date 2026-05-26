# SPDX-License-Identifier: AGPL-3.0-only
# Copyright (c) 2026 sol pbc

"""Canonical journal service start entry point."""

from __future__ import annotations

import logging
import sys
from pathlib import Path

import solstone
from solstone.think.entities.core import atomic_write
from solstone.think.install_guard import alias_paths, install_wrappers
from solstone.think.service import reconcile_installed_unit
from solstone.think.skills_cli import install_project
from solstone.think.user_config import config_path
from solstone.think.utils import get_journal, get_project_root

logger = logging.getLogger(__name__)


def _version_marker_path() -> Path:
    return config_path().parent / ".last-start-version"


def _version_marker_is_current(path: Path) -> bool:
    try:
        return path.read_text(encoding="utf-8") == f"{solstone.__version__}\n"
    except FileNotFoundError:
        return False


def _install_current_wrappers() -> None:
    bin_dir = Path(sys.executable).parent
    paths = alias_paths()
    sol_bins = {binary: str(bin_dir / binary) for binary in paths}
    install_wrappers(get_journal(), sol_bins, paths=paths)


def _refresh_skill_links() -> None:
    report = install_project(Path(get_project_root()), Path(get_journal()), ["all"])
    if report.error_count:
        raise RuntimeError(f"skill refresh failed with {report.error_count} error(s)")


def _refresh_for_version_marker() -> None:
    marker_path = _version_marker_path()
    if _version_marker_is_current(marker_path):
        return

    _install_current_wrappers()
    reconcile_installed_unit()
    _refresh_skill_links()
    atomic_write(
        marker_path,
        f"{solstone.__version__}\n",
        prefix=".last_start_version_",
    )


def main() -> None:
    try:
        reconcile_installed_unit()
        _refresh_for_version_marker()
    except Exception:
        logger.exception("journal start failed during service reconciliation")
        sys.exit(1)

    from solstone.think import supervisor

    supervisor.main()
