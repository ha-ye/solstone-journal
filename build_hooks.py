# SPDX-License-Identifier: AGPL-3.0-only
# Copyright (c) 2026 sol pbc

"""Setuptools build hooks for solstone packaging."""

from __future__ import annotations

import shutil
from pathlib import Path

from setuptools.command.build_py import build_py


class BuildPyWithoutTests(build_py):
    """Build the base wheel without in-package test directories."""

    def run(self) -> None:
        super().run()
        solstone_root = Path(self.build_lib) / "solstone"
        if not solstone_root.exists():
            return
        for tests_dir in sorted(solstone_root.rglob("tests"), reverse=True):
            if tests_dir.is_dir():
                shutil.rmtree(tests_dir)
