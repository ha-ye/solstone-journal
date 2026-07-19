# SPDX-License-Identifier: AGPL-3.0-only
# Copyright (c) 2026 sol pbc

"""Main entry point for the solstone.think.indexer package when run as a module."""

import sys

from .cli import main

if __name__ == "__main__":
    sys.exit(main())
