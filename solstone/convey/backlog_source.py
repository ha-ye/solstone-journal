# SPDX-License-Identifier: AGPL-3.0-only
# Copyright (c) 2026 sol pbc

"""Read-only loader for the serialized journal backlog source."""

from __future__ import annotations

import json
from dataclasses import dataclass
from pathlib import Path
from typing import Any


@dataclass(frozen=True)
class BacklogSource:
    backlog: dict | None
    validity: str
    generated_at: str | None


def load_backlog_source(journal_root: str) -> BacklogSource:
    path = Path(journal_root) / "stats.json"
    try:
        with path.open("r", encoding="utf-8") as handle:
            data: Any = json.load(handle)
    except FileNotFoundError:
        return BacklogSource(backlog=None, validity="missing", generated_at=None)
    except (OSError, ValueError):
        return BacklogSource(backlog=None, validity="unparseable", generated_at=None)

    if not isinstance(data, dict):
        return BacklogSource(backlog=None, validity="malformed", generated_at=None)

    generated_at = data.get("generated_at")
    if not isinstance(generated_at, str):
        generated_at = None

    if "backlog" not in data:
        return BacklogSource(
            backlog=None,
            validity="no_backlog_key",
            generated_at=generated_at,
        )

    backlog = data.get("backlog")
    if not isinstance(backlog, dict):
        return BacklogSource(
            backlog=None,
            validity="malformed",
            generated_at=generated_at,
        )

    return BacklogSource(backlog=backlog, validity="valid", generated_at=generated_at)
