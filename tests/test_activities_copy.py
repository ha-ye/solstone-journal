# SPDX-License-Identifier: AGPL-3.0-only
# Copyright (c) 2026 sol pbc

from pathlib import Path

from solstone.apps.activities import copy as activities_copy

SPEC_LITERALS = {
    "PARTICIPATION_SECTION_ATTENDEES": "Attendees",
    "PARTICIPATION_SECTION_MENTIONED": "Mentioned",
    "PARTICIPATION_PROV_VOICE": "heard them speak in this meeting",
    "PARTICIPATION_PROV_SPEAKER_LABEL": "named in the meeting panel",
    "PARTICIPATION_PROV_TRANSCRIPT": "named in transcript only",
    "PARTICIPATION_PROV_SCREEN": "appeared on screen",
    "PARTICIPATION_PROV_OTHER": "noted in this activity",
    "PARTICIPATION_LESS_CERTAIN": "less certain",
    "PARTICIPATION_EMPTY": "We didn't find anyone in this activity.",
    "PARTICIPATION_UNAVAILABLE": "We couldn't read this activity's people.",
}


def test_constants_match_spec():
    for name, expected in SPEC_LITERALS.items():
        assert getattr(activities_copy, name) == expected


def test_js_parity():
    text = Path("solstone/convey/static/activities_copy.js").read_text(encoding="utf-8")

    for literal in SPEC_LITERALS.values():
        assert literal in text
