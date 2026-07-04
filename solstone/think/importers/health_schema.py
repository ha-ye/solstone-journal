# SPDX-License-Identifier: AGPL-3.0-only
# Copyright (c) 2026 sol pbc

"""Shared schema helpers for health import prototypes."""

from __future__ import annotations

import hashlib
import json
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
