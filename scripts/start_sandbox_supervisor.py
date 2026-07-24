#!/usr/bin/env python3
# SPDX-License-Identifier: AGPL-3.0-only
# Copyright (c) 2026 sol pbc

from __future__ import annotations

import os
import subprocess
from collections.abc import Callable
from pathlib import Path
from typing import Any


def _required_env(name: str) -> str:
    try:
        return os.environ[name]
    except KeyError as exc:
        raise RuntimeError(f"Missing required environment variable {name}") from exc


def start_sandbox_supervisor(*, launcher: Callable[..., Any] = subprocess.Popen) -> int:
    log_path = Path(_required_env("SANDBOX_LOG"))
    sandbox_path = _required_env("SANDBOX_PATH")
    journal_bin = _required_env("JOURNAL_BIN")

    log_path.parent.mkdir(parents=True, exist_ok=True)
    env = os.environ.copy()
    env["PATH"] = sandbox_path

    with log_path.open("ab", buffering=0) as log:
        proc = launcher(
            [journal_bin, "supervisor", "0", "--no-daily"],
            stdin=subprocess.DEVNULL,
            stdout=log,
            stderr=subprocess.STDOUT,
            env=env,
            start_new_session=True,
        )
    return int(proc.pid)


def main() -> int:
    # Pass Popen here because the default is bound before test-time patching.
    print(start_sandbox_supervisor(launcher=subprocess.Popen))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
