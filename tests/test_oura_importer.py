# SPDX-License-Identifier: AGPL-3.0-only
# Copyright (c) 2026 sol pbc

"""Oura importer skeleton tests — parse, normalize, dedupe keys, gate."""

from __future__ import annotations

import json
import sqlite3
from pathlib import Path

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
    SENSITIVE_IMPORTERS,
    PreSaveGateError,
    approval_path_for_journal,
)
from solstone.think.importers.sync import SYNCABLE_REGISTRY

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
        "target_journal_path": str(journal.resolve()),
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


_APPROVAL_ONLY = [
    "imports/_approvals",
    "imports/_approvals/health_import_preflight.json",
]


def _imports_contents(journal: Path) -> list[str]:
    imports_dir = journal / "imports"
    if not imports_dir.exists():
        return []
    return sorted(p.relative_to(journal).as_posix() for p in imports_dir.rglob("*"))


# ---------------------------------------------------------------------------
# Registration and gate membership
# ---------------------------------------------------------------------------


def test_oura_registered_as_file_importer():
    assert "oura" in FILE_IMPORTER_REGISTRY
    assert get_file_importer("oura") is not None


def test_oura_is_a_sensitive_importer():
    assert "oura" in SENSITIVE_IMPORTERS


def test_oura_not_registered_as_syncable_backend():
    # Phase guard: the sync seam raises; a registry entry lands only with
    # the real phase O3 implementation (updated together with this test).
    assert "oura" not in SYNCABLE_REGISTRY


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
    }
    assert len(bundle["daily_sleep"]) == 2
    assert len(bundle["sleep"]) == 2
    assert len(bundle["daily_stress"]) == 1


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

    assert items, "fixtures must normalize to rows"
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
# Revision / upsert de-risk — Oura re-issues documents with corrections
# (same document id, changed payload; scores settle for a day or two).
# Design requirement (oura_design_20260705.md §4c): same id → same dedupe
# key → the upsert UPDATES in place, never duplicates. Fixtures:
# revisions/ re-issues the base daily_readiness page (see README.md).
# Two strict xfails below pin exposed defects for the phase O1/O2 sessions;
# remove each marker together with its fix.
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

    Mirrors the storage the design doc fixes for phase O1 ("mirrors
    apple_health exactly"): per-bundle ``imports/<id>/normalized/<month>.jsonl``
    with ``import_id`` / ``month`` / bundle-prefixed ``normalized_ref``
    stamped on each row, exactly as ``apple_health._save_export`` writes them.
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


@pytest.mark.xfail(
    strict=True,
    reason=(
        "Exposed defect: body's _iter_normalized_rows keeps the first row "
        "per dedupe key over bundles sorted oldest-first, so a revision "
        "arriving in a later bundle surfaces the superseded payload. The "
        "trailing-window re-fetch exists to pick up revisions "
        "(oura_design_20260705.md §5), so the newest revision must win at "
        "day-level reads. Fix belongs in solstone/apps/body/routes.py or "
        "the phase-O1 save path — outside this suite's ownership. Remove "
        "this marker with that fix."
    ),
)
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
    # 9 documents; readiness docs each add a temperature-deviation row.
    assert preview.item_count == 11
    assert "daily_readiness=2" in preview.summary
    assert "sleep=2" in preview.summary
    assert f"source_family={SOURCE_OURA_API}" in preview.summary


def test_dry_run_process_writes_nothing(tmp_path: Path, monkeypatch):
    journal = _use_journal(tmp_path, monkeypatch)

    result = oura.OuraImporter().process(FIXTURE_ROOT, journal, dry_run=True)

    assert result.entries_written == 0
    assert result.files_created == []
    assert result.summary.startswith("Dry run only:")
    assert not (journal / "imports").exists()


# ---------------------------------------------------------------------------
# Pre-save gate enforcement
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
    assert "Sleep 7h 19m · Oura's staging" in summary
    assert "deep 1h 31m" in summary
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
# Sync + OAuth seams: design-only, no side effects
# ---------------------------------------------------------------------------


def test_sync_backend_raises_seam_error_without_touching_journal(
    tmp_path: Path, monkeypatch
):
    journal = _use_journal(tmp_path, monkeypatch)

    with pytest.raises(NotImplementedError, match=oura.DESIGN_DOC):
        oura.backend.sync(journal, dry_run=True)
    with pytest.raises(NotImplementedError, match="OWNER-PRESENT-ONLY"):
        oura.backend.sync(journal, dry_run=False)

    assert list(journal.iterdir()) == []


def test_oauth_seams_raise_with_design_doc_pointer():
    with pytest.raises(NotImplementedError, match="OWNER-PRESENT-ONLY"):
        oura.begin_owner_present_authorization()
    with pytest.raises(NotImplementedError, match=oura.DESIGN_DOC):
        oura.complete_owner_present_authorization("http://localhost/callback?code=x")
    with pytest.raises(NotImplementedError, match=oura.DESIGN_DOC):
        oura.refresh_tokens()


def test_module_contains_no_network_client_imports():
    # Hard rule for this phase: no network code anywhere in the module,
    # not even on dead paths. Guard the import surface.
    import sys

    module = sys.modules[oura.__name__]
    source = Path(module.__file__).read_text(encoding="utf-8")
    for fragment in ("requests", "httpx", "urllib", "aiohttp", "socket"):
        assert fragment not in source, f"network-capable import {fragment!r} found"
