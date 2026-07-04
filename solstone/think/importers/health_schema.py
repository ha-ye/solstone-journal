# SPDX-License-Identifier: AGPL-3.0-only
# Copyright (c) 2026 sol pbc

"""Shared schema helpers for health import prototypes."""

from __future__ import annotations

import hashlib
import json
import re
from dataclasses import dataclass
from typing import Any, Final, Mapping

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
