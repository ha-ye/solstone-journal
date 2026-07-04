# SPDX-License-Identifier: AGPL-3.0-only
# Copyright (c) 2026 sol pbc

from __future__ import annotations

import json
import sqlite3
from pathlib import Path

from solstone.apps.health import routes as health_routes


def _write_json(path: Path, payload: dict) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(payload, indent=2) + "\n", encoding="utf-8")


def _append_jsonl(path: Path, rows: list[dict]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        "".join(json.dumps(row, sort_keys=True) + "\n" for row in rows),
        encoding="utf-8",
    )


def _seed_health_import(journal: Path) -> None:
    import_id = "20260703_120000"
    _write_json(
        journal / "imports" / import_id / "manifest.json",
        {
            "import_id": import_id,
            "source_type": "apple_health",
            "source_hash": "sha256:synthetic",
            "entry_count": 5,
            "days_affected": ["20260703", "20260704"],
            "files_created": [
                str(
                    journal
                    / "chronicle"
                    / "20260703"
                    / "import.apple_health"
                    / "000000_300"
                    / "day_summary_transcript.md"
                )
            ],
            "imported_at": "2026-07-04T01:00:00",
            "imported_via": "test",
        },
    )
    rows = [
        {
            "schema": "solstone.health.normalized.v1",
            "source_family": "apple_health",
            "kind": "record",
            "dedupe_key": "apple-health:glucose:1",
            "record_type": "HKQuantityTypeIdentifierBloodGlucose",
            "day": "20260703",
            "start_date": "2026-07-03T08:00:00-06:00",
            "end_date": "2026-07-03T08:01:00-06:00",
            "source_name": "Synthetic Stelo",
            "unit": "mg/dL",
            "value": "100",
            "import_id": import_id,
            "month": "2026-07",
            "normalized_ref": (f"imports/{import_id}/normalized/2026-07.jsonl#L1"),
        },
        {
            "schema": "solstone.health.normalized.v1",
            "source_family": "apple_health",
            "kind": "record",
            "dedupe_key": "apple-health:glucose:2",
            "record_type": "HKQuantityTypeIdentifierBloodGlucose",
            "day": "20260703",
            "start_date": "2026-07-03T12:00:00-06:00",
            "end_date": "2026-07-03T12:01:00-06:00",
            "source_name": "Synthetic Stelo",
            "unit": "mg/dL",
            "value": "140",
            "import_id": import_id,
            "month": "2026-07",
            "normalized_ref": (f"imports/{import_id}/normalized/2026-07.jsonl#L2"),
        },
        {
            "schema": "solstone.health.normalized.v1",
            "source_family": "apple_health",
            "kind": "record",
            "dedupe_key": "apple-health:heart-rate:1",
            "record_type": "HKQuantityTypeIdentifierHeartRate",
            "day": "20260703",
            "start_date": "2026-07-03T13:00:00-06:00",
            "source_name": "Synthetic Watch",
            "unit": "count/min",
            "value": "72",
            "import_id": import_id,
            "month": "2026-07",
            "normalized_ref": (f"imports/{import_id}/normalized/2026-07.jsonl#L3"),
        },
        {
            "schema": "solstone.health.normalized.v1",
            "source_family": "apple_health",
            "kind": "workout",
            "dedupe_key": "apple-health:workout:1",
            "record_type": "HKWorkoutActivityTypeRunning",
            "day": "20260704",
            "start_date": "2026-07-04T06:30:00-06:00",
            "source_name": "Synthetic Watch",
            "import_id": import_id,
            "month": "2026-07",
            "normalized_ref": (f"imports/{import_id}/normalized/2026-07.jsonl#L4"),
        },
    ]
    _append_jsonl(
        journal / "imports" / import_id / "normalized" / "2026-07.jsonl", rows
    )
    summary = (
        "# Apple Health Summary\n\n"
        "Day: 20260703\n\n"
        "Counts by type:\n"
        "- HKQuantityTypeIdentifierBloodGlucose: 2\n"
        "- HKQuantityTypeIdentifierHeartRate: 1\n"
    )
    summary_path = (
        journal
        / "chronicle"
        / "20260703"
        / "import.apple_health"
        / "000000_300"
        / "day_summary_transcript.md"
    )
    summary_path.parent.mkdir(parents=True, exist_ok=True)
    summary_path.write_text(summary, encoding="utf-8")

    db_path = journal / "imports" / "health-dedupe.sqlite"
    db_path.parent.mkdir(parents=True, exist_ok=True)
    with sqlite3.connect(db_path) as conn:
        conn.execute(
            """
            CREATE TABLE health_dedupe (
                dedupe_key TEXT PRIMARY KEY,
                source_family TEXT NOT NULL,
                source_record_id TEXT,
                record_type TEXT NOT NULL,
                start_time TEXT NOT NULL,
                end_time TEXT,
                value_hash TEXT,
                first_import_id TEXT,
                last_seen_import_id TEXT,
                normalized_ref TEXT,
                raw_ref TEXT,
                created_at TEXT NOT NULL,
                updated_at TEXT NOT NULL
            )
            """
        )
        for row in rows:
            conn.execute(
                """
                INSERT INTO health_dedupe (
                    dedupe_key,
                    source_family,
                    record_type,
                    start_time,
                    end_time,
                    first_import_id,
                    last_seen_import_id,
                    normalized_ref,
                    created_at,
                    updated_at
                )
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    row["dedupe_key"],
                    row["source_family"],
                    row["record_type"],
                    row["start_date"],
                    row.get("end_date"),
                    import_id,
                    import_id,
                    row["normalized_ref"],
                    "2026-07-04T01:00:00Z",
                    "2026-07-04T01:00:00Z",
                ),
            )


def test_status_api_summarizes_synthetic_health_import(health_env):
    env = health_env()
    _seed_health_import(env.journal)

    response = env.client.get("/app/health/api/status")

    assert response.status_code == 200
    status = response.get_json()
    assert status["imports"][0]["import_id"] == "20260703_120000"
    assert status["coverage_window"] == {
        "start": "2026-07-03T08:00:00-06:00",
        "end": "2026-07-04T06:30:00-06:00",
    }
    assert status["normalized"]["total"] == 4
    assert status["normalized"]["by_type"] == {
        "HKQuantityTypeIdentifierBloodGlucose": 2,
        "HKQuantityTypeIdentifierHeartRate": 1,
        "HKWorkoutActivityTypeRunning": 1,
    }
    assert status["normalized"]["by_source"] == {
        "Synthetic Stelo": 2,
        "Synthetic Watch": 2,
    }
    assert status["normalized"]["by_month"] == {"2026-07": 4}
    assert status["dedupe"]["total"] == 4
    assert status["dedupe"]["by_type"] == {
        "HKQuantityTypeIdentifierBloodGlucose": 2,
        "HKQuantityTypeIdentifierHeartRate": 1,
        "HKWorkoutActivityTypeRunning": 1,
    }
    assert status["dedupe"]["by_source"] == {"apple_health": 4}
    assert status["dedupe"]["by_month"] == {"2026-07": 4}
    assert status["latest_by_source"] == {
        "Synthetic Stelo": "2026-07-03T12:00:00-06:00",
        "Synthetic Watch": "2026-07-04T06:30:00-06:00",
    }


def test_status_page_renders_import_and_dedupe_sections(health_env):
    env = health_env()
    _seed_health_import(env.journal)

    response = env.client.get("/app/health/imports")

    assert response.status_code == 200
    html = response.get_data(as_text=True)
    assert "20260703_120000" in html
    assert "By type" in html
    assert "Dedupe by type" in html
    assert "HKQuantityTypeIdentifierBloodGlucose" in html
    assert "apple_health" in html


def test_day_api_returns_summary_and_factual_glucose_stats(health_env):
    env = health_env()
    _seed_health_import(env.journal)

    response = env.client.get("/app/health/api/day/20260703")

    assert response.status_code == 200
    payload = response.get_json()
    assert payload["day"] == "20260703"
    assert "HKQuantityTypeIdentifierBloodGlucose: 2" in payload["summary_markdown"]
    assert payload["glucose"] == {
        "count": 2,
        "min": 100.0,
        "max": 140.0,
        "mean": 120.0,
        "unit": "mg/dL",
    }


def test_day_page_renders_summary_and_glucose_facts_only(health_env):
    env = health_env()
    _seed_health_import(env.journal)

    response = env.client.get("/app/health/imports/20260703")

    assert response.status_code == 200
    html = response.get_data(as_text=True)
    assert "Day summary" in html
    assert "HKQuantityTypeIdentifierBloodGlucose: 2" in html
    assert "Mean" in html
    assert "120" in html
    lowered = html.lower()
    assert "normal glucose" not in lowered
    assert "high glucose" not in lowered
    assert "low glucose" not in lowered


def test_health_templates_and_copy_avoid_surveillance_verbs():
    health_root = Path(health_routes.__file__).resolve().parent
    banned = {"capture", "watch", "record", "monitor", "track", "collect"}
    checked = [health_root / "imports.html"]

    for path in checked:
        source = path.read_text(encoding="utf-8").lower()
        found = {word for word in banned if word in source}
        assert found == set(), f"{path.name} contains banned copy terms: {found}"


def test_health_call_module_uses_convey_http_only():
    health_root = Path(health_routes.__file__).resolve().parent
    source = (health_root / "call.py").read_text(encoding="utf-8")

    assert "solstone.think.convey_client" in source
    assert "from pathlib" not in source
    assert "import os" not in source
    assert "sqlite3" not in source
    assert "open(" not in source


def test_read_routes_create_nothing_in_empty_journal(health_env):
    env = health_env()
    imports_root = env.journal / "imports"
    assert not imports_root.exists()

    assert env.client.get("/app/health/api/status").status_code == 200
    assert env.client.get("/app/health/api/day/20260703").status_code == 200
    assert env.client.get("/app/health/api/stats/2026-07").status_code == 200
    assert env.client.get("/app/health/imports").status_code == 200

    assert not imports_root.exists()
    assert not (imports_root / "health-dedupe.sqlite").exists()


def test_non_health_import_manifests_are_excluded(health_env):
    env = health_env()
    _seed_health_import(env.journal)
    _write_json(
        env.journal / "imports" / "20260601_090000" / "manifest.json",
        {
            "import_id": "20260601_090000",
            "source_type": "plaud",
            "source_hash": "sha256:other",
            "entry_count": 3,
            "days_affected": ["20260601"],
            "files_created": [],
            "imported_at": "2026-06-01T09:00:00",
            "imported_via": "test",
        },
    )
    (env.journal / "imports" / "20260602_090000").mkdir(parents=True)
    (env.journal / "imports" / "20260602_090000" / "manifest.json").write_text(
        "not json", encoding="utf-8"
    )

    response = env.client.get("/app/health/api/status")

    assert response.status_code == 200
    import_ids = [item["import_id"] for item in response.get_json()["imports"]]
    assert import_ids == ["20260703_120000"]


def test_month_stats_api_returns_day_counts(health_env):
    env = health_env()
    _seed_health_import(env.journal)

    response = env.client.get("/app/health/api/stats/2026-07")

    assert response.status_code == 200
    assert response.get_json() == {"20260703": 3, "20260704": 1}


def test_day_api_rejects_invalid_day(health_env):
    env = health_env()

    assert env.client.get("/app/health/api/day/not-a-day").status_code == 400
    assert env.client.get("/app/health/api/day/2026-07-03").status_code == 400
