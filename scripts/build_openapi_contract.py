#!/usr/bin/env python3
# SPDX-License-Identifier: AGPL-3.0-only
# Copyright (c) 2026 sol pbc

"""Build the generated Convey native-client OpenAPI contract."""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

from solstone.convey.contract.assemble import CALLOSUM_REGISTRY, build_document

ROOT = Path(__file__).resolve().parent.parent
ARTIFACT_PATH = ROOT / "docs" / "openapi" / "convey-clients.json"
CONVEY_DOC_PATH = ROOT / "docs" / "CONVEY.md"

BEGIN_MARKER = "<!-- BEGIN GENERATED callosum-registry -->"
END_MARKER = "<!-- END GENERATED callosum-registry -->"


def render_openapi_json() -> str:
    return json.dumps(build_document(), indent=2, sort_keys=True) + "\n"


def render_callosum_registry_block() -> str:
    lines = [
        "| Tract | Events |",
        "|---|---|",
    ]
    for tract in sorted(CALLOSUM_REGISTRY):
        events = ", ".join(f"`{event}`" for event in CALLOSUM_REGISTRY[tract])
        lines.append(f"| `{tract}` | {events} |")
    return "\n".join(lines)


def render_convey_doc(current: str) -> str:
    try:
        before, rest = current.split(BEGIN_MARKER, 1)
        _old, after = rest.split(END_MARKER, 1)
    except ValueError as exc:
        raise ValueError("docs/CONVEY.md is missing Callosum registry markers") from exc

    block = render_callosum_registry_block()
    return f"{before}{BEGIN_MARKER}\n{block}\n{END_MARKER}{after}"


def write_outputs() -> None:
    ARTIFACT_PATH.parent.mkdir(parents=True, exist_ok=True)
    ARTIFACT_PATH.write_text(render_openapi_json(), encoding="utf-8")
    current_doc = CONVEY_DOC_PATH.read_text(encoding="utf-8")
    CONVEY_DOC_PATH.write_text(render_convey_doc(current_doc), encoding="utf-8")
    print(f"wrote {ARTIFACT_PATH.relative_to(ROOT)}")
    print(f"updated {CONVEY_DOC_PATH.relative_to(ROOT)}")


def check_outputs() -> int:
    stale: list[str] = []
    expected_artifact = render_openapi_json()
    try:
        current_artifact = ARTIFACT_PATH.read_text(encoding="utf-8")
    except FileNotFoundError:
        current_artifact = ""
    if current_artifact != expected_artifact:
        stale.append(str(ARTIFACT_PATH.relative_to(ROOT)))

    current_doc = CONVEY_DOC_PATH.read_text(encoding="utf-8")
    expected_doc = render_convey_doc(current_doc)
    if current_doc != expected_doc:
        stale.append(str(CONVEY_DOC_PATH.relative_to(ROOT)))

    if stale:
        paths = ", ".join(stale)
        print(
            f"OpenAPI generated outputs are stale: {paths}. Run: make openapi",
            file=sys.stderr,
        )
        return 1
    return 0


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--check",
        action="store_true",
        help="Check generated outputs without writing files.",
    )
    args = parser.parse_args()

    if args.check:
        return check_outputs()
    write_outputs()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
