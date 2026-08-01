# SPDX-License-Identifier: AGPL-3.0-only
# Copyright (c) 2026 sol pbc

"""In-place native runner for the supervised SPL relay service."""

from __future__ import annotations

import os
import sys
from collections.abc import Callable, Sequence
from pathlib import Path

from solstone.think import core_handshake

HandshakeChecker = Callable[[], core_handshake.CoreHandshakeResult]
HelperLocator = Callable[[], Path]
Execv = Callable[[str, Sequence[str]], None]


def exec_native_service(
    args: Sequence[str],
    *,
    handshake_checker: HandshakeChecker = core_handshake.check_solstone_core_handshake,
    helper_locator: HelperLocator = core_handshake.helper_path_for_executable,
    execv: Execv = os.execv,
) -> int:
    """Handshake, then replace this journal process with native SPL service."""
    handshake = handshake_checker()
    if handshake.status != "ok":
        message = handshake.message or "solstone-core handshake did not complete"
        print(message, file=sys.stderr)
        return core_handshake.EX_CONFIG

    helper_path = helper_locator()
    argv = [str(helper_path), "spl", "service", *args]
    try:
        execv(str(helper_path), argv)
    except OSError as exc:
        print(f"journal spl failed to launch solstone-core: {exc}", file=sys.stderr)
        return 75

    # os.execv only returns when substituted in a unit test.
    return 0


def main() -> int:
    """Preserve ``journal spl [-v|-d]`` while handing the PID to Rust."""
    return exec_native_service(sys.argv[1:])
