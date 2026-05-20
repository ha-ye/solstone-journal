# SPDX-License-Identifier: AGPL-3.0-only
# Copyright (c) 2026 sol pbc

"""Shared journal configuration file helpers."""

from __future__ import annotations

import json
import os
from pathlib import Path
from typing import Any

from solstone.think.utils import get_config, get_journal


def get_journal_config_path() -> Path:
    """Return the canonical journal config path."""

    return Path(get_journal()) / "config" / "journal.json"


def read_journal_config() -> dict[str, Any]:
    """Read journal config through the canonical config resolver."""

    return get_config()


def write_journal_config(config: dict[str, Any]) -> None:
    """Write journal config with stable formatting and private permissions."""

    config_path = get_journal_config_path()
    config_path.parent.mkdir(parents=True, exist_ok=True)
    with open(config_path, "w", encoding="utf-8") as handle:
        json.dump(config, handle, indent=2, ensure_ascii=False)
        handle.write("\n")
    os.chmod(config_path, 0o600)


__all__ = [
    "get_journal_config_path",
    "read_journal_config",
    "write_journal_config",
]
