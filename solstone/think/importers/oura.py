# SPDX-License-Identifier: AGPL-3.0-only
# Copyright (c) 2026 sol pbc

"""Oura API v2 importer skeleton — parse and normalize synthetic fixtures.

Scope of this module today:

- A parse layer that turns Oura-API-v2-shaped JSON documents (synthetic
  fixtures under ``tests/fixtures/importers/health/oura_synthetic/``) into
  normalized health rows with stable dedupe keys via ``health_schema``.
- A ``FileImporter`` whose ``detect``/``preview``/dry-run paths work on
  those documents. The save path enforces the health pre-save gate and
  then stops at a clearly marked seam.
- Sync and OAuth surfaces that exist only as seams: every one raises
  ``NotImplementedError`` pointing at the design doc. There is no network
  code in this module and none may be added outside the phased plan.

Design doc: ``oura_design_20260705.md`` (Codex outputs, 2026-07-03
check-m-2). The first live OAuth authorization is OWNER-PRESENT-ONLY.
Tokens and client credentials live in journal configuration owned by
``solstone/think/journal_config.py`` — never in this repository, never in
environment variables, never in logs.
"""

from __future__ import annotations

import datetime as dt
import json
import logging
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Callable, Final, Iterable, Mapping

from solstone.think.importers.file_importer import ImportPreview, ImportResult
from solstone.think.importers.health_dedupe import HealthDedupeRecord
from solstone.think.importers.health_schema import (
    SOURCE_OURA_API,
    HealthRecordIdentity,
    health_record_dedupe_key,
    health_value_hash,
)
from solstone.think.importers.pre_save_gate import enforce_pre_save_gate

logger = logging.getLogger(__name__)

NORMALIZED_SCHEMA: Final = "solstone.health.oura.v1"
IMPORT_STREAM: Final = "import.oura"
SYNC_BACKEND_NAME: Final = "oura"
# Journal-config key reserved for owner-present OAuth setup (client id and
# tokens). The value never exists in this repository; the key name is the
# only repo-side artifact of the token boundary.
OAUTH_CONFIG_KEY: Final = "oura"
DESIGN_DOC: Final = "oura_design_20260705.md"

_DAY_SUMMARY_SOURCE_LINE: Final = "brought in via the Oura API"

_MONTH_NAMES: Final = (
    "January",
    "February",
    "March",
    "April",
    "May",
    "June",
    "July",
    "August",
    "September",
    "October",
    "November",
    "December",
)

# Oura API v2 usercollection endpoints this importer understands. Each maps
# to the record types its documents normalize into. Fixture files are named
# ``<endpoint>.json`` and carry the API page shape ``{"data": [...],
# "next_token": ...}``.
ENDPOINT_RECORD_TYPES: Final[Mapping[str, tuple[str, ...]]] = {
    "daily_sleep": ("oura.daily_sleep",),
    "daily_readiness": ("oura.daily_readiness", "oura.temperature_deviation"),
    "daily_resilience": ("oura.daily_resilience",),
    "daily_stress": ("oura.daily_stress",),
    "daily_spo2": ("oura.daily_spo2",),
    "sleep": ("oura.sleep",),
}


class OuraDocumentError(ValueError):
    """Raised when an Oura-shaped JSON document does not match the API shape."""


@dataclass(frozen=True, slots=True)
class OuraNormalizedItem:
    """One normalized row plus its importer-owned dedupe record."""

    row: dict[str, Any]
    dedupe_record: HealthDedupeRecord
    day: str
    month: str


def _not_implemented(surface: str, detail: str) -> NotImplementedError:
    return NotImplementedError(
        f"Oura {surface} is design-only in this phase: {detail} "
        f"See {DESIGN_DOC} before wiring anything live."
    )


# ---------------------------------------------------------------------------
# Parse layer (pure reads; no journal access)
# ---------------------------------------------------------------------------


def parse_endpoint_document(
    endpoint: str, document: Mapping[str, Any]
) -> list[dict[str, Any]]:
    """Validate one API-page-shaped document and return its data items.

    The Oura API v2 returns ``{"data": [...], "next_token": ...}`` pages.
    Every data item must be a JSON object carrying ``id`` and ``day`` —
    both present on all supported usercollection endpoints.
    """

    if endpoint not in ENDPOINT_RECORD_TYPES:
        supported = ", ".join(sorted(ENDPOINT_RECORD_TYPES))
        raise OuraDocumentError(
            f"Unsupported Oura endpoint {endpoint!r}; supported: {supported}"
        )
    data = document.get("data")
    if not isinstance(data, list):
        raise OuraDocumentError(
            f"Oura {endpoint} document must carry a 'data' list, "
            f"got {type(data).__name__}"
        )
    items: list[dict[str, Any]] = []
    for index, item in enumerate(data):
        if not isinstance(item, dict):
            raise OuraDocumentError(
                f"Oura {endpoint} data[{index}] must be an object, "
                f"got {type(item).__name__}"
            )
        if not item.get("id") or not item.get("day"):
            raise OuraDocumentError(
                f"Oura {endpoint} data[{index}] is missing 'id' or 'day'"
            )
        items.append(item)
    return items


def parse_oura_bundle(path: Path) -> dict[str, list[dict[str, Any]]]:
    """Read ``<endpoint>.json`` documents from a directory or single file.

    Returns ``{endpoint: [item, ...]}`` for every supported endpoint file
    present. Unknown filenames are ignored so a bundle directory may carry
    a README; a malformed supported file raises.
    """

    if path.is_file():
        endpoint = path.stem
        document = _load_json_document(path)
        return {endpoint: parse_endpoint_document(endpoint, document)}

    if not path.is_dir():
        raise FileNotFoundError(f"No Oura document bundle at {path}")

    bundle: dict[str, list[dict[str, Any]]] = {}
    for child in sorted(path.iterdir()):
        if not child.is_file() or child.suffix.lower() != ".json":
            continue
        if child.stem not in ENDPOINT_RECORD_TYPES:
            continue
        document = _load_json_document(child)
        bundle[child.stem] = parse_endpoint_document(child.stem, document)
    return bundle


def _load_json_document(path: Path) -> dict[str, Any]:
    try:
        loaded = json.loads(path.read_text(encoding="utf-8"))
    except json.JSONDecodeError as exc:
        raise OuraDocumentError(f"Invalid JSON in {path.name}: {exc}") from exc
    if not isinstance(loaded, dict):
        raise OuraDocumentError(
            f"{path.name} must contain a JSON object, got {type(loaded).__name__}"
        )
    return loaded


def parse_oura_day(value: object) -> str | None:
    """Normalize an Oura ``day`` field (``YYYY-MM-DD``) to ``YYYYMMDD``.

    Oura attributes each sleep document to the day the night ended, which
    matches the journal's cross-midnight canon — the value passes through
    without re-attribution.
    """

    if not isinstance(value, str):
        return None
    try:
        return dt.datetime.strptime(value.strip(), "%Y-%m-%d").strftime("%Y%m%d")
    except ValueError:
        return None


# ---------------------------------------------------------------------------
# Normalization
# ---------------------------------------------------------------------------


def normalize_bundle(
    bundle: Mapping[str, Iterable[Mapping[str, Any]]],
    *,
    import_id: str,
    raw_ref_root: str,
) -> list[OuraNormalizedItem]:
    """Normalize parsed endpoint items into rows with stable dedupe keys."""

    items: list[OuraNormalizedItem] = []
    for endpoint in sorted(bundle):
        for index, item in enumerate(bundle[endpoint], start=1):
            raw_ref = f"{raw_ref_root}#{endpoint}-{index}"
            items.extend(
                _normalize_item(
                    endpoint,
                    dict(item),
                    import_id=import_id,
                    raw_ref=raw_ref,
                )
            )
    return items


def _normalize_item(
    endpoint: str,
    item: dict[str, Any],
    *,
    import_id: str,
    raw_ref: str,
) -> list[OuraNormalizedItem]:
    day = parse_oura_day(item.get("day")) or ""
    rows: list[OuraNormalizedItem] = []
    if endpoint == "daily_sleep":
        rows.append(
            _build_item(
                record_type="oura.daily_sleep",
                kind="daily_summary",
                source_record_id=str(item["id"]),
                day=day,
                start_time=str(item.get("timestamp") or item["day"]),
                value=item.get("score"),
                unit="score",
                metadata=_pick(item, ("contributors",)),
                import_id=import_id,
                raw_ref=raw_ref,
            )
        )
    elif endpoint == "daily_readiness":
        rows.append(
            _build_item(
                record_type="oura.daily_readiness",
                kind="daily_summary",
                source_record_id=str(item["id"]),
                day=day,
                start_time=str(item.get("timestamp") or item["day"]),
                value=item.get("score"),
                unit="score",
                metadata=_pick(item, ("contributors", "temperature_trend_deviation")),
                import_id=import_id,
                raw_ref=raw_ref,
            )
        )
        if item.get("temperature_deviation") is not None:
            rows.append(
                _build_item(
                    record_type="oura.temperature_deviation",
                    kind="daily_summary",
                    # The deviation shares the readiness document; a suffix
                    # keeps its identity distinct from the score row.
                    source_record_id=f"{item['id']}/temperature_deviation",
                    day=day,
                    start_time=str(item.get("timestamp") or item["day"]),
                    value=item.get("temperature_deviation"),
                    unit="degC",
                    metadata={},
                    import_id=import_id,
                    raw_ref=raw_ref,
                )
            )
    elif endpoint == "daily_resilience":
        rows.append(
            _build_item(
                record_type="oura.daily_resilience",
                kind="daily_summary",
                source_record_id=str(item["id"]),
                day=day,
                start_time=str(item.get("timestamp") or item["day"]),
                value=item.get("level"),
                unit=None,
                metadata=_pick(item, ("contributors",)),
                import_id=import_id,
                raw_ref=raw_ref,
            )
        )
    elif endpoint == "daily_stress":
        rows.append(
            _build_item(
                record_type="oura.daily_stress",
                kind="daily_summary",
                source_record_id=str(item["id"]),
                day=day,
                start_time=str(item.get("timestamp") or item["day"]),
                value=item.get("day_summary"),
                unit=None,
                metadata=_pick(item, ("stress_high", "recovery_high")),
                import_id=import_id,
                raw_ref=raw_ref,
            )
        )
    elif endpoint == "daily_spo2":
        spo2 = item.get("spo2_percentage")
        average = spo2.get("average") if isinstance(spo2, dict) else None
        rows.append(
            _build_item(
                record_type="oura.daily_spo2",
                kind="daily_summary",
                source_record_id=str(item["id"]),
                day=day,
                start_time=str(item.get("timestamp") or item["day"]),
                value=average,
                unit="%",
                metadata=_pick(item, ("breathing_disturbance_index",)),
                import_id=import_id,
                raw_ref=raw_ref,
            )
        )
    elif endpoint == "sleep":
        rows.append(
            _build_item(
                record_type="oura.sleep",
                kind="sleep_period",
                source_record_id=str(item["id"]),
                day=day,
                start_time=str(item.get("bedtime_start") or item["day"]),
                end_time=(
                    str(item["bedtime_end"]) if item.get("bedtime_end") else None
                ),
                value=item.get("total_sleep_duration"),
                unit="s",
                metadata=_pick(
                    item,
                    (
                        "type",
                        "deep_sleep_duration",
                        "rem_sleep_duration",
                        "light_sleep_duration",
                        "awake_time",
                        "time_in_bed",
                        "efficiency",
                        "latency",
                        "average_heart_rate",
                        "lowest_heart_rate",
                        "average_hrv",
                        "average_breath",
                        "sleep_phase_5_min",
                    ),
                ),
                import_id=import_id,
                raw_ref=raw_ref,
            )
        )
    else:  # pragma: no cover - parse_endpoint_document rejects these first
        raise OuraDocumentError(f"Unsupported Oura endpoint {endpoint!r}")
    return rows


def _pick(item: Mapping[str, Any], keys: tuple[str, ...]) -> dict[str, Any]:
    return {key: item[key] for key in keys if item.get(key) is not None}


def _build_item(
    *,
    record_type: str,
    kind: str,
    source_record_id: str,
    day: str,
    start_time: str,
    end_time: str | None = None,
    value: Any | None,
    unit: str | None,
    metadata: dict[str, Any],
    import_id: str,
    raw_ref: str,
) -> OuraNormalizedItem:
    dedupe_key = health_record_dedupe_key(
        HealthRecordIdentity(
            source_family=SOURCE_OURA_API,
            record_type=record_type,
            start_time=start_time,
            end_time=end_time,
            source_record_id=source_record_id,
            value=value,
            unit=unit,
            metadata=metadata,
        )
    )
    month = f"{day[:4]}-{day[4:6]}" if day else "undated"
    row = {
        "schema": NORMALIZED_SCHEMA,
        "source_family": SOURCE_OURA_API,
        "kind": kind,
        "dedupe_key": dedupe_key,
        "record_type": record_type,
        "day": day,
        "start_date": start_time,
        "end_date": end_time,
        "source_record_id": source_record_id,
        "unit": unit,
        "value": value,
        "metadata": metadata,
        "raw_ref": raw_ref,
    }
    row = {key: val for key, val in row.items() if val is not None}
    return OuraNormalizedItem(
        row=row,
        dedupe_record=HealthDedupeRecord(
            dedupe_key=dedupe_key,
            source_family=SOURCE_OURA_API,
            record_type=record_type,
            start_time=start_time,
            end_time=end_time,
            source_record_id=source_record_id,
            value_hash=health_value_hash(value=value, unit=unit, metadata=metadata),
            first_import_id=import_id,
            last_seen_import_id=import_id,
            normalized_ref=f"normalized/{month}.jsonl#{dedupe_key}",
            raw_ref=raw_ref,
        ),
        day=day,
        month=month,
    )


# ---------------------------------------------------------------------------
# Day-summary rendering (pure; the save-mode write path is a later phase)
# ---------------------------------------------------------------------------


def render_day_summary(
    day: str, rows: Iterable[Mapping[str, Any]], *, import_id: str
) -> str:
    """Render one day's Oura rows as owner-facing markdown.

    Every line is an attributed fact — the score or label is Oura's, named
    as Oura's, never glossed or interpreted. Deterministic for given rows.
    """

    facts: list[str] = []
    for row in sorted(rows, key=lambda r: str(r.get("record_type") or "")):
        fact = _fact_line(row)
        if fact:
            facts.append(fact)
    lines = [f"# Body · {_pretty_day(day)}", ""]
    if facts:
        lines.extend(facts)
    else:
        lines.append("No Oura entries for this day.")
    lines.extend(
        ["", f"*{_DAY_SUMMARY_SOURCE_LINE} · import {import_id}*"],
    )
    return "\n".join(lines)


def _fact_line(row: Mapping[str, Any]) -> str | None:
    record_type = str(row.get("record_type") or "")
    value = row.get("value")
    if value is None:
        return None
    if record_type == "oura.daily_readiness":
        return f"Readiness {value} · Oura's score"
    if record_type == "oura.daily_sleep":
        return f"Sleep score {value} · Oura's score"
    if record_type == "oura.daily_resilience":
        return f"Resilience {value} · Oura's level"
    if record_type == "oura.daily_stress":
        return f"Day stress summary {value} · Oura's label"
    if record_type == "oura.daily_spo2":
        return f"Nightly blood oxygen {value}% · Oura's average"
    if record_type == "oura.temperature_deviation":
        return f"Temperature deviation {value:+.2f} °C · Oura's measurement"
    if record_type == "oura.sleep":
        duration = _format_duration_seconds(value)
        stages = _stage_phrase(row.get("metadata") or {})
        line = f"Sleep {duration} · Oura's staging"
        if stages:
            line += f" — {stages}"
        return line
    return None


def _stage_phrase(metadata: Mapping[str, Any]) -> str:
    parts: list[str] = []
    for key, label in (
        ("deep_sleep_duration", "deep"),
        ("rem_sleep_duration", "REM"),
        ("light_sleep_duration", "light"),
        ("awake_time", "awake"),
    ):
        seconds = metadata.get(key)
        if seconds is not None:
            parts.append(f"{label} {_format_duration_seconds(seconds)}")
    return ", ".join(parts)


def _format_duration_seconds(value: Any) -> str:
    total_minutes = round(float(value) / 60)
    hours, minutes = divmod(total_minutes, 60)
    if hours:
        return f"{hours}h {minutes:02d}m"
    return f"{minutes}m"


def _pretty_day(day: str) -> str:
    try:
        parsed = dt.datetime.strptime(day, "%Y%m%d")
    except ValueError:
        return day
    return f"{_MONTH_NAMES[parsed.month - 1]} {parsed.day}, {parsed.year}"


# ---------------------------------------------------------------------------
# File importer (detect/preview/dry-run active; save is gated + seamed)
# ---------------------------------------------------------------------------


class OuraImporter:
    name = "oura"
    display_name = "Oura"
    file_patterns = ["daily_sleep.json", "daily_readiness.json", "sleep.json"]
    description = (
        "Preview Oura API v2 JSON documents (synthetic fixtures; "
        "save path is a later, gated phase)"
    )

    def detect(self, path: Path) -> bool:
        try:
            bundle = parse_oura_bundle(path)
        except (OuraDocumentError, FileNotFoundError, OSError):
            return False
        return bool(bundle)

    def preview(self, path: Path) -> ImportPreview:
        bundle = parse_oura_bundle(path)
        items = normalize_bundle(bundle, import_id="preview", raw_ref_root="preview")
        days = sorted({item.day for item in items if item.day})
        endpoint_counts = ", ".join(
            f"{endpoint}={len(list(bundle[endpoint]))}" for endpoint in sorted(bundle)
        )
        return ImportPreview(
            date_range=(days[0], days[-1]) if days else ("", ""),
            item_count=len(items),
            entity_count=0,
            summary=(
                f"{endpoint_counts or 'no documents'}, "
                f"rows={len(items)}, days={len(days)}, "
                f"source_family={SOURCE_OURA_API}"
            ),
        )

    def process(
        self,
        path: Path,
        journal_root: Path,
        *,
        facet: str | None = None,
        import_id: str | None = None,
        progress_callback: Callable | None = None,
        dry_run: bool = False,
        confirm_health_save: bool = False,
    ) -> ImportResult:
        if dry_run:
            preview = self.preview(path)
            return ImportResult(
                entries_written=0,
                entities_seeded=0,
                files_created=[],
                errors=[],
                summary=f"Dry run only: {preview.summary}",
                date_range=preview.date_range,
            )
        # Save mode: the health pre-save gate blocks before any setup,
        # parse, or write. Past the gate, the save path is a later phase.
        enforce_pre_save_gate(
            self,
            dry_run=dry_run,
            confirm_health_save=confirm_health_save,
        )
        raise _not_implemented(
            "save path",
            "normalized-shard and day-summary writes land in phase O1 "
            "on synthetic fixtures only.",
        )


importer = OuraImporter()


# ---------------------------------------------------------------------------
# Sync + OAuth seams (design-only; no network code lives here)
# ---------------------------------------------------------------------------


class OuraSyncBackend:
    """Syncable-backend seam for the Oura API lane.

    Deliberately NOT registered in ``SYNCABLE_REGISTRY`` — a registry entry
    lands with the real implementation so no runtime flow can reach a
    half-built sync. Cursor state is designed to live at
    ``imports/oura.json`` through ``sync.load_sync_state`` /
    ``sync.save_sync_state`` once implemented.
    """

    name = SYNC_BACKEND_NAME

    def sync(self, journal_root: Path, *, dry_run: bool = True) -> dict[str, Any]:
        raise _not_implemented(
            "API sync",
            "polling, backfill, and cursor state land in phase O3 after "
            "the OWNER-PRESENT-ONLY OAuth step (phase O2).",
        )


backend = OuraSyncBackend()


def begin_owner_present_authorization() -> None:
    """Seam for the OAuth authorization-code (PKCE) start step.

    OWNER-PRESENT-ONLY: the first authorization runs interactively with
    the owner at the keyboard; nothing here may run unattended. Tokens and
    client credentials land in journal configuration (key
    ``OAUTH_CONFIG_KEY``) through the config owner — never in this repo.
    """

    raise _not_implemented(
        "OAuth authorization",
        "the OWNER-PRESENT-ONLY browser flow is phase O2.",
    )


def complete_owner_present_authorization(callback_url: str) -> None:
    """Seam for the OAuth code-exchange step (see design doc phase O2)."""

    raise _not_implemented(
        "OAuth code exchange",
        "the OWNER-PRESENT-ONLY exchange step is phase O2.",
    )


def refresh_tokens() -> None:
    """Seam for OAuth token refresh (see design doc phase O2/O3)."""

    raise _not_implemented(
        "OAuth token refresh",
        "refresh handling ships with sync in phase O3.",
    )
