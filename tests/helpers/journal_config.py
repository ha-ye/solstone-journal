# SPDX-License-Identifier: AGPL-3.0-only
# Copyright (c) 2026 sol pbc

"""Test-only fixture seeding for config/journal.json."""

from __future__ import annotations

import json
import os
from pathlib import Path
from typing import Any


def seed_journal_config(
    config: dict[str, Any] | object,
    journal_path: str | Path | None = None,
) -> Path:
    """Write an exact journal config fixture and return its path."""

    journal = Path(journal_path or os.environ["SOLSTONE_JOURNAL"])
    config_path = journal / "config" / "journal.json"
    config_path.parent.mkdir(parents=True, exist_ok=True)
    tmp_path = config_path.with_name(f".{config_path.name}.seed.tmp")
    tmp_path.write_text(json.dumps(config, indent=2, ensure_ascii=False) + "\n")
    tmp_path.chmod(0o600)
    tmp_path.replace(config_path)
    return config_path
