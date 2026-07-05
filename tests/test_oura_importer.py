# SPDX-License-Identifier: AGPL-3.0-only
# Copyright (c) 2026 sol pbc

"""Oura importer skeleton tests — parse, normalize, dedupe keys, gate."""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from solstone.think.importers import oura
from solstone.think.importers.file_importer import (
    FILE_IMPORTER_REGISTRY,
    get_file_importer,
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
