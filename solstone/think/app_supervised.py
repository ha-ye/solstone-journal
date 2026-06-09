# SPDX-License-Identifier: AGPL-3.0-only
# Copyright (c) 2026 sol pbc

from __future__ import annotations

import logging
import os
import sys
from typing import Sequence

FLAG = "--app-supervised"
SELECTOR_ENV = "SOLSTONE_APP_SUPERVISED"
PARENT_FD_ENV = "SOLSTONE_PARENT_FD"


def is_app_supervised(argv: Sequence[str] | None = None) -> bool:
    args = sys.argv if argv is None else argv
    return FLAG in args or os.environ.get(SELECTOR_ENV) == "1"


def resolve_parent_fd() -> int:
    raw = os.environ.get(PARENT_FD_ENV)
    if raw is None:
        return 0
    try:
        return int(raw)
    except ValueError:
        logging.getLogger(__name__).warning(
            "invalid %s value %r; defaulting parent-death watch fd to stdin",
            PARENT_FD_ENV,
            raw,
        )
        return 0
