#!/usr/bin/env python3
# SPDX-License-Identifier: AGPL-3.0-only
# Copyright (c) 2026 sol pbc
from __future__ import annotations

import sys
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO_ROOT / "scripts"))

import build_native_sol_root_contract  # noqa: E402

if __name__ == "__main__":
    sys.argv.append("--check")
    raise SystemExit(build_native_sol_root_contract.main())
