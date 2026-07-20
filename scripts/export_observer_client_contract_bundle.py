#!/usr/bin/env python3
# SPDX-License-Identifier: AGPL-3.0-only
# Copyright (c) 2026 sol pbc

"""Export the committed observer-client OpenAPI contract bundle."""

from __future__ import annotations

import argparse
import sys
from pathlib import Path

from solstone.convey.contract.observer_bundle_export import (
    BundleExportRefused,
    export_bundle,
)

ROOT = Path(__file__).resolve().parent.parent


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("destination", type=Path, help="New destination directory.")
    args = parser.parse_args()

    try:
        destination = export_bundle(args.destination, ROOT)
    except BundleExportRefused as exc:
        print(str(exc), file=sys.stderr)
        return 1
    print(f"exported observer client contract bundle to {destination}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
