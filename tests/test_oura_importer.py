# SPDX-License-Identifier: AGPL-3.0-only
# Copyright (c) 2026 sol pbc

"""Oura importer tests — parse, normalize, dedupe, fetch layer, sync, gate."""

from __future__ import annotations

import ast
import datetime as dt
import json
import sqlite3
import sys
from importlib import import_module
from pathlib import Path
from types import SimpleNamespace

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
    approval_path_for_journal,
    oura_sync_approval_path_for_journal,
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

# Fixture bundle shape: 15 documents across 8 endpoints; each readiness
# document also splits out a temperature-deviation row -> 17 rows.
_FIXTURE_ROW_COUNT = 17


def _use_journal(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    journal = tmp_path / "journal"
    journal.mkdir()
    monkeypatch.setenv("SOLSTONE_JOURNAL", str(journal))
    return journal


def _valid_artifact(journal: Path, importers: list[str]) -> dict:
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
        "raw_retention": {
            "decision": "retain_compressed_zip",
            "notes": "Synthetic test decision.",
        },
        "requires_per_run_confirmation": True,
        "no_real_health_data_in_artifact": True,
    }


def _write_artifact(journal: Path, payload: dict) -> Path:
    approval_path = approval_path_for_journal(journal)
    approval_path.parent.mkdir(parents=True)
    approval_path.write_text(json.dumps(payload), encoding="utf-8")
    return approval_path


def _sync_artifact(journal: Path) -> dict:
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
        "raw_retention": {
            "decision": "retain_raw_pages",
            "notes": "Synthetic test decision.",
        },
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


def _imports_contents(journal: Path) -> list[str]:
    imports_dir = journal / "imports"
    if not imports_dir.exists():
        return []
    return sorted(p.relative_to(journal).as_posix() for p in imports_dir.rglob("*"))


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
    # Mocks the OuraTokens contract from local_secrets (built separately).
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
    }
    assert len(bundle["daily_sleep"]) == 2
    assert len(bundle["sleep"]) == 2
    assert len(bundle["daily_stress"]) == 1
    assert len(bundle["daily_activity"]) == 2
    assert len(bundle["heartrate"]) == 4


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


def test_normalize_heartrate_synthesizes_identity_and_day_verbatim():
    items = _normalized_items()
    rows = [item.row for item in items if item.row["record_type"] == "oura.heartrate"]

    assert len(rows) == 4
    first = next(
        row for row in rows if row["start_date"] == "2026-01-02T03:15:00-07:00"
    )
    # Heartrate rows carry no Oura day field: the journal day is the date
    # component of Oura's own offset-bearing timestamp, verbatim.
    assert first["day"] == "20260102"
    assert first["kind"] == "sample"
    assert first["value"] == 49
    assert first["unit"] == "bpm"
    assert first["source_record_id"] == "heartrate/2026-01-02T03:15:00-07:00/sleep"
    assert first["metadata"] == {"source": "sleep"}


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
    # 15 documents; readiness docs each add a temperature-deviation row.
    assert preview.item_count == _FIXTURE_ROW_COUNT
    assert "daily_readiness=2" in preview.summary
    assert "sleep=2" in preview.summary
    assert "daily_activity=2" in preview.summary
    assert "heartrate=4" in preview.summary
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
# Day-summary rendering (§13: attributed facts, no interpretation)
# ---------------------------------------------------------------------------


def test_render_day_summary_attributes_every_score_to_oura():
    rows = [item.row for item in _normalized_items() if item.row["day"] == "20260102"]

    summary = oura.render_day_summary("20260102", rows, import_id="20260105_000000")

    assert summary.splitlines()[0] == "# Body · January 2, 2026"
    assert "Readiness 82 · Oura's score" in summary
    assert "Sleep score 88 · Oura's score" in summary
    assert "Resilience solid · Oura's level" in summary
    assert "Day stress summary normal · Oura's label" in summary
    assert "Nightly blood oxygen 97.4% · Oura's average" in summary
    assert "Temperature deviation -0.21 °C · Oura's measurement" in summary
    assert "Activity score 85 · Oura's score" in summary
    assert "Sleep 7h 19m · Oura's staging" in summary
    assert "deep 1h 31m" in summary
    # Heartrate samples are a series, never summarized into day prose.
    assert "bpm" not in summary
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

    with pytest.raises(oura.OuraApiError, match="after one token refresh"):
        client.fetch_endpoint(
            "daily_sleep", start_day="2026-01-01", end_day="2026-01-10"
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
    (journal / "config").mkdir(parents=True)
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
    assert len(rows) == _FIXTURE_ROW_COUNT
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
    assert manifest["entry_count"] == _FIXTURE_ROW_COUNT
    assert manifest["days_affected"] == ["20260102", "20260103"]
    content_lines = (import_dir / "content_manifest.jsonl").read_text().splitlines()
    assert json.loads(content_lines[0])["type"] == "health_normalized_month"

    # Dedupe upserts: every row inserted exactly once.
    assert _dedupe_row_count(journal) == _FIXTURE_ROW_COUNT

    # Cursor advanced only after the writes, with per-endpoint watermarks.
    state = json.loads((journal / "imports" / "oura.json").read_text())
    assert state["schema"] == oura.SYNC_STATE_SCHEMA
    assert state["trailing_refetch_days"] == oura.TRAILING_REFETCH_DAYS
    assert state["endpoints"]["daily_readiness"]["high_water_day"] == "2026-01-03"
    assert state["endpoints"]["heartrate"]["high_water_day"] == "2026-01-03"
    assert state["last_result"]["import_id"] == import_id
    assert state["last_result"]["inserted"] == _FIXTURE_ROW_COUNT

    # First sync fetches a trailing 30-day window — not deep history.
    readiness_url = next(u for u, _ in transport.calls if "daily_readiness" in u)
    assert "start_date=2025-12-11" in readiness_url
    assert "end_date=2026-01-10" in readiness_url

    assert result["total"] == _FIXTURE_ROW_COUNT
    assert result["downloaded"] == _FIXTURE_ROW_COUNT  # inserted
    assert result["imported"] == 0  # updated
    assert result["source_label"] == "Oura (API)"
    assert "cron_hint" not in result


def test_window_days_overrides_first_sync_window(tmp_path: Path, monkeypatch):
    journal = _use_journal(tmp_path, monkeypatch)
    transport = _fixture_transport()
    # A 91-inclusive-day window chunks heartrate into three <=31-day
    # requests (Oura 400s over-wide heartrate ranges — found live during
    # the 2026-07-06 full-history backfill).
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


def test_window_chunks_split_per_endpoint_limits():
    chunks = oura._window_chunks("heartrate", "2025-10-12", "2026-01-10")
    assert chunks == [
        ("2025-10-12", "2025-11-11"),
        ("2025-11-12", "2025-12-12"),
        ("2025-12-13", "2026-01-10"),
    ]
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
    assert _dedupe_row_count(journal) == _FIXTURE_ROW_COUNT
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
    assert result["total"] == _FIXTURE_ROW_COUNT
    assert result["available"] == _FIXTURE_ROW_COUNT
    # Nothing written: no bundle, no dedupe DB, no cursor.
    assert not (journal / "imports").exists()


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
    artifact["scheduled_sync"] = {"approved": False, "cadence": "every 6 hours"}
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
    artifact["scheduled_sync"] = {"approved": True, "cadence": "every 6 hours"}
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

    assert result["downloaded"] == _FIXTURE_ROW_COUNT
    assert result["cron_hint"].startswith("0 */6 * * * ")
    assert result["cron_hint"].endswith("import --sync oura --save --scheduled")


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
# CLI wiring — connect stub, gate output, cron-hint guidance
# ---------------------------------------------------------------------------


def test_cli_connect_oura_without_auth_layer_errors_clearly(monkeypatch):
    cli = import_module("solstone.think.importers.cli")
    # Force the import failure so this pins the missing-auth-layer message
    # even on checkouts where the O2 auth layer has landed.
    monkeypatch.setitem(sys.modules, "solstone.think.importers.oura_auth", None)
    monkeypatch.delattr(
        import_module("solstone.think.importers"), "oura_auth", raising=False
    )

    with pytest.raises(SystemExit) as exc_info:
        cli._run_connect("oura")

    assert "auth layer not yet installed" in str(exc_info.value)


def test_cli_connect_oura_without_client_id_errors_clearly(tmp_path: Path, monkeypatch):
    _use_journal(tmp_path, monkeypatch)
    cli = import_module("solstone.think.importers.cli")

    with pytest.raises(SystemExit) as exc_info:
        cli._run_connect("oura")

    assert "client_id missing from journal config" in str(exc_info.value)


def test_cli_connect_oura_runs_auth_and_saves_tokens(
    tmp_path: Path, monkeypatch, capsys
):
    journal = _use_journal(tmp_path, monkeypatch)
    (journal / "config").mkdir()
    (journal / "config" / "journal.json").write_text(
        json.dumps({"oura": {"client_id": "config-client-id"}}), encoding="utf-8"
    )
    cli = import_module("solstone.think.importers.cli")
    local_secrets = import_module("solstone.think.importers.local_secrets")
    oura_auth = import_module("solstone.think.importers.oura_auth")

    auth_calls: list[str] = []
    saved: list = []
    tokens = _fake_tokens("connected-access")

    def fake_auth(*, client_id):
        auth_calls.append(client_id)
        return tokens

    monkeypatch.setattr(oura_auth, "run_owner_present_auth", fake_auth)
    monkeypatch.setattr(local_secrets, "save_oura_tokens", saved.append)

    cli._run_connect("oura")

    assert auth_calls == ["config-client-id"]
    assert saved == [tokens]
    out = capsys.readouterr().out
    assert "saved to the local secret store" in out
    # No token material in owner-facing output.
    assert "connected-access" not in out


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
    artifact["scheduled_sync"] = {"approved": True, "cadence": "every 6 hours"}
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
    assert "import --sync oura --save --scheduled" in out


# ---------------------------------------------------------------------------
# No-network guard: the module's only network-capable import lives inside
# the injectable default transport, which tests never construct.
# ---------------------------------------------------------------------------

_NETWORK_ROOTS = {"requests", "httpx", "aiohttp", "socket", "http"}


def _imported_module_names(node: ast.AST) -> list[str]:
    if isinstance(node, ast.Import):
        return [alias.name for alias in node.names]
    if isinstance(node, ast.ImportFrom):
        return [node.module or ""]
    return []


def test_module_has_no_reachable_network_imports():
    source = Path(oura.__file__).read_text(encoding="utf-8")
    tree = ast.parse(source)

    # Module level: no network-capable imports at all. urllib.parse is
    # pure string handling (query encoding) and allowed; the socket-backed
    # urllib.request / urllib.error are not.
    for node in tree.body:
        for name in _imported_module_names(node):
            root = name.split(".")[0]
            assert root not in _NETWORK_ROOTS, f"module-level import {name!r}"
            assert name not in {"urllib", "urllib.request", "urllib.error"}, (
                f"module-level import {name!r}"
            )

    # Function level: network-capable imports may exist ONLY inside the
    # injectable default transport.
    for func in ast.walk(tree):
        if not isinstance(func, (ast.FunctionDef, ast.AsyncFunctionDef)):
            continue
        for node in ast.walk(func):
            for name in _imported_module_names(node):
                root = name.split(".")[0]
                if root in _NETWORK_ROOTS or name.startswith("urllib"):
                    assert func.name == "_default_transport", (
                        f"network-capable import {name!r} outside "
                        f"_default_transport (in {func.name!r})"
                    )
