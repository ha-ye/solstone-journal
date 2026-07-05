# SPDX-License-Identifier: AGPL-3.0-only
# Copyright (c) 2026 sol pbc
"""Build-fails-by-design tombstone for the retired `solstone-journal-host` dist.

`solstone[journal]` / `solstone[journal-cuda]` and the old `solstone-journal-host`
shim are retired. This project is published exactly once at the split release so
every old spelling fails loudly at install time instead of silently
thin-installing. Any build or metadata operation exits nonzero with a migration
message unless SOLSTONE_TOMBSTONE_ALLOW_BUILD=1 is set (the one-time release
build).
"""

import os
import sys

from setuptools import setup

TOMBSTONE_VERSION = "0.7.0"

_MIGRATION_MESSAGE = """solstone[journal] and solstone-journal-host have moved.

The journal is now its own package:

    pip install solstone-journal          # the journal (CPU)
    pip install solstone-journal-cuda     # the journal on NVIDIA CUDA

One-time migration for uv tool installs:

    uv tool uninstall solstone && uv tool install solstone-journal

Nothing was changed by this failed command.
See https://github.com/solpbc/solstone-journal/blob/main/INSTALL.md
"""


if os.environ.get("SOLSTONE_TOMBSTONE_ALLOW_BUILD") != "1":
    sys.stderr.write(_MIGRATION_MESSAGE)
    raise SystemExit(1)

setup(name="solstone-journal-host", version=TOMBSTONE_VERSION)
