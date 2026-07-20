# SPDX-License-Identifier: AGPL-3.0-only
# Copyright (c) 2026 sol pbc

"""Owner-facing copy constants for the speakers app."""

from __future__ import annotations

from typing import Any

SPK_OVERVIEW_YOUR_VOICE_HEADER = "your voice"
SPK_OVERVIEW_YOUR_VOICE_LEARNING = "sol is still learning"
SPK_OVERVIEW_OWNER_PROGRESS_SUFFIX = "longer statements"
SPK_OVERVIEW_OWNER_HELP_LABEL = "help sol learn faster"
SPK_OVERVIEW_OWNER_BUILD_FROM_TAGS_LABEL = "Build from manual tags"
SPK_OVERVIEW_YOUR_VOICE_CONFIRMED = "confirmed"
SPK_OVERVIEW_OWNER_PROGRESS_UNKNOWN = "learning progress unavailable"
SPK_OVERVIEW_OWNER_STATUS_ERROR = "couldn't load voice status"
SPK_OVERVIEW_OWNER_SAMPLES_LABEL = "voice samples"
SPK_OVERVIEW_OWNER_STREAMS_LABEL = "where heard"
SPK_OVERVIEW_OWNER_COHESION_LABEL = "consistency"
SPK_OVERVIEW_OWNER_BUILT_PREFIX = "built"
SPK_OVERVIEW_OWNER_REFRESHED_PREFIX = "refreshed"
SPK_OVERVIEW_OWNER_BUILT_UNKNOWN = "unknown"
SPK_OWNER_TEACH_TITLE = "teach sol your voice"
SPK_OWNER_TEACH_BODY = (
    "pick longer statements that are yours. sol uses them to keep your voice "
    "separate from other speakers."
)
SPK_OWNER_TEACH_START_LABEL = "start teaching"
SPK_OWNER_TEACH_LOADING = "finding teachable statements..."
SPK_OWNER_TEACH_PARTIAL = "some of today could not be included"
SPK_OWNER_TEACH_EMPTY = "no teachable statements are ready yet"
SPK_OWNER_TEACH_PROGRESS_TEMPLATE = "{count} of {minimum} longer statements"
SPK_OWNER_TEACH_MINE_LABEL = "that's me"
SPK_OWNER_TEACH_NOT_ME_LABEL = "not me"
SPK_OWNER_TEACH_NOT_ME_RESPONSE = "ok — moving on"
SPK_OWNER_TEACH_SKIP_LABEL = "skip for now"
SPK_OWNER_TEACH_PAUSE_LABEL = "pause"
SPK_OWNER_TEACH_PAUSED = "teaching paused"
SPK_OWNER_TEACH_EXHAUSTED_TITLE = "that's all for today"
SPK_OWNER_TEACH_EXHAUSTED_BODY_TEMPLATE = (
    "{count} of {minimum} longer statements taught"
)
SPK_OWNER_TEACH_REFUSED_TITLE = "not ready yet"
SPK_OWNER_TEACH_BUSY = "voice update is busy; try the next statement in a moment"
SPK_OWNER_TEACH_FAILED = (
    "voice update did not finish; try another statement in a moment"
)
SPK_OWNER_REVEAL_TITLE = "your voice is confirmed"
SPK_OWNER_REVEAL_STATEMENTS_TEMPLATE = "{count} statements taught"
SPK_OWNER_REVEAL_STREAMS_TEMPLATE = "{count} places heard"
SPK_OWNER_REVEAL_EVIDENCE_TEMPLATE = "{tier} evidence"
SPK_OWNER_REVEAL_CLOSE_LABEL = "continue"
SPK_OVERVIEW_QUALITY_HEADER = "voice quality"
SPK_OVERVIEW_QUALITY_PREBOOTSTRAP = "your voice is still learning"
SPK_OVERVIEW_QUALITY_READY = "recent speaker labels"
SPK_OVERVIEW_QUALITY_HIGH_LABEL = "high confidence"
SPK_OVERVIEW_QUALITY_MEDIUM_LABEL = "needs a look"
SPK_OVERVIEW_QUALITY_UNLABELED_LABEL = "not yet named"
SPK_OVERVIEW_QUALITY_MARGIN_LABEL = "close voice matches"
SPK_OVERVIEW_QUALITY_MISSING_LABEL = "segments not processed"
SPK_OVERVIEW_QUALITY_SKIPPED_LABEL = "set aside for now"
SPK_OVERVIEW_QUALITY_TEACHING_LABEL = "teaching changes"
SPK_OVERVIEW_QUALITY_TEACHING_ZERO = "no recent teaching changes"
SPK_OVERVIEW_QUALITY_UNREADABLE_WARNING = "some local speaker files could not be read"
SPK_OVERVIEW_QUALITY_ERROR_HEADING = "couldn't load voice quality"
SPK_OVERVIEW_KNOWN_VOICES_HEADER = "known voices"
SPK_OVERVIEW_KNOWN_VOICES_SORTS = ["recent", "most samples", "alphabetical"]
SPK_OVERVIEW_CARD_SAMPLES_LABEL = "samples"
SPK_OVERVIEW_CARD_SEGMENTS_LABEL = "segments"
SPK_OVERVIEW_CARD_LAST_HEARD_PREFIX = "last heard"
SPK_OVERVIEW_CARD_STREAMS_PREFIX = "streams"
SPK_OVERVIEW_COHESION_LABELS = [
    "learning",
    "early",
    "improving",
    "good",
    "strong",
    "settled",
]
SPK_OVERVIEW_KNOWN_VOICES_EMPTY = "no one else's voice has been learned yet. once the same voice shows up across a few segments, that person will appear here. you can also tag voices manually in any segment."
SPK_OVERVIEW_NEW_VOICES_HEADER = "new voices"
SPK_OVERVIEW_TODAY_LINK_LABEL = "today's review →"
SPK_FILTER_BY_PREFIX = "filtering by:"
SPK_FILTER_NO_SEGMENTS_TODAY = "no segments attributed to this speaker today"
SPK_GRID_TITLE = "voices to name"
SPK_GRID_BODY = "days with segments still needing names"
SPK_GRID_UNIT_ONE = "segment to name"
SPK_GRID_UNIT_OTHER = "segments to name"
SPK_GRID_UNIT_NONE = "quiet day"
SPK_GRID_ACTIVITY_ONE = "segment, all named"
SPK_GRID_ACTIVITY_OTHER = "segments, all named"
SPK_IDENTIFY_SCOPE = "Naming this voice applies it everywhere it is heard."
SPK_CORRECT_SCOPE = "This fixes this one statement."
SPK_CORRECT_PROPAGATE_OFFER = (
    "Sol can look through segments where these two appear and show you what else "
    "would change."
)
SPK_CORRECT_PROPAGATE_NONE = "Nothing else would change."
SPK_CORRECT_PROPAGATE_APPLY = "Apply these changes"

# API payload prose for owner-status/readiness responses; keep un-prefixed so
# speaker_copy_payload() does not scoop it into template copy.
OWNER_CANDIDATE_CONFIRM_GUIDANCE = (
    "A candidate owner voice exists. It was heard on a single device or stream; "
    "confirm it in the speakers app or with the confirm verb when you ask."
)
OWNER_DETECT_CANDIDATE_GUIDANCE = (
    "Analyze available voice patterns to look for an owner voice candidate."
)
OWNER_REJECTION_COOLDOWN_GUIDANCE = (
    "Wait for the owner voice rejection cooldown before running detection again, "
    "or run sol call speakers detect --force to look now."
)
OWNER_NEEDS_CONFIRM_VOICE_TEXT = (
    "sol found a voice that sounds like you. confirm it in speakers"
)
OWNER_NEEDS_RECURRING_VOICE_TEXT = "sol found a recurring voice. name it in speakers"


def speaker_copy_payload() -> dict[str, Any]:
    """Return copy constants for templates and browser code."""
    return {
        name: value
        for name, value in globals().items()
        if name.startswith("SPK_") and name.isupper()
    }


def speaker_copy_values() -> list[str]:
    """Return all verbatim copy values, flattening list constants."""
    values: list[str] = []
    for value in speaker_copy_payload().values():
        if isinstance(value, str):
            values.append(value)
        elif isinstance(value, list):
            values.extend(item for item in value if isinstance(item, str))
    return values
