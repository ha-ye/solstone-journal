# SPDX-License-Identifier: AGPL-3.0-only
# Copyright (c) 2026 sol pbc

"""App-owned scheduled maintenance routines for speaker suggestions."""

from __future__ import annotations

import argparse
import logging
from pathlib import Path

from solstone.think.maintenance import MaintenanceRoutine
from solstone.think.speaker_review_candidates import (
    record_name_variant_candidate,
    review_candidates_path,
)
from solstone.think.utils import get_journal

logger = logging.getLogger(__name__)


def run_name_variants(args: list[str]) -> int:
    """Refresh speaker name-variant review candidates."""
    from solstone.apps.speakers.bootstrap import detect_name_variant_candidates

    parser = argparse.ArgumentParser(
        prog="journal maintenance run speakers:name-variants"
    )
    parser.parse_args(args)

    journal = Path(get_journal())
    detection = detect_name_variant_candidates()
    created = 0
    updated = 0
    for candidate in detection.get("candidates", []):
        _, was_created = record_name_variant_candidate(
            source_id=candidate["source_id"],
            source_label=candidate["source_label"],
            target_id=candidate["target_id"],
            target_label=candidate["target_label"],
            similarity=candidate["similarity"],
            readiness=candidate["readiness"],
        )
        if was_created:
            created += 1
        else:
            updated += 1

    logger.info(
        "speaker name variant candidates refreshed: journal=%s found=%d created=%d updated=%d path=%s",
        journal,
        len(detection.get("candidates", [])),
        created,
        updated,
        review_candidates_path(),
    )
    return 0


ROUTINES = [
    MaintenanceRoutine(
        name="name-variants",
        description="Find speaker name variants for Suggestions.",
        every="daily",
        run=run_name_variants,
        max_runtime="10m",
    ),
]
