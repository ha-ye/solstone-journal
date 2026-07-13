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
  writes nothing, including no cursor. A save run whose fetched rows are
  all already in the dedupe ledger with identical value hashes is a
  quiet run: it writes no bundle at all — only the cursor advances — and
  returns ``quiet_run: true`` (built for hourly scheduled syncs).

Timezone rule (load-bearing): Oura documents carry their own ``day``
field, already attributed by Oura (a night belongs to the day it ended,
matching the journal's cross-midnight canon; ``enhanced_tag`` spells its
day ``start_day`` — see ``_DOCUMENT_DAY_FIELDS``). The journal day IS
Oura's day field verbatim — never recomputed against local time, and
document datetimes (sleep bedtimes, workout/session intervals, tag
times) are wearer-local offset instants kept verbatim. The instant-only
series (``heartrate``, and ``blood_glucose`` by pinned assumption — see
``_normalize_item``) are the exception: they carry no ``day`` field and
Oura returns UTC instants, so samples are converted to the owner's
journal timezone for ``start_date`` and day/month assignment while the
raw timestamp stays in ``source_record_id`` for stable dedupe.

Endpoint roster: ``SYNC_ENDPOINTS`` is what the engine polls;
``_PARTNER_GATED_ENDPOINTS`` (blood_glucose) stay fully wired for parse/
normalize/dedupe but are never fetched — Oura grants their scope only to
partner integrations (2026-07 portal finding).

Token boundary: OAuth tokens and the confidential-client secret live in
the journal — the one trusted store (owner ruling, 2026-07-07; no
machine-local carve-out for device tokens) — in journal config under the
reserved ``oura`` key, read and written exclusively through the config
owner ``solstone/think/journal_config.py`` (L2) via the
``solstone.think.importers.oura_auth`` loaders/savers, lazily imported
here so this module's import graph stays network-free. The OAuth
``client_id`` is a public-client identifier — not a secret — read from
the same config section.

Design doc: ``docs/design/oura-import.md`` (Codex outputs, 2026-07-03
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
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

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
    ScheduledSyncConsent,
    enforce_oura_sync_gate,
    enforce_pre_save_gate,
    read_oura_sync_approval,
)
from solstone.think.importers.shared import (
    write_content_manifest,
    write_json_file,
    write_jsonl_records,
    write_manifest,
)
from solstone.think.importers.sync import load_sync_state, save_sync_state

logger = logging.getLogger(__name__)

NORMALIZED_SCHEMA: Final = "solstone.health.oura.v1"
IMPORT_STREAM: Final = "import.oura"
SYNC_BACKEND_NAME: Final = "oura"
SYNC_STATE_SCHEMA: Final = "solstone.import_sync.oura.v1"
# Journal-config section holding the Oura OAuth material (client_id,
# client_secret, tokens.*): config/journal.json -> {"oura": {...}}.
# Read-only here; writes route through oura_auth -> journal_config (L2).
OAUTH_CONFIG_KEY: Final = "oura"
DESIGN_DOC: Final = "docs/design/oura-import.md"

API_BASE_URL: Final = "https://api.ouraring.com/v2/usercollection"
SOURCE_LABEL: Final = "Oura (API)"

# First sync fetches a trailing window only — deliberately NOT deep
# history; backfill is a separate, explicitly windowed decision.
DEFAULT_FIRST_SYNC_WINDOW_DAYS: Final = 30
# Oura revises recent documents (scores settle for a day or two), so
# every run re-fetches this trailing window; document-id dedupe keys make
# the re-fetch an in-place upsert (L9-idempotent).
TRAILING_REFETCH_DAYS: Final = 7
# Backfill horizon for endpoints that join SYNC_ENDPOINTS after a journal
# already carries a cursor: the first post-upgrade save walks the new
# endpoint's full account history (chunked per _MAX_WINDOW_DAYS) instead
# of a trailing window. 2015 predates the first consumer Oura ring, so no
# account data can be older.
BACKFILL_HORIZON_DAY: Final = "2015-01-01"

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
    "daily_cardiovascular_age": ("oura.daily_cardiovascular_age",),
    "blood_glucose": ("oura.blood_glucose",),
    # 2026-07-07 granted-scope expansion, shapes verified against
    # openapi-1.35 AND live probes (workout/session/enhanced_tag returned
    # real rows; vO2_max is documented but empty for this account). The
    # ``vO2_max`` key doubles as the route segment — that casing is
    # exact (lowercase ``vo2_max`` 404s live); the record type uses the
    # clean lowercase spelling.
    "workout": ("oura.workout",),
    "session": ("oura.session",),
    "enhanced_tag": ("oura.enhanced_tag",),
    "vO2_max": ("oura.vo2_max",),
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
    "daily_cardiovascular_age",
    "workout",
    "session",
    "enhanced_tag",
    "vO2_max",
)

# Endpoints whose scope Oura grants only to partner integrations. The
# owner's developer portal (checked 2026-07) shows every grantable scope
# already enabled and NO ``metabolic`` option — blood_glucose is
# partner-gated (Tidepool-class integrations only), so polling it 401s
# on every run forever. It stays out of SYNC_ENDPOINTS so hourly runs
# stop reporting the unauthorized error each cycle, while the full
# normalization/fetch machinery (parse, dedupe, fixtures, tests) stays
# wired for a future partner grant or file import. Re-enabling is one
# line: move the name back into SYNC_ENDPOINTS. The cursor never carries
# a partner-gated endpoint (and so never marks it backfill_complete), so
# a future re-enable still backfills from BACKFILL_HORIZON_DAY.
_PARTNER_GATED_ENDPOINTS: Final[tuple[str, ...]] = ("blood_glucose",)

# Series endpoints that paginate by datetime (start_datetime/end_datetime),
# not by day; their rows carry no document id or day field. blood_glucose
# membership is a pinned assumption (see _SERIES_REQUIRED_FIELDS).
_DATETIME_PAGED_ENDPOINTS: Final = frozenset({"heartrate", "blood_glucose"})

# Document endpoints whose day attribution field is not literally
# ``day``. enhanced_tag is the one exception (openapi-1.35
# EnhancedTagModel, live-confirmed 2026-07-07): rows carry required
# ``start_day`` (plus nullable ``end_day``) instead — the journal day is
# Oura's ``start_day`` verbatim, matching the day-verbatim rule.
_DOCUMENT_DAY_FIELDS: Final[Mapping[str, str]] = {"enhanced_tag": "start_day"}

# Required row fields per instant-series endpoint: (timestamp field,
# value field). heartrate is documented (openapi-1.35 PublicHeartRateRow:
# timestamp + bpm). blood_glucose is ABSENT from the published spec
# (verified 2026-07-07: openapi-1.35 has no blood_glucose path or schema,
# though the live route exists — missing-token 400 vs 404 for bogus
# routes); its shape here is pinned to Oura's series-row convention of a
# UTC ``timestamp`` plus a domain-named value field (heartrate -> bpm,
# ring_battery_level -> level, hence blood_glucose -> glucose, mg/dL per
# Oura's Stelo integration). blood_glucose is partner-gated (see
# _PARTNER_GATED_ENDPOINTS) so no fetch can currently falsify the pin —
# it holds for a future partner grant or file import, and a mismatch
# fails loudly in parse_endpoint_document, naming the missing fields.
_SERIES_REQUIRED_FIELDS: Final[Mapping[str, tuple[str, str]]] = {
    "heartrate": ("timestamp", "bpm"),
    "blood_glucose": ("timestamp", "glucose"),
}

# Oura rejects over-wide windows per endpoint (heartrate 400s past ~1
# month of datetime range — hit live during the 2026-07-06 full-history
# backfill; blood_glucose is capped identically as a high-frequency
# series by the same pinned assumption). Requests are chunked to these
# maxima and results concatenated; pagination still runs within every
# chunk.
_MAX_WINDOW_DAYS: Final = {"heartrate": 31, "blood_glucose": 31}
_DEFAULT_MAX_WINDOW_DAYS: Final = 364


class OuraDocumentError(ValueError):
    """Raised when an Oura-shaped JSON document does not match the API shape."""


class OuraApiError(RuntimeError):
    """Raised when the Oura API fetch layer fails loudly."""


class OuraAuthorizationNeeded(OuraApiError):
    """Raised when no usable Oura authorization exists for this journal."""


class OuraEndpointUnauthorized(OuraApiError):
    """Raised when one endpoint stays 401 after a good token refresh.

    Distinguishes a scope gap — this authorization cannot read this
    endpoint, while other endpoints keep working — from token death,
    where the refresh grant itself fails and aborts the run.
    """


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
    Document-shaped endpoints require ``id`` and their day field
    (``day``, except ``_DOCUMENT_DAY_FIELDS`` overrides — enhanced_tag
    carries ``start_day``) on every item; instant-series endpoints
    (``_SERIES_REQUIRED_FIELDS``) instead require a timestamp plus their
    value field (their rows carry no document id — see
    ``_normalize_item``).
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
    series_fields = _SERIES_REQUIRED_FIELDS.get(endpoint)
    day_field = _DOCUMENT_DAY_FIELDS.get(endpoint, "day")
    items: list[dict[str, Any]] = []
    for index, item in enumerate(data):
        if not isinstance(item, dict):
            raise OuraDocumentError(
                f"Oura {endpoint} data[{index}] must be an object, "
                f"got {type(item).__name__}"
            )
        if series_fields is not None:
            timestamp_field, value_field = series_fields
            if not item.get(timestamp_field) or item.get(value_field) is None:
                raise OuraDocumentError(
                    f"Oura {endpoint} data[{index}] is missing "
                    f"{timestamp_field!r} or {value_field!r}"
                )
        elif not item.get("id") or not item.get(day_field):
            raise OuraDocumentError(
                f"Oura {endpoint} data[{index}] is missing 'id' or {day_field!r}"
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


def _owner_timezone_for_journal(journal_root: Path | None = None) -> ZoneInfo:
    """Resolve the owner timezone for Oura instant-only endpoints.

    Journal config wins when a journal root is available. Pure preview and
    fixture calls fall back through the existing host/system helper.
    """

    if journal_root is not None:
        try:
            from solstone.think.journal_config import read_journal_config

            config = read_journal_config(journal_root)
        except Exception as exc:  # pragma: no cover - defensive fallback
            logger.warning(
                "Could not read journal timezone from %s; falling back to host timezone: %s",
                journal_root,
                exc,
            )
        else:
            configured = str(config.get("identity", {}).get("timezone") or "").strip()
            if configured:
                try:
                    return ZoneInfo(configured)
                except ZoneInfoNotFoundError:
                    logger.warning(
                        "Invalid identity.timezone %r; falling back to host timezone",
                        configured,
                    )

    from solstone.think.utils import get_owner_timezone

    return get_owner_timezone()


def _timezone_label(owner_timezone: dt.tzinfo) -> str:
    key = getattr(owner_timezone, "key", None)
    if isinstance(key, str) and key:
        return key
    return str(owner_timezone)


def _parse_oura_instant(value: str) -> dt.datetime:
    raw = value.strip()
    normalized = raw[:-1] + "+00:00" if raw.endswith("Z") else raw
    parsed = dt.datetime.fromisoformat(normalized)
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=dt.timezone.utc)
    return parsed


def _owner_local_timestamp(value: str, owner_timezone: dt.tzinfo) -> str:
    return _parse_oura_instant(value).astimezone(owner_timezone).isoformat()


# ---------------------------------------------------------------------------
# Normalization
# ---------------------------------------------------------------------------


def normalize_bundle(
    bundle: Mapping[str, Iterable[Mapping[str, Any]]],
    *,
    import_id: str,
    raw_ref_root: str,
    owner_timezone: dt.tzinfo | None = None,
) -> list[OuraNormalizedItem]:
    """Normalize parsed endpoint items into rows with stable dedupe keys."""

    resolved_timezone = owner_timezone or _owner_timezone_for_journal()
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
                    owner_timezone=resolved_timezone,
                )
            )
    return items


def _normalize_item(
    endpoint: str,
    item: dict[str, Any],
    *,
    import_id: str,
    raw_ref: str,
    owner_timezone: dt.tzinfo,
) -> list[OuraNormalizedItem]:
    day = parse_oura_day(item.get(_DOCUMENT_DAY_FIELDS.get(endpoint, "day"))) or ""
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
        # synthesized from Oura's raw timestamp plus the sample source,
        # while day/month assignment uses the owner's local timezone.
        timestamp = str(item["timestamp"])
        local_timestamp = _owner_local_timestamp(timestamp, owner_timezone)
        source = str(item.get("source") or "unknown")
        rows.append(
            _build_item(
                record_type="oura.heartrate",
                kind="sample",
                source_record_id=f"heartrate/{timestamp}/{source}",
                day=parse_oura_day(local_timestamp[:10]) or "",
                start_time=local_timestamp,
                value=item.get("bpm"),
                unit="bpm",
                metadata={
                    "source": source,
                    "raw_timestamp": timestamp,
                    "timezone": _timezone_label(owner_timezone),
                },
                import_id=import_id,
                raw_ref=raw_ref,
            )
        )
    elif endpoint == "daily_cardiovascular_age":
        # Documented day-granularity endpoint (openapi-1.35
        # PublicDailyCardiovascularAge: id + day required,
        # pulse_wave_velocity m/s and vascular_age years nullable). The
        # journal day is Oura's ``day`` field verbatim, like every other
        # daily document.
        rows.append(
            _build_item(
                record_type="oura.daily_cardiovascular_age",
                kind="daily_summary",
                source_record_id=str(item["id"]),
                day=day,
                start_time=str(item.get("timestamp") or item["day"]),
                value=item.get("vascular_age"),
                unit="years",
                metadata=_pick(item, ("pulse_wave_velocity",)),
                import_id=import_id,
                raw_ref=raw_ref,
            )
        )
    elif endpoint == "blood_glucose":
        # PINNED ASSUMPTION (endpoint absent from openapi-1.35; see
        # _SERIES_REQUIRED_FIELDS): blood_glucose is an instant series
        # shaped like heartrate — no document id, no day field, and
        # ``timestamp`` is a UTC instant (the spec types every series-row
        # timestamp as UtcDateTime; heartrate empirically returns UTC-Z,
        # commit e42d67a7). Day/month assignment therefore converts to
        # the owner's journal timezone while the raw timestamp stays in
        # source_record_id for stable dedupe; values are mg/dL (Oura's
        # Stelo integration). The first post-reauthorization sync
        # confirms or falsifies each pin — tests mark the fixture side.
        timestamp = str(item["timestamp"])
        local_timestamp = _owner_local_timestamp(timestamp, owner_timezone)
        glucose_metadata: dict[str, Any] = {
            "raw_timestamp": timestamp,
            "timezone": _timezone_label(owner_timezone),
        }
        if item.get("source") is not None:
            glucose_metadata["source"] = str(item["source"])
        rows.append(
            _build_item(
                record_type="oura.blood_glucose",
                kind="sample",
                # One CGM stream per account: the raw timestamp alone is
                # the stable sample identity (unlike heartrate, whose
                # sources can collide on a timestamp).
                source_record_id=f"blood_glucose/{timestamp}",
                day=parse_oura_day(local_timestamp[:10]) or "",
                start_time=local_timestamp,
                value=item.get("glucose"),
                unit="mg/dL",
                metadata=glucose_metadata,
                import_id=import_id,
                raw_ref=raw_ref,
            )
        )
    elif endpoint == "workout":
        # PublicWorkout (openapi-1.35; live-confirmed 2026-07-07):
        # required id/activity/day/start_datetime/end_datetime/intensity/
        # source; calories (kcal), distance (m), and label nullable.
        # Datetimes are LocalizedDateTime — wearer-local offsets (live
        # rows carry -04:00..-07:00, never UTC-Z) — so they pass through
        # verbatim like sleep periods, and the journal day is Oura's
        # ``day`` verbatim (a 23:12 workout stays on its local day even
        # though its UTC instant crosses midnight). An event row, like
        # Apple Health workouts: no scalar value — calories/distance are
        # metadata facts and duration derives from the interval at render
        # time. Same-ring-two-pipes precedence against the AH mirror's
        # HKWorkoutActivityType* rows is presentation-side (O-5C).
        rows.append(
            _build_item(
                record_type="oura.workout",
                kind="workout",
                source_record_id=str(item["id"]),
                day=day,
                start_time=str(item.get("start_datetime") or item["day"]),
                end_time=(
                    str(item["end_datetime"]) if item.get("end_datetime") else None
                ),
                value=None,
                unit=None,
                metadata=_pick(
                    item,
                    (
                        "activity",
                        "intensity",
                        "source",
                        "label",
                        "calories",
                        "distance",
                    ),
                ),
                import_id=import_id,
                raw_ref=raw_ref,
            )
        )
    elif endpoint == "session":
        # PublicSession (openapi-1.35; live-confirmed): required id/day/
        # start_datetime/end_datetime/type (breathing|meditation|nap|
        # relaxation|rest|body_status); mood and the heart_rate/
        # heart_rate_variability/motion_count sample blocks nullable.
        # Datetimes are wearer-local offsets, verbatim. The sample blocks
        # ({interval, items[], timestamp}) stay in the raw page — reached
        # through raw_ref — never in normalized metadata: this is an
        # event row, not a series carrier.
        rows.append(
            _build_item(
                record_type="oura.session",
                kind="session",
                source_record_id=str(item["id"]),
                day=day,
                start_time=str(item.get("start_datetime") or item["day"]),
                end_time=(
                    str(item["end_datetime"]) if item.get("end_datetime") else None
                ),
                value=None,
                unit=None,
                metadata=_pick(item, ("type", "mood")),
                import_id=import_id,
                raw_ref=raw_ref,
            )
        )
    elif endpoint == "enhanced_tag":
        # EnhancedTagModel (openapi-1.35; live-confirmed): required id/
        # start_time/start_day — the one document endpoint with no
        # ``day`` field (see _DOCUMENT_DAY_FIELDS). The journal day is
        # Oura's ``start_day`` verbatim; start/end times are wearer-local
        # offsets, verbatim. tag_type_code/comment/custom_name are the
        # owner's own note content — metadata facts, never a value.
        rows.append(
            _build_item(
                record_type="oura.enhanced_tag",
                kind="tag",
                source_record_id=str(item["id"]),
                day=day,
                start_time=str(item.get("start_time") or item["start_day"]),
                end_time=str(item["end_time"]) if item.get("end_time") else None,
                value=None,
                unit=None,
                metadata=_pick(
                    item,
                    ("tag_type_code", "comment", "custom_name", "end_day"),
                ),
                import_id=import_id,
                raw_ref=raw_ref,
            )
        )
    elif endpoint == "vO2_max":
        # PublicVO2Max (openapi-1.35): required id/day/timestamp/vo2_max
        # (integer). Zero rows on this account today, so the shape is
        # documented-only until data appears; the route casing is exactly
        # ``vO2_max`` (lowercase 404s live — verified 2026-07-07). Day is
        # Oura's verbatim; ``timestamp`` is a wearer-local offset
        # instant, verbatim. VO2 max is mL/kg/min by definition (the spec
        # carries no unit field).
        rows.append(
            _build_item(
                record_type="oura.vo2_max",
                kind="daily_summary",
                source_record_id=str(item["id"]),
                day=day,
                start_time=str(item.get("timestamp") or item["day"]),
                value=item.get("vo2_max"),
                unit="mL/kg/min",
                metadata={},
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
    # Document endpoints keep Oura's own offsets. The no-day heartrate
    # endpoint passes owner-local ``start_time`` in from the caller.
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
        return f"Resilience {value} · Oura's label"
    if record_type == "oura.daily_stress":
        return f"Day stress summary {value} · Oura's label"
    if record_type == "oura.daily_spo2":
        return f"Nightly blood oxygen {value}% · Oura's average"
    if record_type == "oura.daily_activity":
        return f"Activity score {value} · Oura's score"
    if record_type == "oura.daily_cardiovascular_age":
        return f"Cardiovascular age {value} · Oura's estimate"
    if record_type == "oura.vo2_max":
        return f"VO2 max {value} · Oura's estimate"
    if record_type == "oura.temperature_deviation":
        return f"Temperature deviation {value:+.2f} °C · Oura's measurement"
    if record_type == "oura.sleep":
        duration = _format_duration_seconds(value)
        stages = _stage_phrase(row.get("metadata") or {})
        line = f"Sleep {duration} · Oura's staging"
        if stages:
            line += f" — {stages}"
        return line
    # Heartrate and blood-glucose samples are series, not day facts —
    # never summarized into prose here (no derived aggregates presented
    # as ours). Workouts, sessions, and tags are value-less event rows —
    # they exit at the value-is-None check above; day surfaces render
    # them from their kind, never from prose here.
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


# oura_auth is imported lazily throughout: it carries network-capable
# machinery (urllib.request, webbrowser, http.server) that must stay out
# of this module's import graph (see the no-network guard in tests).


def _load_tokens_from_config(journal_root: Path | None) -> Any:
    from solstone.think.importers.oura_auth import load_oura_tokens

    return load_oura_tokens(journal_root)


def _save_tokens_to_config(tokens: Any, journal_root: Path | None) -> None:
    from solstone.think.importers.oura_auth import save_oura_tokens

    save_oura_tokens(tokens, journal_root)


def _refresh_tokens_via_oura_auth(
    tokens: Any, *, client_id: str, journal_root: Path | None
) -> Any:
    from solstone.think.importers.oura_auth import refresh_tokens

    return refresh_tokens(tokens, client_id=client_id, journal_root=journal_root)


class OuraApiClient:
    """Minimal Oura API v2 GET client over an injectable transport.

    Rate-limit courtesy: Oura's documented budget is generous relative to
    this client (historically ~5000 requests / 5 minutes); syncs here are
    sequential — one request per page per endpoint per run — and 429s are
    honored with bounded exponential backoff rather than retried hot.

    Auth flow: bearer token from journal config (via ``oura_auth``, lazy
    import; tests inject fakes). A 401 triggers at most one
    ``oura_auth.refresh_tokens`` attempt per client instance (persisted
    via ``save_oura_tokens``), then the request is retried once; a 401
    after a successful refresh raises ``OuraEndpointUnauthorized`` — the
    authorization is missing that endpoint's scope, which the sync engine
    degrades to a per-endpoint skip instead of failing the whole run.
    Backoff sleeps go through an injectable ``sleep`` callable so tests
    never wait.
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
        self._load_tokens = load_tokens or (
            lambda: _load_tokens_from_config(self._journal_root)
        )
        self._save_tokens = save_tokens or (
            lambda tokens: _save_tokens_to_config(tokens, self._journal_root)
        )
        self._refresh_tokens = refresh_tokens or (
            lambda tokens, *, client_id: _refresh_tokens_via_oura_auth(
                tokens, client_id=client_id, journal_root=self._journal_root
            )
        )
        self._sleep = sleep if sleep is not None else time.sleep
        self._max_attempts = max_attempts
        self._backoff_base_seconds = backoff_base_seconds
        self._max_pages_per_endpoint = max_pages_per_endpoint
        self._tokens: Any = None
        self._refresh_attempted = False

    def fetch_endpoint(
        self, endpoint: str, *, start_day: str, end_day: str
    ) -> OuraFetchResult:
        """Fetch every page of one endpoint for a day window.

        Wide windows are split into per-endpoint chunks (Oura rejects
        over-wide ranges; see _MAX_WINDOW_DAYS) and concatenated.
        """

        if endpoint not in ENDPOINT_RECORD_TYPES:
            supported = ", ".join(sorted(ENDPOINT_RECORD_TYPES))
            raise OuraApiError(
                f"Unsupported Oura endpoint {endpoint!r}; supported: {supported}"
            )
        items: list[dict[str, Any]] = []
        pages: list[dict[str, Any]] = []
        requests_made = 0
        for chunk_start, chunk_end in _window_chunks(endpoint, start_day, end_day):
            result = self._fetch_window(
                endpoint, start_day=chunk_start, end_day=chunk_end
            )
            items.extend(result.items)
            pages.extend(result.pages)
            requests_made += result.requests
        return OuraFetchResult(items=items, pages=pages, requests=requests_made)

    def _fetch_window(
        self, endpoint: str, *, start_day: str, end_day: str
    ) -> OuraFetchResult:
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
        retryable_failures = 0
        while True:
            headers = {"Authorization": f"Bearer {self._access_token()}"}
            response = self._transport(url, headers)
            if response.status == 401:
                # At most one refresh per client instance: a 401 with a
                # freshly refreshed token means the grant is missing this
                # endpoint's scope, not that the token expired.
                if self._refresh_attempted:
                    raise OuraEndpointUnauthorized(
                        f"Oura API {endpoint}: still unauthorized after a "
                        "token refresh — the authorization is missing this "
                        "endpoint's scope. Re-run owner-present: "
                        "journal importer --connect oura"
                    )
                self._refresh_once()
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
                "Oura authorization needed — no tokens in journal config. "
                "Run owner-present: journal importer --connect oura"
            )
        return self._tokens.access_token

    def _refresh_once(self) -> None:
        self._refresh_attempted = True
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
                "run owner-present: journal importer --connect oura"
            )
        return client_id


def _window_chunks(
    endpoint: str, start_day: str, end_day: str
) -> list[tuple[str, str]]:
    """Split a day window into endpoint-safe chunks, inclusive bounds."""

    import datetime as _dt

    limit = _MAX_WINDOW_DAYS.get(endpoint, _DEFAULT_MAX_WINDOW_DAYS)
    start = _dt.date.fromisoformat(start_day)
    end = _dt.date.fromisoformat(end_day)
    if start > end:
        return [(start_day, end_day)]
    chunks: list[tuple[str, str]] = []
    cursor = start
    while cursor <= end:
        chunk_end = min(cursor + _dt.timedelta(days=limit - 1), end)
        chunks.append((cursor.isoformat(), chunk_end.isoformat()))
        cursor = chunk_end + _dt.timedelta(days=1)
    return chunks


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
        _gate_decision = None
        if not dry_run:
            _gate_decision = enforce_oura_sync_gate(
                journal_root,
                confirm_health_save=confirm_health_save,
                scheduled=scheduled,
            )

        state = _load_cursor_state(journal_root)
        # Oura day fields are wearer-local; the local date is the best
        # available "newest day". Windows over-fetch and upserts are
        # idempotent, so exactness is not load-bearing.
        resolved_today = today or dt.date.today()
        owner_timezone = _owner_timezone_for_journal(journal_root)

        api = client or OuraApiClient(journal_root=journal_root)
        bundle: dict[str, list[dict[str, Any]]] = {}
        raw_pages: dict[str, list[dict[str, Any]]] = {}
        windows: dict[str, tuple[str, str]] = {}
        errors: list[str] = []
        fetched_endpoints: set[str] = set()
        pages_fetched = 0
        for endpoint in SYNC_ENDPOINTS:
            window = _sync_window(
                state, endpoint, today=resolved_today, window_days=window_days
            )
            windows[endpoint] = window
            try:
                fetched = api.fetch_endpoint(
                    endpoint, start_day=window[0], end_day=window[1]
                )
            except OuraEndpointUnauthorized as exc:
                # Scope gap on one endpoint (e.g. a newly added endpoint
                # before the owner reauthorizes with its scope): skip it,
                # keep every other endpoint syncing, and report the fact.
                # The cursor never marks a skipped endpoint backfilled,
                # so the first post-reauthorization save fetches it from
                # the backfill horizon. Token death is different — the
                # refresh grant itself fails and aborts the whole run.
                logger.warning("Oura sync: %s", exc)
                errors.append(str(exc))
                bundle[endpoint] = []
                raw_pages[endpoint] = []
                continue
            bundle[endpoint] = fetched.items
            raw_pages[endpoint] = fetched.pages
            fetched_endpoints.add(endpoint)
            pages_fetched += fetched.requests

        if dry_run:
            items = normalize_bundle(
                bundle,
                import_id="catalog",
                raw_ref_root="catalog",
                owner_timezone=owner_timezone,
            )
            known = _count_known_rows(journal_root, items)
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
                known_rows=known,
                errors=errors,
            )

        assert _gate_decision is not None

        # Quiet-run check: classify the fetch against the dedupe ledger
        # BEFORE allocating an import id or writing anything. Dedupe keys
        # and value hashes are import-id-independent, so a placeholder
        # normalization compares exactly.
        probe_items = normalize_bundle(
            bundle,
            import_id="quiet",
            raw_ref_root="quiet",
            owner_timezone=owner_timezone,
        )
        new_rows, changed_rows = _ledger_delta(journal_root, probe_items)
        if new_rows == 0 and changed_rows == 0:
            # Nothing new and nothing revised: write NO bundle, NO raw
            # pages, NO normalized shards, NO manifest — only the cursor
            # advances (watermarks/last_sync exactly as a full save would
            # have). A changed value hash anywhere — a trailing-refetch
            # revision — never reaches this branch; that is the
            # revision-capture path and always writes a full bundle.
            new_state = _advance_cursor_state(
                state,
                bundle=bundle,
                import_id=None,
                rows=len(probe_items),
                inserted=0,
                updated=0,
                pages=pages_fetched,
                quiet_run=True,
                fetched_endpoints=fetched_endpoints,
            )
            save_sync_state(journal_root, SYNC_BACKEND_NAME, new_state)
            return _sync_result(
                dry_run=False,
                items=probe_items,
                bundle=bundle,
                windows=windows,
                pages_fetched=pages_fetched,
                import_id=None,
                inserted=0,
                updated=0,
                months=[],
                cron_hint=_scheduled_cron_hint(
                    journal_root,
                    scheduled_sync=_gate_decision.scheduled_sync,
                    read_artifact=False,
                ),
                quiet_run=True,
                errors=errors,
            )

        import_id = _new_import_id(journal_root)
        items = normalize_bundle(
            bundle,
            import_id=import_id,
            raw_ref_root=f"imports/{import_id}/raw/oura",
            owner_timezone=owner_timezone,
        )
        saved = _save_sync_bundle(
            journal_root,
            import_id=import_id,
            items=items,
            raw_pages=raw_pages,
            bundle=bundle,
            windows=windows,
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
            fetched_endpoints=fetched_endpoints,
        )
        save_sync_state(journal_root, SYNC_BACKEND_NAME, new_state)

        cron_hint = _scheduled_cron_hint(
            journal_root,
            scheduled_sync=_gate_decision.scheduled_sync,
            read_artifact=False,
        )
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
            errors=errors,
        )


backend = OuraSyncBackend()


def _count_known_rows(journal_root: Path, items: list[OuraNormalizedItem]) -> int:
    """How many normalized rows the dedupe ledger already holds (read-only)."""

    keys = [item.dedupe_record.dedupe_key for item in items]
    if not keys:
        return 0
    db_path = journal_root / "imports" / "health-dedupe.sqlite"
    if not db_path.is_file():
        return 0
    import sqlite3

    known = 0
    conn = sqlite3.connect(f"file:{db_path}?mode=ro", uri=True, timeout=15)
    try:
        for start in range(0, len(keys), 500):
            chunk = keys[start : start + 500]
            marks = ",".join("?" for _ in chunk)
            (count,) = conn.execute(
                f"SELECT COUNT(*) FROM health_dedupe WHERE dedupe_key IN ({marks})",
                chunk,
            ).fetchone()
            known += count
    finally:
        conn.close()
    return known


def _ledger_delta(
    journal_root: Path, items: list[OuraNormalizedItem]
) -> tuple[int, int]:
    """(new, changed) dedupe keys vs the dedupe ledger (read-only).

    ``new`` counts keys the ledger has never seen; ``changed`` counts
    known keys whose stored value hash or time bounds differ from this
    fetch's. Value comparisons come from ``health_value_hash`` — the
    fetch side via ``_build_item``, the stored side persisted verbatim by
    ``upsert_health_dedupe_records`` — so the canonicalization cannot
    drift. Time comparisons catch normalization corrections such as the
    Oura heartrate UTC→owner-local fix. Any duplicate of a key within the
    batch (page overlap) counts as changed if any copy mismatches.
    """

    if not items:
        return (0, 0)
    keys = sorted({item.dedupe_record.dedupe_key for item in items})
    db_path = journal_root / "imports" / "health-dedupe.sqlite"
    stored: dict[str, tuple[str | None, str | None, str | None]] = {}
    if db_path.is_file():
        import sqlite3

        conn = sqlite3.connect(f"file:{db_path}?mode=ro", uri=True, timeout=15)
        try:
            for start in range(0, len(keys), 500):
                chunk = keys[start : start + 500]
                marks = ",".join("?" for _ in chunk)
                for key, value_hash, start_time, end_time in conn.execute(
                    "SELECT dedupe_key, value_hash, start_time, end_time "
                    "FROM health_dedupe "
                    f"WHERE dedupe_key IN ({marks})",
                    chunk,
                ):
                    stored[str(key)] = (value_hash, start_time, end_time)
        finally:
            conn.close()
    new_keys: set[str] = set()
    changed_keys: set[str] = set()
    for item in items:
        key = item.dedupe_record.dedupe_key
        if key not in stored:
            new_keys.add(key)
            continue
        value_hash, start_time, end_time = stored[key]
        if (
            value_hash != item.dedupe_record.value_hash
            or start_time != item.dedupe_record.start_time
            or end_time != item.dedupe_record.end_time
        ):
            changed_keys.add(key)
    return (len(new_keys), len(changed_keys))


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
    known_rows: int = 0,
    quiet_run: bool = False,
    errors: list[str] | None = None,
) -> dict[str, Any]:
    rows = len(items)
    days = sorted({item.day for item in items if item.day})
    endpoint_counts = {endpoint: len(bundle[endpoint]) for endpoint in SYNC_ENDPOINTS}
    if dry_run:
        summary = (
            f"{SOURCE_LABEL} catalog: rows={rows}, days={len(days)}, "
            f"pages={pages_fetched} (nothing written)"
        )
    elif quiet_run:
        summary = f"{SOURCE_LABEL} sync quiet run: nothing new (rows={rows} all known)"
    else:
        summary = (
            f"Saved {SOURCE_LABEL} sync import: rows={rows}, new={inserted}, "
            f"revised={updated}, normalized_months={len(months)}, "
            f"import {import_id}"
        )
    result: dict[str, Any] = {
        "backend": SYNC_BACKEND_NAME,
        "dry_run": dry_run,
        "quiet_run": quiet_run,
        "source_family": SOURCE_OURA_API,
        "source_label": SOURCE_LABEL,
        # Keys the generic sync CLI prints:
        "total": rows,
        # Catalog "available" counts only rows the dedupe ledger has never
        # seen; trailing-refetch re-reads are reported separately (upstream
        # verification finding: refetch rows masqueraded as importable).
        "available": max(rows - known_rows, 0) if dry_run else 0,
        "known_refetch": known_rows if dry_run else 0,
        "imported": known_rows if dry_run else updated,
        "downloaded": 0 if dry_run else inserted,
        "errors": list(errors or []),
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


def _endpoint_backfill_complete(state: Mapping[str, Any] | None, endpoint: str) -> bool:
    """Whether a completed save run has ever covered this endpoint."""

    if not state:
        return False
    info = (state.get("endpoints") or {}).get(endpoint) or {}
    return info.get("backfill_complete") is True


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
        if watermark is not None:
            # No-gap + revision rule: resume the day after the watermark,
            # but never start later than the trailing revision window.
            start = min(
                watermark + dt.timedelta(days=1),
                today - dt.timedelta(days=TRAILING_REFETCH_DAYS),
            )
        elif state is None or not _endpoint_backfill_complete(state, endpoint):
            if state is None:
                # First sync of a fresh install: a trailing window, NOT
                # deep history (a deliberate --window-days decision).
                start = today - dt.timedelta(days=DEFAULT_FIRST_SYNC_WINDOW_DAYS)
            else:
                # Cursor-upgrade path: this endpoint joined SYNC_ENDPOINTS
                # after the journal already had a cursor. Treat it as
                # never-synced and backfill its full history (chunked per
                # _MAX_WINDOW_DAYS) — not just the trailing window.
                start = dt.date.fromisoformat(BACKFILL_HORIZON_DAY)
        else:
            # Backfilled before but no data ever seen (e.g. no CGM on the
            # account): poll a modest trailing window instead of walking
            # the full horizon every run.
            start = today - dt.timedelta(days=DEFAULT_FIRST_SYNC_WINDOW_DAYS)
    if start > today:
        start = today
    return (start.isoformat(), today.isoformat())


def _item_day_iso(endpoint: str, item: Mapping[str, Any]) -> str | None:
    """One item's Oura day as YYYY-MM-DD, verbatim from Oura's own fields."""

    if endpoint in _DATETIME_PAGED_ENDPOINTS:
        raw = str(item.get("timestamp") or "")[:10]
    else:
        raw = str(item.get(_DOCUMENT_DAY_FIELDS.get(endpoint, "day")) or "")
    return raw if parse_oura_day(raw) else None


def _advance_cursor_state(
    previous: Mapping[str, Any] | None,
    *,
    bundle: Mapping[str, list[dict[str, Any]]],
    import_id: str | None,
    rows: int,
    inserted: int,
    updated: int,
    pages: int,
    quiet_run: bool = False,
    fetched_endpoints: set[str] | None = None,
) -> dict[str, Any]:
    fetched_ok = fetched_endpoints if fetched_endpoints is not None else set()
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
            # An endpoint whose fetch completed on any save run has had
            # its history covered (this window plus prior state) — even
            # when the fetch came back empty. A skipped endpoint (scope
            # gap) never earns the flag, so the first sync after
            # reauthorization backfills it from the horizon. Once earned,
            # the flag sticks. Note the deliberate asymmetry with
            # high_water_day: the watermark is data-based (no-gap rule),
            # this flag is fetch-based (anti-thrash for empty endpoints).
            "backfill_complete": (
                endpoint in fetched_ok
                or _endpoint_backfill_complete(previous, endpoint)
            ),
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
            # A quiet run has no import bundle — import_id stays None.
            "import_id": import_id,
            "quiet_run": quiet_run,
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
    windows: Mapping[str, tuple[str, str]],
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
    write_json_file(
        import_dir / "fetch_windows.json",
        {
            "schema": "solstone.oura_fetch_windows.v1",
            "windows": {ep: list(win) for ep, win in windows.items()},
            "chunk_limits": {
                ep: _MAX_WINDOW_DAYS.get(ep, _DEFAULT_MAX_WINDOW_DAYS)
                for ep in SYNC_ENDPOINTS
            },
        },
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


def _scheduled_cron_hint(
    journal_root: Path,
    *,
    scheduled_sync: ScheduledSyncConsent | None = None,
    read_artifact: bool = True,
) -> str | None:
    """The exact crontab line to suggest when scheduled consent exists.

    Guidance only — nothing is ever installed. Scheduled runs pass
    ``--scheduled`` and rely on the artifact's standing consent instead of
    the per-run confirmation flag.
    """

    if scheduled_sync is None:
        if not read_artifact:
            return None
        artifact = read_oura_sync_approval(journal_root)
        if not isinstance(artifact, dict):
            return None
        consent = artifact.get("scheduled_sync")
        if not isinstance(consent, dict) or consent.get("approved") is not True:
            return None
        cadence = str(consent.get("cadence") or "")
    else:
        cadence = scheduled_sync.cadence
    hours = _cadence_hours(cadence)
    # The host-side importer CLI (`journal importer`) is the sync surface;
    # the thin `sol import` client rejects --sync on purpose.
    journal_path = shutil.which("journal") or "journal"
    return f"0 */{hours} * * * {journal_path} importer --sync oura --save --scheduled"


def _cadence_hours(cadence: str) -> int:
    """Best-effort hour count from a human cadence string; defaults to 6."""

    match = re.search(r"(\d+)", cadence)
    if match:
        hours = int(match.group(1))
        if 1 <= hours <= 23:
            return hours
    return 6
