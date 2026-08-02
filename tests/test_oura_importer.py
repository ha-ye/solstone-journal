# SPDX-License-Identifier: AGPL-3.0-only
# Copyright (c) 2026 sol pbc

"""Oura importer tests — parse, normalize, dedupe, fetch layer, sync, gate."""

from __future__ import annotations

import datetime as dt
import json
import os
import sqlite3
import stat
import subprocess
import sys
import time
from contextlib import contextmanager
from dataclasses import replace
from importlib import import_module
from pathlib import Path
from types import SimpleNamespace
from zoneinfo import ZoneInfo

import pytest

from solstone.apps.body.routes import _iter_normalized_rows
from solstone.think.importers import oura
from solstone.think.importers.file_importer import (
    FILE_IMPORTER_REGISTRY,
    get_file_importer,
)
from solstone.think.importers.health_dedupe import (
    get_health_dedupe_record,
    health_dedupe_db_path,
    upsert_health_dedupe_records,
)
from solstone.think.importers.health_schema import (
    SOURCE_APPLE_HEALTH,
    SOURCE_OURA_API,
    HealthRecordIdentity,
    health_record_dedupe_key,
)
from solstone.think.importers.pre_save_gate import (
    APPROVAL_SCHEMA,
    CHECKLIST_DESTINATIONS,
    CHECKLIST_VERSION,
    OURA_SYNC_APPROVAL_SCHEMA,
    OURA_SYNC_CHECKLIST_VERSION,
    SENSITIVE_IMPORTERS,
    PreSaveGateError,
    RawRetentionDecision,
    approval_path_for_journal,
    oura_sync_approval_path_for_journal,
)
from solstone.think.importers.shared import (
    ImportLockTimeout,
    hold_private_import_lock,
)
from solstone.think.importers.sync import SYNCABLE_REGISTRY, get_syncable_backends

FIXTURE_ROOT = (
    Path(__file__).parent / "fixtures" / "importers" / "health" / "oura_synthetic"
)
REVISION_ROOT = FIXTURE_ROOT / "revisions"
APPLE_FIXTURE_ROOT = (
    Path(__file__).parent
    / "fixtures"
    / "importers"
    / "health"
    / "apple_health_synthetic"
)

# Fixture bundle shape: 29 documents across 14 endpoints; each readiness
# document also splits out a temperature-deviation row -> 31 rows.
_FIXTURE_ROW_COUNT = 31
# Sync runs fetch SYNC_ENDPOINTS only: the partner-gated blood_glucose
# fixture (4 rows) is parse/normalize-only, never polled.
_SYNC_ROW_COUNT = _FIXTURE_ROW_COUNT - 4
_SCHEDULED_VALID_UNTIL = "2099-01-01T00:00:00Z"


def _use_journal(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    journal = tmp_path / "journal"
    journal.mkdir()
    config_path = journal / "config" / "journal.json"
    config_path.parent.mkdir()
    config_path.write_text(
        json.dumps({"identity": {"timezone": "America/Denver"}}),
        encoding="utf-8",
    )
    monkeypatch.setenv("SOLSTONE_JOURNAL", str(journal))
    return journal


def _raw_retention(decision: str = RawRetentionDecision.RETAIN_PARSED.value) -> dict:
    return {
        "decision": decision,
        "notes": "Synthetic test decision.",
    }


def _scheduled_sync_consent() -> dict:
    return {
        "approved": True,
        "cadence": "every 6 hours",
        "valid_until": _SCHEDULED_VALID_UNTIL,
    }


def _valid_artifact(
    journal: Path,
    importers: list[str],
    *,
    raw_retention_decision: str = RawRetentionDecision.RETAIN_PARSED.value,
) -> dict:
    return {
        "schema": APPROVAL_SCHEMA,
        "checklist_version": CHECKLIST_VERSION,
        "approved_by": "Jack",
        "approved_at": "2026-07-05T00:00:00-06:00",
        "journal_root": str(journal.resolve()),
        "approved_importers": importers,
        "replication_destinations": {
            destination: {
                "decision": "approved" if destination == "time_machine" else "excluded",
                "notes": "Synthetic test decision.",
            }
            for destination in CHECKLIST_DESTINATIONS
        },
        "raw_retention": _raw_retention(raw_retention_decision),
        "requires_per_run_confirmation": True,
        "no_real_health_data_in_artifact": True,
    }


def _write_artifact(journal: Path, payload: dict) -> Path:
    approval_path = approval_path_for_journal(journal)
    approval_path.parent.mkdir(parents=True)
    approval_path.write_text(json.dumps(payload), encoding="utf-8")
    return approval_path


def _sync_artifact(
    journal: Path,
    *,
    raw_retention_decision: str = RawRetentionDecision.RETAIN_PARSED.value,
) -> dict:
    return {
        "schema": OURA_SYNC_APPROVAL_SCHEMA,
        "checklist_version": OURA_SYNC_CHECKLIST_VERSION,
        "approved_by": "Jack",
        "approved_at": "2026-07-06T09:00:00-06:00",
        "journal_root": str(journal.resolve()),
        "replication_destinations": {
            destination: {
                "decision": "approved" if destination == "time_machine" else "excluded",
                "notes": "Synthetic test decision.",
            }
            for destination in CHECKLIST_DESTINATIONS
        },
        "raw_retention": _raw_retention(raw_retention_decision),
        "requires_per_run_confirmation": True,
        "no_real_health_data_in_artifact": True,
    }


def _write_sync_artifact(journal: Path, payload: dict) -> Path:
    approval_path = oura_sync_approval_path_for_journal(journal)
    approval_path.parent.mkdir(parents=True, exist_ok=True)
    approval_path.write_text(json.dumps(payload), encoding="utf-8")
    return approval_path


_APPROVAL_ONLY = [
    "imports/_approvals",
    "imports/_approvals/health_import_preflight.json",
]

_SYNC_APPROVAL_ONLY = [
    "imports/_approvals",
    "imports/_approvals/oura_sync_preflight.json",
]


@contextmanager
def _temporary_umask(mask: int):
    old = os.umask(mask)
    try:
        yield
    finally:
        os.umask(old)


def _mode(path: Path) -> int:
    return stat.S_IMODE(path.stat().st_mode)


def _wait_for_path(path: Path, *, timeout: float = 5.0) -> None:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if path.exists():
            return
        time.sleep(0.02)
    raise AssertionError(f"timed out waiting for {path}")


def _subprocess_env() -> dict[str, str]:
    env = os.environ.copy()
    repo_root = Path(__file__).resolve().parents[1]
    existing = env.get("PYTHONPATH")
    env["PYTHONPATH"] = (
        str(repo_root) if not existing else f"{repo_root}{os.pathsep}{existing}"
    )
    return env


def _imports_contents(journal: Path) -> list[str]:
    imports_dir = journal / "imports"
    if not imports_dir.exists():
        return []
    return sorted(
        p.relative_to(journal).as_posix()
        for p in imports_dir.rglob("*")
        # sqlite WAL sidecars appear whenever a connection opens the dedupe
        # ledger (even read-only) and vanish on checkpoint — connection
        # artifacts, not import writes.
        if not p.name.endswith(("-shm", "-wal"))
    )


# ---------------------------------------------------------------------------
# Canned transport + client (no test ever touches the default transport)
# ---------------------------------------------------------------------------


class ScriptedTransport:
    """Canned per-endpoint responses; fails loudly on unscripted requests."""

    def __init__(self) -> None:
        self.calls: list[tuple[str, dict[str, str]]] = []
        self._scripts: dict[str, list[oura.OuraTransportResponse]] = {}

    def script(self, endpoint: str, *responses: oura.OuraTransportResponse) -> None:
        self._scripts.setdefault(endpoint, []).extend(responses)

    def __call__(self, url: str, headers: dict) -> oura.OuraTransportResponse:
        self.calls.append((url, dict(headers)))
        endpoint = url.split("/usercollection/")[1].split("?")[0]
        script = self._scripts.get(endpoint)
        if not script:
            raise AssertionError(f"unscripted request to {endpoint}: {url}")
        return script.pop(0)


def _ok_page(
    items: list[dict], next_token: str | None = None
) -> oura.OuraTransportResponse:
    return oura.OuraTransportResponse(
        status=200, body=json.dumps({"data": items, "next_token": next_token})
    )


def _status(code: int, body: str = "") -> oura.OuraTransportResponse:
    return oura.OuraTransportResponse(status=code, body=body)


def _fixture_page(
    endpoint: str, root: Path = FIXTURE_ROOT
) -> oura.OuraTransportResponse:
    return oura.OuraTransportResponse(
        status=200, body=(root / f"{endpoint}.json").read_text(encoding="utf-8")
    )


def _fixture_items(endpoint: str) -> list[dict]:
    document = json.loads((FIXTURE_ROOT / f"{endpoint}.json").read_text())
    return document["data"]


def _fixture_transport() -> ScriptedTransport:
    transport = ScriptedTransport()
    for endpoint in oura.SYNC_ENDPOINTS:
        transport.script(endpoint, _fixture_page(endpoint))
    return transport


def _fake_tokens(access: str = "synthetic-access") -> SimpleNamespace:
    # Mocks the OuraTokens contract from oura_auth.
    return SimpleNamespace(
        access_token=access,
        refresh_token="synthetic-refresh",
        expires_at=0.0,
        token_type="Bearer",
    )


def _unexpected_sleep(seconds: float) -> None:
    raise AssertionError(f"unexpected backoff sleep({seconds})")


def _unexpected_refresh(tokens, *, client_id):
    raise AssertionError("unexpected token refresh")


def _canned_client(transport: ScriptedTransport, **overrides) -> oura.OuraApiClient:
    kwargs = dict(
        transport=transport,
        client_id="synthetic-client-id",
        load_tokens=_fake_tokens,
        save_tokens=lambda tokens: None,
        refresh_tokens=_unexpected_refresh,
        sleep=_unexpected_sleep,
    )
    kwargs.update(overrides)
    return oura.OuraApiClient(**kwargs)


# ---------------------------------------------------------------------------
# Registration and gate membership
# ---------------------------------------------------------------------------


def test_oura_registered_as_file_importer():
    assert "oura" in FILE_IMPORTER_REGISTRY
    assert get_file_importer("oura") is not None


def test_oura_is_a_sensitive_importer():
    assert "oura" in SENSITIVE_IMPORTERS


def test_oura_registered_as_syncable_backend():
    assert SYNCABLE_REGISTRY["oura"] == "solstone.think.importers.oura"
    assert any(backend.name == "oura" for backend in get_syncable_backends())


# ---------------------------------------------------------------------------
# Parse layer
# ---------------------------------------------------------------------------


def test_parse_bundle_reads_all_supported_endpoint_files():
    bundle = oura.parse_oura_bundle(FIXTURE_ROOT)

    assert set(bundle) == {
        "daily_sleep",
        "daily_readiness",
        "daily_resilience",
        "daily_stress",
        "daily_spo2",
        "sleep",
        "daily_activity",
        "heartrate",
        "daily_cardiovascular_age",
        "blood_glucose",
        "workout",
        "session",
        "enhanced_tag",
        "vO2_max",
    }
    assert len(bundle["daily_sleep"]) == 2
    assert len(bundle["sleep"]) == 2
    assert len(bundle["daily_stress"]) == 1
    assert len(bundle["daily_activity"]) == 2
    assert len(bundle["heartrate"]) == 4
    assert len(bundle["daily_cardiovascular_age"]) == 2
    assert len(bundle["blood_glucose"]) == 4
    assert len(bundle["workout"]) == 2
    assert len(bundle["session"]) == 2
    assert len(bundle["enhanced_tag"]) == 2
    assert len(bundle["vO2_max"]) == 2


def test_parse_single_endpoint_file():
    bundle = oura.parse_oura_bundle(FIXTURE_ROOT / "daily_readiness.json")

    assert set(bundle) == {"daily_readiness"}
    assert bundle["daily_readiness"][0]["id"] == "synthetic-readiness-2026-01-02"


def test_parse_rejects_unknown_endpoint():
    with pytest.raises(oura.OuraDocumentError, match="Unsupported Oura endpoint"):
        oura.parse_endpoint_document("daily_unicorns", {"data": []})


def test_parse_rejects_document_without_data_list():
    with pytest.raises(oura.OuraDocumentError, match="'data' list"):
        oura.parse_endpoint_document("daily_sleep", {"items": []})


def test_parse_rejects_item_missing_id_or_day():
    with pytest.raises(oura.OuraDocumentError, match="missing 'id' or 'day'"):
        oura.parse_endpoint_document(
            "daily_sleep", {"data": [{"id": "x", "score": 10}]}
        )


def test_parse_heartrate_requires_timestamp_and_bpm():
    with pytest.raises(oura.OuraDocumentError, match="missing 'timestamp' or 'bpm'"):
        oura.parse_endpoint_document("heartrate", {"data": [{"bpm": 60}]})
    with pytest.raises(oura.OuraDocumentError, match="missing 'timestamp' or 'bpm'"):
        oura.parse_endpoint_document(
            "heartrate", {"data": [{"timestamp": "2026-01-02T03:15:00-07:00"}]}
        )


def test_parse_blood_glucose_requires_timestamp_and_glucose():
    # Pins the assumed row shape for the undocumented blood_glucose series
    # (see _SERIES_REQUIRED_FIELDS): if the live API names its value field
    # differently, the first post-reauthorization fetch fails loudly here.
    with pytest.raises(
        oura.OuraDocumentError, match="missing 'timestamp' or 'glucose'"
    ):
        oura.parse_endpoint_document("blood_glucose", {"data": [{"glucose": 92}]})
    with pytest.raises(
        oura.OuraDocumentError, match="missing 'timestamp' or 'glucose'"
    ):
        oura.parse_endpoint_document(
            "blood_glucose", {"data": [{"timestamp": "2026-01-02T15:05:00Z"}]}
        )


def test_parse_enhanced_tag_requires_id_and_start_day():
    # enhanced_tag is the one document endpoint with no `day` field
    # (openapi-1.35 EnhancedTagModel): its day attribution field is
    # `start_day`, enforced through _DOCUMENT_DAY_FIELDS.
    with pytest.raises(oura.OuraDocumentError, match="missing 'id' or 'start_day'"):
        oura.parse_endpoint_document(
            "enhanced_tag",
            {"data": [{"id": "tag-1", "start_time": "2026-01-02T14:45:12-07:00"}]},
        )
    with pytest.raises(oura.OuraDocumentError, match="missing 'id' or 'start_day'"):
        oura.parse_endpoint_document(
            "enhanced_tag", {"data": [{"start_day": "2026-01-02"}]}
        )
    # The other granted-scope endpoints validate on the plain id+day rule.
    with pytest.raises(oura.OuraDocumentError, match="missing 'id' or 'day'"):
        oura.parse_endpoint_document("workout", {"data": [{"id": "workout-1"}]})
    with pytest.raises(oura.OuraDocumentError, match="missing 'id' or 'day'"):
        oura.parse_endpoint_document(
            "vO2_max", {"data": [{"id": "vo2-1", "vo2_max": 41}]}
        )


def test_parse_oura_day_normalizes_and_rejects():
    assert oura.parse_oura_day("2026-01-02") == "20260102"
    assert oura.parse_oura_day("2026-13-02") is None
    assert oura.parse_oura_day(20260102) is None


# ---------------------------------------------------------------------------
# Normalization
# ---------------------------------------------------------------------------


def _normalized_items() -> list[oura.OuraNormalizedItem]:
    bundle = oura.parse_oura_bundle(FIXTURE_ROOT)
    return oura.normalize_bundle(
        bundle,
        import_id="20260105_000000",
        raw_ref_root="imports/20260105_000000/raw/oura",
        owner_timezone=ZoneInfo("America/Denver"),
    )


def test_normalize_bundle_rows_carry_schema_family_and_days():
    items = _normalized_items()

    assert len(items) == _FIXTURE_ROW_COUNT
    for item in items:
        assert item.row["schema"] == oura.NORMALIZED_SCHEMA
        assert item.row["source_family"] == SOURCE_OURA_API
        assert item.row["dedupe_key"].startswith("sha256:")
        assert item.row["day"] in {"20260102", "20260103"}
        assert item.month in {"2026-01"}
        assert item.dedupe_record.source_family == SOURCE_OURA_API


def test_normalize_bundle_emits_expected_record_types():
    items = _normalized_items()
    by_type = {}
    for item in items:
        by_type.setdefault(item.row["record_type"], []).append(item.row)

    assert set(by_type) == {
        "oura.daily_sleep",
        "oura.daily_readiness",
        "oura.temperature_deviation",
        "oura.daily_resilience",
        "oura.daily_stress",
        "oura.daily_spo2",
        "oura.sleep",
        "oura.daily_activity",
        "oura.heartrate",
        "oura.daily_cardiovascular_age",
        "oura.blood_glucose",
        "oura.workout",
        "oura.session",
        "oura.enhanced_tag",
        "oura.vo2_max",
    }
    # Temperature deviation splits out of each readiness document.
    assert len(by_type["oura.temperature_deviation"]) == 2
    assert by_type["oura.temperature_deviation"][0]["unit"] == "degC"
    # Scores stay attributed values, not re-derived numbers.
    scores = {row["day"]: row["value"] for row in by_type["oura.daily_sleep"]}
    assert scores == {"20260102": 88, "20260103": 74}


def test_normalize_sleep_period_keeps_stage_durations_in_metadata():
    items = _normalized_items()
    periods = [item.row for item in items if item.row["record_type"] == "oura.sleep"]

    night = next(row for row in periods if row["day"] == "20260102")
    assert night["kind"] == "sleep_period"
    assert night["start_date"] == "2026-01-01T22:41:00-07:00"
    assert night["end_date"] == "2026-01-02T06:32:00-07:00"
    assert night["value"] == 26340
    assert night["unit"] == "s"
    metadata = night["metadata"]
    assert metadata["deep_sleep_duration"] == 5460
    assert metadata["rem_sleep_duration"] == 6480
    assert metadata["light_sleep_duration"] == 14400
    assert metadata["awake_time"] == 1920
    assert metadata["sleep_phase_5_min"].startswith("4444")


def test_normalize_daily_activity_keeps_oura_score_and_totals():
    items = _normalized_items()
    rows = [
        item.row for item in items if item.row["record_type"] == "oura.daily_activity"
    ]

    assert len(rows) == 2
    first = next(row for row in rows if row["day"] == "20260102")
    assert first["kind"] == "daily_summary"
    assert first["value"] == 85
    assert first["unit"] == "score"
    assert first["source_record_id"] == "synthetic-activity-2026-01-02"
    assert first["metadata"]["steps"] == 11423
    assert first["metadata"]["active_calories"] == 512
    assert first["metadata"]["contributors"]["meet_daily_targets"] == 92


def test_normalize_heartrate_synthesizes_identity_and_owner_local_day():
    items = _normalized_items()
    rows = [item.row for item in items if item.row["record_type"] == "oura.heartrate"]

    assert len(rows) == 4
    first = next(
        row for row in rows if row["start_date"] == "2026-01-02T03:15:00-07:00"
    )
    # Heartrate rows carry no Oura day field: the journal day is derived
    # from the timestamp after conversion to the owner's local timezone.
    assert first["day"] == "20260102"
    assert first["kind"] == "sample"
    assert first["value"] == 49
    assert first["unit"] == "bpm"
    assert first["source_record_id"] == "heartrate/2026-01-02T03:15:00-07:00/sleep"
    assert first["metadata"] == {
        "source": "sleep",
        "raw_timestamp": "2026-01-02T03:15:00-07:00",
        "timezone": "America/Denver",
    }


def test_normalize_heartrate_converts_utc_to_owner_local_day_and_hour():
    [item] = oura.normalize_bundle(
        {
            "heartrate": [
                {
                    "timestamp": "2026-07-01T00:02:57.000Z",
                    "bpm": 63,
                    "source": "sleep",
                }
            ]
        },
        import_id="20260706_000000",
        raw_ref_root="imports/20260706_000000/raw/oura",
        owner_timezone=ZoneInfo("America/Denver"),
    )

    row = item.row
    assert row["source_record_id"] == "heartrate/2026-07-01T00:02:57.000Z/sleep"
    assert row["day"] == "20260630"
    assert item.month == "2026-06"
    assert row["start_date"] == "2026-06-30T18:02:57-06:00"
    assert row["metadata"] == {
        "source": "sleep",
        "raw_timestamp": "2026-07-01T00:02:57.000Z",
        "timezone": "America/Denver",
    }


def test_normalize_daily_cardiovascular_age_uses_oura_day_verbatim():
    # Documented endpoint (openapi-1.35 PublicDailyCardiovascularAge):
    # day-granularity documents with id + day required. The journal day is
    # Oura's day field verbatim — no local-time recomputation.
    items = _normalized_items()
    rows = [
        item.row
        for item in items
        if item.row["record_type"] == "oura.daily_cardiovascular_age"
    ]

    assert len(rows) == 2
    first = next(row for row in rows if row["day"] == "20260102")
    assert first["kind"] == "daily_summary"
    assert first["value"] == 34
    assert first["unit"] == "years"
    assert first["source_record_id"] == "synthetic-cardio-age-2026-01-02"
    assert first["metadata"] == {"pulse_wave_velocity": 7.1}
    # The second document carries a null pulse_wave_velocity — it must
    # not appear in metadata (nulls are dropped, like every other field).
    second = next(row for row in rows if row["day"] == "20260103")
    assert second["value"] == 35
    assert second["metadata"] == {}


def test_normalize_blood_glucose_converts_utc_and_synthesizes_identity():
    # PINNED ASSUMPTIONS for the undocumented blood_glucose series (see
    # _normalize_item): heartrate-shaped rows, UTC instants, mg/dL. The
    # first post-reauthorization sync confirms or falsifies these; if the
    # live shape differs, fix the fixture AND this test together.
    items = _normalized_items()
    rows = [
        item.row for item in items if item.row["record_type"] == "oura.blood_glucose"
    ]

    assert len(rows) == 4
    first = next(
        row
        for row in rows
        if row["metadata"]["raw_timestamp"].startswith("2026-01-02T15")
    )
    assert first["kind"] == "sample"
    assert first["value"] == 92
    assert first["unit"] == "mg/dL"
    assert first["source_record_id"] == "blood_glucose/2026-01-02T15:05:00Z"
    assert first["day"] == "20260102"
    assert first["start_date"] == "2026-01-02T08:05:00-07:00"
    assert first["metadata"] == {
        "raw_timestamp": "2026-01-02T15:05:00Z",
        "timezone": "America/Denver",
    }
    # The UTC->owner-local conversion is load-bearing: a 04:20Z sample
    # belongs to the PREVIOUS Denver day.
    cross_midnight = next(
        row
        for row in rows
        if row["metadata"]["raw_timestamp"] == "2026-01-03T04:20:00Z"
    )
    assert cross_midnight["day"] == "20260102"
    assert cross_midnight["start_date"] == "2026-01-02T21:20:00-07:00"


def test_normalize_workout_is_event_row_with_verbatim_local_times():
    # PublicWorkout (openapi-1.35; live-confirmed 2026-07-07). Workouts
    # are event rows like Apple Health workouts: kind="workout", no
    # scalar value, activity/intensity/calories/distance as metadata.
    items = _normalized_items()
    rows = [item.row for item in items if item.row["record_type"] == "oura.workout"]

    assert len(rows) == 2
    first = next(row for row in rows if row["day"] == "20260102")
    assert first["kind"] == "workout"
    assert "value" not in first
    assert "unit" not in first
    assert first["source_record_id"] == "synthetic-workout-2026-01-02"
    # Datetimes are wearer-local offsets and pass through VERBATIM —
    # never UTC-converted, never re-derived.
    assert first["start_date"] == "2026-01-02T08:05:00.000-07:00"
    assert first["end_date"] == "2026-01-02T08:41:00.000-07:00"
    # Null label drops from metadata like every other null field.
    assert first["metadata"] == {
        "activity": "walking",
        "intensity": "moderate",
        "source": "confirmed",
        "calories": 148.5,
        "distance": 2412.9,
    }
    # Timezone pin: the second workout starts at 23:12 local, so its UTC
    # instant is the NEXT calendar day — the journal day must stay Oura's
    # `day` verbatim, with no local-time recomputation.
    second = next(row for row in rows if row["start_date"].startswith("2026-01-03T23"))
    assert second["day"] == "20260103"
    assert second["metadata"]["label"] == "synthetic night ride"


def test_normalize_session_keeps_type_and_mood_never_sample_series():
    # PublicSession (openapi-1.35; live-confirmed). The heart_rate/
    # heart_rate_variability/motion_count sample blocks stay in the raw
    # page only — normalized metadata carries just type and mood.
    items = _normalized_items()
    rows = [item.row for item in items if item.row["record_type"] == "oura.session"]

    assert len(rows) == 2
    first = next(row for row in rows if row["day"] == "20260102")
    assert first["kind"] == "session"
    assert "value" not in first
    assert first["source_record_id"] == "synthetic-session-2026-01-02"
    assert first["start_date"] == "2026-01-02T17:10:00.000-07:00"
    assert first["end_date"] == "2026-01-02T17:30:00.000-07:00"
    assert first["metadata"] == {"type": "meditation", "mood": "good"}
    # Null mood drops; sample blocks never enter metadata.
    second = next(row for row in rows if row["day"] == "20260103")
    assert second["metadata"] == {"type": "rest"}


def test_normalize_enhanced_tag_uses_start_day_verbatim():
    # EnhancedTagModel (openapi-1.35; live-confirmed): no `day` field —
    # the journal day is Oura's `start_day` verbatim, even for a tag
    # whose span ends on a later day.
    items = _normalized_items()
    rows = [
        item.row for item in items if item.row["record_type"] == "oura.enhanced_tag"
    ]

    assert len(rows) == 2
    first = next(row for row in rows if row["day"] == "20260102")
    assert first["kind"] == "tag"
    assert "value" not in first
    assert first["source_record_id"] == "synthetic-tag-2026-01-02"
    assert first["start_date"] == "2026-01-02T14:45:12-07:00"
    assert "end_date" not in first
    assert first["metadata"] == {"tag_type_code": "tag_generic_nocaffeine"}
    # A spanning tag: attributed to its start_day, end fields kept.
    second = next(row for row in rows if row["day"] == "20260103")
    assert second["end_date"] == "2026-01-04T07:10:00-07:00"
    assert second["metadata"] == {
        "comment": "synthetic note text",
        "custom_name": "synthetic custom tag",
        "end_day": "2026-01-04",
    }


def test_normalize_vo2_max_uses_documented_shape():
    # PublicVO2Max (openapi-1.35): {id, day, timestamp, vo2_max}. Zero
    # rows on this account today, so the fixture follows the documented
    # shape; VO2 max is mL/kg/min by definition (the spec has no unit).
    items = _normalized_items()
    rows = [item.row for item in items if item.row["record_type"] == "oura.vo2_max"]

    assert len(rows) == 2
    first = next(row for row in rows if row["day"] == "20260102")
    assert first["kind"] == "daily_summary"
    assert first["value"] == 41
    assert first["unit"] == "mL/kg/min"
    assert first["source_record_id"] == "synthetic-vo2max-2026-01-02"
    assert first["metadata"] == {}


def test_workout_dedupe_key_is_payload_independent():
    # Oura documents revise in place (same id, corrected payload) — a
    # re-fetched workout with corrected calories must upsert, not
    # duplicate, exactly like every other document-id-keyed endpoint.
    workout = {
        "id": "synthetic-workout-2026-01-02",
        "activity": "walking",
        "calories": 148.5,
        "day": "2026-01-02",
        "distance": 2412.9,
        "end_datetime": "2026-01-02T08:41:00.000-07:00",
        "intensity": "moderate",
        "label": None,
        "source": "confirmed",
        "start_datetime": "2026-01-02T08:05:00.000-07:00",
    }
    revised = dict(workout, calories=152.0, intensity="hard")

    def key(item: dict) -> str:
        normalized = oura.normalize_bundle(
            {"workout": [item]}, import_id="x", raw_ref_root="x"
        )
        return normalized[0].row["dedupe_key"]

    assert key(workout) == key(revised)


def test_blood_glucose_dedupe_key_is_value_independent():
    # A re-fetched sample at the same timestamp with a corrected reading
    # must update in place, exactly like heartrate revisions.
    sample = {"timestamp": "2026-01-02T15:05:00Z", "glucose": 92}
    revised = dict(sample, glucose=95)

    def key(item: dict) -> str:
        normalized = oura.normalize_bundle(
            {"blood_glucose": [item]}, import_id="x", raw_ref_root="x"
        )
        return normalized[0].row["dedupe_key"]

    assert key(sample) == key(revised)


def test_heartrate_dedupe_key_is_bpm_independent():
    # A re-fetched sample at the same timestamp+source with a corrected
    # bpm must update in place, exactly like document-id revisions.
    sample = {
        "timestamp": "2026-01-02T03:15:00-07:00",
        "timestamp_unix": 1767348900,
        "bpm": 49,
        "source": "sleep",
    }
    revised = dict(sample, bpm=51)

    def key(item: dict) -> str:
        normalized = oura.normalize_bundle(
            {"heartrate": [item]}, import_id="x", raw_ref_root="x"
        )
        return normalized[0].row["dedupe_key"]

    assert key(sample) == key(revised)


def test_dedupe_keys_are_stable_across_reparses():
    first = {item.row["dedupe_key"] for item in _normalized_items()}
    second = {item.row["dedupe_key"] for item in _normalized_items()}

    assert first == second
    assert len(first) == len(_normalized_items()), "keys must be unique per row"


def test_dedupe_keys_use_source_record_id_identity():
    items = _normalized_items()
    readiness = next(
        item
        for item in items
        if item.row["record_type"] == "oura.daily_readiness"
        and item.row["day"] == "20260102"
    )

    expected = health_record_dedupe_key(
        HealthRecordIdentity(
            source_family=SOURCE_OURA_API,
            record_type="oura.daily_readiness",
            start_time=readiness.row["start_date"],
            source_record_id="synthetic-readiness-2026-01-02",
        )
    )
    assert readiness.row["dedupe_key"] == expected


def test_dedupe_keys_do_not_collide_across_source_families():
    identity = dict(
        record_type="oura.daily_sleep",
        start_time="2026-01-02T00:00:00-07:00",
        source_record_id="synthetic-sleep-2026-01-02",
    )
    oura_key = health_record_dedupe_key(
        HealthRecordIdentity(source_family=SOURCE_OURA_API, **identity)
    )
    apple_key = health_record_dedupe_key(
        HealthRecordIdentity(source_family=SOURCE_APPLE_HEALTH, **identity)
    )

    assert oura_key != apple_key


def test_normalized_rows_round_trip_through_jsonl_encoding():
    items = _normalized_items()
    encoded = [json.dumps(item.row, sort_keys=True) for item in items]
    decoded = [json.loads(line) for line in encoded]

    assert decoded == [json.loads(json.dumps(item.row)) for item in items]
    for row in decoded:
        assert row["schema"] == oura.NORMALIZED_SCHEMA
        assert row["source_family"] == SOURCE_OURA_API


# ---------------------------------------------------------------------------
# Revision / upsert — Oura re-issues documents with corrections (same
# document id, changed payload; scores settle for a day or two). Same id →
# same dedupe key → the upsert UPDATES in place, never duplicates.
# ---------------------------------------------------------------------------

_IMPORT_A = "20260105_000000"  # earlier bundle
_IMPORT_B = "20260112_000000"  # later re-fetch of a trailing window


def _normalize_for_import(path: Path, import_id: str) -> list[oura.OuraNormalizedItem]:
    bundle = oura.parse_oura_bundle(path)
    return oura.normalize_bundle(
        bundle,
        import_id=import_id,
        raw_ref_root=f"imports/{import_id}/raw/oura",
        owner_timezone=ZoneInfo("America/Denver"),
    )


def _items_by_row_identity(
    items: list[oura.OuraNormalizedItem],
) -> dict[tuple[str, str], oura.OuraNormalizedItem]:
    return {
        (item.row["record_type"], item.row["source_record_id"]): item for item in items
    }


def _dedupe_row_count(journal: Path) -> int:
    with sqlite3.connect(health_dedupe_db_path(journal)) as conn:
        return conn.execute("SELECT COUNT(*) FROM health_dedupe").fetchone()[0]


def _write_bundle_shard(
    journal: Path, import_id: str, items: list[oura.OuraNormalizedItem]
) -> None:
    """Materialize one bundle's normalized month shards in a temp journal.

    Mirrors the storage the sync engine writes: per-bundle
    ``imports/<id>/normalized/<month>.jsonl`` with ``import_id`` /
    ``month`` / bundle-prefixed ``normalized_ref`` stamped on each row.
    """

    by_month: dict[str, list[str]] = {}
    for item in items:
        lines = by_month.setdefault(item.month, [])
        row = dict(item.row)
        row["import_id"] = import_id
        row["month"] = item.month
        row["normalized_ref"] = (
            f"imports/{import_id}/normalized/{item.month}.jsonl#L{len(lines) + 1}"
        )
        lines.append(json.dumps(row, sort_keys=True))
    for month, lines in by_month.items():
        shard = journal / "imports" / import_id / "normalized" / f"{month}.jsonl"
        shard.parent.mkdir(parents=True, exist_ok=True)
        shard.write_text("\n".join(lines) + "\n", encoding="utf-8")


def test_revision_reissue_keeps_dedupe_keys_payload_independent():
    base = _items_by_row_identity(
        _normalize_for_import(FIXTURE_ROOT / "daily_readiness.json", _IMPORT_A)
    )
    revised = _items_by_row_identity(_normalize_for_import(REVISION_ROOT, _IMPORT_B))

    assert set(base) == set(revised)
    changed_id = ("oura.daily_readiness", "synthetic-readiness-2026-01-02")
    # Same document id → same key, even though the score, both temperature
    # fields, a contributor, and the timestamp all changed. The key must
    # not include the payload or revisions would insert instead of update.
    assert revised[changed_id].row["dedupe_key"] == base[changed_id].row["dedupe_key"]
    assert base[changed_id].row["value"] == 82
    assert revised[changed_id].row["value"] == 79
    assert revised[changed_id].row["start_date"] != base[changed_id].row["start_date"]
    assert (
        revised[changed_id].dedupe_record.value_hash
        != base[changed_id].dedupe_record.value_hash
    )

    # The derived temperature-deviation row revises through the same id.
    temp_id = (
        "oura.temperature_deviation",
        "synthetic-readiness-2026-01-02/temperature_deviation",
    )
    assert revised[temp_id].row["dedupe_key"] == base[temp_id].row["dedupe_key"]
    assert base[temp_id].row["value"] == -0.21
    assert revised[temp_id].row["value"] == -0.05

    # The byte-identical re-issue is a pure duplicate: same key, same hash.
    same_id = ("oura.daily_readiness", "synthetic-readiness-2026-01-03")
    assert revised[same_id].row["dedupe_key"] == base[same_id].row["dedupe_key"]
    assert (
        revised[same_id].dedupe_record.value_hash
        == base[same_id].dedupe_record.value_hash
    )


def test_identical_payload_reimport_adds_no_dedupe_rows(tmp_path: Path):
    journal = tmp_path
    first = _normalize_for_import(FIXTURE_ROOT, _IMPORT_A)
    again = _normalize_for_import(FIXTURE_ROOT, _IMPORT_B)

    first_result = upsert_health_dedupe_records(
        journal, [item.dedupe_record for item in first]
    )
    again_result = upsert_health_dedupe_records(
        journal, [item.dedupe_record for item in again]
    )

    assert first_result.inserted == len(first)
    assert first_result.updated == 0
    assert again_result.inserted == 0
    assert again_result.updated == len(again)
    assert _dedupe_row_count(journal) == len(first)
    sample = get_health_dedupe_record(journal, first[0].dedupe_record.dedupe_key)
    assert sample is not None
    assert sample["first_import_id"] == _IMPORT_A
    assert sample["last_seen_import_id"] == _IMPORT_B


def test_changed_payload_revision_updates_in_place(tmp_path: Path):
    journal = tmp_path
    base = _normalize_for_import(FIXTURE_ROOT / "daily_readiness.json", _IMPORT_A)
    revised = _normalize_for_import(REVISION_ROOT, _IMPORT_B)

    upsert_health_dedupe_records(journal, [item.dedupe_record for item in base])
    result = upsert_health_dedupe_records(
        journal, [item.dedupe_record for item in revised]
    )

    # Corrected documents update the existing ledger rows — never a second
    # row per document (the revision requirement, design doc §4c).
    assert result.inserted == 0
    assert result.updated == len(revised)
    assert _dedupe_row_count(journal) == len(base)
    changed = _items_by_row_identity(revised)[
        ("oura.daily_readiness", "synthetic-readiness-2026-01-02")
    ]
    row = get_health_dedupe_record(journal, changed.dedupe_record.dedupe_key)
    assert row is not None
    assert row["first_import_id"] == _IMPORT_A
    assert row["last_seen_import_id"] == _IMPORT_B
    # The ledger names the latest sighting of the document.
    assert row["raw_ref"] == changed.dedupe_record.raw_ref


def test_revision_refreshes_value_hash_in_dedupe_ledger(tmp_path: Path):
    journal = tmp_path
    base = _normalize_for_import(FIXTURE_ROOT / "daily_readiness.json", _IMPORT_A)
    revised = _items_by_row_identity(_normalize_for_import(REVISION_ROOT, _IMPORT_B))

    upsert_health_dedupe_records(journal, [item.dedupe_record for item in base])
    changed = revised[("oura.daily_readiness", "synthetic-readiness-2026-01-02")]
    upsert_health_dedupe_records(journal, [changed.dedupe_record])

    row = get_health_dedupe_record(journal, changed.dedupe_record.dedupe_key)
    assert row is not None
    assert row["value_hash"] == changed.dedupe_record.value_hash


def test_heartrate_timezone_revision_updates_same_ledger_key_and_month_ref(
    tmp_path: Path,
):
    journal = tmp_path
    sample = {
        "timestamp": "2026-07-01T00:02:57.000Z",
        "bpm": 63,
        "source": "sleep",
    }

    [old] = oura.normalize_bundle(
        {"heartrate": [sample]},
        import_id=_IMPORT_A,
        raw_ref_root=f"imports/{_IMPORT_A}/raw/oura",
        owner_timezone=ZoneInfo("UTC"),
    )
    [corrected] = oura.normalize_bundle(
        {"heartrate": [sample]},
        import_id=_IMPORT_B,
        raw_ref_root=f"imports/{_IMPORT_B}/raw/oura",
        owner_timezone=ZoneInfo("America/Denver"),
    )

    assert old.row["day"] == "20260701"
    assert old.month == "2026-07"
    assert corrected.row["day"] == "20260630"
    assert corrected.month == "2026-06"
    assert corrected.row["source_record_id"] == old.row["source_record_id"]
    assert corrected.row["dedupe_key"] == old.row["dedupe_key"]
    assert corrected.dedupe_record.value_hash != old.dedupe_record.value_hash

    first = replace(
        old.dedupe_record,
        normalized_ref=f"imports/{_IMPORT_A}/normalized/{old.month}.jsonl#L1",
    )
    second = replace(
        corrected.dedupe_record,
        normalized_ref=f"imports/{_IMPORT_B}/normalized/{corrected.month}.jsonl#L1",
    )

    assert upsert_health_dedupe_records(journal, [first]).inserted == 1
    result = upsert_health_dedupe_records(journal, [second])

    assert result.inserted == 0
    assert result.updated == 1
    row = get_health_dedupe_record(journal, corrected.row["dedupe_key"])
    assert row is not None
    assert row["first_import_id"] == _IMPORT_A
    assert row["last_seen_import_id"] == _IMPORT_B
    assert row["start_time"] == "2026-06-30T18:02:57-06:00"
    assert row["value_hash"] == corrected.dedupe_record.value_hash
    assert row["normalized_ref"] == f"imports/{_IMPORT_B}/normalized/2026-06.jsonl#L1"


def test_within_bundle_page_overlap_collapses_to_one_row(tmp_path: Path):
    journal = tmp_path
    document = oura.parse_oura_bundle(FIXTURE_ROOT / "daily_readiness.json")
    # Oura pagination overlap: the same document arrives on two pages of
    # one sync run. Both copies carry one dedupe key; the batch counts the
    # second as an update and the ledger holds exactly one row.
    overlapped = {"daily_readiness": document["daily_readiness"][:1] * 2}
    items = oura.normalize_bundle(
        overlapped,
        import_id=_IMPORT_A,
        raw_ref_root=f"imports/{_IMPORT_A}/raw/oura",
    )

    result = upsert_health_dedupe_records(
        journal, [item.dedupe_record for item in items]
    )

    assert len(items) == 4  # readiness + temperature deviation, twice
    assert len({item.row["dedupe_key"] for item in items}) == 2
    assert result.inserted == 2
    assert result.updated == 2
    assert _dedupe_row_count(journal) == 2


def test_document_id_shared_across_endpoints_never_collides():
    shared_id = "synthetic-shared-2026-01-02"
    day = "2026-01-02"
    bundle = {
        "daily_sleep": [{"id": shared_id, "day": day, "score": 80}],
        "daily_readiness": [{"id": shared_id, "day": day, "score": 70}],
        "daily_resilience": [{"id": shared_id, "day": day, "level": "solid"}],
        "daily_stress": [{"id": shared_id, "day": day, "day_summary": "normal"}],
        "daily_spo2": [
            {"id": shared_id, "day": day, "spo2_percentage": {"average": 97.0}}
        ],
        "daily_activity": [{"id": shared_id, "day": day, "score": 66}],
        "sleep": [
            {
                "id": shared_id,
                "day": day,
                "bedtime_start": "2026-01-01T22:41:00-07:00",
                "total_sleep_duration": 26340,
            }
        ],
    }
    items = oura.normalize_bundle(
        bundle,
        import_id=_IMPORT_A,
        raw_ref_root=f"imports/{_IMPORT_A}/raw/oura",
    )

    keys = [item.row["dedupe_key"] for item in items]
    assert len(items) == len(bundle)
    assert len(set(keys)) == len(keys)
    by_type = {item.row["record_type"]: item.row["dedupe_key"] for item in items}
    # The most collision-prone pair: one endpoint name prefixes the other.
    assert by_type["oura.daily_sleep"] != by_type["oura.sleep"]


def test_cross_bundle_day_read_surfaces_one_row_per_key(tmp_path: Path):
    journal = tmp_path
    base = _normalize_for_import(FIXTURE_ROOT, _IMPORT_A)
    revised = _normalize_for_import(REVISION_ROOT, _IMPORT_B)
    _write_bundle_shard(journal, _IMPORT_A, base)
    _write_bundle_shard(journal, _IMPORT_B, revised)

    rows = _iter_normalized_rows(journal, month="2026-01")

    keys = [row["dedupe_key"] for row in rows]
    assert len(keys) == len(set(keys)), "day reads must dedupe by key"
    assert len(rows) == len(base)  # revision re-issues add no rows
    readiness = next(
        row
        for row in rows
        if row["record_type"] == "oura.daily_readiness" and row["day"] == "20260102"
    )
    # The surfaced row remembers both bundles for the audit drawer.
    assert readiness["import_ids"] == [_IMPORT_A, _IMPORT_B]


def test_cross_bundle_day_read_surfaces_latest_revision(tmp_path: Path):
    journal = tmp_path
    base = _normalize_for_import(FIXTURE_ROOT, _IMPORT_A)
    revised = _normalize_for_import(REVISION_ROOT, _IMPORT_B)
    _write_bundle_shard(journal, _IMPORT_A, base)
    _write_bundle_shard(journal, _IMPORT_B, revised)

    rows = _iter_normalized_rows(journal, month="2026-01")

    readiness = next(
        row
        for row in rows
        if row["record_type"] == "oura.daily_readiness" and row["day"] == "20260102"
    )
    temperature = next(
        row
        for row in rows
        if row["record_type"] == "oura.temperature_deviation"
        and row["day"] == "20260102"
    )
    assert readiness["value"] == 79, "the corrected readiness score must surface"
    assert temperature["value"] == -0.05


# ---------------------------------------------------------------------------
# File importer surface (detect/preview/dry-run)
# ---------------------------------------------------------------------------


def test_detect_matches_only_oura_shaped_bundles():
    importer = oura.OuraImporter()

    assert importer.detect(FIXTURE_ROOT) is True
    assert importer.detect(FIXTURE_ROOT / "sleep.json") is True
    assert importer.detect(APPLE_FIXTURE_ROOT) is False
    assert importer.detect(Path(__file__)) is False


def test_preview_counts_documents_and_days():
    preview = oura.OuraImporter().preview(FIXTURE_ROOT)

    assert preview.date_range == ("20260102", "20260103")
    assert preview.entity_count == 0
    # 29 documents; readiness docs each add a temperature-deviation row.
    assert preview.item_count == _FIXTURE_ROW_COUNT
    assert "daily_readiness=2" in preview.summary
    assert "sleep=2" in preview.summary
    assert "daily_activity=2" in preview.summary
    assert "heartrate=4" in preview.summary
    assert "daily_cardiovascular_age=2" in preview.summary
    assert "blood_glucose=4" in preview.summary
    assert "workout=2" in preview.summary
    assert "session=2" in preview.summary
    assert "enhanced_tag=2" in preview.summary
    assert "vO2_max=2" in preview.summary
    assert f"source_family={SOURCE_OURA_API}" in preview.summary


def test_dry_run_process_writes_nothing(tmp_path: Path, monkeypatch):
    journal = _use_journal(tmp_path, monkeypatch)

    result = oura.OuraImporter().process(FIXTURE_ROOT, journal, dry_run=True)

    assert result.entries_written == 0
    assert result.files_created == []
    assert result.summary.startswith("Dry run only:")
    assert not (journal / "imports").exists()


# ---------------------------------------------------------------------------
# Pre-save gate enforcement (file-import path)
# ---------------------------------------------------------------------------


def test_save_without_approval_artifact_blocks_before_any_write(
    tmp_path: Path, monkeypatch
):
    journal = _use_journal(tmp_path, monkeypatch)

    with pytest.raises(PreSaveGateError) as exc_info:
        oura.OuraImporter().process(FIXTURE_ROOT, journal, dry_run=False)

    payload = exc_info.value.to_dict()
    assert payload["reason"] == "health_pre_save_gate_required"
    assert payload["gate_reason"] == "missing_approval_artifact"
    assert payload["importer"] == "oura"
    assert not (journal / "imports").exists()


def test_save_with_artifact_missing_oura_approval_blocks(tmp_path: Path, monkeypatch):
    journal = _use_journal(tmp_path, monkeypatch)
    _write_artifact(journal, _valid_artifact(journal, importers=["apple_health"]))

    with pytest.raises(PreSaveGateError) as exc_info:
        oura.OuraImporter().process(
            FIXTURE_ROOT, journal, dry_run=False, confirm_health_save=True
        )

    payload = exc_info.value.to_dict()
    assert payload["gate_reason"] == "importer_not_approved"
    assert payload["invalid_fields"] == ["approved_importers"]
    assert _imports_contents(journal) == _APPROVAL_ONLY


def test_save_without_per_run_confirmation_blocks(tmp_path: Path, monkeypatch):
    journal = _use_journal(tmp_path, monkeypatch)
    _write_artifact(
        journal, _valid_artifact(journal, importers=["apple_health", "oura"])
    )

    with pytest.raises(PreSaveGateError) as exc_info:
        oura.OuraImporter().process(FIXTURE_ROOT, journal, dry_run=False)

    payload = exc_info.value.to_dict()
    assert payload["gate_reason"] == "per_run_confirmation_missing"
    assert payload["missing_fields"] == ["confirm_health_save"]
    assert _imports_contents(journal) == _APPROVAL_ONLY


def test_oura_never_accepts_legacy_artifact_without_journal_root(
    tmp_path: Path, monkeypatch
):
    journal = _use_journal(tmp_path, monkeypatch)
    artifact = _valid_artifact(journal, importers=["oura"])
    # A legacy-shaped artifact: binding recorded only as target_journal_path.
    del artifact["journal_root"]
    artifact["target_journal_path"] = str(journal.resolve())
    _write_artifact(journal, artifact)

    with pytest.raises(PreSaveGateError) as exc_info:
        oura.OuraImporter().process(
            FIXTURE_ROOT, journal, dry_run=False, confirm_health_save=True
        )

    payload = exc_info.value.to_dict()
    assert payload["gate_reason"] == "journal_root_binding_missing"
    assert payload["missing_fields"] == ["journal_root"]
    assert _imports_contents(journal) == _APPROVAL_ONLY


def test_save_past_gate_stops_at_seam_and_still_writes_nothing(
    tmp_path: Path, monkeypatch
):
    journal = _use_journal(tmp_path, monkeypatch)
    _write_artifact(journal, _valid_artifact(journal, importers=["oura"]))

    with pytest.raises(NotImplementedError, match=oura.DESIGN_DOC):
        oura.OuraImporter().process(
            FIXTURE_ROOT, journal, dry_run=False, confirm_health_save=True
        )

    assert not (journal / "imports" / "health-dedupe.sqlite").exists()
    # Only the approval artifact exists under imports/.
    assert _imports_contents(journal) == _APPROVAL_ONLY


def test_gate_failure_payload_carries_no_fixture_paths_or_values(
    tmp_path: Path, monkeypatch
):
    journal = _use_journal(tmp_path, monkeypatch)

    with pytest.raises(PreSaveGateError) as exc_info:
        oura.OuraImporter().process(FIXTURE_ROOT, journal, dry_run=False)

    failure_json = json.dumps(exc_info.value.to_dict(), sort_keys=True)
    assert str(FIXTURE_ROOT) not in failure_json
    assert "synthetic-readiness" not in failure_json
    assert "temperature_deviation" not in failure_json


# ---------------------------------------------------------------------------
# Day-summary rendering: attributed facts, no interpretation
# ---------------------------------------------------------------------------


def test_render_day_summary_attributes_every_score_to_oura():
    rows = [item.row for item in _normalized_items() if item.row["day"] == "20260102"]

    summary = oura.render_day_summary("20260102", rows, import_id="20260105_000000")

    assert summary.splitlines()[0] == "# Body · January 2, 2026"
    assert "Readiness 82 · Oura's score" in summary
    assert "Sleep score 88 · Oura's score" in summary
    assert "Resilience solid · Oura's label" in summary
    assert "Day stress summary normal · Oura's label" in summary
    assert "Nightly blood oxygen 97.4% · Oura's average" in summary
    assert "Temperature deviation -0.21 °C · Oura's measurement" in summary
    assert "Activity score 85 · Oura's score" in summary
    assert "Cardiovascular age 34 · Oura's estimate" in summary
    assert "VO2 max 41 · Oura's estimate" in summary
    assert "Sleep 7h 19m · Oura's staging" in summary
    assert "deep 1h 31m" in summary
    # Heartrate and blood-glucose samples are series, never summarized
    # into day prose.
    assert "bpm" not in summary
    assert "glucose" not in summary.lower()
    # Workouts, sessions, and tags are event rows — day surfaces render
    # them from their kind, never as day-summary prose.
    assert "walking" not in summary.lower()
    assert "meditation" not in summary.lower()
    assert "nocaffeine" not in summary.lower()
    assert "brought in via the Oura API · import 20260105_000000" in summary


def test_render_day_summary_makes_no_medical_gloss():
    rows = [item.row for item in _normalized_items() if item.row["day"] == "20260103"]

    summary = oura.render_day_summary("20260103", rows, import_id="x").lower()

    for banned in ("recovered well", "should", "healthy", "poor", "good", "bad"):
        assert banned not in summary


def test_render_day_summary_without_rows_is_factual():
    summary = oura.render_day_summary("20260104", [], import_id="x")

    assert "No Oura entries for this day." in summary


# ---------------------------------------------------------------------------
# Fetch layer — canned transport, pagination, backoff, 401 refresh
# ---------------------------------------------------------------------------


def test_fetch_endpoint_follows_next_token_pagination():
    items = _fixture_items("daily_readiness")
    transport = ScriptedTransport()
    transport.script(
        "daily_readiness",
        _ok_page(items[:1], next_token="page-2-token"),
        _ok_page(items[1:]),
    )
    client = _canned_client(transport)

    fetched = client.fetch_endpoint(
        "daily_readiness", start_day="2026-01-01", end_day="2026-01-10"
    )

    assert len(fetched.items) == 2
    assert len(fetched.pages) == 2
    assert fetched.requests == 2
    first_url, first_headers = transport.calls[0]
    assert "start_date=2026-01-01" in first_url
    assert "end_date=2026-01-10" in first_url
    assert "next_token" not in first_url
    assert first_headers["Authorization"] == "Bearer synthetic-access"
    assert "next_token=page-2-token" in transport.calls[1][0]


def test_fetch_retries_429_and_5xx_with_bounded_backoff():
    sleeps: list[float] = []
    transport = ScriptedTransport()
    transport.script(
        "daily_sleep",
        _status(429),
        _status(503),
        _ok_page(_fixture_items("daily_sleep")),
    )
    client = _canned_client(transport, sleep=sleeps.append)

    fetched = client.fetch_endpoint(
        "daily_sleep", start_day="2026-01-01", end_day="2026-01-10"
    )

    assert len(fetched.items) == 2
    # Exponential and bounded; injectable, so no wall-clock sleep happened.
    assert sleeps == [1.0, 2.0]


def test_fetch_gives_up_after_bounded_attempts():
    sleeps: list[float] = []
    transport = ScriptedTransport()
    transport.script(
        "daily_sleep", _status(500), _status(500), _status(500), _status(500)
    )
    client = _canned_client(transport, sleep=sleeps.append)

    with pytest.raises(oura.OuraApiError, match="HTTP 500 after 4 attempts"):
        client.fetch_endpoint(
            "daily_sleep", start_day="2026-01-01", end_day="2026-01-10"
        )

    assert sleeps == [1.0, 2.0, 4.0]
    assert len(transport.calls) == 4


def test_fetch_401_refreshes_once_saves_and_retries():
    saved: list = []
    refresh_calls: list[tuple[str, str]] = []

    def refresh(tokens, *, client_id):
        refresh_calls.append((tokens.access_token, client_id))
        return _fake_tokens("refreshed-access")

    transport = ScriptedTransport()
    transport.script(
        "daily_sleep", _status(401), _ok_page(_fixture_items("daily_sleep"))
    )
    client = _canned_client(transport, refresh_tokens=refresh, save_tokens=saved.append)

    fetched = client.fetch_endpoint(
        "daily_sleep", start_day="2026-01-01", end_day="2026-01-10"
    )

    assert len(fetched.items) == 2
    assert refresh_calls == [("synthetic-access", "synthetic-client-id")]
    assert len(saved) == 1 and saved[0].access_token == "refreshed-access"
    assert transport.calls[1][1]["Authorization"] == "Bearer refreshed-access"


def test_fetch_second_401_after_refresh_fails_loud():
    refresh_calls: list[str] = []

    def refresh(tokens, *, client_id):
        refresh_calls.append(client_id)
        return _fake_tokens("refreshed-access")

    transport = ScriptedTransport()
    transport.script("daily_sleep", _status(401), _status(401))
    client = _canned_client(transport, refresh_tokens=refresh)

    with pytest.raises(
        oura.OuraEndpointUnauthorized, match="missing this endpoint's scope"
    ):
        client.fetch_endpoint(
            "daily_sleep", start_day="2026-01-01", end_day="2026-01-10"
        )

    assert refresh_calls == ["synthetic-client-id"]


def test_client_refreshes_at_most_once_per_instance():
    # A second endpoint that 401s after an earlier successful refresh is a
    # scope gap: no second refresh grant is spent (refresh tokens rotate),
    # and the failure is the endpoint-scoped exception the sync engine
    # degrades on.
    refresh_calls: list[str] = []

    def refresh(tokens, *, client_id):
        refresh_calls.append(client_id)
        return _fake_tokens("refreshed-access")

    transport = ScriptedTransport()
    transport.script(
        "daily_sleep", _status(401), _ok_page(_fixture_items("daily_sleep"))
    )
    transport.script("blood_glucose", _status(401))
    client = _canned_client(transport, refresh_tokens=refresh)

    fetched = client.fetch_endpoint(
        "daily_sleep", start_day="2026-01-01", end_day="2026-01-10"
    )
    assert len(fetched.items) == 2

    with pytest.raises(oura.OuraEndpointUnauthorized):
        client.fetch_endpoint(
            "blood_glucose", start_day="2026-01-04", end_day="2026-01-10"
        )

    assert refresh_calls == ["synthetic-client-id"]


def test_fetch_without_tokens_raises_authorization_needed():
    transport = ScriptedTransport()
    client = _canned_client(transport, load_tokens=lambda: None)

    with pytest.raises(oura.OuraAuthorizationNeeded, match="authorization needed"):
        client.fetch_endpoint(
            "daily_sleep", start_day="2026-01-01", end_day="2026-01-10"
        )

    assert transport.calls == []


def test_client_reads_client_id_from_journal_config(tmp_path: Path):
    journal = tmp_path / "journal"
    (journal / "config").mkdir(parents=True, exist_ok=True)
    (journal / "config" / "journal.json").write_text(
        json.dumps({"oura": {"client_id": "config-client-id"}}), encoding="utf-8"
    )
    refresh_calls: list[str] = []

    def refresh(tokens, *, client_id):
        refresh_calls.append(client_id)
        return _fake_tokens("refreshed-access")

    transport = ScriptedTransport()
    transport.script(
        "daily_sleep", _status(401), _ok_page(_fixture_items("daily_sleep"))
    )
    client = _canned_client(
        transport, client_id=None, journal_root=journal, refresh_tokens=refresh
    )

    client.fetch_endpoint("daily_sleep", start_day="2026-01-01", end_day="2026-01-10")

    assert refresh_calls == ["config-client-id"]


def test_heartrate_fetch_uses_datetime_window_params():
    transport = ScriptedTransport()
    transport.script("heartrate", _ok_page(_fixture_items("heartrate")))
    client = _canned_client(transport)

    client.fetch_endpoint("heartrate", start_day="2026-01-01", end_day="2026-01-10")

    url = transport.calls[0][0]
    assert "start_datetime=2026-01-01T00%3A00%3A00%2B00%3A00" in url
    assert "end_datetime=2026-01-10T23%3A59%3A59%2B00%3A00" in url
    assert "start_date=" not in url


def test_blood_glucose_fetch_uses_datetime_window_params():
    # Pinned assumption: blood_glucose paginates by datetime like the
    # heartrate series (the endpoint is absent from openapi-1.35).
    transport = ScriptedTransport()
    transport.script("blood_glucose", _ok_page(_fixture_items("blood_glucose")))
    client = _canned_client(transport)

    client.fetch_endpoint("blood_glucose", start_day="2026-01-01", end_day="2026-01-10")

    url = transport.calls[0][0]
    assert "start_datetime=2026-01-01T00%3A00%3A00%2B00%3A00" in url
    assert "end_datetime=2026-01-10T23%3A59%3A59%2B00%3A00" in url
    assert "start_date=" not in url


def test_granted_endpoints_fetch_day_paged_with_exact_route_casing():
    # The four granted-scope endpoints are day-paged documents. The
    # vO2_max route casing is exact — lowercase vo2_max 404s live
    # (verified 2026-07-07) — so the URL must carry it verbatim.
    for endpoint in ("workout", "session", "enhanced_tag", "vO2_max"):
        transport = ScriptedTransport()
        transport.script(endpoint, _ok_page(_fixture_items(endpoint)))
        client = _canned_client(transport)

        client.fetch_endpoint(endpoint, start_day="2026-01-01", end_day="2026-01-10")

        url = transport.calls[0][0]
        assert f"/usercollection/{endpoint}?" in url
        assert "start_date=2026-01-01" in url
        assert "end_date=2026-01-10" in url
        assert "start_datetime=" not in url


# ---------------------------------------------------------------------------
# Sync engine — end-to-end into a temp journal with canned transport
# ---------------------------------------------------------------------------


def test_first_sync_save_writes_bundle_dedupe_and_cursor(tmp_path: Path, monkeypatch):
    journal = _use_journal(tmp_path, monkeypatch)
    _write_sync_artifact(journal, _sync_artifact(journal))
    transport = _fixture_transport()
    client = _canned_client(transport)

    result = oura.backend.sync(
        journal,
        dry_run=False,
        confirm_health_save=True,
        client=client,
        today=dt.date(2026, 1, 10),
    )

    import_id = result["import_id"]
    import_dir = journal / "imports" / import_id

    # Normalized monthly shard with every row stamped for this bundle.
    shard = import_dir / "normalized" / "2026-01.jsonl"
    rows = [json.loads(line) for line in shard.read_text().splitlines()]
    assert len(rows) == _SYNC_ROW_COUNT
    assert {row["source_family"] for row in rows} == {SOURCE_OURA_API}
    assert {row["import_id"] for row in rows} == {import_id}
    assert rows[0]["normalized_ref"].startswith(f"imports/{import_id}/normalized/")

    # Raw API pages, one verbatim page per line, per endpoint.
    raw_readiness = import_dir / "raw" / "oura" / "daily_readiness.jsonl"
    raw_page = json.loads(raw_readiness.read_text().splitlines()[0])
    assert raw_page["data"][0]["id"] == "synthetic-readiness-2026-01-02"
    assert (import_dir / "raw" / "oura" / "heartrate.jsonl").exists()

    # Manifests match the apple_health bundle shape.
    manifest = json.loads((import_dir / "manifest.json").read_text())
    assert manifest["source_type"] == SOURCE_OURA_API
    assert manifest["entry_count"] == _SYNC_ROW_COUNT
    assert manifest["days_affected"] == ["20260102", "20260103"]
    assert manifest["raw_retention"] == RawRetentionDecision.RETAIN_PARSED.value
    content_lines = (import_dir / "content_manifest.jsonl").read_text().splitlines()
    assert json.loads(content_lines[0])["type"] == "health_normalized_month"

    # Dedupe upserts: every row inserted exactly once.
    assert _dedupe_row_count(journal) == _SYNC_ROW_COUNT

    # Cursor advanced only after the writes, with per-endpoint watermarks.
    state = json.loads((journal / "imports" / "oura.json").read_text())
    assert state["schema"] == oura.SYNC_STATE_SCHEMA
    assert state["trailing_refetch_days"] == oura.TRAILING_REFETCH_DAYS
    assert state["endpoints"]["daily_readiness"]["high_water_day"] == "2026-01-03"
    # Datetime-paged watermarks come from the raw (UTC) day of the newest
    # sample; the >=7-day trailing refetch absorbs the local-day skew.
    assert state["endpoints"]["heartrate"]["high_water_day"] == "2026-01-03"
    assert (
        state["endpoints"]["daily_cardiovascular_age"]["high_water_day"] == "2026-01-03"
    )
    # The granted-scope endpoints watermark like every other document
    # endpoint (enhanced_tag from its start_day field).
    assert state["endpoints"]["workout"]["high_water_day"] == "2026-01-03"
    assert state["endpoints"]["session"]["high_water_day"] == "2026-01-03"
    assert state["endpoints"]["enhanced_tag"]["high_water_day"] == "2026-01-03"
    assert state["endpoints"]["vO2_max"]["high_water_day"] == "2026-01-03"
    # Partner-gated endpoints never enter the cursor (so a future
    # re-enable backfills from the horizon).
    assert "blood_glucose" not in state["endpoints"]
    # Every fetched endpoint is marked covered so empty endpoints never
    # re-walk the backfill horizon on later runs.
    assert all(
        state["endpoints"][endpoint]["backfill_complete"] is True
        for endpoint in oura.SYNC_ENDPOINTS
    )
    assert state["last_result"]["import_id"] == import_id
    assert state["last_result"]["inserted"] == _SYNC_ROW_COUNT

    # First sync fetches a trailing 30-day window — not deep history.
    readiness_url = next(u for u, _ in transport.calls if "daily_readiness" in u)
    assert "start_date=2025-12-11" in readiness_url
    assert "end_date=2026-01-10" in readiness_url

    assert result["total"] == _SYNC_ROW_COUNT
    assert result["downloaded"] == _SYNC_ROW_COUNT  # inserted
    assert result["imported"] == 0  # updated
    assert result["source_label"] == "Oura (API)"
    assert "cron_hint" not in result


@pytest.mark.parametrize(
    ("retention", "expect_raw_pages"),
    [
        (RawRetentionDecision.RETAIN_PARSED, True),
        (RawRetentionDecision.DISCARD, False),
    ],
)
def test_oura_sync_applies_raw_retention_choice(
    tmp_path: Path,
    monkeypatch,
    retention: RawRetentionDecision,
    expect_raw_pages: bool,
) -> None:
    journal = _use_journal(tmp_path, monkeypatch)
    _write_sync_artifact(
        journal,
        _sync_artifact(journal, raw_retention_decision=retention.value),
    )

    result = oura.backend.sync(
        journal,
        dry_run=False,
        confirm_health_save=True,
        client=_canned_client(_fixture_transport()),
        today=dt.date(2026, 1, 10),
    )

    import_dir = journal / "imports" / result["import_id"]
    raw_dir = import_dir / "raw"
    rows = [
        json.loads(line)
        for line in (import_dir / "normalized" / "2026-01.jsonl")
        .read_text()
        .splitlines()
    ]
    manifest = json.loads((import_dir / "manifest.json").read_text())
    dedupe_row = get_health_dedupe_record(journal, rows[0]["dedupe_key"])

    assert result["raw_retention"] == retention.value
    assert manifest["raw_retention"] == retention.value
    assert dedupe_row is not None
    if expect_raw_pages:
        assert (raw_dir / "oura" / "daily_readiness.jsonl").is_file()
        assert rows[0]["raw_ref"].startswith(f"imports/{result['import_id']}/raw/oura#")
        assert dedupe_row["raw_ref"] == rows[0]["raw_ref"]
    else:
        assert not raw_dir.exists()
        assert all("raw_ref" not in row for row in rows)
        assert dedupe_row["raw_ref"] is None
        assert "raw/" not in json.dumps(manifest)


def test_oura_sync_private_modes_under_permissive_umask(
    tmp_path: Path,
    monkeypatch,
) -> None:
    journal = _use_journal(tmp_path, monkeypatch)
    import_id = "20260110_120000"
    _write_sync_artifact(journal, _sync_artifact(journal))
    imports_dir = journal / "imports"
    stale_bundle = imports_dir / import_id
    stale_raw = stale_bundle / "raw"
    stale_normalized = stale_bundle / "normalized"
    stale_raw.mkdir(parents=True, mode=0o755)
    stale_normalized.mkdir(parents=True, mode=0o755)
    for directory in (imports_dir, stale_bundle, stale_raw, stale_normalized):
        directory.chmod(0o755)
    monkeypatch.setattr(oura, "_new_import_id", lambda _journal: import_id)

    with _temporary_umask(0o000):
        result = oura.backend.sync(
            journal,
            dry_run=False,
            confirm_health_save=True,
            client=_canned_client(_fixture_transport()),
            today=dt.date(2026, 1, 10),
        )

    assert result["import_id"] == import_id
    import_dir = journal / "imports" / import_id
    for directory in (
        journal / "imports",
        import_dir,
        import_dir / "raw",
        import_dir / "raw" / "oura",
        import_dir / "normalized",
    ):
        assert _mode(directory) == 0o700

    for file_path in (
        import_dir / "raw" / "oura" / "daily_readiness.jsonl",
        import_dir / "normalized" / "2026-01.jsonl",
        import_dir / "manifest.json",
        import_dir / "content_manifest.jsonl",
        import_dir / "fetch_windows.json",
        journal / "imports" / "oura.json",
        journal / "imports" / "oura.json.lock",
    ):
        assert _mode(file_path) == 0o600


def test_oura_sync_missing_auth_repairs_imports_dir_before_lock(
    tmp_path: Path,
    monkeypatch,
) -> None:
    journal = _use_journal(tmp_path, monkeypatch)
    _write_sync_artifact(journal, _sync_artifact(journal))
    imports_dir = journal / "imports"
    imports_dir.chmod(0o777)

    with _temporary_umask(0o000):
        with pytest.raises(oura.OuraAuthorizationNeeded):
            oura.backend.sync(
                journal,
                dry_run=False,
                confirm_health_save=True,
                today=dt.date(2026, 1, 10),
            )

    assert _mode(imports_dir) == 0o700
    assert _mode(imports_dir / "oura.json.lock") == 0o600
    assert not (imports_dir / "oura.json").exists()
    assert not any(
        entry.startswith("imports/2026") for entry in _imports_contents(journal)
    )


def test_private_import_lock_excludes_distinct_fds_in_one_process(
    tmp_path: Path,
) -> None:
    lock_path = tmp_path / "journal" / "imports" / "oura.json"

    with hold_private_import_lock(lock_path, timeout=1.0, poll_interval=0.0):
        # This pins flock's per-open-file-description behavior. If this lock ever
        # becomes POSIX per-process locking, the second acquire would succeed.
        with pytest.raises(ImportLockTimeout) as exc_info:
            with hold_private_import_lock(
                lock_path,
                timeout=0.0,
                poll_interval=0.0,
            ):
                pass

    assert exc_info.value.path == lock_path
    assert exc_info.value.timeout == 0.0


def test_oura_sync_lock_timeout_with_same_process_distinct_fd(
    tmp_path: Path,
    monkeypatch,
) -> None:
    journal = _use_journal(tmp_path, monkeypatch)
    _write_sync_artifact(journal, _sync_artifact(journal))
    lock_path = journal / "imports" / "oura.json"
    transport = _fixture_transport()
    monkeypatch.setattr(oura, "OURA_SYNC_LOCK_TIMEOUT", 0.2)

    with hold_private_import_lock(lock_path, timeout=1.0, poll_interval=0.0):
        with pytest.raises(oura.OuraSyncLockError) as exc_info:
            oura.backend.sync(
                journal,
                dry_run=False,
                confirm_health_save=True,
                client=_canned_client(transport),
                today=dt.date(2026, 1, 10),
            )

    payload = exc_info.value.to_dict()
    text = (
        f"{exc_info.value!s}\n"
        f"{exc_info.value.format_text()}\n"
        f"{exc_info.value.to_dict()!r}"
    )
    assert payload["error"] == "oura_sync_lock_timeout"
    assert payload["journal_root"] == str(journal)
    assert payload["lock_path"] == str(lock_path)
    assert payload["timeout_seconds"] == 0.2
    assert str(journal) in text
    assert str(lock_path) in text
    for forbidden in (
        "Bearer",
        "synthetic-access",
        "synthetic-refresh",
        "access_token",
        "refresh_token",
        "api.ouraring",
        "daily_readiness",
        "synthetic-readiness",
    ):
        assert forbidden not in text

    assert transport.calls == []
    contents = _imports_contents(journal)
    assert "imports/oura.json.lock" in contents
    assert "imports/oura.json" not in contents
    assert "imports/health-dedupe.sqlite" not in contents
    assert not any(entry.startswith("imports/2026") for entry in contents)
    assert not any(entry.endswith("manifest.json") for entry in contents)
    assert "imports/content_manifest.jsonl" not in contents
    assert not any("/raw/" in entry or "/normalized/" in entry for entry in contents)


@pytest.mark.integration
def test_overlapping_save_sync_loser_gets_structured_lock_timeout(
    tmp_path: Path,
    monkeypatch,
) -> None:
    journal = _use_journal(tmp_path, monkeypatch)
    _write_sync_artifact(journal, _sync_artifact(journal))
    ready = tmp_path / "slow-sync-ready"
    child_code = f"""
import datetime as dt
import time
from pathlib import Path

from solstone.think.importers import oura


class SlowClient:
    def __init__(self):
        self.signaled = False

    def fetch_endpoint(self, endpoint, *, start_day, end_day):
        if not self.signaled:
            Path({str(ready)!r}).write_text("fetching", encoding="utf-8")
            self.signaled = True
            time.sleep(1.5)
        return oura.OuraFetchResult(items=[], pages=[], requests=1)


oura.backend.sync(
    Path({str(journal)!r}),
    dry_run=False,
    confirm_health_save=True,
    client=SlowClient(),
    today=dt.date(2026, 1, 10),
)
"""
    child = subprocess.Popen(
        [sys.executable, "-c", child_code],
        cwd=Path(__file__).resolve().parents[1],
        env=_subprocess_env(),
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
    )
    try:
        _wait_for_path(ready)
        monkeypatch.setattr(oura, "OURA_SYNC_LOCK_TIMEOUT", 0.2)

        with pytest.raises(oura.OuraSyncLockError) as exc_info:
            oura.backend.sync(
                journal,
                dry_run=False,
                confirm_health_save=True,
                client=_canned_client(_fixture_transport()),
                today=dt.date(2026, 1, 10),
            )

        payload = exc_info.value.to_dict()
        text = exc_info.value.format_text()
        assert payload["error"] == "oura_sync_lock_timeout"
        assert payload["journal_root"] == str(journal)
        assert payload["lock_path"] == str(journal / "imports" / "oura.json")
        assert payload["timeout_seconds"] == 0.2
        assert str(journal) in text
        assert str(journal / "imports" / "oura.json") in text
        for forbidden in (
            "Bearer",
            "access_token",
            "refresh_token",
            "api.ouraring",
            "daily_readiness",
        ):
            assert forbidden not in text
    finally:
        stdout, stderr = child.communicate(timeout=10)
    assert child.returncode == 0, stdout + stderr


@pytest.mark.integration
def test_in_lock_gate_rechecks_artifact_before_fetch_after_wait(
    tmp_path: Path,
    monkeypatch,
) -> None:
    journal = _use_journal(tmp_path, monkeypatch)
    _write_sync_artifact(journal, _sync_artifact(journal))
    lock_path = journal / "imports" / "oura.json"
    ready = tmp_path / "lock-ready"
    release = tmp_path / "release-lock"
    fetched = tmp_path / "fetch-called"
    invalidator_done = tmp_path / "invalidated"

    holder_code = f"""
import time
from pathlib import Path

from solstone.think.importers.shared import hold_private_import_lock

with hold_private_import_lock(Path({str(lock_path)!r}), timeout=5.0):
    Path({str(ready)!r}).write_text("locked", encoding="utf-8")
    release = Path({str(release)!r})
    while not release.exists():
        time.sleep(0.02)
"""
    holder = subprocess.Popen(
        [sys.executable, "-c", holder_code],
        cwd=Path(__file__).resolve().parents[1],
        env=_subprocess_env(),
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
    )

    invalidator_code = f"""
import json
import time
from pathlib import Path

artifact_path = Path({str(oura_sync_approval_path_for_journal(journal))!r})
time.sleep(0.4)
artifact = json.loads(artifact_path.read_text(encoding="utf-8"))
artifact["raw_retention"]["decision"] = "retain_raw_pages"
artifact_path.write_text(json.dumps(artifact), encoding="utf-8")
Path({str(invalidator_done)!r}).write_text("invalidated", encoding="utf-8")
Path({str(release)!r}).write_text("release", encoding="utf-8")
"""

    class MarkerClient:
        def fetch_endpoint(self, endpoint, *, start_day, end_day):
            fetched.write_text(endpoint, encoding="utf-8")
            return oura.OuraFetchResult(items=[], pages=[], requests=1)

    try:
        _wait_for_path(ready)
        invalidator = subprocess.Popen(
            [sys.executable, "-c", invalidator_code],
            cwd=Path(__file__).resolve().parents[1],
            env=_subprocess_env(),
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
        )

        with pytest.raises(PreSaveGateError) as exc_info:
            oura.backend.sync(
                journal,
                dry_run=False,
                confirm_health_save=True,
                client=MarkerClient(),
                today=dt.date(2026, 1, 10),
                now=dt.datetime(2026, 7, 13, 12, 0, tzinfo=dt.UTC),
            )

        invalidator_stdout, invalidator_stderr = invalidator.communicate(timeout=10)
        assert invalidator.returncode == 0, invalidator_stdout + invalidator_stderr
    finally:
        release.write_text("release", encoding="utf-8")
        holder_stdout, holder_stderr = holder.communicate(timeout=10)

    assert holder.returncode == 0, holder_stdout + holder_stderr
    payload = exc_info.value.to_dict()
    assert payload["gate_reason"] == "raw_retention_decision_invalid"
    assert payload["invalid_fields"] == ["raw_retention.decision"]
    assert not fetched.exists()
    assert invalidator_done.exists()
    contents = _imports_contents(journal)
    assert "imports/oura.json.lock" in contents
    assert "imports/oura.json" not in contents
    assert not any(entry.startswith("imports/2026") for entry in contents)
    assert "imports/health-dedupe.sqlite" not in contents


def test_window_days_overrides_first_sync_window(tmp_path: Path, monkeypatch):
    journal = _use_journal(tmp_path, monkeypatch)
    transport = _fixture_transport()
    # A 91-inclusive-day window chunks the datetime-paged series into
    # three <=31-day requests (Oura 400s over-wide heartrate ranges —
    # found live during the 2026-07-06 full-history backfill).
    transport.script(
        "heartrate", _fixture_page("heartrate"), _fixture_page("heartrate")
    )
    client = _canned_client(transport)

    oura.backend.sync(
        journal,
        dry_run=True,
        window_days=90,
        client=client,
        today=dt.date(2026, 1, 10),
    )

    readiness_url = next(u for u, _ in transport.calls if "daily_readiness" in u)
    assert "start_date=2025-10-12" in readiness_url
    heartrate_urls = [u for u, _ in transport.calls if "heartrate" in u]
    assert len(heartrate_urls) == 3
    assert "start_datetime=2025-10-12" in heartrate_urls[0]
    assert "start_datetime=2025-11-12" in heartrate_urls[1]
    assert "start_datetime=2025-12-13" in heartrate_urls[2]
    assert "end_datetime=2026-01-10" in heartrate_urls[2]
    # Partner-gated: even an explicit window never polls blood_glucose.
    assert not [u for u, _ in transport.calls if "blood_glucose" in u]


def test_window_chunks_split_per_endpoint_limits():
    chunks = oura._window_chunks("heartrate", "2025-10-12", "2026-01-10")
    assert chunks == [
        ("2025-10-12", "2025-11-11"),
        ("2025-11-12", "2025-12-12"),
        ("2025-12-13", "2026-01-10"),
    ]
    # blood_glucose shares the 31-day series cap (pinned assumption).
    assert oura._window_chunks("blood_glucose", "2025-10-12", "2026-01-10") == chunks
    assert oura._window_chunks("daily_readiness", "2025-10-12", "2026-01-10") == [
        ("2025-10-12", "2026-01-10")
    ]
    long = oura._window_chunks("daily_readiness", "2022-06-01", "2026-07-06")
    assert len(long) == 5
    assert long[0][0] == "2022-06-01" and long[-1][1] == "2026-07-06"


def test_second_sync_refetches_trailing_window_and_upserts_revisions(
    tmp_path: Path, monkeypatch
):
    journal = _use_journal(tmp_path, monkeypatch)
    _write_sync_artifact(journal, _sync_artifact(journal))

    first_client = _canned_client(_fixture_transport())
    first = oura.backend.sync(
        journal,
        dry_run=False,
        confirm_health_save=True,
        client=first_client,
        today=dt.date(2026, 1, 4),
    )

    # Second run: Oura re-issued the readiness page with corrections.
    second_transport = ScriptedTransport()
    for endpoint in oura.SYNC_ENDPOINTS:
        if endpoint == "daily_readiness":
            second_transport.script(endpoint, _fixture_page(endpoint, REVISION_ROOT))
        else:
            second_transport.script(endpoint, _ok_page([]))
    second = oura.backend.sync(
        journal,
        dry_run=False,
        confirm_health_save=True,
        client=_canned_client(second_transport),
        today=dt.date(2026, 1, 5),
    )

    assert second["import_id"] != first["import_id"]
    # Trailing refetch: watermark is 2026-01-03, today-7d is 2025-12-29;
    # the earlier bound wins so revisions inside the window are re-fetched.
    readiness_url = next(u for u, _ in second_transport.calls if "daily_readiness" in u)
    assert "start_date=2025-12-29" in readiness_url

    # Revisions update in place: no new dedupe rows, refreshed value_hash.
    assert _dedupe_row_count(journal) == _SYNC_ROW_COUNT
    assert second["downloaded"] == 0  # nothing inserted
    assert second["imported"] == 4  # readiness + temperature rows updated
    revised = _items_by_row_identity(
        _normalize_for_import(REVISION_ROOT, second["import_id"])
    )[("oura.daily_readiness", "synthetic-readiness-2026-01-02")]
    ledger_row = get_health_dedupe_record(journal, revised.row["dedupe_key"])
    assert ledger_row is not None
    assert ledger_row["value_hash"] == revised.dedupe_record.value_hash
    assert ledger_row["first_import_id"] == first["import_id"]
    assert ledger_row["last_seen_import_id"] == second["import_id"]

    # Day-level reads surface the corrected score exactly once.
    rows = _iter_normalized_rows(journal, month="2026-01")
    readiness_rows = [
        row
        for row in rows
        if row["record_type"] == "oura.daily_readiness" and row["day"] == "20260102"
    ]
    assert len(readiness_rows) == 1
    assert readiness_rows[0]["value"] == 79

    # Watermarks never regress.
    state = json.loads((journal / "imports" / "oura.json").read_text())
    assert state["endpoints"]["daily_readiness"]["high_water_day"] == "2026-01-03"
    assert state["last_result"]["import_id"] == second["import_id"]


def test_catalog_sync_writes_nothing_and_needs_no_approval(tmp_path: Path, monkeypatch):
    journal = _use_journal(tmp_path, monkeypatch)
    client = _canned_client(_fixture_transport())

    result = oura.backend.sync(
        journal, dry_run=True, client=client, today=dt.date(2026, 1, 10)
    )

    assert result["dry_run"] is True
    assert result["total"] == _SYNC_ROW_COUNT
    assert result["available"] == _SYNC_ROW_COUNT
    # Nothing written: no bundle, no dedupe DB, no cursor.
    assert not (journal / "imports").exists()
    assert not (journal / "imports" / "oura.json.lock").exists()


def test_sync_save_without_artifact_blocks_before_any_fetch(
    tmp_path: Path, monkeypatch
):
    journal = _use_journal(tmp_path, monkeypatch)
    transport = ScriptedTransport()  # any request would raise

    with pytest.raises(PreSaveGateError) as exc_info:
        oura.backend.sync(
            journal,
            dry_run=False,
            confirm_health_save=True,
            client=_canned_client(transport),
            today=dt.date(2026, 1, 10),
        )

    payload = exc_info.value.to_dict()
    assert payload["gate_reason"] == "missing_approval_artifact"
    assert payload["flow"] == "sync"
    assert transport.calls == []
    assert not (journal / "imports").exists()


def test_sync_save_without_confirmation_blocks(tmp_path: Path, monkeypatch):
    journal = _use_journal(tmp_path, monkeypatch)
    _write_sync_artifact(journal, _sync_artifact(journal))
    transport = ScriptedTransport()

    with pytest.raises(PreSaveGateError) as exc_info:
        oura.backend.sync(
            journal,
            dry_run=False,
            client=_canned_client(transport),
            today=dt.date(2026, 1, 10),
        )

    assert exc_info.value.to_dict()["gate_reason"] == "per_run_confirmation_missing"
    assert transport.calls == []
    assert _imports_contents(journal) == _SYNC_APPROVAL_ONLY


def test_sync_gate_journal_binding_mismatch_fails_closed(tmp_path: Path, monkeypatch):
    journal = _use_journal(tmp_path, monkeypatch)
    artifact = _sync_artifact(journal)
    artifact["journal_root"] = str((tmp_path / "other-journal").resolve())
    _write_sync_artifact(journal, artifact)
    transport = ScriptedTransport()

    with pytest.raises(PreSaveGateError) as exc_info:
        oura.backend.sync(
            journal,
            dry_run=False,
            confirm_health_save=True,
            client=_canned_client(transport),
            today=dt.date(2026, 1, 10),
        )

    payload = exc_info.value.to_dict()
    assert payload["gate_reason"] == "journal_root_binding_mismatch"
    assert payload["invalid_fields"] == ["journal_root"]
    assert transport.calls == []
    assert _imports_contents(journal) == _SYNC_APPROVAL_ONLY


def test_scheduled_sync_without_consent_fails_closed(tmp_path: Path, monkeypatch):
    journal = _use_journal(tmp_path, monkeypatch)
    _write_sync_artifact(journal, _sync_artifact(journal))  # no scheduled_sync
    transport = ScriptedTransport()

    with pytest.raises(PreSaveGateError) as exc_info:
        oura.backend.sync(
            journal,
            dry_run=False,
            scheduled=True,
            client=_canned_client(transport),
            today=dt.date(2026, 1, 10),
        )

    payload = exc_info.value.to_dict()
    assert payload["gate_reason"] == "scheduled_sync_consent_missing"
    assert payload["missing_fields"] == ["scheduled_sync"]
    assert transport.calls == []
    assert _imports_contents(journal) == _SYNC_APPROVAL_ONLY


def test_scheduled_sync_with_unapproved_consent_fails_closed(
    tmp_path: Path, monkeypatch
):
    journal = _use_journal(tmp_path, monkeypatch)
    artifact = _sync_artifact(journal)
    artifact["scheduled_sync"] = {
        "approved": False,
        "cadence": "every 6 hours",
    }
    _write_sync_artifact(journal, artifact)

    with pytest.raises(PreSaveGateError) as exc_info:
        oura.backend.sync(
            journal,
            dry_run=False,
            scheduled=True,
            client=_canned_client(ScriptedTransport()),
            today=dt.date(2026, 1, 10),
        )

    assert exc_info.value.to_dict()["gate_reason"] == "scheduled_sync_not_approved"


def test_scheduled_sync_with_consent_passes_and_emits_cron_hint(
    tmp_path: Path, monkeypatch
):
    journal = _use_journal(tmp_path, monkeypatch)
    artifact = _sync_artifact(journal)
    artifact["scheduled_sync"] = _scheduled_sync_consent()
    _write_sync_artifact(journal, artifact)
    client = _canned_client(_fixture_transport())

    # Scheduled runs rely on the standing consent — no per-run flag.
    result = oura.backend.sync(
        journal,
        dry_run=False,
        scheduled=True,
        client=client,
        today=dt.date(2026, 1, 10),
    )

    assert result["downloaded"] == _SYNC_ROW_COUNT
    assert result["cron_hint"].startswith("0 */6 * * * ")
    assert result["cron_hint"].endswith("importer --sync oura --save --scheduled")


def test_save_sync_cron_hint_uses_gate_decision_without_rereading_artifact(
    tmp_path: Path, monkeypatch
):
    journal = _use_journal(tmp_path, monkeypatch)
    artifact = _sync_artifact(journal)
    artifact["scheduled_sync"] = _scheduled_sync_consent()
    _write_sync_artifact(journal, artifact)

    result = oura.backend.sync(
        journal,
        dry_run=False,
        confirm_health_save=True,
        client=_canned_client(_fixture_transport()),
        today=dt.date(2026, 1, 10),
    )

    assert not hasattr(oura, "read_oura_sync_approval")
    assert result["cron_hint"].startswith("0 */6 * * * ")


def test_sync_fails_loud_on_corrupt_cursor(tmp_path: Path, monkeypatch):
    journal = _use_journal(tmp_path, monkeypatch)
    _write_sync_artifact(journal, _sync_artifact(journal))
    cursor = journal / "imports" / "oura.json"
    cursor.parent.mkdir(parents=True, exist_ok=True)
    cursor.write_text("{not json", encoding="utf-8")
    transport = ScriptedTransport()

    with pytest.raises(oura.OuraSyncStateError, match="Corrupt Oura sync cursor"):
        oura.backend.sync(
            journal,
            dry_run=False,
            confirm_health_save=True,
            client=_canned_client(transport),
            today=dt.date(2026, 1, 10),
        )

    assert transport.calls == []
    # The corrupt cursor is left untouched for inspection.
    assert cursor.read_text(encoding="utf-8") == "{not json"


def test_sync_fails_loud_on_unknown_cursor_schema(tmp_path: Path, monkeypatch):
    journal = _use_journal(tmp_path, monkeypatch)
    cursor = journal / "imports" / "oura.json"
    cursor.parent.mkdir(parents=True, exist_ok=True)
    cursor.write_text(json.dumps({"schema": "something.else.v9"}), encoding="utf-8")

    with pytest.raises(oura.OuraSyncStateError, match="Unsupported Oura sync cursor"):
        oura.backend.sync(
            journal,
            dry_run=True,
            client=_canned_client(ScriptedTransport()),
            today=dt.date(2026, 1, 10),
        )


def test_sync_rejects_nonpositive_window_days(tmp_path: Path, monkeypatch):
    journal = _use_journal(tmp_path, monkeypatch)

    with pytest.raises(ValueError, match="window_days"):
        oura.backend.sync(
            journal,
            dry_run=True,
            window_days=0,
            client=_canned_client(ScriptedTransport()),
        )


# ---------------------------------------------------------------------------
# Quiet runs — save mode writes nothing when every fetched row is already
# in the dedupe ledger with an identical value hash (hourly schedules)
# ---------------------------------------------------------------------------


def _imports_stat_snapshot(
    journal: Path, *, exclude: tuple[str, ...] = ()
) -> dict[str, tuple[int, int | None]]:
    """(mtime_ns, size) for everything under imports/, minus exclusions."""

    snapshot: dict[str, tuple[int, int | None]] = {}
    for path in sorted((journal / "imports").rglob("*")):
        rel = path.relative_to(journal).as_posix()
        if rel in exclude:
            continue
        # sqlite connection artifacts (see _imports_contents): the ledger's
        # sidecars and its own header mtime move whenever any connection
        # opens it — not import writes.
        # The Oura sync lock sidecar is also metadata: every save-mode run
        # opens it to prove cross-process exclusion.
        if (
            rel == "imports/oura.json.lock"
            or path.name.endswith(("-shm", "-wal"))
            or path.name == "health-dedupe.sqlite"
        ):
            continue
        stat = path.stat()
        snapshot[rel] = (stat.st_mtime_ns, stat.st_size if path.is_file() else None)
    return snapshot


def test_quiet_second_sync_writes_nothing_and_advances_cursor(
    tmp_path: Path, monkeypatch
):
    journal = _use_journal(tmp_path, monkeypatch)
    _write_sync_artifact(journal, _sync_artifact(journal))

    first = oura.backend.sync(
        journal,
        dry_run=False,
        confirm_health_save=True,
        client=_canned_client(_fixture_transport()),
        today=dt.date(2026, 1, 4),
    )
    assert first["quiet_run"] is False

    listing_before = _imports_contents(journal)
    stats_before = _imports_stat_snapshot(journal, exclude=("imports/oura.json",))

    # Second run re-fetches the byte-identical pages: nothing new, nothing
    # revised — a quiet run.
    second = oura.backend.sync(
        journal,
        dry_run=False,
        confirm_health_save=True,
        client=_canned_client(_fixture_transport()),
        today=dt.date(2026, 1, 5),
    )

    assert second["quiet_run"] is True
    assert "import_id" not in second
    # NO bundle, NO raw pages, NO shards, NO manifest: the imports/ dir
    # listing is identical and every pre-existing path is mtime-stable —
    # only the cursor file changed.
    assert _imports_contents(journal) == listing_before
    assert (
        _imports_stat_snapshot(journal, exclude=("imports/oura.json",)) == stats_before
    )

    # CLI-facing keys: total says what was fetched, nothing importable.
    assert second["total"] == _SYNC_ROW_COUNT
    assert second["available"] == 0
    assert second["imported"] == 0
    assert second["downloaded"] == 0
    assert second["months"] == []
    assert second["summary"] == (
        f"Oura (API) sync quiet run: nothing new (rows={_SYNC_ROW_COUNT} all known)"
    )

    # The cursor advanced exactly as a full save would have.
    state = json.loads((journal / "imports" / "oura.json").read_text())
    assert state["schema"] == oura.SYNC_STATE_SCHEMA
    assert state["endpoints"]["daily_readiness"]["high_water_day"] == "2026-01-03"
    assert state["last_result"]["quiet_run"] is True
    assert state["last_result"]["import_id"] is None
    assert state["last_result"]["rows"] == _SYNC_ROW_COUNT

    # The dedupe ledger is untouched (reads only).
    assert _dedupe_row_count(journal) == _SYNC_ROW_COUNT


def test_revision_within_refetch_window_triggers_full_bundle_not_quiet(
    tmp_path: Path, monkeypatch
):
    journal = _use_journal(tmp_path, monkeypatch)
    _write_sync_artifact(journal, _sync_artifact(journal))
    oura.backend.sync(
        journal,
        dry_run=False,
        confirm_health_save=True,
        client=_canned_client(_fixture_transport()),
        today=dt.date(2026, 1, 4),
    )

    # Oura re-issued the readiness page with corrections inside the
    # trailing refetch window; every other endpoint returns empty.
    transport = ScriptedTransport()
    for endpoint in oura.SYNC_ENDPOINTS:
        if endpoint == "daily_readiness":
            transport.script(endpoint, _fixture_page(endpoint, REVISION_ROOT))
        else:
            transport.script(endpoint, _ok_page([]))

    second = oura.backend.sync(
        journal,
        dry_run=False,
        confirm_health_save=True,
        client=_canned_client(transport),
        today=dt.date(2026, 1, 5),
    )

    # A changed value hash is the revision-capture path — never quiet.
    assert second["quiet_run"] is False
    import_dir = journal / "imports" / second["import_id"]
    shard = import_dir / "normalized" / "2026-01.jsonl"
    rows = [json.loads(line) for line in shard.read_text().splitlines()]
    # The bundle is the complete fetch — all four readiness-page rows,
    # including the byte-identical re-issue — not a changed-rows delta.
    assert len(rows) == 4
    assert (import_dir / "manifest.json").exists()
    assert second["downloaded"] == 0  # nothing inserted
    assert second["imported"] == 4  # updated in place
    state = json.loads((journal / "imports" / "oura.json").read_text())
    assert state["last_result"]["import_id"] == second["import_id"]
    assert state["last_result"]["quiet_run"] is False


def test_new_data_run_writes_full_bundle_with_all_rows(tmp_path: Path, monkeypatch):
    journal = _use_journal(tmp_path, monkeypatch)
    _write_sync_artifact(journal, _sync_artifact(journal))
    oura.backend.sync(
        journal,
        dry_run=False,
        confirm_health_save=True,
        client=_canned_client(_fixture_transport()),
        today=dt.date(2026, 1, 4),
    )

    # One brand-new document arrives alongside the known re-fetch.
    new_doc = {
        "id": "synthetic-sleep-2026-01-04",
        "day": "2026-01-04",
        "timestamp": "2026-01-04T00:00:00-07:00",
        "score": 91,
    }
    transport = ScriptedTransport()
    for endpoint in oura.SYNC_ENDPOINTS:
        if endpoint == "daily_sleep":
            transport.script(endpoint, _ok_page(_fixture_items(endpoint) + [new_doc]))
        else:
            transport.script(endpoint, _fixture_page(endpoint))

    second = oura.backend.sync(
        journal,
        dry_run=False,
        confirm_health_save=True,
        client=_canned_client(transport),
        today=dt.date(2026, 1, 5),
    )

    # Any new row means a full save, unaffected by the quiet-run path:
    # the bundle keeps the complete-fetch property (all rows, not a delta).
    assert second["quiet_run"] is False
    shard = journal / "imports" / second["import_id"] / "normalized" / "2026-01.jsonl"
    rows = [json.loads(line) for line in shard.read_text().splitlines()]
    assert len(rows) == _SYNC_ROW_COUNT + 1
    assert second["downloaded"] == 1  # the new document
    assert second["imported"] == _SYNC_ROW_COUNT  # known rows upserted
    assert _dedupe_row_count(journal) == _SYNC_ROW_COUNT + 1
    state = json.loads((journal / "imports" / "oura.json").read_text())
    assert state["endpoints"]["daily_sleep"]["high_water_day"] == "2026-01-04"


def test_gate_still_blocks_would_be_quiet_run(tmp_path: Path, monkeypatch):
    journal = _use_journal(tmp_path, monkeypatch)
    _write_sync_artifact(journal, _sync_artifact(journal))
    first = oura.backend.sync(
        journal,
        dry_run=False,
        confirm_health_save=True,
        client=_canned_client(_fixture_transport()),
        today=dt.date(2026, 1, 4),
    )

    # The gate governs the RUN, not just the write: a run that would have
    # been quiet still blocks without per-run confirmation — before any
    # fetch, and the cursor does not move.
    transport = _fixture_transport()
    with pytest.raises(PreSaveGateError) as exc_info:
        oura.backend.sync(
            journal,
            dry_run=False,
            client=_canned_client(transport),
            today=dt.date(2026, 1, 5),
        )

    assert exc_info.value.to_dict()["gate_reason"] == "per_run_confirmation_missing"
    assert transport.calls == []
    state = json.loads((journal / "imports" / "oura.json").read_text())
    assert state["last_result"]["import_id"] == first["import_id"]


def test_scheduled_quiet_run_relies_on_standing_consent(tmp_path: Path, monkeypatch):
    journal = _use_journal(tmp_path, monkeypatch)
    artifact = _sync_artifact(journal)
    artifact["scheduled_sync"] = _scheduled_sync_consent()
    _write_sync_artifact(journal, artifact)

    oura.backend.sync(
        journal,
        dry_run=False,
        scheduled=True,
        client=_canned_client(_fixture_transport()),
        today=dt.date(2026, 1, 4),
    )
    listing_before = _imports_contents(journal)

    # The --scheduled consent check is orthogonal to quiet runs: standing
    # consent still substitutes for the per-run flag, and the quiet run
    # still returns the cron hint.
    second = oura.backend.sync(
        journal,
        dry_run=False,
        scheduled=True,
        client=_canned_client(_fixture_transport()),
        today=dt.date(2026, 1, 5),
    )

    assert second["quiet_run"] is True
    assert "import_id" not in second
    assert second["cron_hint"].startswith("0 */6 * * * ")
    assert _imports_contents(journal) == listing_before


def test_quiet_backfill_with_window_days_writes_nothing(tmp_path: Path, monkeypatch):
    journal = _use_journal(tmp_path, monkeypatch)
    _write_sync_artifact(journal, _sync_artifact(journal))
    oura.backend.sync(
        journal,
        dry_run=False,
        confirm_health_save=True,
        client=_canned_client(_fixture_transport()),
        today=dt.date(2026, 1, 10),
    )
    listing_before = _imports_contents(journal)

    # An explicit 90-day backfill window re-fetches only known rows (the
    # datetime-paged heartrate series chunks into three requests). Quiet
    # backfills are rare but must also write nothing.
    transport = _fixture_transport()
    transport.script(
        "heartrate", _fixture_page("heartrate"), _fixture_page("heartrate")
    )
    second = oura.backend.sync(
        journal,
        dry_run=False,
        confirm_health_save=True,
        window_days=90,
        client=_canned_client(transport),
        today=dt.date(2026, 1, 10),
    )

    # --window-days behavior is preserved on the quiet path.
    readiness_url = next(u for u, _ in transport.calls if "daily_readiness" in u)
    assert "start_date=2025-10-12" in readiness_url
    assert len([u for u, _ in transport.calls if "heartrate" in u]) == 3
    assert not [u for u, _ in transport.calls if "blood_glucose" in u]

    assert second["quiet_run"] is True
    assert second["total"] > _SYNC_ROW_COUNT  # chunk overlap re-reads
    assert _imports_contents(journal) == listing_before
    assert _dedupe_row_count(journal) == _SYNC_ROW_COUNT


# ---------------------------------------------------------------------------
# Cursor upgrade — endpoints added to SYNC_ENDPOINTS after a journal
# already carries a cursor backfill from the horizon, not the trailing
# window; endpoints that fetched empty never re-walk the horizon.
# ---------------------------------------------------------------------------

# The endpoint set a pre-upgrade cursor knows about (live cursors written
# before daily_cardiovascular_age joined SYNC_ENDPOINTS, and before
# backfill_complete existed).
_PRE_UPGRADE_ENDPOINTS = (
    "daily_readiness",
    "daily_sleep",
    "daily_stress",
    "daily_resilience",
    "daily_spo2",
    "sleep",
    "daily_activity",
    "heartrate",
)

# The endpoints that joined SYNC_ENDPOINTS after those cursors were
# written (daily_cardiovascular_age, then the 2026-07-07 granted-scope
# four). Computed from the registry so the pin tracks future additions.
_POST_UPGRADE_ENDPOINTS = tuple(
    endpoint
    for endpoint in oura.SYNC_ENDPOINTS
    if endpoint not in _PRE_UPGRADE_ENDPOINTS
)


def _write_pre_upgrade_cursor(journal: Path, high_water: str = "2026-01-03") -> None:
    state = {
        "schema": oura.SYNC_STATE_SCHEMA,
        "last_sync": "2026-01-04T00:00:00+00:00",
        "trailing_refetch_days": oura.TRAILING_REFETCH_DAYS,
        "endpoints": {
            endpoint: {"high_water_day": high_water, "next_token": None}
            for endpoint in _PRE_UPGRADE_ENDPOINTS
        },
        "last_result": {
            "import_id": "20260104_000000",
            "quiet_run": False,
            "rows": 17,
            "inserted": 17,
            "updated": 0,
            "pages": 8,
        },
    }
    cursor = journal / "imports" / "oura.json"
    cursor.parent.mkdir(parents=True, exist_ok=True)
    cursor.write_text(json.dumps(state), encoding="utf-8")


def _script_horizon_pages(
    transport: ScriptedTransport,
    endpoint: str,
    today: dt.date,
    *,
    data_on_last_chunk: bool = False,
) -> int:
    """Script one empty page per horizon chunk; optionally data on the last."""

    chunks = oura._window_chunks(endpoint, oura.BACKFILL_HORIZON_DAY, today.isoformat())
    for index in range(len(chunks)):
        if data_on_last_chunk and index == len(chunks) - 1:
            transport.script(endpoint, _fixture_page(endpoint))
        else:
            transport.script(endpoint, _ok_page([]))
    return len(chunks)


def test_cursor_upgrade_fetches_new_endpoints_from_backfill_horizon(
    tmp_path: Path, monkeypatch
):
    journal = _use_journal(tmp_path, monkeypatch)
    _write_pre_upgrade_cursor(journal)
    today = dt.date(2026, 1, 10)

    transport = ScriptedTransport()
    for endpoint in _PRE_UPGRADE_ENDPOINTS:
        transport.script(endpoint, _fixture_page(endpoint))
    chunk_counts = {
        endpoint: _script_horizon_pages(transport, endpoint, today)
        for endpoint in _POST_UPGRADE_ENDPOINTS
    }

    result = oura.backend.sync(
        journal, dry_run=True, client=_canned_client(transport), today=today
    )

    # Known endpoints resume from their watermark/trailing window …
    readiness_url = next(u for u, _ in transport.calls if "daily_readiness" in u)
    assert "start_date=2026-01-03" in readiness_url
    # … the endpoints the cursor has never seen walk their FULL history
    # from the backfill horizon, chunked per endpoint limits — not just
    # the trailing window.
    for endpoint in _POST_UPGRADE_ENDPOINTS:
        urls = [u for u, _ in transport.calls if f"/{endpoint}?" in u]
        # 364-day chunks since 2015 — a dozen requests per endpoint.
        assert len(urls) == chunk_counts[endpoint] > 10
        assert "start_date=2015-01-01" in urls[0]
        assert result["windows"][endpoint][0] == "2015-01-01"
    # The partner-gated endpoint is never fetched, upgrade or not.
    assert not [u for u, _ in transport.calls if "blood_glucose" in u]
    # Catalog runs never advance the cursor, so the pre-upgrade cursor is
    # byte-identical afterwards.
    assert json.loads((journal / "imports" / "oura.json").read_text())[
        "endpoints"
    ].keys() == set(_PRE_UPGRADE_ENDPOINTS)


def test_cursor_upgrade_save_adopts_new_endpoints_and_marks_backfill(
    tmp_path: Path, monkeypatch
):
    journal = _use_journal(tmp_path, monkeypatch)
    _write_sync_artifact(journal, _sync_artifact(journal))
    _write_pre_upgrade_cursor(journal)
    today = dt.date(2026, 1, 10)

    transport = ScriptedTransport()
    for endpoint in _PRE_UPGRADE_ENDPOINTS:
        transport.script(endpoint, _fixture_page(endpoint))
    for endpoint in _POST_UPGRADE_ENDPOINTS:
        _script_horizon_pages(transport, endpoint, today, data_on_last_chunk=True)

    result = oura.backend.sync(
        journal,
        dry_run=False,
        confirm_health_save=True,
        client=_canned_client(transport),
        today=today,
    )

    assert result["downloaded"] == _SYNC_ROW_COUNT  # fresh journal: all new
    state = json.loads((journal / "imports" / "oura.json").read_text())
    # The upgraded cursor now carries every polled endpoint: watermarks
    # preserved or established, the new endpoints marked backfilled, and
    # the partner-gated endpoint still absent.
    assert set(state["endpoints"]) == set(oura.SYNC_ENDPOINTS)
    assert state["endpoints"]["daily_readiness"]["high_water_day"] == "2026-01-03"
    for endpoint in _POST_UPGRADE_ENDPOINTS:
        assert state["endpoints"][endpoint]["high_water_day"] == "2026-01-03"
        assert state["endpoints"][endpoint]["backfill_complete"] is True
    assert "blood_glucose" not in state["endpoints"]


def test_empty_backfilled_endpoint_polls_trailing_window_not_horizon(
    tmp_path: Path, monkeypatch
):
    journal = _use_journal(tmp_path, monkeypatch)
    _write_sync_artifact(journal, _sync_artifact(journal))
    _write_pre_upgrade_cursor(journal)
    today = dt.date(2026, 1, 10)

    # First post-upgrade save: the new endpoints' full-horizon walks come
    # back EMPTY (no VO2 max estimates on the account, say).
    transport = ScriptedTransport()
    for endpoint in _PRE_UPGRADE_ENDPOINTS:
        transport.script(endpoint, _fixture_page(endpoint))
    for endpoint in _POST_UPGRADE_ENDPOINTS:
        _script_horizon_pages(transport, endpoint, today)
    oura.backend.sync(
        journal,
        dry_run=False,
        confirm_health_save=True,
        client=_canned_client(transport),
        today=today,
    )

    state = json.loads((journal / "imports" / "oura.json").read_text())
    assert state["endpoints"]["vO2_max"]["high_water_day"] is None
    assert state["endpoints"]["vO2_max"]["backfill_complete"] is True

    # Next run: the empty-but-backfilled endpoint polls a modest trailing
    # window — one request, never the dozen-chunk horizon walk again.
    second_transport = ScriptedTransport()
    for endpoint in oura.SYNC_ENDPOINTS:
        second_transport.script(endpoint, _ok_page([]))
    oura.backend.sync(
        journal,
        dry_run=True,
        client=_canned_client(second_transport),
        today=dt.date(2026, 1, 11),
    )

    vo2_urls = [u for u, _ in second_transport.calls if "vO2_max" in u]
    assert len(vo2_urls) == 1
    assert "start_date=2015-01-01" not in vo2_urls[0]
    assert "start_date=2025-12-12" in vo2_urls[0]  # today - 30d


# ---------------------------------------------------------------------------
# Partner-gated endpoints — blood_glucose stays fully wired for parse/
# normalize/dedupe but is never polled: Oura's developer portal (2026-07)
# exposes no `metabolic` scope to standard apps, so every fetch would 401
# forever and the hourly lane would report the gap each cycle.
# ---------------------------------------------------------------------------


def test_partner_gate_registry_membership():
    assert set(oura._PARTNER_GATED_ENDPOINTS) == {"blood_glucose"}
    # Gated endpoints are never polled but keep their machinery.
    assert set(oura._PARTNER_GATED_ENDPOINTS).isdisjoint(oura.SYNC_ENDPOINTS)
    assert set(oura._PARTNER_GATED_ENDPOINTS) <= set(oura.ENDPOINT_RECORD_TYPES)
    # The 2026-07-07 granted-scope endpoints ARE polled.
    for endpoint in ("workout", "session", "enhanced_tag", "vO2_max"):
        assert endpoint in oura.SYNC_ENDPOINTS


def test_sync_never_polls_partner_gated_blood_glucose(tmp_path: Path, monkeypatch):
    # The directive behind the demotion: a full-registry sync must run
    # clean — zero blood_glucose requests, zero errors — instead of
    # reporting the unauthorized endpoint every scheduled cycle.
    journal = _use_journal(tmp_path, monkeypatch)
    _write_sync_artifact(journal, _sync_artifact(journal))

    transport = _fixture_transport()
    result = oura.backend.sync(
        journal,
        dry_run=False,
        confirm_health_save=True,
        client=_canned_client(transport),
        today=dt.date(2026, 1, 10),
    )

    assert not [u for u, _ in transport.calls if "blood_glucose" in u]
    assert result["errors"] == []
    assert "blood_glucose" not in result["endpoints"]
    assert "blood_glucose" not in result["windows"]
    # Never backfill_complete: a future re-enable (one line — move the
    # name back into SYNC_ENDPOINTS) still walks the full horizon.
    state = json.loads((journal / "imports" / "oura.json").read_text())
    assert "blood_glucose" not in state["endpoints"]


def test_cursor_upgrade_drops_stale_partner_gated_entry(tmp_path: Path, monkeypatch):
    # The live 2026-07 cursor generation carries a blood_glucose entry
    # (never backfilled — every poll 401d on the missing scope). After
    # the demotion, the next save rewrites the cursor without it, never
    # fetches it, and backfills the granted-scope four from the horizon.
    journal = _use_journal(tmp_path, monkeypatch)
    _write_sync_artifact(journal, _sync_artifact(journal))
    today = dt.date(2026, 1, 10)
    ten_era_endpoints = (*_PRE_UPGRADE_ENDPOINTS, "daily_cardiovascular_age")
    state = {
        "schema": oura.SYNC_STATE_SCHEMA,
        "last_sync": "2026-01-04T00:00:00+00:00",
        "trailing_refetch_days": oura.TRAILING_REFETCH_DAYS,
        "endpoints": {
            **{
                endpoint: {
                    "high_water_day": "2026-01-03",
                    "backfill_complete": True,
                    "next_token": None,
                }
                for endpoint in ten_era_endpoints
            },
            "blood_glucose": {
                "high_water_day": None,
                "backfill_complete": False,
                "next_token": None,
            },
        },
        "last_result": {
            "import_id": "20260104_000000",
            "quiet_run": False,
            "rows": 19,
            "inserted": 19,
            "updated": 0,
            "pages": 10,
        },
    }
    cursor = journal / "imports" / "oura.json"
    cursor.parent.mkdir(parents=True, exist_ok=True)
    cursor.write_text(json.dumps(state), encoding="utf-8")

    transport = ScriptedTransport()
    for endpoint in ten_era_endpoints:
        transport.script(endpoint, _fixture_page(endpoint))
    for endpoint in ("workout", "session", "enhanced_tag", "vO2_max"):
        _script_horizon_pages(transport, endpoint, today, data_on_last_chunk=True)

    result = oura.backend.sync(
        journal,
        dry_run=False,
        confirm_health_save=True,
        client=_canned_client(transport),
        today=today,
    )

    assert not [u for u, _ in transport.calls if "blood_glucose" in u]
    assert result["errors"] == []
    new_state = json.loads((journal / "imports" / "oura.json").read_text())
    assert "blood_glucose" not in new_state["endpoints"]
    for endpoint in ("workout", "session", "enhanced_tag", "vO2_max"):
        assert new_state["endpoints"][endpoint]["backfill_complete"] is True
        assert new_state["endpoints"][endpoint]["high_water_day"] == "2026-01-03"


# ---------------------------------------------------------------------------
# Scope degradation — an endpoint the authorization cannot read (401 after
# a good refresh) is skipped, reported, and backfilled after reauth; the
# rest of the run proceeds.
# ---------------------------------------------------------------------------


def test_sync_skips_endpoint_missing_scope_and_keeps_run_alive(
    tmp_path: Path, monkeypatch
):
    journal = _use_journal(tmp_path, monkeypatch)
    _write_sync_artifact(journal, _sync_artifact(journal))
    refresh_calls: list[str] = []

    def refresh(tokens, *, client_id):
        refresh_calls.append(client_id)
        return _fake_tokens("refreshed-access")

    transport = ScriptedTransport()
    for endpoint in oura.SYNC_ENDPOINTS:
        if endpoint == "workout":
            # A scope gap (say, a token minted before the workout scope
            # existed on this grant): the endpoint 401s, once before the
            # refresh and once after it.
            transport.script(endpoint, _status(401), _status(401))
        else:
            transport.script(endpoint, _fixture_page(endpoint))

    result = oura.backend.sync(
        journal,
        dry_run=False,
        confirm_health_save=True,
        client=_canned_client(transport, refresh_tokens=refresh),
        today=dt.date(2026, 1, 10),
    )

    # One refresh attempt total, then the endpoint-scoped skip.
    assert refresh_calls == ["synthetic-client-id"]
    # Every other endpoint landed: full sync bundle minus 2 workout rows.
    assert result["downloaded"] == _SYNC_ROW_COUNT - 2
    assert _dedupe_row_count(journal) == _SYNC_ROW_COUNT - 2
    assert result["endpoints"]["workout"] == 0
    # The gap is reported factually, with the reauthorization command.
    assert len(result["errors"]) == 1
    assert "workout" in result["errors"][0]
    assert "journal importer --connect oura" in result["errors"][0]
    # The skipped endpoint is not marked backfilled, so the first sync
    # after reauthorization walks it from the horizon.
    state = json.loads((journal / "imports" / "oura.json").read_text())
    assert state["endpoints"]["workout"]["backfill_complete"] is False
    assert state["endpoints"]["workout"]["high_water_day"] is None
    assert state["endpoints"]["daily_readiness"]["backfill_complete"] is True

    # Post-reauthorization: the next save backfills workout from the
    # horizon and clears the gap.
    today = dt.date(2026, 1, 11)
    second_transport = ScriptedTransport()
    for endpoint in oura.SYNC_ENDPOINTS:
        if endpoint != "workout":
            second_transport.script(endpoint, _ok_page([]))
    _script_horizon_pages(second_transport, "workout", today, data_on_last_chunk=True)
    second = oura.backend.sync(
        journal,
        dry_run=False,
        confirm_health_save=True,
        client=_canned_client(second_transport),
        today=today,
    )

    workout_urls = [u for u, _ in second_transport.calls if "/workout?" in u]
    assert "start_date=2015-01-01" in workout_urls[0]
    assert second["errors"] == []
    assert second["downloaded"] == 2
    state = json.loads((journal / "imports" / "oura.json").read_text())
    assert state["endpoints"]["workout"]["backfill_complete"] is True
    assert state["endpoints"]["workout"]["high_water_day"] == "2026-01-03"


def test_token_death_still_fails_the_whole_run(tmp_path: Path, monkeypatch):
    journal = _use_journal(tmp_path, monkeypatch)
    _write_sync_artifact(journal, _sync_artifact(journal))

    def dead_refresh(tokens, *, client_id):
        raise oura.OuraApiError("refresh grant rejected")

    transport = ScriptedTransport()
    transport.script("daily_readiness", _status(401))

    with pytest.raises(oura.OuraApiError, match="refresh grant rejected"):
        oura.backend.sync(
            journal,
            dry_run=False,
            confirm_health_save=True,
            client=_canned_client(transport, refresh_tokens=dead_refresh),
            today=dt.date(2026, 1, 10),
        )

    # The failed run leaves no cursor: the next run re-fetches the same
    # windows and converges.
    assert not (journal / "imports" / "oura.json").exists()


# ---------------------------------------------------------------------------
# Default auth wiring — tokens flow to and from journal config through the
# config owner (the journal is the one trusted store)
# ---------------------------------------------------------------------------


def test_client_default_loaders_round_trip_tokens_through_journal_config(
    tmp_path: Path, monkeypatch
):
    journal = _use_journal(tmp_path, monkeypatch)
    config_path = journal / "config" / "journal.json"
    config_path.write_text(
        json.dumps(
            {
                "identity": {"timezone": "America/Denver"},
                "oura": {
                    "client_id": "config-client-id",
                    "client_secret": "config-secret-sensitive",
                    "tokens": {
                        "access_token": "config-access",
                        "refresh_token": "config-refresh",
                        "expires_at": 4102444800.0,
                        "token_type": "bearer",
                    },
                },
            }
        ),
        encoding="utf-8",
    )

    def refresh(tokens, *, client_id):
        # The default loader handed the config-held tokens to the refresh.
        assert tokens.access_token == "config-access"
        assert tokens.refresh_token == "config-refresh"
        assert client_id == "config-client-id"
        return _fake_tokens("refreshed-access")

    transport = ScriptedTransport()
    transport.script(
        "daily_sleep", _status(401), _ok_page(_fixture_items("daily_sleep"))
    )
    client = oura.OuraApiClient(
        transport,
        journal_root=journal,
        refresh_tokens=refresh,
        sleep=_unexpected_sleep,
    )

    fetched = client.fetch_endpoint(
        "daily_sleep", start_day="2026-01-01", end_day="2026-01-10"
    )

    assert len(fetched.items) == 2
    assert transport.calls[0][1]["Authorization"] == "Bearer config-access"
    assert transport.calls[1][1]["Authorization"] == "Bearer refreshed-access"
    # The refreshed tokens persisted back into journal config through the
    # config owner, preserving every other key in the file.
    config = json.loads(config_path.read_text(encoding="utf-8"))
    assert config["oura"]["tokens"]["access_token"] == "refreshed-access"
    assert config["oura"]["tokens"]["refresh_token"] == "synthetic-refresh"
    assert config["oura"]["client_id"] == "config-client-id"
    assert config["oura"]["client_secret"] == "config-secret-sensitive"
    assert config["identity"]["timezone"] == "America/Denver"


def test_client_without_config_tokens_raises_authorization_needed(
    tmp_path: Path, monkeypatch
):
    journal = _use_journal(tmp_path, monkeypatch)  # config has no oura section
    transport = ScriptedTransport()
    client = oura.OuraApiClient(
        transport, journal_root=journal, sleep=_unexpected_sleep
    )

    with pytest.raises(oura.OuraAuthorizationNeeded, match="--connect oura"):
        client.fetch_endpoint(
            "daily_sleep", start_day="2026-01-01", end_day="2026-01-10"
        )

    assert transport.calls == []


# ---------------------------------------------------------------------------
# CLI wiring — connect stub, gate output, cron-hint guidance
# ---------------------------------------------------------------------------


def test_cli_connect_oura_without_client_id_errors_clearly(tmp_path: Path, monkeypatch):
    _use_journal(tmp_path, monkeypatch)
    cli = import_module("solstone.think.importers.cli")

    with pytest.raises(SystemExit) as exc_info:
        cli._run_connect("oura")

    assert "client_id missing from journal config" in str(exc_info.value)


def test_cli_connect_oura_prints_scopes_and_saves_tokens_to_config(
    tmp_path: Path, monkeypatch, capsys
):
    journal = _use_journal(tmp_path, monkeypatch)
    (journal / "config" / "journal.json").write_text(
        json.dumps({"oura": {"client_id": "config-client-id"}}), encoding="utf-8"
    )
    cli = import_module("solstone.think.importers.cli")
    oura_auth = import_module("solstone.think.importers.oura_auth")

    auth_calls: list[str] = []
    tokens = oura_auth.OuraTokens(
        access_token="connected-access",
        refresh_token="connected-refresh",
        expires_at=4102444800.0,
    )

    def fake_auth(*, client_id):
        auth_calls.append(client_id)
        return tokens

    monkeypatch.setattr(oura_auth, "run_owner_present_auth", fake_auth)

    cli._run_connect("oura")

    assert auth_calls == ["config-client-id"]
    # Tokens landed in journal config — the one trusted store — with the
    # client_id preserved beside them.
    config = json.loads(
        (journal / "config" / "journal.json").read_text(encoding="utf-8")
    )
    assert config["oura"]["client_id"] == "config-client-id"
    assert config["oura"]["tokens"]["access_token"] == "connected-access"
    assert config["oura"]["tokens"]["refresh_token"] == "connected-refresh"
    out = capsys.readouterr().out
    # The owner sees exactly which scopes are being requested — including
    # the blood-glucose (metabolic) scope this reauthorization adds.
    assert "Requesting scopes: " + " ".join(oura_auth.OAUTH_SCOPES) in out
    assert "metabolic" in out
    assert "email" not in out
    assert "personal" not in out
    assert "saved to journal config" in out
    # No token material in owner-facing output.
    assert "connected-access" not in out
    assert "connected-refresh" not in out


def test_cli_connect_rejects_unknown_backend():
    cli = import_module("solstone.think.importers.cli")

    with pytest.raises(SystemExit, match="Unknown connect backend"):
        cli._run_connect("plaud")


def test_cli_sync_prints_gate_block_and_exits_2(tmp_path: Path, monkeypatch, capsys):
    journal = _use_journal(tmp_path, monkeypatch)
    cli = import_module("solstone.think.importers.cli")
    transport = ScriptedTransport()

    with pytest.raises(SystemExit) as exc_info:
        cli._run_sync(
            "oura",
            dry_run=False,
            confirm_health_save=True,
            client=_canned_client(transport),
            today=dt.date(2026, 1, 10),
        )

    assert exc_info.value.code == 2
    out = capsys.readouterr().out
    assert "Health sync save blocked before journal write." in out
    assert str(journal.resolve()) in out
    assert transport.calls == []
    assert not (journal / "imports").exists()


def test_cli_sync_prints_cron_hint_when_scheduled_consent_exists(
    tmp_path: Path, monkeypatch, capsys
):
    journal = _use_journal(tmp_path, monkeypatch)
    artifact = _sync_artifact(journal)
    artifact["scheduled_sync"] = _scheduled_sync_consent()
    _write_sync_artifact(journal, artifact)
    cli = import_module("solstone.think.importers.cli")

    cli._run_sync(
        "oura",
        dry_run=False,
        confirm_health_save=True,
        client=_canned_client(_fixture_transport()),
        today=dt.date(2026, 1, 10),
    )

    out = capsys.readouterr().out
    assert "Crontab line (not installed):" in out
    assert "importer --sync oura --save --scheduled" in out


def test_cli_sync_lock_timeout_prints_typed_message(
    tmp_path: Path, monkeypatch, capsys
):
    journal = _use_journal(tmp_path, monkeypatch)
    cli = import_module("solstone.think.importers.cli")
    lock_path = journal / "imports" / "oura.json"

    def busy_sync(_journal_root: Path, **_kwargs):
        raise oura.OuraSyncLockError(
            journal_root=journal,
            lock_path=lock_path,
            timeout=0.2,
        )

    monkeypatch.setattr(oura.backend, "sync", busy_sync)

    with pytest.raises(SystemExit) as exc_info:
        cli._run_sync("oura", dry_run=False, confirm_health_save=True)

    assert exc_info.value.code == oura.OuraSyncLockError.exit_code
    out = capsys.readouterr().out
    assert "Oura sync save could not acquire the journal lock." in out
    assert str(journal) in out
    assert str(lock_path) in out
    assert "Traceback" not in out
    assert "Bearer" not in out


def test_save_records_fetch_windows_and_catalog_reports_known_refetch(
    tmp_path: Path, monkeypatch
):
    # Upstream verification findings: catalog counted trailing-refetch rows
    # as importable, and bundles carried no fetch-window evidence for the
    # chunker. Both pinned here.
    journal = _use_journal(tmp_path, monkeypatch)
    _write_sync_artifact(journal, _sync_artifact(journal))

    first = oura.backend.sync(
        journal,
        dry_run=False,
        confirm_health_save=True,
        client=_canned_client(_fixture_transport()),
        today=dt.date(2026, 1, 4),
    )
    bundle_dir = journal / "imports" / first["import_id"]
    windows_doc = json.loads((bundle_dir / "fetch_windows.json").read_text())
    assert windows_doc["schema"] == "solstone.oura_fetch_windows.v1"
    assert "daily_readiness" in windows_doc["windows"]
    assert windows_doc["chunk_limits"]["heartrate"] == 31

    catalog = oura.backend.sync(
        journal,
        dry_run=True,
        client=_canned_client(_fixture_transport()),
        today=dt.date(2026, 1, 4),
    )
    assert catalog["available"] == 0
    assert catalog["known_refetch"] == catalog["rows"]
