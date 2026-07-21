# SPDX-License-Identifier: AGPL-3.0-only
# Copyright (c) 2026 sol pbc

"""Pin byte-exact Google model aliases in thinking provider config."""

from __future__ import annotations

from pathlib import Path
from typing import Any

from solstone.apps.thinking.google_model_pins import (
    GOOGLE_PRO_ALIAS,
    ChangedModelField,
    pin_google_model_aliases,
    read_google_pro_alias_paths,
)
from solstone.think.journal_config import (
    JournalConfigMutation,
    mutate_journal_config,
)
from solstone.think.utils import get_journal

MAINT_RETRY_ON_NEXT_START = True
MAINT_BLOCKS_SUPERVISOR_START = True


def _pin_line(field: ChangedModelField) -> str:
    path, old_model, new_model = field
    return f"{path}: {old_model} -> {new_model}"


def _pro_line(path: str) -> str:
    return f"{path}: {GOOGLE_PRO_ALIAS} -> choose exact Gemini model"


def _history_lines(
    changed_fields: list[ChangedModelField],
    pro_paths: list[str],
) -> list[str]:
    return [_pin_line(field) for field in changed_fields] + [
        _pro_line(path) for path in pro_paths
    ]


def main() -> None:
    journal = Path(get_journal())

    def apply(config: dict[str, Any]) -> JournalConfigMutation[list[str]]:
        changed_fields = pin_google_model_aliases(config)
        pro_paths = read_google_pro_alias_paths(config)
        return JournalConfigMutation(
            changed=bool(changed_fields),
            value=_history_lines(changed_fields, pro_paths),
        )

    result = mutate_journal_config(apply, journal_path=journal)
    if not result.value:
        print("Google model aliases already pinned.")
        return
    for line in result.value:
        print(line)


if __name__ == "__main__":
    main()
