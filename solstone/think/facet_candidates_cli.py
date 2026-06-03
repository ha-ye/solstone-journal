# SPDX-License-Identifier: AGPL-3.0-only
# Copyright (c) 2026 sol pbc

"""CLI entrypoint for recurring facet review-candidate recording."""

from __future__ import annotations

import argparse
from datetime import datetime

from solstone.think.facet_review_candidates import record_facet_candidate
from solstone.think.facets import aggregate_speculative_facets
from solstone.think.utils import require_solstone, setup_cli


def run() -> int:
    """Record recurring facet candidates and return the number refreshed."""
    day = datetime.now().strftime("%Y%m%d")
    candidates = aggregate_speculative_facets()
    for candidate in candidates:
        record_facet_candidate(
            name=candidate["name"],
            name_key=candidate["name_key"],
            count=candidate["count"],
            window_days=candidate["window_days"],
            samples=candidate["samples"],
            day=day,
        )

    count = len(candidates)
    print(f"Recorded/updated {count} facet candidate(s).")
    return count


def main() -> None:
    """Entry point for ``journal facet-candidates``."""
    parser = argparse.ArgumentParser(
        description="Record recurring facet review candidates."
    )
    setup_cli(parser)
    require_solstone()
    run()


if __name__ == "__main__":
    main()
