#!/usr/bin/env python3
# SPDX-License-Identifier: AGPL-3.0-only
# Copyright (c) 2026 sol pbc
"""Lint: the thin-base / journal-host package menu stays internally consistent.

After the package split (`pip install solstone` = thin `sol` client;
`pip install 'solstone[journal]'` = the full journal host), the invariants are:

  1. Base `[project.dependencies]` is exactly the thin access partition — the
     boundary the access-surface import-clean guard enforces. No heavy host
     dependency may leak into base.
  2. There is no `[all]` extra (retired — the menu is base or `[journal]`).
  3. `[journal]` and `[journal-cuda]` are the same stack and BOTH compose
     `[journal-host]` (the shared heavy core minus the transcription runtime).
  4. `[journal-host]` folds in the `[pdf]` and `[whisper]` building blocks
     ("choose journal, get it all").
  5. The CPU/CUDA ONNX runtime split holds: `[journal]` pulls the CPU
     `onnxruntime` and NOT `onnxruntime-gpu`; `[journal-cuda]` pulls
     `onnxruntime-gpu` and NOT the CPU `onnxruntime`. They must never both
     install (the packages own the same `onnxruntime/` import dir).
"""

import sys
import tomllib
from pathlib import Path

# The thin access partition. Adding anything here must keep the `sol` access
# commands import-clean (scripts/check_access_imports_clean.py) — keep this in
# lockstep with pyproject's [project.dependencies].
THIN_BASE = {
    "setproctitle",
    "typer",
    "requests",
    "timefhuman",
    "cryptography>=42",
    "pyOpenSSL>=24.0",
    "websockets>=13.0",
    "psutil",
    "userpath>=1.9.2,<2",
}


def _names(reqs: list[str]) -> set[str]:
    """Bare distribution names (drop version specifiers and markers)."""
    out = set()
    for r in reqs:
        head = r.split(";", 1)[0].strip()
        for sep in ("[", ">", "<", "=", "!", "~", " "):
            head = head.split(sep, 1)[0]
        out.add(head.strip().lower())
    return out


def main() -> int:
    pyproject = Path(__file__).resolve().parent.parent / "pyproject.toml"
    data = tomllib.loads(pyproject.read_text(encoding="utf-8"))
    project = data["project"]
    base = project.get("dependencies", [])
    extras = project.get("optional-dependencies", {})
    errors: list[str] = []

    # 1. Base stays exactly the thin access partition.
    if set(base) != THIN_BASE:
        missing = sorted(THIN_BASE - set(base))
        unexpected = sorted(set(base) - THIN_BASE)
        errors.append("base [project.dependencies] drifted from the thin partition")
        if unexpected:
            errors.append(
                f"  unexpected in base (move to [journal-host]?): {unexpected}"
            )
        if missing:
            errors.append(f"  missing from base: {missing}")

    # 2. [all] is retired.
    if "all" in extras:
        errors.append("[all] extra must be removed — the menu is base or [journal]")

    # 3. journal / journal-cuda both compose journal-host.
    for name in ("journal", "journal-cuda", "journal-host"):
        if name not in extras:
            errors.append(f"missing required extra: [{name}]")
    if not errors:
        for name in ("journal", "journal-cuda"):
            if "solstone[journal-host]" not in extras[name]:
                errors.append(f"[{name}] must reference solstone[journal-host]")

        # 4. journal-host folds pdf + whisper.
        host = extras["journal-host"]
        for block in ("solstone[pdf]", "solstone[whisper]"):
            if block not in host:
                errors.append(f"[journal-host] must fold in {block}")

        # 5. CPU/CUDA ONNX runtime split — never both in one extra.
        journal_names = _names(extras["journal"])
        cuda_names = _names(extras["journal-cuda"])
        if "onnxruntime" not in journal_names:
            errors.append("[journal] must pull the CPU onnxruntime")
        if "onnxruntime-gpu" in journal_names:
            errors.append(
                "[journal] must NOT pull onnxruntime-gpu (that is [journal-cuda])"
            )
        if "onnxruntime-gpu" not in cuda_names:
            errors.append("[journal-cuda] must pull onnxruntime-gpu")
        if "onnxruntime" in cuda_names:
            errors.append(
                "[journal-cuda] must NOT pull the CPU onnxruntime (clobbers the GPU runtime)"
            )

    if errors:
        print("ERROR: package-menu consistency check failed", file=sys.stderr)
        for e in errors:
            print(f"  {e}", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
