# SPDX-License-Identifier: AGPL-3.0-only
# Copyright (c) 2026 sol pbc

"""Dynamic-loader contract for the nvattest provider payload."""

from __future__ import annotations

from pathlib import Path

NVATTEST_LIB_RELPATH = Path("lib")


def nvattest_library_env(nvattest_dir: Path) -> dict[str, str]:
    # The published 1.2.2-sol.2 payload's bin/nvattest carries
    # RUNPATH: [/src/build/release/nv-attestation-sdk-build:] and NEEDED:
    # libnvat.so.1. Without LD_LIBRARY_PATH pointing at the payload's own
    # lib/ directory, the binary exits 127 with "error while loading shared
    # libraries".
    return {"LD_LIBRARY_PATH": str(nvattest_dir / NVATTEST_LIB_RELPATH)}


__all__ = ["NVATTEST_LIB_RELPATH", "nvattest_library_env"]
