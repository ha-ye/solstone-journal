# SPDX-License-Identifier: AGPL-3.0-only
# Copyright (c) 2026 sol pbc

"""Shared schema helpers for health import prototypes."""

from __future__ import annotations

import datetime as dt
import hashlib
import json
import re
from dataclasses import dataclass
from typing import Any, Final, Iterable, Mapping, Sequence

SOURCE_APPLE_HEALTH: Final = "apple_health"
SOURCE_OURA: Final = "oura"
SOURCE_DEXCOM_CLARITY: Final = "dexcom_clarity"

KNOWN_SOURCE_FAMILIES: Final = frozenset(
    {
        SOURCE_APPLE_HEALTH,
        SOURCE_OURA,
        SOURCE_DEXCOM_CLARITY,
    }
)

DEFAULT_HEALTH_IMPORT_STREAM: Final = "import.apple_health"

# Owner-facing names for record types whose derived form reads poorly.
# Everything else falls through to the prettifier below, so new owners'
# device types render acceptably with no code change.
FRIENDLY_TYPE_NAMES: Final[Mapping[str, str]] = {
    "HKQuantityTypeIdentifierBloodGlucose": "Glucose",
    "HKQuantityTypeIdentifierHeartRate": "Heart rate",
    "HKQuantityTypeIdentifierHeartRateVariabilitySDNN": "Heart rate variability",
    "HKQuantityTypeIdentifierRestingHeartRate": "Resting heart rate",
    "HKQuantityTypeIdentifierWalkingHeartRateAverage": "Walking heart rate average",
    "HKQuantityTypeIdentifierHeartRateRecoveryOneMinute": "Heart rate recovery",
    "HKQuantityTypeIdentifierOxygenSaturation": "Blood oxygen",
    "HKQuantityTypeIdentifierRespiratoryRate": "Respiratory rate",
    "HKQuantityTypeIdentifierBloodPressureSystolic": "Blood pressure (systolic)",
    "HKQuantityTypeIdentifierBloodPressureDiastolic": "Blood pressure (diastolic)",
    "HKQuantityTypeIdentifierVO2Max": "VO2 max",
    "HKQuantityTypeIdentifierStepCount": "Step count",
    "HKQuantityTypeIdentifierActiveEnergyBurned": "Active energy",
    "HKQuantityTypeIdentifierBasalEnergyBurned": "Resting energy",
    "HKQuantityTypeIdentifierDistanceWalkingRunning": "Walking + running distance",
    "HKQuantityTypeIdentifierDistanceCycling": "Cycling distance",
    "HKQuantityTypeIdentifierFlightsClimbed": "Flights climbed",
    "HKQuantityTypeIdentifierAppleExerciseTime": "Exercise minutes",
    "HKQuantityTypeIdentifierAppleStandTime": "Stand time",
    "HKCategoryTypeIdentifierAppleStandHour": "Stand hours",
    "HKQuantityTypeIdentifierPhysicalEffort": "Physical effort",
    "HKCategoryTypeIdentifierSleepAnalysis": "Sleep",
    "HKQuantityTypeIdentifierAppleSleepingWristTemperature": "Wrist temperature",
    "HKCategoryTypeIdentifierMindfulSession": "Mindful sessions",
    "HKQuantityTypeIdentifierHeadphoneAudioExposure": "Headphone audio level",
    "HKQuantityTypeIdentifierEnvironmentalAudioExposure": "Environmental audio level",
    "HKQuantityTypeIdentifierTimeInDaylight": "Time in daylight",
    "HKQuantityTypeIdentifierBodyMass": "Body mass",
    "HKQuantityTypeIdentifierBodyMassIndex": "Body mass index",
    "HKQuantityTypeIdentifierBodyFatPercentage": "Body fat",
    "HKQuantityTypeIdentifierLeanBodyMass": "Lean body mass",
    "HKQuantityTypeIdentifierHeight": "Height",
}

# Sleep-analysis intervals from one source separated by less than this gap
# merge into one session (brief wake windows stay inside the night).
SLEEP_SESSION_GAP_MINUTES: Final = 60

# One source's sleep interval or merged session as (start, end).
SleepInterval = tuple[dt.datetime, dt.datetime]


@dataclass(frozen=True, slots=True)
class DaySleep:
    """Canonical sleep for one day: the primary source's night and naps.

    ``main`` is the session that ended that morning (it usually started the
    previous evening); ``naps`` are later sessions fully inside the day.
    Multiple sources are never summed — ``source`` is the longest-coverage
    source and ``other_sources`` are only named.
    """

    source: str
    other_sources: tuple[str, ...]
    main: SleepInterval | None
    naps: tuple[SleepInterval, ...]


def merge_sleep_sessions(
    intervals: Iterable[SleepInterval],
    *,
    gap_minutes: int = SLEEP_SESSION_GAP_MINUTES,
) -> list[SleepInterval]:
    """Merge one source's sleep intervals into sessions, oldest first.

    Intervals separated by ``gap_minutes`` or less join one session; an end
    before its start clamps to a zero-length interval. All datetimes must be
    mutually comparable (all aware or all naive) — callers normalize.
    """

    ordered = sorted(intervals, key=lambda interval: interval[0])
    gap = dt.timedelta(minutes=gap_minutes)
    merged: list[list[dt.datetime]] = []
    for start, end in ordered:
        if end < start:
            end = start
        if merged and start <= merged[-1][1] + gap:
            if end > merged[-1][1]:
                merged[-1][1] = end
        else:
            merged.append([start, end])
    return [(start, end) for start, end in merged]


def pick_main_session(
    sessions: Iterable[SleepInterval],
    day: dt.date,
) -> tuple[SleepInterval | None, list[SleepInterval]]:
    """Split one source's sessions into (main night, naps) for ``day``.

    Only sessions ending on ``day`` count. The main session is the first,
    by end time, that crossed midnight or ended by noon — the night that
    ended that morning. Other sessions starting on ``day`` are naps.
    """

    noon = dt.time(12, 0)
    ending_today = [s for s in sessions if s[1].date() == day]
    main: SleepInterval | None = None
    naps: list[SleepInterval] = []
    for session in sorted(ending_today, key=lambda s: s[1]):
        crosses_midnight = session[0].date() < day
        ends_morning = session[1].time() <= noon
        if main is None and (crosses_midnight or ends_morning):
            main = session
        elif session[0].date() == day:
            naps.append(session)
    return main, naps


def pick_day_sleep(
    intervals_by_source: Mapping[str, Sequence[SleepInterval]],
    day: dt.date,
    *,
    gap_minutes: int = SLEEP_SESSION_GAP_MINUTES,
) -> DaySleep | None:
    """The canonical sleep report for ``day`` across sources.

    Each source's intervals merge into sessions and split into main + naps;
    the source with the longest coverage (main duration, or summed naps when
    it has no main) is primary. Ties resolve to the alphabetically first
    source. Returns ``None`` when no source has a session for the day.
    """

    per_source: dict[str, tuple[SleepInterval | None, list[SleepInterval]]] = {}
    for source, intervals in intervals_by_source.items():
        sessions = merge_sleep_sessions(intervals, gap_minutes=gap_minutes)
        main, naps = pick_main_session(sessions, day)
        if main is not None or naps:
            per_source[source] = (main, naps)
    if not per_source:
        return None

    def _coverage_seconds(source: str) -> float:
        main, naps = per_source[source]
        if main is not None:
            return (main[1] - main[0]).total_seconds()
        return sum((nap[1] - nap[0]).total_seconds() for nap in naps)

    primary = max(sorted(per_source), key=_coverage_seconds)
    main, naps = per_source[primary]
    return DaySleep(
        source=primary,
        other_sources=tuple(name for name in sorted(per_source) if name != primary),
        main=main,
        naps=tuple(naps),
    )


_HK_PREFIX_RE: Final = re.compile(
    r"^HK(?:Quantity|Category)TypeIdentifier|^HKDataType|^HKWorkoutActivityType"
)
_CAMEL_RE: Final = re.compile(r"(?<=[a-z0-9])(?=[A-Z])|(?<=[A-Z])(?=[A-Z][a-z])")


def friendly_type_name(record_type: str) -> str:
    """Owner-facing name for a health record type, never a raw identifier."""

    known = FRIENDLY_TYPE_NAMES.get(record_type)
    if known:
        return known
    stripped = _HK_PREFIX_RE.sub("", record_type)
    words = _CAMEL_RE.sub(" ", stripped)
    return (words[:1].upper() + words[1:].lower()) if words else record_type


@dataclass(frozen=True, slots=True)
class HealthRecordIdentity:
    """Stable identity inputs for one source health record."""

    source_family: str
    record_type: str
    start_time: str
    end_time: str | None = None
    source_record_id: str | None = None
    source_name: str | None = None
    value: Any | None = None
    unit: str | None = None
    metadata: Mapping[str, Any] | None = None


def canonical_json(value: Any) -> str:
    """Return stable ASCII JSON for dedupe hashing."""

    return json.dumps(
        value,
        sort_keys=True,
        separators=(",", ":"),
        ensure_ascii=True,
        default=str,
    )


def health_hash(*parts: str) -> str:
    """Hash namespace-separated parts with a visible algorithm prefix."""

    digest = hashlib.sha256()
    for part in parts:
        digest.update(part.encode("utf-8"))
        digest.update(b"\x1f")
    return f"sha256:{digest.hexdigest()}"


def health_value_hash(
    *,
    value: Any | None,
    unit: str | None = None,
    metadata: Mapping[str, Any] | None = None,
) -> str:
    """Hash a health record value payload without source identity fields."""

    return health_hash(
        "health-value",
        canonical_json(
            {
                "metadata": dict(metadata or {}),
                "unit": unit or "",
                "value": value,
            }
        ),
    )


def health_record_dedupe_key(identity: HealthRecordIdentity) -> str:
    """Return the importer-owned dedupe key for a health record."""

    source_family = identity.source_family.strip().lower()
    record_type = identity.record_type.strip()
    start_time = identity.start_time.strip()
    if not source_family:
        raise ValueError("source_family is required")
    if not record_type:
        raise ValueError("record_type is required")
    if not start_time:
        raise ValueError("start_time is required")

    source_record_id = (identity.source_record_id or "").strip()
    if source_record_id:
        return health_hash(
            "health-record/source-id",
            canonical_json(
                {
                    "record_type": record_type,
                    "source_family": source_family,
                    "source_record_id": source_record_id,
                }
            ),
        )

    return health_hash(
        "health-record/composite",
        canonical_json(
            {
                "end_time": identity.end_time or start_time,
                "record_type": record_type,
                "source_family": source_family,
                "source_name": identity.source_name or "",
                "start_time": start_time,
                "value_hash": health_value_hash(
                    value=identity.value,
                    unit=identity.unit,
                    metadata=identity.metadata,
                ),
            }
        ),
    )
