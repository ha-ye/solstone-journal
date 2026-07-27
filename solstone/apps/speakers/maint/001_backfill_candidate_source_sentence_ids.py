# SPDX-License-Identifier: AGPL-3.0-only
# Copyright (c) 2026 sol pbc

"""Backfill speaker candidate source-segment member sentence ids."""

from __future__ import annotations

import argparse

from solstone.apps.speakers.candidate_tracker import CandidateTracker
from solstone.think.utils import setup_cli


def main() -> None:
    parser = argparse.ArgumentParser(
        description=__doc__.split("\n")[0],
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    setup_cli(parser)

    result = CandidateTracker().backfill_source_sentence_ids()
    print("Speaker candidate source sentence-id backfill")
    print(f"  Updated:         {result['updated']}")
    print(f"  Already present: {result['already_present']}")
    print(f"  Unresolved:      {result['unresolved']}")


if __name__ == "__main__":
    main()
