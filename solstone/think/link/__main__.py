# SPDX-License-Identifier: AGPL-3.0-only
# Copyright (c) 2026 sol pbc

"""python -m solstone.think.link — entry point for caller-side `sol link`."""

from solstone.think.link.cli import main

if __name__ == "__main__":
    raise SystemExit(main())
