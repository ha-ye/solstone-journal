#!/usr/bin/env python3
# SPDX-License-Identifier: AGPL-3.0-only
# Copyright (c) 2026 sol pbc
from __future__ import annotations

import hashlib
import subprocess
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[1]
EXPECTED_SHA256 = "1d14f01a819f2f44bfe229603aa38861cda3460ff1ca66b9593a33b6172a772d"
FILES = [
    "solstone/apps/activities/call.py",
    "solstone/apps/awareness/call.py",
    "solstone/apps/body/call.py",
    "solstone/apps/chat/call.py",
    "solstone/apps/entities/call.py",
    "solstone/apps/facets/call.py",
    "solstone/apps/import/call.py",
    "solstone/apps/network/call.py",
    "solstone/apps/settings/call.py",
    "solstone/apps/sol/call.py",
    "solstone/apps/speakers/call.py",
    "solstone/apps/support/call.py",
    "solstone/apps/thinking/call.py",
    "solstone/apps/transcripts/call.py",
    "solstone/think/tools/ledger.py",
    "solstone/think/tools/profile.py",
    "solstone/think/chat_cli.py",
    "solstone/think/import_client.py",
    "solstone/think/sol_cli.py",
    "solstone/think/convey_client.py",
]


def manifest_bytes() -> bytes:
    raw = subprocess.check_output(
        ["git", "ls-tree", "HEAD", "--", *FILES],
        cwd=REPO_ROOT,
    )
    lines = raw.splitlines()
    if len(lines) != len(FILES):
        found_paths = {
            line.decode("utf-8").split("\t", maxsplit=1)[1]
            for line in lines
            if b"\t" in line
        }
        missing = sorted(set(FILES) - found_paths)
        raise RuntimeError(
            f"git ls-tree returned {len(lines)} entries; missing: {', '.join(missing)}"
        )
    return b"\n".join(sorted(lines)) + b"\n"


def main() -> int:
    data = manifest_bytes()
    digest = hashlib.sha256(data).hexdigest()
    if digest != EXPECTED_SHA256:
        print("native sol Python manifest digest drifted")
        print(f"expected {EXPECTED_SHA256}")
        print(f"actual   {digest}")
        return 1
    print(f"native sol Python manifest ok: files={len(FILES)} sha256={digest}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
