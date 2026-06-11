# SPDX-License-Identifier: AGPL-3.0-only
# Copyright (c) 2026 sol pbc

"""Pin the curation app into existing Convey nav configs."""

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
        _fail("Could not resolve journal for curation nav pin", exc)

    convey_state.journal_root = str(journal)

    def _pin_curation(config: dict[str, Any]) -> dict[str, Any] | None:
        apps = config.get("apps")
        if not isinstance(apps, dict):
            return None
        changed = False
        starred = apps.get("starred")
        if isinstance(starred, list) and "curation" not in starred:
            starred.append("curation")
            changed = True
        order = apps.get("order")
        if isinstance(order, list) and order and "curation" not in order:
            order.append("curation")
            changed = True
        return config if changed else None

    try:
        result = locked_modify_convey_config(_pin_curation)
    except Exception as exc:
        _fail("pin curation nav convey-config PERSIST failed", exc)

    if result is None:
        print("Curation already pinned.")
        return

    print("Pinned curation into app navigation.")


if __name__ == "__main__":
    main()
