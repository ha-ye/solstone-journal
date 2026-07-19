# SPDX-License-Identifier: AGPL-3.0-only
# Copyright (c) 2026 sol pbc

"""Move provider install truth to provider-owned status and manifest records."""

from __future__ import annotations

from pathlib import Path
from typing import Any

from solstone.think.providers.install_state import (
    migrate_legacy_provider_artifact_truth,
)
from solstone.think.utils import get_journal

MAINT_RETRY_ON_NEXT_START = True
MAINT_BLOCKS_SUPERVISOR_START = True


def migrate(config: dict[str, Any], journal: Path) -> bool:
    result = migrate_legacy_provider_artifact_truth(journal_path=journal)
    return bool(
        result["actions"] or result["cleanup"]["removed"] or result["cleanup"]["moved"]
    )


def main() -> None:
    journal = Path(get_journal())
    result = migrate_legacy_provider_artifact_truth(journal_path=journal)
    changed = bool(
        result["actions"] or result["cleanup"]["removed"] or result["cleanup"]["moved"]
    )
    if not changed:
        print("Provider install state already uses provider-owned records.")
        return

    for action in result["actions"]:
        print(action["message"])
    cleanup = result["cleanup"]
    if cleanup["moved"]:
        print("Moved local Vulkan device override to providers.local.")
    if cleanup["removed"]:
        print("Removed legacy provider install state from providers.bundled.")


if __name__ == "__main__":
    main()
