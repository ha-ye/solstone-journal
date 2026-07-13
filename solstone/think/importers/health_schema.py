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
# Rows normalized from Oura API v2 documents. Distinct from SOURCE_OURA,
# which stays reserved for a one-time Oura account export, and from Oura
# rows mirrored through Apple Health (family "apple_health", source_name
# names the ring). Cross-family records never collapse at import time.
SOURCE_OURA_API: Final = "oura_api"
SOURCE_DEXCOM_CLARITY: Final = "dexcom_clarity"

# Card streams contain derived health summaries and are excluded from
# sense/think/entities so they are not mined back into the journal.
HEALTH_CARD_STREAM_BY_FAMILY: Final[Mapping[str, str | None]] = {
    SOURCE_APPLE_HEALTH: "import.apple_health",
    SOURCE_OURA_API: "import.oura",
    SOURCE_OURA: None,
    SOURCE_DEXCOM_CLARITY: None,
}

KNOWN_SOURCE_FAMILIES: Final = frozenset(HEALTH_CARD_STREAM_BY_FAMILY)
HEALTH_CARD_STREAMS: Final = frozenset(
    stream for stream in HEALTH_CARD_STREAM_BY_FAMILY.values() if stream is not None
)

class HealthCardStreamError(ValueError):
    """Raised when a health source family cannot write a chronicle card stream."""


def health_card_stream(family: str) -> str:
    """Return the chronicle card stream for a health source family."""

    try:
        stream = HEALTH_CARD_STREAM_BY_FAMILY[family]
    except KeyError as exc:
        raise HealthCardStreamError(
            f"Unknown health source family: {family!r}"
        ) from exc
    if stream is None:
        raise HealthCardStreamError(
            f"Health source family {family!r} does not declare a chronicle card stream"
        )
    return stream


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
    # Heart-rhythm notifications and AFib burden (Apple Watch, and rhythm-
    # capable cuffs mirrored through HealthKit). Identifiers match the
    # published HealthKit constants: the three category events shipped in
    # iOS 12.2, AtrialFibrillationBurden in iOS 16. Names state what the
    # device reported — notification/burden facts, never a diagnosis.
    "HKCategoryTypeIdentifierIrregularHeartRhythmEvent": (
        "Irregular rhythm notification"
    ),
    "HKCategoryTypeIdentifierHighHeartRateEvent": "High heart-rate notification",
    "HKCategoryTypeIdentifierLowHeartRateEvent": "Low heart-rate notification",
    "HKQuantityTypeIdentifierAtrialFibrillationBurden": "AFib burden",
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
    # Oura API v2 record types (solstone.think.importers.oura). Names stay
    # neutral facts; attribution ("· Oura's score") is applied at render
    # time from the row's source family, never baked into the type name.
    "oura.daily_sleep": "Sleep score",
    "oura.daily_readiness": "Readiness",
    "oura.daily_resilience": "Resilience",
    "oura.daily_stress": "Daytime stress",
    "oura.daily_spo2": "Nightly blood oxygen",
    "oura.temperature_deviation": "Temperature deviation",
    "oura.sleep": "Sleep period",
    # AH-mirror overlap endpoints (O-5C): the same ring data can also
    # arrive mirrored through Apple Health. Names match the signal, not
    # the pipe — presentation-side supersede keeps one canonical pipe.
    "oura.daily_activity": "Daily activity",
    "oura.heartrate": "Heart rate",
    "oura.daily_cardiovascular_age": "Cardiovascular age",
    "oura.blood_glucose": "Blood glucose",
    # 2026-07-07 granted-scope expansion. Workouts/sessions/tags are
    # event rows (no scalar value); the generic name labels the kind and
    # detail (activity, session type, tag text) lives in row metadata for
    # the display side to surface.
    "oura.workout": "Workout",
    "oura.session": "Session",
    "oura.enhanced_tag": "Tag",
    "oura.vo2_max": "VO2 max",
}

# Owner-facing names for Oura score-contributor keys — the anatomies of
# the readiness and sleep scores (Oura API v2 ``contributors`` objects).
# Values are Oura's numbers and render attributed; these labels only name
# them. Unknown keys prettify through ``friendly_contributor_name``.
FRIENDLY_CONTRIBUTOR_NAMES: Final[Mapping[str, str]] = {
    # Readiness contributors.
    "activity_balance": "Activity balance",
    "body_temperature": "Body temperature",
    "hrv_balance": "HRV balance",
    "previous_day_activity": "Previous day activity",
    "previous_night": "Previous night",
    "recovery_index": "Recovery index",
    "resting_heart_rate": "Resting heart rate",
    "sleep_balance": "Sleep balance",
    "sleep_regularity": "Sleep regularity",
    # Sleep-score contributors.
    "deep_sleep": "Deep sleep",
    "efficiency": "Efficiency",
    "latency": "Latency",
    "rem_sleep": "REM sleep",
    "restfulness": "Restfulness",
    "timing": "Timing",
    "total_sleep": "Total sleep",
}


def friendly_contributor_name(key: str) -> str:
    """Owner-facing name for an Oura contributor key, never a raw key."""

    known = FRIENDLY_CONTRIBUTOR_NAMES.get(key)
    if known:
        return known
    words = key.replace("_", " ").strip()
    return (words[:1].upper() + words[1:]) if words else key


# Sleep-analysis intervals from one source separated by less than this gap
# merge into one session (brief wake windows stay inside the night).
SLEEP_SESSION_GAP_MINUTES: Final = 60

# One source's sleep interval or merged session as (start, end).
SleepInterval = tuple[dt.datetime, dt.datetime]

# A sleep interval optionally tagged with its stage value — the raw row
# value such as "HKCategoryValueSleepAnalysisAsleepCore" (or None when the
# source carries no stage detail).
SleepStagedInterval = tuple[dt.datetime, dt.datetime, str | None]


@dataclass(frozen=True, slots=True)
class DaySleep:
    """Canonical sleep for one day: the primary source's night and naps.

    ``main`` is the session that ended that morning (it usually started the
    previous evening); ``naps`` are other sessions fully inside the day —
    a session that runs past the day's midnight belongs to the following
    day's night, never to both days. Multiple sources are never summed —
    ``source`` is the longest-coverage source and ``other_sources`` are
    only named.

    ``in_bed_minutes`` is the merged main-session span; ``asleep_minutes``
    sums the asleep-stage intervals inside it. When the primary source has
    no asleep-stage detail, ``asleep_minutes`` falls back to the merged
    span and ``has_stage_detail`` stays False.
    """

    source: str
    other_sources: tuple[str, ...]
    main: SleepInterval | None
    naps: tuple[SleepInterval, ...]
    in_bed_minutes: float | None = None
    asleep_minutes: float | None = None
    has_stage_detail: bool = False


def sleep_stage_kind(value: object) -> str:
    """Bucket a sleep-analysis row value: asleep / awake / in_bed / unknown.

    Tolerant of raw HealthKit constants ("HKCategoryValueSleepAnalysis
    AsleepCore", "…InBed") and lowercase variants from other sources.
    """

    if value is None:
        return "unknown"
    text = str(value).lower()
    if "asleep" in text:
        return "asleep"
    if "awake" in text:
        return "awake"
    if "inbed" in text or "in_bed" in text or "in bed" in text:
        return "in_bed"
    return "unknown"


def _asleep_minutes_in_session(
    session: SleepInterval, staged: Sequence[SleepStagedInterval]
) -> float | None:
    """Minutes of asleep-stage intervals inside ``session``.

    Returns ``None`` when no interval carries an asleep stage — the caller
    treats that as "no stage detail" and falls back to the merged span.
    Overlapping asleep intervals are unioned so they never double-count.
    """

    clipped: list[SleepInterval] = []
    for start, end, stage in staged:
        if sleep_stage_kind(stage) != "asleep":
            continue
        clip_start = max(start, session[0])
        clip_end = min(end, session[1])
        if clip_end > clip_start:
            clipped.append((clip_start, clip_end))
    if not clipped:
        return None
    merged = merge_sleep_sessions(clipped, gap_minutes=0)
    return sum((end - start).total_seconds() / 60 for start, end in merged)


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
    """Split one source's merged sessions into (main night, naps) for ``day``.

    The main session is the first, by end time, of the sessions ending on
    ``day`` that crossed midnight or ended by noon — the night that ended
    that morning. A merged session is ``day``'s nap only when it starts
    AND ends on ``day`` and is not the main session. A session that ends
    after ``day``'s midnight is the following day's night (or the session
    that day picks as its main) and never attributes to ``day`` — so no
    interval ever lands on two days. Callers must merge over enough of
    the following day's intervals that a bedtime fragment joins its night
    instead of reading as a same-evening nap; a doze that genuinely did
    not merge into the next night stays a nap.
    """

    noon = dt.time(12, 0)
    main: SleepInterval | None = None
    naps: list[SleepInterval] = []
    for session in sorted(sessions, key=lambda s: s[1]):
        if session[1].date() != day:
            # Ends before ``day`` (an earlier night) or past ``day``'s
            # midnight (the next day's night, including the session the
            # next day would pick as its main) — not this day's.
            continue
        crosses_midnight = session[0].date() < day
        ends_morning = session[1].time() <= noon
        if main is None and (crosses_midnight or ends_morning):
            main = session
        elif session[0].date() == day:
            naps.append(session)
    return main, naps


def pick_day_sleep(
    intervals_by_source: Mapping[str, Sequence[SleepInterval | SleepStagedInterval]],
    day: dt.date,
    *,
    gap_minutes: int = SLEEP_SESSION_GAP_MINUTES,
) -> DaySleep | None:
    """The canonical sleep report for ``day`` across sources.

    Each source's intervals merge into sessions and split into main + naps;
    the source with the longest coverage (main duration, or summed naps when
    it has no main) is primary. Ties resolve to the alphabetically first
    source. Returns ``None`` when no source has a session for the day.

    Callers pass intervals spanning the previous day (the night that ended
    this morning starts there) **and** the following day's morning — without
    it, a bedtime fragment cannot merge into the next night and would
    misread as this day's nap.

    Intervals may carry an optional third element — the row's raw stage
    value — which feeds ``asleep_minutes`` / ``has_stage_detail``. Bare
    ``(start, end)`` intervals keep the fallback behavior: asleep equals
    the merged in-bed span.
    """

    per_source: dict[str, tuple[SleepInterval | None, list[SleepInterval]]] = {}
    staged_by_source: dict[str, list[SleepStagedInterval]] = {}
    for source, intervals in intervals_by_source.items():
        staged = [
            (interval[0], interval[1], interval[2] if len(interval) > 2 else None)
            for interval in intervals
        ]
        sessions = merge_sleep_sessions(
            [(start, end) for start, end, _ in staged], gap_minutes=gap_minutes
        )
        main, naps = pick_main_session(sessions, day)
        if main is not None or naps:
            per_source[source] = (main, naps)
            staged_by_source[source] = staged
    if not per_source:
        return None

    def _coverage_seconds(source: str) -> float:
        main, naps = per_source[source]
        if main is not None:
            return (main[1] - main[0]).total_seconds()
        return sum((nap[1] - nap[0]).total_seconds() for nap in naps)

    primary = max(sorted(per_source), key=_coverage_seconds)
    main, naps = per_source[primary]
    in_bed_minutes: float | None = None
    asleep_minutes: float | None = None
    has_stage_detail = False
    if main is not None:
        in_bed_minutes = (main[1] - main[0]).total_seconds() / 60
        staged_asleep = _asleep_minutes_in_session(main, staged_by_source[primary])
        if staged_asleep is not None:
            asleep_minutes = staged_asleep
            has_stage_detail = True
        else:
            asleep_minutes = in_bed_minutes
    return DaySleep(
        source=primary,
        other_sources=tuple(name for name in sorted(per_source) if name != primary),
        main=main,
        naps=tuple(naps),
        in_bed_minutes=in_bed_minutes,
        asleep_minutes=asleep_minutes,
        has_stage_detail=has_stage_detail,
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


# HealthKit stores these percentage types as 0–1 fractions with unit '%';
# owner-facing values scale by 100 so 0.98 renders as 98%, never "1.0 %".
# Matching is by identifier fragment, mirroring the type prettifier above.
_FRACTION_PERCENT_FRAGMENTS: Final = (
    "OxygenSaturation",
    "AppleWalkingSteadiness",
    "WalkingAsymmetryPercentage",
    "WalkingDoubleSupportPercentage",
    "BodyFatPercentage",
    # AFib burden is a HealthKit percent quantity; percent quantities in
    # the XML export follow the 0–1 fraction convention (OxygenSaturation
    # above is the verified precedent). Not yet confirmed against a real
    # AtrialFibrillationBurden export row — if a device exports whole
    # percents, revisit this entry.
    "AtrialFibrillationBurden",
)


# Raw units whose owner-facing label differs regardless of record type.
# HealthKit exports audio levels as 'dBASPL', speeds as 'mi/hr' / 'km/hr';
# energy rows say 'Cal' natively but other exporters write 'kcal'. Oura API
# rows carry 'score' (a unitless number — the label drops) and 'degC' for
# the temperature deviation, whose value renders with its explicit sign.
_FRIENDLY_UNIT_LABELS: Final[Mapping[str, str]] = {
    "dBASPL": "dB",
    "kcal": "Cal",
    "mi/hr": "mph",
    "km/hr": "km/h",
    "score": "",
    "degC": "°C",
}


def friendly_unit_label(record_type: str, unit: str | None) -> str | None:
    """Owner-facing unit label for a health record's raw unit.

    'count/min' reads as 'bpm' for heart-rate-family types (heart rate,
    resting heart rate, walking heart rate average, heart-rate recovery)
    and 'breaths/min' for respiratory rate. Type-independent raw units
    relabel through ``_FRIENDLY_UNIT_LABELS`` ('dBASPL' → 'dB', 'mi/hr' →
    'mph', 'degC' → '°C'). A bare 'count' or Oura 'score' drops to an
    empty label so values render as plain numbers. '%' stays '%'; unknown
    units pass through unchanged.
    """

    if unit is None:
        return None
    if unit == "count/min":
        if "RespiratoryRate" in record_type:
            return "breaths/min"
        if "HeartRate" in record_type:
            return "bpm"
    known = _FRIENDLY_UNIT_LABELS.get(unit)
    if known is not None:
        return known
    if unit == "count":
        return ""
    return unit


def _format_quantity(value: float) -> str:
    if value == int(value):
        return f"{int(value):,}"
    return f"{value:,.1f}"


def display_number(record_type: str, value: float, unit: str | None) -> str:
    """Scaled, formatted value without its unit ('98', '22.3', '6,412').

    Applies the HealthKit fraction-percent convention: types that store
    0–1 fractions with unit '%' scale by 100.
    """

    scaled = value
    if unit == "%" and any(
        fragment in record_type for fragment in _FRACTION_PERCENT_FRAGMENTS
    ):
        scaled = round(value * 100, 6)
    return _format_quantity(scaled)


def display_value(record_type: str, value: float, unit: str | None) -> str:
    """Owner-facing value with its normalized unit ('98%', '72 bpm', '23')."""

    number = display_number(record_type, value, unit)
    unit_label = friendly_unit_label(record_type, unit)
    if not unit_label:
        return number
    if unit_label == "%":
        return f"{number}%"
    return f"{number} {unit_label}"


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
