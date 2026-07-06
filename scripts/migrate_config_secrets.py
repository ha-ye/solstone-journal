#!/usr/bin/env python3
# SPDX-License-Identifier: AGPL-3.0-only
# Copyright (c) 2026 sol pbc

"""Dry-run or apply the journal-config secret migration."""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from solstone.think.config_secrets_migration import migrate_config_secrets


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description=(
            "Move legacy credential-shaped journal config values into the "
            "machine-local Solstone secret boundary. Defaults to dry-run."
        )
    )
    parser.add_argument(
        "--journal",
        type=Path,
        default=None,
        help="Journal root. Defaults to SOLSTONE_JOURNAL / configured journal.",
    )
    parser.add_argument(
        "--apply",
        action="store_true",
        help="Apply the migration. Without this flag, only prints the plan.",
    )
    args = parser.parse_args(argv)

    result = migrate_config_secrets(journal_path=args.journal, apply=args.apply)
    payload = {
        "journal_path": str(result.journal_path),
        "applied": result.applied,
        "moves": [
            {
                "path": move.path,
                "integration": move.integration,
                "secret_name": move.secret_name,
                "owner": move.owner,
                "true_secret": move.true_secret,
                "note": move.note,
                "action": move.action,
            }
            for move in result.moves
        ],
    }
    print(json.dumps(payload, indent=2, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
