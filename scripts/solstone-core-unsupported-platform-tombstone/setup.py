# SPDX-License-Identifier: AGPL-3.0-only
# Copyright (c) 2026 sol pbc
"""Build-fails tombstone for unsupported native solstone-core platforms.

The root solstone package depends on solstone-core for every platform where the
native binaries are published. Unsupported platforms receive this package
instead so installation cannot appear successful without a working `sol`.
"""

import os
import sys

from setuptools import setup

TOMBSTONE_VERSION = "1.0.19"
ALLOW_BUILD_ENV = "SOLSTONE_CORE_UNSUPPORTED_PLATFORM_TOMBSTONE_ALLOW_BUILD"

UNSUPPORTED_PLATFORM_MESSAGE = """solstone requires a native solstone-core wheel for this platform.

Supported platform triples:
    x86_64-unknown-linux-musl
    aarch64-unknown-linux-musl
    aarch64-apple-darwin

A nominally successful install without a working `sol` is impossible.

Nothing was changed by this failed command.
See https://github.com/solpbc/solstone-journal/blob/main/INSTALL.md
"""


if os.environ.get(ALLOW_BUILD_ENV) != "1":
    sys.stderr.write(UNSUPPORTED_PLATFORM_MESSAGE)
    raise SystemExit(1)

setup(name="solstone-core-unsupported-platform", version=TOMBSTONE_VERSION)
