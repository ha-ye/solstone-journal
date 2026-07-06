# SPDX-License-Identifier: AGPL-3.0-only
# Copyright (c) 2026 sol pbc

"""Oura API v2 importer — parse/normalize layer, fetch client, sync engine.

Scope of this module:

- A parse layer that turns Oura-API-v2-shaped JSON documents into
  normalized health rows with stable dedupe keys via ``health_schema``.
- A ``FileImporter`` whose ``detect``/``preview``/dry-run paths work on
  those documents. The file-import save path enforces the health pre-save
  gate and then stops at a clearly marked seam (a later phase).
- An API fetch layer (``OuraApiClient``) with an **injectable transport**:
  the network-capable ``urllib.request`` import lives only inside
  ``_default_transport`` and is never constructed by tests — every test
  injects canned responses (guard-tested below in the suite).
- A sync engine (``OuraSyncBackend``, registered in ``SYNCABLE_REGISTRY``)
  whose save mode writes one import bundle per run exactly like the
  apple_health save path (normalized monthly shards + manifests + dedupe
  upserts), gated by ``enforce_oura_sync_gate``. Catalog (dry-run) sync
  writes nothing, including no cursor.

Timezone rule (load-bearing): Oura documents carry their own ``day``
field, already attributed by Oura (a night belongs to the day it ended,
matching the journal's cross-midnight canon). The journal day IS Oura's
``day`` field verbatim — never recomputed against local time. Timestamps
are stored as raw ISO strings with their original offsets. The
``heartrate`` series carries no ``day`` field; its day is the date
component of Oura's own offset-bearing timestamp, again verbatim.

Token boundary: access/refresh tokens live outside the journal behind
``solstone.think.importers.local_secrets`` (L2 owner, built separately);
this module only calls its loader/saver through lazy imports. The OAuth
``client_id`` is a PKCE public-client identifier — not a secret — and is
read (read-only) from journal config ``{"oura": {"client_id": ...}}``.

Design doc: ``oura_design_20260705.md`` (Codex outputs, 2026-07-03
check-m-2), amended by the locked morning decisions O-1..O-9 (O-5
amended to C: the AH-mirror overlap endpoints ``heartrate`` and
``daily_activity`` are imported; presentation precedence is a
body-app concern).
"""

from __future__ import annotations

import datetime as dt
import hashlib
import json
import logging
import re
import shutil
import time
from collections import defaultdict
from dataclasses import dataclass, replace
from pathlib import Path
from typing import Any, Callable, Final, Iterable, Mapping

# urllib.parse is pure string handling (query encoding). The
# network-capable urllib.request import lives only inside
# _default_transport — see the no-network guard in tests.
from urllib.parse import urlencode

from solstone.think.importers.file_importer import ImportPreview, ImportResult
from solstone.think.importers.health_dedupe import (
    HealthDedupeRecord,
    upsert_health_dedupe_records,
)
from solstone.think.importers.health_schema import (
    SOURCE_OURA_API,
    HealthRecordIdentity,
    health_record_dedupe_key,
    health_value_hash,
)
from solstone.think.importers.pre_save_gate import (
    enforce_oura_sync_gate,
    enforce_pre_save_gate,
    read_oura_sync_approval,
)
from solstone.think.importers.shared import (
    write_content_manifest,
    write_jsonl_records,
    write_manifest,
)
from solstone.think.importers.sync import load_sync_state, save_sync_state

logger = logging.getLogger(__name__)

NORMALIZED_SCHEMA: Final = "solstone.health.oura.v1"
IMPORT_STREAM: Final = "import.oura"
SYNC_BACKEND_NAME: Final = "oura"
SYNC_STATE_SCHEMA: Final = "solstone.import_sync.oura.v1"
# Journal-config key holding the PKCE public client id (never tokens):
# config/journal.json -> {"oura": {"client_id": ...}} — read-only here.
OAUTH_CONFIG_KEY: Final = "oura"
DESIGN_DOC: Final = "oura_design_20260705.md"

API_BASE_URL: Final = "https://api.ouraring.com/v2/usercollection"
SOURCE_LABEL: Final = "Oura (API)"

# First sync fetches a trailing window only — deliberately NOT deep
# history; backfill is a separate, explicitly windowed decision.
DEFAULT_FIRST_SYNC_WINDOW_DAYS: Final = 30
# Oura revises recent documents (scores settle for a day or two), so
# every run re-fetches this trailing window; document-id dedupe keys make
# the re-fetch an in-place upsert (L9-idempotent).
TRAILING_REFETCH_DAYS: Final = 7

AUTH_LAYER_MISSING_MESSAGE: Final = (
    "Oura auth layer not yet installed "
    "(solstone.think.importers.local_secrets / oura_auth) — the "
    "owner-present OAuth step (phase O2) installs it."
)

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
# "next_token": ...}``. ``daily_activity`` and ``heartrate`` overlap the
# Apple Health mirror and are imported anyway per decision O-5C.
ENDPOINT_RECORD_TYPES: Final[Mapping[str, tuple[str, ...]]] = {
    "daily_sleep": ("oura.daily_sleep",),
    "daily_readiness": ("oura.daily_readiness", "oura.temperature_deviation"),
    "daily_resilience": ("oura.daily_resilience",),
    "daily_stress": ("oura.daily_stress",),
    "daily_spo2": ("oura.daily_spo2",),
    "sleep": ("oura.sleep",),
    "daily_activity": ("oura.daily_activity",),
    "heartrate": ("oura.heartrate",),
}

# Endpoints the sync engine polls, in a fixed fetch order.
SYNC_ENDPOINTS: Final[tuple[str, ...]] = (
    "daily_readiness",
    "daily_sleep",
    "daily_stress",
    "daily_resilience",
    "daily_spo2",
    "sleep",
    "daily_activity",
    "heartrate",
)

# The heartrate series paginates by datetime (start_datetime/end_datetime),
# not by day, and its rows carry no document id or day field.
_DATETIME_PAGED_ENDPOINTS: Final = frozenset({"heartrate"})


class OuraDocumentError(ValueError):
    """Raised when an Oura-shaped JSON document does not match the API shape."""


class OuraApiError(RuntimeError):
    """Raised when the Oura API fetch layer fails loudly."""


class OuraAuthorizationNeeded(OuraApiError):
    """Raised when no usable Oura authorization exists on this machine."""


class OuraSyncStateError(RuntimeError):
    """Raised when the sync cursor at imports/oura.json is unusable."""


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
    Document-shaped endpoints require ``id`` and ``day`` on every item;
    the ``heartrate`` series instead requires ``timestamp`` and ``bpm``
    (its rows carry no document id — see ``_normalize_item``).
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
        if endpoint == "heartrate":
            if not item.get("timestamp") or item.get("bpm") is None:
                raise OuraDocumentError(
                    f"Oura {endpoint} data[{index}] is missing 'timestamp' or 'bpm'"
                )
        elif not item.get("id") or not item.get("day"):
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

    The journal day IS Oura's day field verbatim — Oura attributes each
    sleep document to the day the night ended, which matches the journal's
    cross-midnight canon, so the value passes through with no local-time
    recomputation.
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
    elif endpoint == "daily_activity":
        # AH-mirror overlap endpoint, imported per decision O-5C. Oura's
        # activity score and totals are Oura's numbers; any precedence
        # against mirrored Apple Health steps is presentation-side.
        rows.append(
            _build_item(
                record_type="oura.daily_activity",
                kind="daily_summary",
                source_record_id=str(item["id"]),
                day=day,
                start_time=str(item.get("timestamp") or item["day"]),
                value=item.get("score"),
                unit="score",
                metadata=_pick(
                    item,
                    (
                        "contributors",
                        "steps",
                        "active_calories",
                        "total_calories",
                        "equivalent_walking_distance",
                        "high_activity_time",
                        "medium_activity_time",
                        "low_activity_time",
                        "sedentary_time",
                        "resting_time",
                        "non_wear_time",
                        "average_met_minutes",
                    ),
                ),
                import_id=import_id,
                raw_ref=raw_ref,
            )
        )
    elif endpoint == "heartrate":
        # AH-mirror overlap series, imported per decision O-5C. Heartrate
        # rows carry no document id and no day field: identity is
        # synthesized from Oura's own offset-bearing timestamp plus the
        # sample source, and the day is the date component of that same
        # timestamp verbatim (no local-time recomputation).
        timestamp = str(item["timestamp"])
        source = str(item.get("source") or "unknown")
        rows.append(
            _build_item(
                record_type="oura.heartrate",
                kind="sample",
                source_record_id=f"heartrate/{timestamp}/{source}",
                day=parse_oura_day(timestamp[:10]) or "",
                start_time=timestamp,
                value=item.get("bpm"),
                unit="bpm",
                metadata={"source": source},
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
    # start_date/end_date stay raw ISO strings with Oura's own offsets.
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
    if record_type == "oura.daily_activity":
        return f"Activity score {value} · Oura's score"
    if record_type == "oura.temperature_deviation":
        return f"Temperature deviation {value:+.2f} °C · Oura's measurement"
    if record_type == "oura.sleep":
        duration = _format_duration_seconds(value)
        stages = _stage_phrase(row.get("metadata") or {})
        line = f"Sleep {duration} · Oura's staging"
        if stages:
            line += f" — {stages}"
        return line
    # Heartrate samples are a series, not a day fact — never summarized
    # into prose here (no derived aggregates presented as ours).
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
        # parse, or write — root-explicit against the journal this call
        # would actually write. Past the gate, the save path is a later
        # phase.
        enforce_pre_save_gate(
            self,
            dry_run=dry_run,
            confirm_health_save=confirm_health_save,
            journal_root=journal_root,
        )
        raise _not_implemented(
            "file-import save path",
            "normalized-shard and day-summary writes for file bundles land "
            "in a later phase on synthetic fixtures only (API sync saves "
            "are live via the sync backend).",
        )


importer = OuraImporter()


# ---------------------------------------------------------------------------
# Fetch layer — injectable transport, bounded backoff, one-refresh 401 path
# ---------------------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class OuraTransportResponse:
    """One HTTP response as seen by the fetch layer."""

    status: int
    body: str


# A transport takes (url, headers) and returns an OuraTransportResponse.
OuraTransport = Callable[[str, Mapping[str, str]], OuraTransportResponse]


def _default_transport(url: str, headers: Mapping[str, str]) -> OuraTransportResponse:
    """Live HTTP GET transport.

    The ONLY network-capable code in this module, imported lazily so the
    module itself never touches the network machinery at import time.
    Tests always inject canned transports and never construct this one
    (guard-tested).
    """

    import urllib.error
    import urllib.request

    request = urllib.request.Request(url, headers=dict(headers))
    try:
        with urllib.request.urlopen(request, timeout=30) as response:
            return OuraTransportResponse(
                status=response.status,
                body=response.read().decode("utf-8"),
            )
    except urllib.error.HTTPError as exc:
        return OuraTransportResponse(
            status=exc.code,
            body=exc.read().decode("utf-8", "replace"),
        )


@dataclass(frozen=True, slots=True)
class OuraFetchResult:
    """All pages of one endpoint fetch."""

    items: list[dict[str, Any]]
    pages: list[dict[str, Any]]
    requests: int


def _load_tokens_via_local_secrets() -> Any:
    try:
        from solstone.think.importers.local_secrets import load_oura_tokens
    except ImportError as exc:
        raise OuraAuthorizationNeeded(AUTH_LAYER_MISSING_MESSAGE) from exc
    return load_oura_tokens()


def _save_tokens_via_local_secrets(tokens: Any) -> None:
    from solstone.think.importers.local_secrets import save_oura_tokens

    save_oura_tokens(tokens)


def _refresh_tokens_via_oura_auth(tokens: Any, *, client_id: str) -> Any:
    try:
        from solstone.think.importers.oura_auth import refresh_tokens
    except ImportError as exc:
        raise OuraAuthorizationNeeded(AUTH_LAYER_MISSING_MESSAGE) from exc
    return refresh_tokens(tokens, client_id=client_id)


class OuraApiClient:
    """Minimal Oura API v2 GET client over an injectable transport.

    Rate-limit courtesy: Oura's documented budget is generous relative to
    this client (historically ~5000 requests / 5 minutes); syncs here are
    sequential — one request per page per endpoint per run — and 429s are
    honored with bounded exponential backoff rather than retried hot.

    Auth flow: bearer token from ``local_secrets`` (lazy import; tests
    inject fakes). A 401 triggers exactly one ``oura_auth.refresh_tokens``
    attempt (persisted via ``save_oura_tokens``), then the request is
    retried once; a second 401 fails loud. Backoff sleeps go through an
    injectable ``sleep`` callable so tests never wait.
    """

    def __init__(
        self,
        transport: OuraTransport | None = None,
        *,
        journal_root: Path | str | None = None,
        client_id: str | None = None,
        load_tokens: Callable[[], Any] | None = None,
        save_tokens: Callable[[Any], None] | None = None,
        refresh_tokens: Callable[..., Any] | None = None,
        sleep: Callable[[float], None] | None = None,
        max_attempts: int = 4,
        backoff_base_seconds: float = 1.0,
        max_pages_per_endpoint: int = 100,
    ) -> None:
        self._transport = transport if transport is not None else _default_transport
        self._journal_root = Path(journal_root) if journal_root is not None else None
        self._client_id = client_id
        self._load_tokens = load_tokens or _load_tokens_via_local_secrets
        self._save_tokens = save_tokens or _save_tokens_via_local_secrets
        self._refresh_tokens = refresh_tokens or _refresh_tokens_via_oura_auth
        self._sleep = sleep if sleep is not None else time.sleep
        self._max_attempts = max_attempts
        self._backoff_base_seconds = backoff_base_seconds
        self._max_pages_per_endpoint = max_pages_per_endpoint
        self._tokens: Any = None

    def fetch_endpoint(
        self, endpoint: str, *, start_day: str, end_day: str
    ) -> OuraFetchResult:
        """Fetch every page of one endpoint for a day window."""

        if endpoint not in ENDPOINT_RECORD_TYPES:
            supported = ", ".join(sorted(ENDPOINT_RECORD_TYPES))
            raise OuraApiError(
                f"Unsupported Oura endpoint {endpoint!r}; supported: {supported}"
            )
        params = _endpoint_query(endpoint, start_day, end_day)
        items: list[dict[str, Any]] = []
        pages: list[dict[str, Any]] = []
        requests_made = 0
        next_token: str | None = None
        while True:
            query = dict(params)
            if next_token:
                query["next_token"] = next_token
            page = self._request_json(endpoint, query)
            requests_made += 1
            items.extend(parse_endpoint_document(endpoint, page))
            pages.append(page)
            raw_token = page.get("next_token")
            next_token = raw_token if isinstance(raw_token, str) and raw_token else None
            if next_token is None:
                break
            if len(pages) >= self._max_pages_per_endpoint:
                raise OuraApiError(
                    f"Oura API {endpoint}: pagination exceeded "
                    f"{self._max_pages_per_endpoint} pages — refusing a "
                    "runaway next_token loop."
                )
        return OuraFetchResult(items=items, pages=pages, requests=requests_made)

    def _request_json(self, endpoint: str, query: dict[str, str]) -> dict[str, Any]:
        url = f"{API_BASE_URL}/{endpoint}?{urlencode(sorted(query.items()))}"
        refreshed = False
        retryable_failures = 0
        while True:
            headers = {"Authorization": f"Bearer {self._access_token()}"}
            response = self._transport(url, headers)
            if response.status == 401:
                if refreshed:
                    raise OuraApiError(
                        f"Oura API {endpoint}: still unauthorized after one "
                        "token refresh — re-run owner-present authorization "
                        "(sol import --connect oura)."
                    )
                self._refresh_once()
                refreshed = True
                continue
            if response.status == 429 or 500 <= response.status <= 599:
                retryable_failures += 1
                if retryable_failures >= self._max_attempts:
                    raise OuraApiError(
                        f"Oura API {endpoint}: HTTP {response.status} after "
                        f"{retryable_failures} attempts — giving up."
                    )
                # Bounded exponential backoff; injectable, so tests never
                # actually sleep.
                self._sleep(
                    self._backoff_base_seconds * (2 ** (retryable_failures - 1))
                )
                continue
            if response.status != 200:
                raise OuraApiError(
                    f"Oura API {endpoint}: unexpected HTTP {response.status}."
                )
            try:
                page = json.loads(response.body)
            except json.JSONDecodeError as exc:
                raise OuraApiError(
                    f"Oura API {endpoint}: response is not valid JSON ({exc})."
                ) from exc
            if not isinstance(page, dict):
                raise OuraApiError(
                    f"Oura API {endpoint}: response must be a JSON object, "
                    f"got {type(page).__name__}."
                )
            return page

    def _access_token(self) -> str:
        if self._tokens is None:
            self._tokens = self._load_tokens()
        if self._tokens is None:
            raise OuraAuthorizationNeeded(
                "Oura authorization needed — no tokens on this machine. "
                "Run owner-present: sol import --connect oura"
            )
        return self._tokens.access_token

    def _refresh_once(self) -> None:
        refreshed = self._refresh_tokens(
            self._tokens, client_id=self._resolve_client_id()
        )
        self._save_tokens(refreshed)
        self._tokens = refreshed

    def _resolve_client_id(self) -> str:
        if self._client_id:
            return self._client_id
        # client_id is a PKCE public-client identifier, NOT a secret; it
        # lives in journal config and is read-only here through the config
        # owner's reader (L2: journal_config.py owns config/journal.json).
        from solstone.think.journal_config import read_journal_config

        config = read_journal_config(self._journal_root)
        section = config.get(OAUTH_CONFIG_KEY)
        client_id = section.get("client_id") if isinstance(section, dict) else None
        if not isinstance(client_id, str) or not client_id:
            raise OuraAuthorizationNeeded(
                "Oura client_id missing from journal config "
                '(config/journal.json -> {"oura": {"client_id": ...}}) — '
                "run owner-present: sol import --connect oura"
            )
        return client_id


def _endpoint_query(endpoint: str, start_day: str, end_day: str) -> dict[str, str]:
    if endpoint in _DATETIME_PAGED_ENDPOINTS:
        # The heartrate series paginates by datetime. UTC bounds derived
        # from the day window can skew against the wearer's local days by
        # up to one offset; the >= TRAILING_REFETCH_DAYS overlap on every
        # run plus idempotent upserts make that skew converge (L9).
        return {
            "start_datetime": f"{start_day}T00:00:00+00:00",
            "end_datetime": f"{end_day}T23:59:59+00:00",
        }
    return {"start_date": start_day, "end_date": end_day}


# ---------------------------------------------------------------------------
# Sync engine — one import bundle per save run, cursor at imports/oura.json
# ---------------------------------------------------------------------------


class OuraSyncBackend:
    """Syncable backend for the Oura API lane.

    Save mode mirrors the apple_health save path: one import bundle per
    run under ``imports/<id>/`` (raw page documents, normalized monthly
    shards, manifest, content manifest) plus dedupe upserts into
    ``imports/health-dedupe.sqlite``. Catalog (dry-run) mode fetches and
    reports but writes nothing — including no cursor advance.
    """

    name = SYNC_BACKEND_NAME

    def sync(
        self,
        journal_root: Path,
        *,
        dry_run: bool = True,
        window_days: int | None = None,
        scheduled: bool = False,
        confirm_health_save: bool = False,
        client: OuraApiClient | None = None,
        today: dt.date | None = None,
    ) -> dict[str, Any]:
        journal_root = Path(journal_root)
        if window_days is not None and window_days < 1:
            raise ValueError("window_days must be a positive number of days")

        # Gate before anything else in save mode — before the first fetch,
        # long before the first write. Catalog mode writes nothing (no
        # cursor), so it needs no approval.
        if not dry_run:
            enforce_oura_sync_gate(
                journal_root,
                confirm_health_save=confirm_health_save,
                scheduled=scheduled,
            )

        state = _load_cursor_state(journal_root)
        # Oura day fields are wearer-local; the local date is the best
        # available "newest day". Windows over-fetch and upserts are
        # idempotent, so exactness is not load-bearing.
        resolved_today = today or dt.date.today()

        api = client or OuraApiClient(journal_root=journal_root)
        bundle: dict[str, list[dict[str, Any]]] = {}
        raw_pages: dict[str, list[dict[str, Any]]] = {}
        windows: dict[str, tuple[str, str]] = {}
        pages_fetched = 0
        for endpoint in SYNC_ENDPOINTS:
            window = _sync_window(
                state, endpoint, today=resolved_today, window_days=window_days
            )
            windows[endpoint] = window
            fetched = api.fetch_endpoint(
                endpoint, start_day=window[0], end_day=window[1]
            )
            bundle[endpoint] = fetched.items
            raw_pages[endpoint] = fetched.pages
            pages_fetched += fetched.requests

        if dry_run:
            items = normalize_bundle(
                bundle, import_id="catalog", raw_ref_root="catalog"
            )
            return _sync_result(
                dry_run=True,
                items=items,
                bundle=bundle,
                windows=windows,
                pages_fetched=pages_fetched,
                import_id=None,
                inserted=0,
                updated=0,
                months=[],
                cron_hint=None,
            )

        import_id = _new_import_id(journal_root)
        items = normalize_bundle(
            bundle,
            import_id=import_id,
            raw_ref_root=f"imports/{import_id}/raw/oura",
        )
        saved = _save_sync_bundle(
            journal_root,
            import_id=import_id,
            items=items,
            raw_pages=raw_pages,
            bundle=bundle,
        )

        # The cursor advances only after every bundle write succeeded; a
        # failed run leaves the old cursor so the next run re-fetches the
        # same window and converges (idempotent upserts).
        new_state = _advance_cursor_state(
            state,
            bundle=bundle,
            import_id=import_id,
            rows=len(items),
            inserted=saved["inserted"],
            updated=saved["updated"],
            pages=pages_fetched,
        )
        save_sync_state(journal_root, SYNC_BACKEND_NAME, new_state)

        cron_hint = _scheduled_cron_hint(journal_root)
        return _sync_result(
            dry_run=False,
            items=items,
            bundle=bundle,
            windows=windows,
            pages_fetched=pages_fetched,
            import_id=import_id,
            inserted=saved["inserted"],
            updated=saved["updated"],
            months=saved["months"],
            cron_hint=cron_hint,
        )


backend = OuraSyncBackend()


def _sync_result(
    *,
    dry_run: bool,
    items: list[OuraNormalizedItem],
    bundle: Mapping[str, list[dict[str, Any]]],
    windows: Mapping[str, tuple[str, str]],
    pages_fetched: int,
    import_id: str | None,
    inserted: int,
    updated: int,
    months: list[str],
    cron_hint: str | None,
) -> dict[str, Any]:
    rows = len(items)
    days = sorted({item.day for item in items if item.day})
    endpoint_counts = {endpoint: len(bundle[endpoint]) for endpoint in SYNC_ENDPOINTS}
    if dry_run:
        summary = (
            f"{SOURCE_LABEL} catalog: rows={rows}, days={len(days)}, "
            f"pages={pages_fetched} (nothing written)"
        )
    else:
        summary = (
            f"Saved {SOURCE_LABEL} sync import: rows={rows}, new={inserted}, "
            f"revised={updated}, normalized_months={len(months)}, "
            f"import {import_id}"
        )
    result: dict[str, Any] = {
        "backend": SYNC_BACKEND_NAME,
        "dry_run": dry_run,
        "source_family": SOURCE_OURA_API,
        "source_label": SOURCE_LABEL,
        # Keys the generic sync CLI prints:
        "total": rows,
        "available": rows if dry_run else 0,
        "imported": 0 if dry_run else updated,
        "downloaded": 0 if dry_run else inserted,
        "errors": [],
        # Oura-specific detail:
        "rows": rows,
        "inserted": inserted,
        "updated": updated,
        "pages": pages_fetched,
        "endpoints": endpoint_counts,
        "windows": {endpoint: list(window) for endpoint, window in windows.items()},
        "days": days,
        "months": months,
        "summary": summary,
    }
    if import_id is not None:
        result["import_id"] = import_id
    if cron_hint is not None:
        result["cron_hint"] = cron_hint
    return result


def _new_import_id(journal_root: Path) -> str:
    """Allocate a timestamp import id, suffixing on same-second collisions.

    Two save runs inside one second (or a sync racing a file import) must
    never share a bundle directory (L9). Suffixed ids sort after the bare
    timestamp, so newest-bundle-wins day reads stay correct.
    """

    base = dt.datetime.now().strftime("%Y%m%d_%H%M%S")
    candidate = base
    counter = 1
    while (journal_root / "imports" / candidate).exists():
        counter += 1
        candidate = f"{base}_{counter:02d}"
    return candidate


def _load_cursor_state(journal_root: Path) -> dict[str, Any] | None:
    """Load the sync cursor, failing loud on corruption.

    ``load_sync_state`` returns None for both "absent" and "unreadable";
    a cursor file that exists but cannot be read or carries the wrong
    schema must never be silently treated as a first sync — that would
    quietly re-window history.
    """

    state_path = journal_root / "imports" / f"{SYNC_BACKEND_NAME}.json"
    if not state_path.exists():
        return None
    state = load_sync_state(journal_root, SYNC_BACKEND_NAME)
    if state is None:
        raise OuraSyncStateError(
            f"Corrupt Oura sync cursor at {state_path} — refusing to guess "
            "a window. Inspect and repair or remove the file, then re-run."
        )
    if state.get("schema") != SYNC_STATE_SCHEMA:
        raise OuraSyncStateError(
            f"Unsupported Oura sync cursor schema {state.get('schema')!r} at "
            f"{state_path} (expected {SYNC_STATE_SCHEMA})."
        )
    return state


def _endpoint_watermark(
    state: Mapping[str, Any] | None, endpoint: str
) -> dt.date | None:
    if not state:
        return None
    info = (state.get("endpoints") or {}).get(endpoint) or {}
    raw = info.get("high_water_day")
    if raw is None:
        return None
    try:
        return dt.date.fromisoformat(str(raw))
    except ValueError:
        raise OuraSyncStateError(
            f"Corrupt Oura sync cursor: endpoint {endpoint!r} high_water_day "
            f"{raw!r} is not YYYY-MM-DD."
        ) from None


def _sync_window(
    state: Mapping[str, Any] | None,
    endpoint: str,
    *,
    today: dt.date,
    window_days: int | None,
) -> tuple[str, str]:
    """Resolve one endpoint's fetch window as (start_day, end_day) ISO days."""

    if window_days is not None:
        # An explicit window always wins — the owner asked for it.
        start = today - dt.timedelta(days=window_days)
    else:
        watermark = _endpoint_watermark(state, endpoint)
        if watermark is None:
            # First sync: a trailing window, NOT deep history.
            start = today - dt.timedelta(days=DEFAULT_FIRST_SYNC_WINDOW_DAYS)
        else:
            # No-gap + revision rule: resume the day after the watermark,
            # but never start later than the trailing revision window.
            start = min(
                watermark + dt.timedelta(days=1),
                today - dt.timedelta(days=TRAILING_REFETCH_DAYS),
            )
    if start > today:
        start = today
    return (start.isoformat(), today.isoformat())


def _item_day_iso(endpoint: str, item: Mapping[str, Any]) -> str | None:
    """One item's Oura day as YYYY-MM-DD, verbatim from Oura's own fields."""

    if endpoint in _DATETIME_PAGED_ENDPOINTS:
        raw = str(item.get("timestamp") or "")[:10]
    else:
        raw = str(item.get("day") or "")
    return raw if parse_oura_day(raw) else None


def _advance_cursor_state(
    previous: Mapping[str, Any] | None,
    *,
    bundle: Mapping[str, list[dict[str, Any]]],
    import_id: str,
    rows: int,
    inserted: int,
    updated: int,
    pages: int,
) -> dict[str, Any]:
    endpoints_state: dict[str, dict[str, Any]] = {}
    for endpoint in SYNC_ENDPOINTS:
        watermark = _endpoint_watermark(previous, endpoint)
        fetched_days = [
            day
            for day in (_item_day_iso(endpoint, item) for item in bundle[endpoint])
            if day is not None
        ]
        candidates = [day.isoformat() for day in ([watermark] if watermark else [])]
        candidates.extend(fetched_days)
        endpoints_state[endpoint] = {
            "high_water_day": max(candidates) if candidates else None,
            # next_token is transient pagination state; a completed run
            # always drained it. Persisted for schema completeness.
            "next_token": None,
        }
    return {
        "schema": SYNC_STATE_SCHEMA,
        "last_sync": dt.datetime.now(dt.UTC).isoformat(timespec="seconds"),
        "trailing_refetch_days": TRAILING_REFETCH_DAYS,
        "endpoints": endpoints_state,
        "last_result": {
            "import_id": import_id,
            "rows": rows,
            "inserted": inserted,
            "updated": updated,
            "pages": pages,
        },
    }


def _bundle_content_hash(bundle: Mapping[str, list[dict[str, Any]]]) -> str:
    canonical = json.dumps(
        {endpoint: bundle[endpoint] for endpoint in sorted(bundle)},
        sort_keys=True,
    )
    return hashlib.sha256(canonical.encode("utf-8")).hexdigest()


def _save_sync_bundle(
    journal_root: Path,
    *,
    import_id: str,
    items: list[OuraNormalizedItem],
    raw_pages: Mapping[str, list[dict[str, Any]]],
    bundle: Mapping[str, list[dict[str, Any]]],
) -> dict[str, Any]:
    """Write one sync run's import bundle, mirroring apple_health save mode.

    All file writes route through the importer-owned ``shared`` helpers
    (L2: importer bundle content under ``imports/<id>/``); dedupe rows go
    through ``health_dedupe``.
    """

    import_dir = journal_root / "imports" / import_id

    # Raw page documents land first so every row's raw_ref points at bytes
    # that already exist: one JSONL per endpoint, one verbatim API page
    # per line, under imports/<id>/raw/oura/.
    for endpoint in sorted(raw_pages):
        pages = raw_pages[endpoint]
        if pages:
            write_jsonl_records(
                import_dir / "raw" / "oura" / f"{endpoint}.jsonl", pages
            )

    normalized_by_month: dict[str, list[dict[str, Any]]] = defaultdict(list)
    dedupe_records: list[HealthDedupeRecord] = []
    days: set[str] = set()
    for item in items:
        month_rows = normalized_by_month[item.month]
        normalized_ref = (
            f"imports/{import_id}/normalized/{item.month}.jsonl#L{len(month_rows) + 1}"
        )
        item.row["import_id"] = import_id
        item.row["month"] = item.month
        item.row["normalized_ref"] = normalized_ref
        month_rows.append(item.row)
        dedupe_records.append(
            replace(item.dedupe_record, normalized_ref=normalized_ref)
        )
        if item.day:
            days.add(item.day)

    normalized_paths: list[Path] = []
    for month, month_rows in sorted(normalized_by_month.items()):
        out_path = import_dir / "normalized" / f"{month}.jsonl"
        normalized_paths.append(write_jsonl_records(out_path, month_rows))

    dedupe_result = upsert_health_dedupe_records(journal_root, dedupe_records)

    write_content_manifest(
        import_id,
        _content_manifest_entries(normalized_paths, import_id=import_id),
        journal_root=journal_root,
    )
    write_manifest(
        journal_root,
        import_id,
        SOURCE_OURA_API,
        _bundle_content_hash(bundle),
        len(items),
        files_created=[],
        days_affected=sorted(days),
    )

    return {
        "inserted": dedupe_result.inserted,
        "updated": dedupe_result.updated,
        "months": [path.stem for path in normalized_paths],
    }


def _content_manifest_entries(
    normalized_paths: list[Path], *, import_id: str
) -> list[dict[str, Any]]:
    entries: list[dict[str, Any]] = []
    for path in normalized_paths:
        entries.append(
            {
                "id": f"normalized-{path.stem}",
                "title": f"{SOURCE_LABEL} normalized {path.stem}",
                "date": path.stem.replace("-", ""),
                "type": "health_normalized_month",
                "preview": "Monthly normalized Oura API records",
                "meta": {
                    "import_id": import_id,
                    "path": path.name,
                },
                "segments": [],
            }
        )
    return entries


def _scheduled_cron_hint(journal_root: Path) -> str | None:
    """The exact crontab line to suggest when scheduled consent exists.

    Guidance only — nothing is ever installed. Scheduled runs pass
    ``--scheduled`` and rely on the artifact's standing consent instead of
    the per-run confirmation flag.
    """

    artifact = read_oura_sync_approval(journal_root)
    if not isinstance(artifact, dict):
        return None
    consent = artifact.get("scheduled_sync")
    if not isinstance(consent, dict) or consent.get("approved") is not True:
        return None
    cadence = str(consent.get("cadence") or "")
    hours = _cadence_hours(cadence)
    sol_path = shutil.which("sol") or "sol"
    return f"0 */{hours} * * * {sol_path} import --sync oura --save --scheduled"


def _cadence_hours(cadence: str) -> int:
    """Best-effort hour count from a human cadence string; defaults to 6."""

    match = re.search(r"(\d+)", cadence)
    if match:
        hours = int(match.group(1))
        if 1 <= hours <= 23:
            return hours
    return 6
