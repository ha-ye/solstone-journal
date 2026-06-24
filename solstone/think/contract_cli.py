# SPDX-License-Identifier: AGPL-3.0-only
# Copyright (c) 2026 sol pbc

"""Build and check the journal at-rest contract bundle."""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

from solstone.think.contract import journal


def _cmd_build(args: argparse.Namespace) -> int:
    if args.check:
        stale = journal.check_artifact()
        if stale:
            for item in stale:
                print(item, file=sys.stderr)
            return 1
        return 0
    journal.write_bundle()
    print(f"wrote {journal.ARTIFACT_PATH.relative_to(journal.ROOT)}")
    return 0


def _cmd_check(args: argparse.Namespace) -> int:
    current = journal.build_bundle()
    try:
        committed = json.loads(journal.ARTIFACT_PATH.read_text(encoding="utf-8"))
    except FileNotFoundError:
        print(
            "journal contract bundle is missing; run `sol contract build`",
            file=sys.stderr,
        )
        return 1

    failed = False
    breaking = journal.classify_breaking_changes(current, committed)
    if breaking:
        failed = True
        for item in breaking:
            print(item, file=sys.stderr)
        print(
            "Journal contract breaking changes detected. Write a forward maint migration "
            "or restore the removed field/path.",
            file=sys.stderr,
        )

    stale = journal.check_artifact()
    if stale:
        failed = True
        for item in stale:
            print(item, file=sys.stderr)

    fixture = journal.ROOT / "tests" / "fixtures" / "journal"
    for root in [fixture, *[Path(value) for value in args.journal]]:
        issues = journal.validate_journal_tree(root, current)
        if issues:
            failed = True
            print(f"journal contract validation failed for {root}:", file=sys.stderr)
            for issue in issues:
                print(f"  {issue}", file=sys.stderr)

    return 1 if failed else 0


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    sub = parser.add_subparsers(dest="command", required=True)

    build = sub.add_parser("build", help="regenerate the committed bundle")
    build.add_argument(
        "--check", action="store_true", help="fail if generated bundle is stale"
    )
    build.set_defaults(func=_cmd_build)

    check = sub.add_parser("check", help="run break, staleness, and conformance checks")
    check.add_argument(
        "--journal",
        action="append",
        default=[],
        help="additional journal root to validate; may be supplied multiple times",
    )
    check.set_defaults(func=_cmd_check)

    args = parser.parse_args(argv)
    return int(args.func(args))


if __name__ == "__main__":
    raise SystemExit(main())
