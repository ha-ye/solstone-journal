# SPDX-License-Identifier: AGPL-3.0-only
# Copyright (c) 2026 sol pbc

"""Detect breaking changes in the generated native-client OpenAPI contract."""

from __future__ import annotations

import json
import sys
from pathlib import Path

from solstone.convey.contract.assemble import build_document
from solstone.convey.contract.diff import classify_changes

ARTIFACT_PATH = Path("docs/openapi/convey-clients.json")


def main() -> int:
    current = build_document()
    committed = json.loads(ARTIFACT_PATH.read_text(encoding="utf-8"))
    breaking = classify_changes(current, committed)
    if not breaking:
        return 0

    for item in breaking:
        print(item, file=sys.stderr)
    print(
        "OpenAPI contract breaking changes detected: "
        f"{len(breaking)} item(s) above. If intentional, run `make openapi` "
        "to re-pin and notify native-client owners; otherwise revert.",
        file=sys.stderr,
    )
    return 1


if __name__ == "__main__":
    raise SystemExit(main())
