# SPDX-License-Identifier: AGPL-3.0-only
# Copyright (c) 2026 sol pbc

"""Remove the dissolved services app from existing Convey nav configs."""

from __future__ import annotations

import logging
import sys
from typing import Any

import solstone.convey.state as convey_state
from solstone.convey.config import locked_modify_convey_config
from solstone.think.utils import get_journal

logger = logging.getLogger(__name__)


def _fail(message: str, exc: Exception | None = None) -> None:
    if exc is None:
        logger.error(message)
    else:
        logger.exception(message)
    print(f"ERROR: {message}", file=sys.stderr)
    raise SystemExit(1)


def main():
    try:
        journal = get_journal()
    except Exception as exc:
        _fail("Could not resolve journal for services nav removal", exc)

    convey_state.journal_root = str(journal)

    def _drop_services(config: dict[str, Any]) -> dict[str, Any] | None:
        apps = config.get("apps")
        if not isinstance(apps, dict):
            return None
        changed = False
        for key in ("order", "starred"):
            values = apps.get(key)
            if not isinstance(values, list):
                continue
            filtered = [value for value in values if value != "services"]
            if len(filtered) != len(values):
                apps[key] = filtered
                changed = True
        return config if changed else None

    try:
        result = locked_modify_convey_config(_drop_services)
    except Exception as exc:
        _fail("drop services nav convey-config PERSIST failed", exc)

    if result is None:
        print("Services already absent from app navigation.")
        return

    print("Removed services from app navigation.")


if __name__ == "__main__":
    main()
