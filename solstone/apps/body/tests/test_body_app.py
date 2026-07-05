# SPDX-License-Identifier: AGPL-3.0-only
# Copyright (c) 2026 sol pbc

from __future__ import annotations

import json
import math
import sqlite3
from datetime import date
from pathlib import Path

import pytest

from solstone.apps.body import routes as body_routes
from solstone.think.importers import health_schema

DEDUPE_TABLE_SQL = """
CREATE TABLE IF NOT EXISTS health_dedupe (
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


def _write_json(path: Path, payload: dict) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(payload, indent=2) + "\n", encoding="utf-8")


def _append_jsonl(path: Path, rows: list[dict]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        "".join(json.dumps(row, sort_keys=True) + "\n" for row in rows),
        encoding="utf-8",
    )


def _row(
    record_type: str,
    start: str,
    end: str | None = None,
    *,
    value: str | None = None,
    unit: str | None = None,
    source: str = "Synthetic Device",
    kind: str = "record",
    metadata: dict | None = None,
) -> dict:
    day = start[:10].replace("-", "")
    row = {
        "schema": "solstone.health.normalized.v1",
        "source_family": "apple_health",
        "kind": kind,
        "dedupe_key": f"apple-health:{record_type}:{source}:{start}",
        "record_type": record_type,
        "day": day,
        "start_date": start,
        "source_name": source,
        "month": start[:7],
    }
    if end is not None:
        row["end_date"] = end
    if value is not None:
        row["value"] = value
    if unit is not None:
        row["unit"] = unit
    if metadata is not None:
        row["metadata"] = metadata
    return row


def _insert_dedupe_rows(journal: Path, rows: list[dict], import_id: str) -> None:
    db_path = journal / "imports" / "health-dedupe.sqlite"
    db_path.parent.mkdir(parents=True, exist_ok=True)
    with sqlite3.connect(db_path) as conn:
        conn.execute(DEDUPE_TABLE_SQL)
        for row in rows:
            conn.execute(
                """
                INSERT OR IGNORE INTO health_dedupe (
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
                    row.get("normalized_ref", ""),
                    "2026-07-04T01:00:00Z",
                    "2026-07-04T01:00:00Z",
                ),
            )


def _seed_import(
    journal: Path,
    import_id: str,
    rows: list[dict],
    *,
    imported_at: str = "2026-07-04T01:00:00",
) -> None:
    """Write manifest + month shards + dedupe rows for synthetic entries."""
    for row in rows:
        row.setdefault("import_id", import_id)
    days = sorted({row["day"] for row in rows})
    _write_json(
        journal / "imports" / import_id / "manifest.json",
        {
            "import_id": import_id,
            "source_type": "apple_health",
            "source_hash": f"sha256:{import_id}",
            "entry_count": len(rows),
            "days_affected": days,
            "files_created": [],
            "imported_at": imported_at,
            "imported_via": "test",
        },
    )
    by_month: dict[str, list[dict]] = {}
    for row in rows:
        by_month.setdefault(row["month"], []).append(row)
    for month, month_rows in by_month.items():
        _append_jsonl(
            journal / "imports" / import_id / "normalized" / f"{month}.jsonl",
            month_rows,
        )
    _insert_dedupe_rows(journal, rows, import_id)


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
    _insert_dedupe_rows(journal, rows, import_id)


def test_status_api_summarizes_synthetic_health_import(body_env):
    env = body_env()
    _seed_health_import(env.journal)

    response = env.client.get("/app/body/api/status")

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


def test_status_page_renders_import_and_dedupe_sections(body_env):
    env = body_env()
    _seed_health_import(env.journal)

    response = env.client.get("/app/body/")

    assert response.status_code == 200
    html = response.get_data(as_text=True)
    assert "20260703_120000" in html
    assert "By type" in html
    assert "Dedupe by type" in html
    assert "HKQuantityTypeIdentifierBloodGlucose" in html
    assert "apple_health" in html


def test_day_api_returns_summary_and_factual_glucose_stats(body_env):
    env = body_env()
    _seed_health_import(env.journal)

    response = env.client.get("/app/body/api/day/20260703")

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


def test_day_page_renders_summary_and_glucose_facts_only(body_env):
    env = body_env()
    _seed_health_import(env.journal)

    response = env.client.get("/app/body/20260703")

    assert response.status_code == 200
    html = response.get_data(as_text=True)
    assert "Day summary" in html
    assert "HKQuantityTypeIdentifierBloodGlucose: 2" in html
    assert "What was glucose doing?" in html
    assert "avg" in html
    assert "120" in html
    lowered = html.lower()
    assert "normal glucose" not in lowered
    assert "high glucose" not in lowered
    assert "low glucose" not in lowered


def test_day_page_lede_renders_once_in_hero_card(body_env):
    env = body_env()
    _seed_health_import(env.journal)

    lede = env.client.get("/app/body/api/day/20260703").get_json()["lede"]
    html = env.client.get("/app/body/20260703").get_data(as_text=True)

    # The lede lives in the "What your body added to the day" hero card
    # only — the page header carries just the date context.
    assert lede
    assert html.count(lede) == 1
    assert "What your body added to the day" in html


def test_body_workspace_template_avoids_surveillance_verbs():
    body_root = Path(body_routes.__file__).resolve().parent
    banned = {"capture", "watch", "record", "monitor", "track", "collect"}
    checked = [body_root / "workspace.html"]

    for path in checked:
        source = path.read_text(encoding="utf-8").lower()
        found = {word for word in banned if word in source}
        assert found == set(), f"{path.name} contains banned copy terms: {found}"


def test_body_call_module_uses_convey_http_only():
    body_root = Path(body_routes.__file__).resolve().parent
    source = (body_root / "call.py").read_text(encoding="utf-8")

    assert "solstone.think.convey_client" in source
    assert '@app.command("window")' in source
    assert "/app/body/api/window" in source
    assert "from pathlib" not in source
    assert "import os" not in source
    assert "sqlite3" not in source
    assert "open(" not in source


def test_read_routes_create_nothing_in_empty_journal(body_env):
    env = body_env()
    imports_root = env.journal / "imports"
    assert not imports_root.exists()

    assert env.client.get("/app/body/api/status").status_code == 200
    assert env.client.get("/app/body/api/day/20260703").status_code == 200
    assert env.client.get("/app/body/api/stats/2026-07").status_code == 200
    recent = env.client.get("/app/body/api/recent?before=20260703")
    assert recent.status_code == 200
    assert recent.get_json() == {"days": [], "has_more": False, "html": ""}
    assert env.client.get("/app/body/").status_code == 200
    assert env.client.get("/app/body/20260703").status_code == 200

    assert not imports_root.exists()
    assert not (imports_root / "health-dedupe.sqlite").exists()


def test_non_health_import_manifests_are_excluded(body_env):
    env = body_env()
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

    response = env.client.get("/app/body/api/status")

    assert response.status_code == 200
    import_ids = [item["import_id"] for item in response.get_json()["imports"]]
    assert import_ids == ["20260703_120000"]


def test_month_stats_api_returns_day_counts(body_env):
    env = body_env()
    _seed_health_import(env.journal)

    response = env.client.get("/app/body/api/stats/2026-07")

    assert response.status_code == 200
    assert response.get_json() == {"20260703": 3, "20260704": 1}


def test_day_api_rejects_invalid_day(body_env):
    env = body_env()

    assert env.client.get("/app/body/api/day/not-a-day").status_code == 400
    assert env.client.get("/app/body/api/day/2026-07-03").status_code == 400
    assert env.client.get("/app/body/api/day/20261399").status_code == 400


def test_day_api_counts_overlapping_bundles_once(body_env):
    env = body_env()
    _seed_health_import(env.journal)

    first_shard = (
        env.journal / "imports" / "20260703_120000" / "normalized" / "2026-07.jsonl"
    )
    second_shard = (
        env.journal / "imports" / "20260704_090000" / "normalized" / "2026-07.jsonl"
    )
    second_shard.parent.mkdir(parents=True)
    second_shard.write_text(first_shard.read_text(encoding="utf-8"), encoding="utf-8")
    _write_json(
        env.journal / "imports" / "20260704_090000" / "manifest.json",
        {
            "import_id": "20260704_090000",
            "source_type": "apple_health",
            "source_hash": "sha256:full-backfill",
            "entry_count": 4,
            "days_affected": ["20260703", "20260704"],
            "files_created": [],
            "imported_at": "2026-07-04T09:00:00",
            "imported_via": "test",
        },
    )

    response = env.client.get("/app/body/api/day/20260703")

    assert response.status_code == 200
    payload = response.get_json()
    assert payload["glucose"]["count"] == 2
    assert payload["entry_total"] == 3


def test_status_api_counts_latest_sources_from_overlapping_bundles_once(body_env):
    env = body_env()
    _seed_health_import(env.journal)

    first_shard = (
        env.journal / "imports" / "20260703_120000" / "normalized" / "2026-07.jsonl"
    )
    second_shard = (
        env.journal / "imports" / "20260704_090000" / "normalized" / "2026-07.jsonl"
    )
    second_shard.parent.mkdir(parents=True)
    second_shard.write_text(first_shard.read_text(encoding="utf-8"), encoding="utf-8")
    _write_json(
        env.journal / "imports" / "20260704_090000" / "manifest.json",
        {
            "import_id": "20260704_090000",
            "source_type": "apple_health",
            "source_hash": "sha256:full-backfill",
            "entry_count": 4,
            "days_affected": ["20260703", "20260704"],
            "files_created": [],
            "imported_at": "2026-07-04T09:00:00",
            "imported_via": "test",
        },
    )

    response = env.client.get("/app/body/api/status")

    assert response.status_code == 200
    status = response.get_json()
    assert status["normalized"]["by_source"] == {
        "Synthetic Stelo": 2,
        "Synthetic Watch": 2,
    }


def test_status_api_cache_invalidates_when_dedupe_db_changes(body_env):
    env = body_env()
    _seed_health_import(env.journal)

    first = env.client.get("/app/body/api/status")
    assert first.status_code == 200
    assert first.get_json()["dedupe"]["total"] == 4

    db_path = env.journal / "imports" / "health-dedupe.sqlite"
    with sqlite3.connect(db_path) as conn:
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
                "apple-health:glucose:cache-invalidates",
                "apple_health",
                "HKQuantityTypeIdentifierBloodGlucose",
                "2026-07-04T07:30:00-06:00",
                "2026-07-04T07:31:00-06:00",
                "20260703_120000",
                "20260703_120000",
                "imports/20260703_120000/normalized/2026-07.jsonl#L5",
                "2026-07-04T01:00:00Z",
                "2026-07-04T01:00:00Z",
            ),
        )

    second = env.client.get("/app/body/api/status")

    assert second.status_code == 200
    payload = second.get_json()
    assert payload["dedupe"]["total"] == 5
    assert payload["normalized"]["total"] == 5


# --- Day view: sleep --------------------------------------------------------


SLEEP_TYPE = "HKCategoryTypeIdentifierSleepAnalysis"
STEP_TYPE = "HKQuantityTypeIdentifierStepCount"
GLUCOSE_TYPE = "HKQuantityTypeIdentifierBloodGlucose"


def _seed_cross_midnight_sleep(journal: Path) -> None:
    rows = [
        _row(
            SLEEP_TYPE,
            "2026-06-30T22:58:00-06:00",
            "2026-07-01T02:00:00-06:00",
            value="HKCategoryValueSleepAnalysisAsleepCore",
            source="Synthetic Ring",
        ),
        _row(
            SLEEP_TYPE,
            "2026-07-01T02:00:00-06:00",
            "2026-07-01T07:08:00-06:00",
            value="HKCategoryValueSleepAnalysisAsleepDeep",
            source="Synthetic Ring",
        ),
        _row(
            SLEEP_TYPE,
            "2026-06-30T23:30:00-06:00",
            "2026-07-01T06:30:00-06:00",
            value="HKCategoryValueSleepAnalysisAsleepUnspecified",
            source="Synthetic Wrist",
        ),
        _row(
            SLEEP_TYPE,
            "2026-07-01T14:00:00-06:00",
            "2026-07-01T14:45:00-06:00",
            value="HKCategoryValueSleepAnalysisAsleepCore",
            source="Synthetic Ring",
        ),
    ]
    _seed_import(journal, "20260801_000000", rows)


def test_day_api_sleep_session_crosses_midnight_and_month_boundary(body_env):
    env = body_env()
    _seed_cross_midnight_sleep(env.journal)

    response = env.client.get("/app/body/api/day/20260701")

    assert response.status_code == 200
    sleep = response.get_json()["sleep"]
    assert sleep is not None
    # The session ending that morning starts the previous evening — in the
    # prior month's shard.
    assert sleep["window"] == "10:58 PM – 7:08 AM"
    assert sleep["duration"] == "8h 10m"
    # Longest-coverage source is primary; the other is named, never summed.
    assert sleep["source"] == "Synthetic Ring"
    assert sleep["other_sources"] == ["Synthetic Wrist"]
    # A session fully inside the day lists as a nap.
    assert sleep["naps"] == [{"window": "2:00 PM – 2:45 PM", "duration": "45m"}]
    kinds = [segment["kind"] for segment in sleep["bar"]["segments"]]
    assert kinds == ["main", "nap"]


def test_day_api_sleep_not_attributed_to_the_night_start_day(body_env):
    env = body_env()
    _seed_cross_midnight_sleep(env.journal)

    response = env.client.get("/app/body/api/day/20260630")

    assert response.status_code == 200
    # The night that starts on the 30th ends on July 1 — it belongs to the
    # July 1 card, not this one.
    assert response.get_json()["sleep"] is None


def test_day_api_bedtime_fragment_attributes_only_to_next_days_night(body_env):
    env = body_env()
    rows = [
        # The night that ends this day's morning (starts the prior evening).
        _row(
            SLEEP_TYPE,
            "2023-08-14T23:02:00-06:00",
            "2023-08-15T06:35:00-06:00",
            value="HKCategoryValueSleepAnalysisInBed",
            source="Synthetic Phone",
        ),
        # Bedtime fragment before midnight — the first slice of the night
        # that ends the NEXT morning, day-attributed by its start time.
        _row(
            SLEEP_TYPE,
            "2023-08-15T23:17:00-06:00",
            "2023-08-15T23:58:00-06:00",
            value="HKCategoryValueSleepAnalysisInBed",
            source="Synthetic Phone",
        ),
        # The rest of that night, within the merge gap, on the next day.
        _row(
            SLEEP_TYPE,
            "2023-08-16T00:20:00-06:00",
            "2023-08-16T06:35:00-06:00",
            value="HKCategoryValueSleepAnalysisInBed",
            source="Synthetic Phone",
        ),
    ]
    _seed_import(env.journal, "20260910_110000", rows)

    day = env.client.get("/app/body/api/day/20230815").get_json()["sleep"]
    # Main night unchanged; the 11:17 PM fragment merges into the following
    # night instead of misreading as this day's nap.
    assert day["window"] == "11:02 PM – 6:35 AM"
    assert day["naps"] == []
    assert [segment["kind"] for segment in day["bar"]["segments"]] == ["main"]

    next_day = env.client.get("/app/body/api/day/20230816").get_json()["sleep"]
    # The fragment appears exactly once: as the start of the next day's main.
    assert next_day["window"] == "11:17 PM – 6:35 AM"
    assert next_day["naps"] == []


def test_day_api_bedtime_fragment_merges_across_month_boundary(body_env):
    env = body_env()
    rows = [
        _row(
            SLEEP_TYPE,
            "2026-08-30T23:00:00-06:00",
            "2026-08-31T06:30:00-06:00",
            value="HKCategoryValueSleepAnalysisInBed",
            source="Synthetic Phone",
        ),
        # Month-boundary bedtime fragment: its continuation lives in the
        # NEXT month's shard, which the day view must also read.
        _row(
            SLEEP_TYPE,
            "2026-08-31T23:17:00-06:00",
            "2026-08-31T23:58:00-06:00",
            value="HKCategoryValueSleepAnalysisInBed",
            source="Synthetic Phone",
        ),
        _row(
            SLEEP_TYPE,
            "2026-09-01T00:15:00-06:00",
            "2026-09-01T06:40:00-06:00",
            value="HKCategoryValueSleepAnalysisInBed",
            source="Synthetic Phone",
        ),
    ]
    _seed_import(env.journal, "20260910_120000", rows)

    day = env.client.get("/app/body/api/day/20260831").get_json()["sleep"]
    assert day["window"] == "11:00 PM – 6:30 AM"
    assert day["naps"] == []

    next_day = env.client.get("/app/body/api/day/20260901").get_json()["sleep"]
    assert next_day["window"] == "11:17 PM – 6:40 AM"
    assert next_day["naps"] == []


def test_recent_rail_sleep_matches_day_page_sleep(body_env):
    env = body_env()
    _seed_cross_midnight_sleep(env.journal)

    archive = env.client.get("/app/body/api/status").get_json()["archive"]
    day = env.client.get("/app/body/api/day/20260701").get_json()

    # One canonical sleep number: the recent-days rail and the day page
    # answer with the same merged cross-midnight session.
    rail = {item["day"]: item for item in archive["recent_days"]}
    assert day["sleep"]["duration"] == "8h 10m"
    assert rail["20260701"]["sleep_duration"] == day["sleep"]["duration"]


# --- Day view: activity / steps ---------------------------------------------


def test_day_api_steps_total_only_with_single_source(body_env):
    env = body_env()
    rows = [
        _row(
            STEP_TYPE,
            "2026-07-10T08:00:00-06:00",
            value="5000",
            unit="count",
            source="Synthetic Phone",
        ),
        _row(
            STEP_TYPE,
            "2026-07-10T12:00:00-06:00",
            value="1412",
            unit="count",
            source="Synthetic Phone",
        ),
        _row(
            STEP_TYPE,
            "2026-07-11T08:00:00-06:00",
            value="3000",
            unit="count",
            source="Synthetic Phone",
        ),
        _row(
            STEP_TYPE,
            "2026-07-11T09:00:00-06:00",
            value="2000",
            unit="count",
            source="Synthetic Wrist",
        ),
    ]
    _seed_import(env.journal, "20260802_000000", rows)

    single = env.client.get("/app/body/api/day/20260710").get_json()
    steps = single["activity"]["steps"]
    assert steps["mode"] == "total"
    assert steps["total"] == 6412
    assert steps["total_label"] == "6,412"
    assert steps["source"] == "Synthetic Phone"

    multi = env.client.get("/app/body/api/day/20260711").get_json()
    steps = multi["activity"]["steps"]
    assert steps["mode"] == "samples"
    assert steps["samples"] == 2
    assert "total" not in steps


# --- Window API -------------------------------------------------------------


def test_window_api_returns_factual_window_aggregates(body_env):
    env = body_env()
    rows = [
        _row(
            GLUCOSE_TYPE,
            "2026-07-10T10:00:00-06:00",
            value="88",
            unit="mg/dL",
            source="Synthetic Stelo",
        ),
        _row(
            GLUCOSE_TYPE,
            "2026-07-10T10:30:00-06:00",
            value="96",
            unit="mg/dL",
            source="Synthetic Stelo",
        ),
        _row(
            "HKQuantityTypeIdentifierHeartRate",
            "2026-07-10T10:05:00-06:00",
            value="62",
            unit="count/min",
            source="Synthetic Watch",
        ),
        _row(
            "HKQuantityTypeIdentifierHeartRate",
            "2026-07-10T10:45:00-06:00",
            value="89",
            unit="count/min",
            source="Synthetic Watch",
        ),
        _row(
            STEP_TYPE,
            "2026-07-10T10:15:00-06:00",
            value="200",
            unit="count",
            source="Synthetic Phone",
        ),
        _row(
            STEP_TYPE,
            "2026-07-10T10:45:00-06:00",
            value="212",
            unit="count",
            source="Synthetic Phone",
        ),
        _row(
            "HKWorkoutActivityTypeWalking",
            "2026-07-10T10:20:00-06:00",
            "2026-07-10T11:20:00-06:00",
            source="Synthetic Watch",
            kind="workout",
            metadata={
                "duration": "60",
                "durationUnit": "min",
                "totalDistance": "4.2",
                "totalDistanceUnit": "mi",
                "totalDistanceType": ("HKQuantityTypeIdentifierDistanceWalkingRunning"),
                "totalEnergyBurned": "198.4",
                "totalEnergyBurnedUnit": "Cal",
                "totalEnergyBurnedType": ("HKQuantityTypeIdentifierActiveEnergyBurned"),
            },
        ),
    ]
    _seed_import(env.journal, "20260808_000000", rows)

    response = env.client.get(
        "/app/body/api/window"
        "?from=2026-07-10T10:00:00-06:00&to=2026-07-10T11:00:00-06:00"
    )

    assert response.status_code == 200
    payload = response.get_json()
    assert payload["has_data"] is True
    assert payload["entry_total"] == 7
    assert payload["heart_rate"]["count"] == 2
    assert payload["heart_rate"]["min"] == 62.0
    assert payload["heart_rate"]["max"] == 89.0
    assert payload["glucose"]["delta_label"] == "88 → 96 mg/dL"
    assert payload["glucose"]["range_label"] == "88–96 mg/dL"
    assert [reading["value"] for reading in payload["glucose"]["readings"]] == [
        88.0,
        96.0,
    ]
    assert payload["steps"]["mode"] == "total"
    assert payload["steps"]["total"] == 412
    assert payload["workouts"][0]["name"] == "Walking"
    assert payload["workouts"][0]["overlap_label"] == "40m"
    assert payload["workouts"][0]["distance"]["label"] == "4.2 mi"
    assert payload["workouts"][0]["energy"]["label"] == "198 Cal"
    assert payload["workouts"][0]["metric_labels"] == ["4.2 mi", "198 Cal"]
    assert payload["workouts"][0]["metrics_label"] == "4.2 mi · 198 Cal"
    assert {event["kind"] for event in payload["events"]} == {"workout"}
    assert payload["events"][0]["metrics_label"] == "4.2 mi · 198 Cal"
    assert payload["sources"]["names"] == [
        "Synthetic Phone",
        "Synthetic Stelo",
        "Synthetic Watch",
    ]
    assert [family["name"] for family in payload["families"]] == [
        "Glucose",
        "Activity",
        "Heart",
    ]
    assert len(payload["hourly"]) == 1
    hour = payload["hourly"][0]
    assert hour["range_label"] == "10:00 AM – 11:00 AM"
    assert hour["entry_total"] == 7
    assert hour["glucose"]["range_label"] == "88–96 mg/dL"
    assert hour["heart_rate"]["label"] == "62–89 bpm"
    assert hour["events"][0]["metrics_label"] == "4.2 mi · 198 Cal"
    assert hour["steps"]["label"] == "412 steps"
    assert [event["label"] for event in hour["events"]] == ["Walking"]
    assert hour["summary"] == [
        "Walking",
        "Glucose 88–96 mg/dL",
        "HR 62–89 bpm",
        "412 steps",
    ]


def test_window_api_reads_cross_month_and_dedupes_overlapping_bundles(body_env):
    env = body_env()
    rows = [
        _row(
            GLUCOSE_TYPE,
            "2026-06-30T23:55:00-06:00",
            value="100",
            unit="mg/dL",
            source="Synthetic Stelo",
        ),
        _row(
            GLUCOSE_TYPE,
            "2026-07-01T00:05:00-06:00",
            value="104",
            unit="mg/dL",
            source="Synthetic Stelo",
        ),
    ]
    _seed_import(env.journal, "20260809_000000", rows)
    first_june = (
        env.journal / "imports" / "20260809_000000" / "normalized" / "2026-06.jsonl"
    )
    first_july = (
        env.journal / "imports" / "20260809_000000" / "normalized" / "2026-07.jsonl"
    )
    second_root = env.journal / "imports" / "20260809_010000" / "normalized"
    second_root.mkdir(parents=True)
    (second_root / "2026-06.jsonl").write_text(
        first_june.read_text(encoding="utf-8"), encoding="utf-8"
    )
    (second_root / "2026-07.jsonl").write_text(
        first_july.read_text(encoding="utf-8"), encoding="utf-8"
    )
    _write_json(
        env.journal / "imports" / "20260809_010000" / "manifest.json",
        {
            "import_id": "20260809_010000",
            "source_type": "apple_health",
            "source_hash": "sha256:overlap",
            "entry_count": 2,
            "days_affected": ["20260630", "20260701"],
            "files_created": [],
            "imported_at": "2026-08-09T01:00:00",
            "imported_via": "test",
        },
    )

    response = env.client.get(
        "/app/body/api/window"
        "?from=2026-06-30T23:45:00-06:00&to=2026-07-01T00:15:00-06:00"
    )

    assert response.status_code == 200
    payload = response.get_json()
    assert payload["entry_total"] == 2
    assert payload["glucose"]["delta_label"] == "100 → 104 mg/dL"


def test_window_api_steps_do_not_total_multiple_sources(body_env):
    env = body_env()
    rows = [
        _row(
            STEP_TYPE,
            "2026-07-10T09:00:00-06:00",
            value="300",
            unit="count",
            source="Synthetic Phone",
        ),
        _row(
            STEP_TYPE,
            "2026-07-10T09:05:00-06:00",
            value="200",
            unit="count",
            source="Synthetic Watch",
        ),
    ]
    _seed_import(env.journal, "20260810_000000", rows)

    response = env.client.get(
        "/app/body/api/window"
        "?from=2026-07-10T09:00:00-06:00&to=2026-07-10T10:00:00-06:00"
    )

    assert response.status_code == 200
    steps = response.get_json()["steps"]
    assert steps["mode"] == "samples"
    assert steps["samples"] == 2
    assert "total" not in steps


def test_window_api_preserves_full_day_as_hourly_context(body_env):
    env = body_env()
    rows = [
        _row(
            GLUCOSE_TYPE,
            "2026-07-10T00:05:00-06:00",
            value="88",
            unit="mg/dL",
            source="Synthetic Stelo",
        ),
        _row(
            GLUCOSE_TYPE,
            "2026-07-10T13:15:00-06:00",
            value="101",
            unit="mg/dL",
            source="Synthetic Stelo",
        ),
        _row(
            "HKQuantityTypeIdentifierHeartRate",
            "2026-07-10T13:30:00-06:00",
            value="82",
            unit="count/min",
            source="Synthetic Watch",
        ),
    ]
    _seed_import(env.journal, "20260810_010000", rows)

    response = env.client.get(
        "/app/body/api/window"
        "?from=2026-07-10T00:00:00-06:00&to=2026-07-11T00:00:00-06:00"
    )

    assert response.status_code == 200
    hourly = response.get_json()["hourly"]
    assert len(hourly) == 24
    assert hourly[0]["range_label"] == "12:00 AM – 1:00 AM"
    assert hourly[0]["glucose"]["range_label"] == "88 mg/dL"
    assert hourly[12]["has_data"] is False
    assert hourly[13]["range_label"] == "1:00 PM – 2:00 PM"
    assert hourly[13]["summary"] == [
        "Glucose 101 mg/dL",
        "HR 82 bpm",
    ]


def test_window_api_includes_sleep_events_from_previous_day(body_env):
    env = body_env()
    _seed_cross_midnight_sleep(env.journal)

    response = env.client.get(
        "/app/body/api/window"
        "?from=2026-07-01T00:00:00-06:00&to=2026-07-02T00:00:00-06:00"
    )

    assert response.status_code == 200
    events = response.get_json()["events"]
    sleep_events = [event for event in events if event["kind"] == "sleep"]
    assert sleep_events
    assert len(sleep_events) == 2
    assert [event["source"] for event in sleep_events] == [
        "Synthetic Ring",
        "Synthetic Ring",
    ]
    assert sleep_events[0]["start"] == "2026-06-30T22:58:00-06:00"
    assert sleep_events[0]["end"] == "2026-07-01T07:08:00-06:00"
    assert sleep_events[0]["overlap_label"] == "7h 08m"
    assert sleep_events[1]["start"] == "2026-07-01T14:00:00-06:00"
    assert sleep_events[1]["overlap_label"] == "45m"


def test_window_api_rejects_invalid_and_too_large_spans(body_env):
    env = body_env()

    missing = env.client.get("/app/body/api/window")
    assert missing.status_code == 400
    assert missing.get_json()["reason_code"] == "invalid_request_value"

    reversed_span = env.client.get(
        "/app/body/api/window"
        "?from=2026-07-10T10:00:00-06:00&to=2026-07-10T09:00:00-06:00"
    )
    assert reversed_span.status_code == 400

    too_large = env.client.get(
        "/app/body/api/window"
        "?from=2026-07-01T00:00:00-06:00&to=2026-07-09T00:00:00-06:00"
    )
    assert too_large.status_code == 400
    assert "7 days or less" in too_large.get_json()["detail"]


def test_window_api_empty_window_is_read_only(body_env):
    env = body_env()
    imports_root = env.journal / "imports"
    assert not imports_root.exists()

    response = env.client.get(
        "/app/body/api/window"
        "?from=2026-07-10T09:00:00-06:00&to=2026-07-10T10:00:00-06:00"
    )

    assert response.status_code == 200
    payload = response.get_json()
    assert payload["has_data"] is False
    assert payload["entry_total"] == 0
    assert payload["families"] == []
    assert payload["events"] == []
    assert not imports_root.exists()


# --- Day view: glucose curve -------------------------------------------------


def test_day_api_glucose_curve_points_and_sparse_dots(body_env):
    env = body_env()
    _seed_health_import(env.journal)

    payload = env.client.get("/app/body/api/day/20260703").get_json()

    series = payload["glucose_series"]
    assert len(series) == 1
    curve = series[0]
    assert curve["unit"] == "mg/dL"
    assert curve["count"] == 2
    # Server-computed [minute-of-day, value] pairs in the record's own offset.
    assert curve["points"] == [[480, 100.0], [720, 140.0]]
    # Readings four hours apart do not get a line drawn across the gap.
    assert curve["svg"]["paths"] == []
    assert len(curve["svg"]["dots"]) == 2


def test_day_api_glucose_curve_path_for_contiguous_readings(body_env):
    env = body_env()
    rows = [
        _row(
            GLUCOSE_TYPE,
            "2026-07-12T08:00:00-06:00",
            value="100",
            unit="mg/dL",
            source="Synthetic Stelo",
        ),
        _row(
            GLUCOSE_TYPE,
            "2026-07-12T08:05:00-06:00",
            value="105",
            unit="mg/dL",
            source="Synthetic Stelo",
        ),
        _row(
            GLUCOSE_TYPE,
            "2026-07-12T08:10:00-06:00",
            value="102",
            unit="mg/dL",
            source="Synthetic Stelo",
        ),
    ]
    _seed_import(env.journal, "20260803_000000", rows)

    payload = env.client.get("/app/body/api/day/20260712").get_json()

    curve = payload["glucose_series"][0]
    assert curve["points"][0] == [480, 100.0]
    assert len(curve["svg"]["paths"]) == 1
    assert curve["svg"]["paths"][0].startswith("M480 ")
    assert curve["svg"]["dots"] == []


# --- Day view: empty days ----------------------------------------------------


def test_day_api_empty_day_links_nearest_days_with_data(body_env):
    env = body_env()
    _seed_health_import(env.journal)

    before = env.client.get("/app/body/api/day/20260601").get_json()
    assert before["has_data"] is False
    assert before["entry_total"] == 0
    assert before["nearest"]["prev"] is None
    assert before["nearest"]["next"] == "20260703"

    after = env.client.get("/app/body/api/day/20260710").get_json()
    assert after["nearest"]["prev"] == "20260704"
    assert after["nearest"]["next"] is None


def test_day_page_empty_day_renders_honest_empty_state(body_env):
    env = body_env()
    _seed_health_import(env.journal)

    response = env.client.get("/app/body/20260601")

    assert response.status_code == 200
    html = response.get_data(as_text=True)
    assert "No body data present for this day." in html
    assert "/app/body/20260703" in html


# --- Day view: prompts hook --------------------------------------------------


def test_day_page_renders_ask_prompts_with_chat_hook(body_env):
    env = body_env()
    _seed_health_import(env.journal)

    payload = env.client.get("/app/body/api/day/20260703").get_json()
    assert len(payload["prompts"]) == 3
    assert any("glucose peak" in prompt for prompt in payload["prompts"])

    html = env.client.get("/app/body/20260703").get_data(as_text=True)
    assert "Ask Solstone about this day" in html
    assert "data-prompt=" in html
    assert "window.fillChat" in html


# --- Archive: day grid, families, rail ----------------------------------------


def _grid_cells(block: dict) -> list[dict]:
    return [cell for week in block["weeks"] for cell in week if cell is not None]


def test_status_api_day_grid_groups_years_and_log_scales(body_env):
    env = body_env()
    december = [
        _row(
            GLUCOSE_TYPE,
            f"2025-12-30T0{i}:00:00-06:00",
            value="100",
            unit="mg/dL",
            source="Synthetic Stelo",
        )
        for i in range(2)
    ]
    july = [
        _row(
            GLUCOSE_TYPE,
            f"2026-07-03T0{i}:00:00-06:00",
            value="100",
            unit="mg/dL",
            source="Synthetic Stelo",
        )
        for i in range(5)
    ]
    _seed_import(env.journal, "20260804_000000", december + july)

    archive = env.client.get("/app/body/api/status").get_json()["archive"]

    grid = archive["day_grid"]
    assert [block["year"] for block in grid] == [2025, 2026]

    # Weeks are Monday-first columns of exactly seven weekday slots; the
    # first day with data sits at its own weekday row after None padding.
    for block in grid:
        assert all(len(week) == 7 for week in block["weeks"])
    assert grid[0]["weeks"][0][date(2025, 12, 30).weekday()]["day"] == "20251230"

    # The span runs the first through the last day with data, continuously
    # across the year-block boundary.
    first_cells = _grid_cells(grid[0])
    second_cells = _grid_cells(grid[1])
    assert first_cells[0]["day"] == "20251230"
    assert first_cells[-1]["day"] == "20251231"
    assert second_cells[0]["day"] == "20260101"
    assert second_cells[-1]["day"] == "20260703"
    assert len(second_cells) == (date(2026, 7, 3) - date(2026, 1, 1)).days + 1

    by_day = {cell["day"]: cell for cell in first_cells + second_cells}
    # Log scaling: monotone in count, busiest day pinned to 1.0, and above
    # what a linear ramp (2/5 = 0.4) would give.
    assert by_day["20260703"]["intensity"] == 1.0
    assert 0.0 < by_day["20251230"]["intensity"] < by_day["20260703"]["intensity"]
    assert by_day["20251230"]["intensity"] == pytest.approx(
        math.log1p(2) / math.log1p(5), abs=1e-3
    )
    # Days without data stay pale: zero count, zero intensity.
    assert by_day["20260102"]["count"] == 0
    assert by_day["20260102"]["intensity"] == 0.0
    assert by_day["20260703"]["title"] == "Jul 3, 2026 · 5 entries"
    assert by_day["20260102"]["title"] == "Jan 2, 2026 · no entries"


def test_status_page_day_grid_links_only_days_with_data(body_env):
    env = body_env()
    rows = [
        _row(
            GLUCOSE_TYPE,
            "2026-03-15T08:00:00-06:00",
            value="100",
            unit="mg/dL",
            source="Synthetic Stelo",
        ),
        _row(
            GLUCOSE_TYPE,
            "2026-07-03T08:00:00-06:00",
            value="100",
            unit="mg/dL",
            source="Synthetic Stelo",
        ),
    ]
    _seed_import(env.journal, "20260807_000000", rows)

    html = env.client.get("/app/body/").get_data(as_text=True)

    assert 'href="/app/body/20260315"' in html
    assert "Mar 15, 2026 · 1 entry" in html
    # A day inside the span with no entries renders pale and unlinked.
    assert 'href="/app/body/20260401"' not in html
    assert "Apr 1, 2026 · no entries" in html


def test_dedupe_stats_include_per_type_time_ranges(body_env):
    env = body_env()
    rows = [
        _row(
            SLEEP_TYPE,
            "2026-03-10T23:00:00-06:00",
            "2026-03-11T06:00:00-06:00",
            value="HKCategoryValueSleepAnalysisAsleepCore",
            source="Synthetic Ring",
        ),
        _row(
            SLEEP_TYPE,
            "2026-07-01T23:00:00-06:00",
            "2026-07-02T06:00:00-06:00",
            value="HKCategoryValueSleepAnalysisAsleepCore",
            source="Synthetic Ring",
        ),
        _row(
            GLUCOSE_TYPE,
            "2026-07-03T08:00:00-06:00",
            value="100",
            unit="mg/dL",
            source="Synthetic Stelo",
        ),
    ]
    _seed_import(env.journal, "20260805_000000", rows)

    stats = body_routes._read_health_dedupe_stats(env.journal)

    assert stats["type_ranges"][SLEEP_TYPE] == {
        "first": "2026-03-10T23:00:00-06:00",
        "last": "2026-07-01T23:00:00-06:00",
    }
    assert stats["type_ranges"][GLUCOSE_TYPE] == {
        "first": "2026-07-03T08:00:00-06:00",
        "last": "2026-07-03T08:00:00-06:00",
    }


def test_status_api_coverage_families_fold_type_ranges(body_env):
    env = body_env()
    rows = [
        _row(
            SLEEP_TYPE,
            "2026-03-10T23:00:00-06:00",
            "2026-03-11T06:00:00-06:00",
            value="HKCategoryValueSleepAnalysisAsleepCore",
            source="Synthetic Ring",
        ),
        _row(
            SLEEP_TYPE,
            "2026-07-01T23:00:00-06:00",
            "2026-07-02T06:00:00-06:00",
            value="HKCategoryValueSleepAnalysisAsleepCore",
            source="Synthetic Ring",
        ),
        _row(
            GLUCOSE_TYPE,
            "2026-07-03T08:00:00-06:00",
            value="100",
            unit="mg/dL",
            source="Synthetic Stelo",
        ),
    ]
    _seed_import(env.journal, "20260806_000000", rows)

    archive = env.client.get("/app/body/api/status").get_json()["archive"]

    families = {family["name"]: family for family in archive["families"]}
    assert families["Sleep"]["range_label"] == "Mar 2026 – Jul 2026"
    assert families["Glucose"]["range_label"] == "Jul 2026"
    # Friendly names, never raw identifiers, on the owner-facing chips.
    assert "Sleep" in families["Sleep"]["types_label"]
    assert "HK" not in families["Glucose"]["types_label"]


def test_status_api_recent_day_rail_has_per_day_facts(body_env):
    env = body_env()
    _seed_health_import(env.journal)

    archive = env.client.get("/app/body/api/status").get_json()["archive"]

    rail = archive["recent_days"]
    assert [item["day"] for item in rail] == ["20260704", "20260703"]
    # Both archive days fit in the initial rail — nothing older remains.
    assert archive["recent_days_has_more"] is False
    glucose_day = rail[1]
    assert glucose_day["glucose_label"] == "100–140 mg/dL · avg 120"
    assert glucose_day["workout_count"] == 0
    assert glucose_day["source_count"] == 2
    workout_day = rail[0]
    assert workout_day["workout_count"] == 1
    assert workout_day["sleep_duration"] is None


def test_status_api_recent_day_rail_caps_at_fourteen_days(body_env):
    env = body_env()
    rows = [
        _row(
            GLUCOSE_TYPE,
            f"2026-07-{day:02d}T08:00:00-06:00",
            value="100",
            unit="mg/dL",
            source="Synthetic Stelo",
        )
        for day in range(1, 19)
    ]
    _seed_import(env.journal, "20260801_000000", rows)

    archive = env.client.get("/app/body/api/status").get_json()["archive"]
    rail = archive["recent_days"]

    # 18 days with data collapse to the newest 14, newest first; the
    # archive flags that older days remain for the carousel to page in.
    assert len(rail) == 14
    assert [item["day"] for item in rail] == [
        f"202607{day:02d}" for day in range(18, 4, -1)
    ]
    assert archive["recent_days_has_more"] is True


def test_overview_recent_days_render_as_snap_carousel(body_env):
    env = body_env()
    _seed_health_import(env.journal)

    html = env.client.get("/app/body/").get_data(as_text=True)

    # The rail is a horizontal scroll-snap carousel with fixed-width cards
    # and a thin scrollbar; the page itself never scrolls sideways.
    assert 'class="body-recent-carousel"' in html
    assert "scroll-snap-type: x mandatory" in html
    assert "overflow-x: auto" in html
    assert "scroll-snap-align: start" in html
    assert "scrollbar-width: thin" in html

    # Paging buttons: newest-first order puts newer days to the left, so
    # the labels follow content, not direction.
    assert 'aria-label="Newer days"' in html
    assert 'aria-label="Earlier days"' in html

    # Buttons disable at the respective end of the scroll range and the
    # control cluster hides entirely when every card fits without overflow.
    assert "backBtn.disabled" in html
    assert "fwdBtn.disabled" in html
    assert "controls.hidden = true" in html


def _seed_july_days(journal: Path, last_day: int) -> None:
    """One glucose entry per day for 2026-07-01 … 2026-07-``last_day``."""
    rows = [
        _row(
            GLUCOSE_TYPE,
            f"2026-07-{day:02d}T08:00:00-06:00",
            value="100",
            unit="mg/dL",
            source="Synthetic Stelo",
        )
        for day in range(1, last_day + 1)
    ]
    _seed_import(journal, "20260801_000000", rows)


def test_recent_api_pages_strictly_older_newest_first(body_env):
    env = body_env()
    _seed_july_days(env.journal, 18)

    response = env.client.get("/app/body/api/recent?before=20260715")

    assert response.status_code == 200
    batch = response.get_json()
    days = [item["day"] for item in batch["days"]]
    # Strictly older than the cursor, newest first, default batch of 14.
    assert days == [f"202607{day:02d}" for day in range(14, 0, -1)]
    assert all(day < "20260715" for day in days)
    # The batch reaches the archive's earliest day, so nothing remains.
    assert batch["has_more"] is False
    # Same per-card payload the overview's rail carries.
    assert batch["days"][0]["glucose_label"] == "100–100 mg/dL · avg 100"
    assert batch["days"][0]["source_count"] == 1


def test_recent_api_two_page_walk_covers_archive_without_duplicates(body_env):
    env = body_env()
    _seed_july_days(env.journal, 18)

    first = env.client.get("/app/body/api/recent?before=20260719").get_json()
    first_days = [item["day"] for item in first["days"]]
    assert first_days == [f"202607{day:02d}" for day in range(18, 4, -1)]
    assert first["has_more"] is True

    # The next cursor is the oldest card of the previous page.
    second = env.client.get(f"/app/body/api/recent?before={first_days[-1]}").get_json()
    second_days = [item["day"] for item in second["days"]]
    assert second_days == [f"202607{day:02d}" for day in range(4, 0, -1)]
    assert second["has_more"] is False

    assert set(first_days) & set(second_days) == set()
    assert sorted(first_days + second_days) == [
        f"202607{day:02d}" for day in range(1, 19)
    ]


def test_recent_api_limit_respected_and_capped(body_env):
    env = body_env()
    june = [
        _row(
            GLUCOSE_TYPE,
            f"2026-06-{day:02d}T08:00:00-06:00",
            value="100",
            unit="mg/dL",
            source="Synthetic Stelo",
        )
        for day in range(1, 31)
    ]
    july = [
        _row(
            GLUCOSE_TYPE,
            f"2026-07-{day:02d}T08:00:00-06:00",
            value="100",
            unit="mg/dL",
            source="Synthetic Stelo",
        )
        for day in range(1, 11)
    ]
    _seed_import(env.journal, "20260801_000000", june + july)

    small = env.client.get("/app/body/api/recent?before=20260711&limit=3").get_json()
    assert [item["day"] for item in small["days"]] == [
        "20260710",
        "20260709",
        "20260708",
    ]
    assert small["has_more"] is True

    # An oversized limit clamps to the batch cap instead of folding an
    # unbounded stretch of the archive into one response.
    large = env.client.get("/app/body/api/recent?before=20260711&limit=100").get_json()
    assert len(large["days"]) == body_routes.RECENT_BATCH_LIMIT_CAP
    assert large["days"][0]["day"] == "20260710"
    assert large["days"][-1]["day"] == "20260610"
    assert large["has_more"] is True


def test_recent_api_rejects_invalid_before_and_limit(body_env):
    env = body_env()

    assert env.client.get("/app/body/api/recent").status_code == 400
    assert env.client.get("/app/body/api/recent?before=2026-07-03").status_code == 400
    assert env.client.get("/app/body/api/recent?before=notaday1").status_code == 400
    assert env.client.get("/app/body/api/recent?before=20261399").status_code == 400
    assert (
        env.client.get("/app/body/api/recent?before=20260703&limit=abc").status_code
        == 400
    )
    assert (
        env.client.get("/app/body/api/recent?before=20260703&limit=0").status_code
        == 400
    )


def test_recent_api_skips_empty_days_across_month_boundary(body_env):
    env = body_env()
    rows = [
        _row(
            GLUCOSE_TYPE,
            f"{stamp}T08:00:00-06:00",
            value="100",
            unit="mg/dL",
            source="Synthetic Stelo",
        )
        for stamp in ("2026-06-28", "2026-06-30", "2026-07-02")
    ]
    _seed_import(env.journal, "20260801_000000", rows)

    first = env.client.get("/app/body/api/recent?before=20260704&limit=2").get_json()
    # Only days with entries appear: the gaps at 06-29 and 07-01 fold away
    # and the batch spans the June/July boundary.
    assert [item["day"] for item in first["days"]] == ["20260702", "20260630"]
    assert first["has_more"] is True

    second = env.client.get("/app/body/api/recent?before=20260630").get_json()
    assert [item["day"] for item in second["days"]] == ["20260628"]
    assert second["has_more"] is False


def test_recent_api_fragment_renders_shared_card_macro(body_env):
    env = body_env()
    _seed_health_import(env.journal)

    page = env.client.get("/app/body/").get_data(as_text=True)
    batch = env.client.get("/app/body/api/recent?before=20260704").get_json()

    assert [item["day"] for item in batch["days"]] == ["20260703"]
    assert batch["has_more"] is False
    assert 'data-day="20260703"' in batch["html"]
    assert 'href="/app/body/20260703"' in batch["html"]
    # The fragment is the same macro output the overview rendered inline,
    # so paged-in cards can never drift from the server-rendered ones.
    assert batch["html"].strip() in page

    body_root = Path(body_routes.__file__).resolve().parent
    workspace = (body_root / "workspace.html").read_text(encoding="utf-8")
    assert "{% macro body_day_card(recent) %}" in workspace
    assert "{{ body_day_card(recent) }}" in workspace
    routes_source = (body_root / "routes.py").read_text(encoding="utf-8")
    assert (
        'get_template_attribute("body/workspace.html", "body_day_card")'
        in routes_source
    )


def test_overview_carousel_pages_archive_with_guarded_fetches(body_env):
    env = body_env()
    _seed_july_days(env.journal, 18)

    html = env.client.get("/app/body/").get_data(as_text=True)

    # The initial render stays the newest 14 SSR cards and flags that
    # older days remain for the carousel to page in.
    assert html.count('data-day="') == 14
    assert 'data-has-more="true"' in html

    # Cursor-paged fetch of earlier days, triggered within ~2 card widths
    # of the right end, one request in flight at a time, deduped by day.
    assert "/app/body/api/recent?before=" in html
    assert "remaining > 2 * cardStep()" in html
    assert "if (!hasMore || fetching) return;" in html
    assert "present[card.dataset.day]" in html

    # A neutral placeholder card shows while a batch loads.
    assert "Loading earlier days" in html
    assert "body-recent-loading" in html

    # The forward control only hard-disables at the true archive end.
    assert "fwdBtn.disabled = !hasMore && carousel.scrollLeft >= maxScroll - 1;" in html
    assert "hasMore = !!batch.has_more;" in html


def test_status_page_renders_archive_sections(body_env):
    env = body_env()
    _seed_health_import(env.journal)

    html = env.client.get("/app/body/").get_data(as_text=True)

    assert "Body archive" in html
    assert "Recent body days" in html
    assert "Explore all history" in html
    assert "Coverage areas" in html
    assert "Sources represented" in html
    assert "body-day-cell" in html
    assert "months observed" in html
    # Month labels above the grid and the ramp legend under it.
    assert "body-days-months" in html
    assert "more body data" in html


def test_status_api_archive_latest_day_and_month_labels(body_env):
    env = body_env()
    december = [
        _row(
            GLUCOSE_TYPE,
            f"2025-12-30T0{i}:00:00-06:00",
            value="100",
            unit="mg/dL",
            source="Synthetic Stelo",
        )
        for i in range(2)
    ]
    july = [
        _row(
            GLUCOSE_TYPE,
            f"2026-07-03T0{i}:00:00-06:00",
            value="100",
            unit="mg/dL",
            source="Synthetic Stelo",
        )
        for i in range(5)
    ]
    _seed_import(env.journal, "20260808_000000", december + july)

    archive = env.client.get("/app/body/api/status").get_json()["archive"]

    assert archive["latest_day"] == "20260703"

    grid = archive["day_grid"]
    assert [block["year"] for block in grid] == [2025, 2026]
    # Each month labels the week column it first leads; a final month too
    # short to lead a column (July here, span ends Jul 3) gets no label.
    assert grid[0]["month_labels"] == [{"index": 0, "label": "Dec"}]
    labels = grid[1]["month_labels"]
    assert [item["label"] for item in labels] == [
        "Jan",
        "Feb",
        "Mar",
        "Apr",
        "May",
        "Jun",
    ]
    indices = [item["index"] for item in labels]
    assert labels[0] == {"index": 0, "label": "Jan"}
    assert indices == sorted(indices)
    assert len(set(indices)) == len(indices)
    assert all(0 <= index < len(grid[1]["weeks"]) for index in indices)


def test_overview_quick_entry_row_and_section_order(body_env):
    env = body_env()
    _seed_health_import(env.journal)

    html = env.client.get("/app/body/").get_data(as_text=True)

    # Quick-entry row: solid button to the latest day with data, outline
    # button opening the jump-to-date calendar. No "This week" button.
    assert "Open latest day" in html
    assert 'href="/app/body/20260704"' in html
    assert "Jump to date" in html
    assert 'id="body-jump-pop"' in html
    assert 'data-start-month="2026-07"' in html
    assert 'data-end-month="2026-07"' in html
    assert "This week" not in html

    # Latest-first order: hero, quick entry, recent days, all history,
    # coverage/sources panels, audit drawer last.
    order = [
        html.index("Body archive"),
        html.index("Open latest day"),
        html.index("Recent body days"),
        html.index("Explore all history"),
        html.index("Coverage areas"),
        html.index("Sources represented"),
        html.index("Audit"),
    ]
    assert order == sorted(order)


# --- Overview vs day-page navigation model --------------------------------------


def test_overview_is_stable_home_without_day_nav(body_env):
    env = body_env()
    _seed_health_import(env.journal)

    html = env.client.get("/app/body/").get_data(as_text=True)

    # The overview is the stable Body home: the day grid and recent-day
    # rail are the pickers, so the date-nav pill must NOT mount here.
    assert 'id="date-nav-label"' not in html


def test_day_page_mounts_day_nav_and_overview_backlink(body_env):
    env = body_env()
    _seed_health_import(env.journal)

    html = env.client.get("/app/body/20260703").get_data(as_text=True)

    assert 'id="date-nav-label"' in html
    assert "Body overview" in html


# --- Shared display normalizers ------------------------------------------------


def test_display_normalizers_scale_fraction_percents_and_relabel_units():
    # HK fraction-percent convention: 0–1 fractions with unit '%' scale by
    # 100 — blood oxygen must never render as "1.0 %".
    assert (
        health_schema.display_value(
            "HKQuantityTypeIdentifierOxygenSaturation", 0.98, "%"
        )
        == "98%"
    )
    assert (
        health_schema.display_value(
            "HKQuantityTypeIdentifierBodyFatPercentage", 0.223, "%"
        )
        == "22.3%"
    )
    assert (
        health_schema.display_value(
            "HKQuantityTypeIdentifierAppleWalkingSteadiness", 0.87, "%"
        )
        == "87%"
    )
    # Heart-family 'count/min' reads as bpm; respiratory as breaths/min.
    for record_type in (
        "HKQuantityTypeIdentifierHeartRate",
        "HKQuantityTypeIdentifierRestingHeartRate",
        "HKQuantityTypeIdentifierWalkingHeartRateAverage",
        "HKQuantityTypeIdentifierHeartRateRecoveryOneMinute",
    ):
        assert health_schema.friendly_unit_label(record_type, "count/min") == "bpm"
    assert (
        health_schema.friendly_unit_label(
            "HKQuantityTypeIdentifierRespiratoryRate", "count/min"
        )
        == "breaths/min"
    )
    # '%' stays '%'; unknown units pass through; bare counts drop the unit.
    assert (
        health_schema.friendly_unit_label(
            "HKQuantityTypeIdentifierOxygenSaturation", "%"
        )
        == "%"
    )
    assert (
        health_schema.friendly_unit_label("HKQuantityTypeIdentifierBodyMass", "lb")
        == "lb"
    )
    assert (
        health_schema.display_value(
            "HKQuantityTypeIdentifierFlightsClimbed", 23, "count"
        )
        == "23"
    )
    assert (
        health_schema.display_value(
            "HKQuantityTypeIdentifierRestingHeartRate", 52.0, "count/min"
        )
        == "52 bpm"
    )
    # Type-independent raw exporter units relabel for owners.
    assert (
        health_schema.friendly_unit_label(
            "HKQuantityTypeIdentifierHeadphoneAudioExposure", "dBASPL"
        )
        == "dB"
    )
    assert (
        health_schema.friendly_unit_label(
            "HKQuantityTypeIdentifierWalkingSpeed", "mi/hr"
        )
        == "mph"
    )
    assert (
        health_schema.friendly_unit_label(
            "HKQuantityTypeIdentifierWalkingSpeed", "km/hr"
        )
        == "km/h"
    )
    assert (
        health_schema.friendly_unit_label(
            "HKQuantityTypeIdentifierActiveEnergyBurned", "kcal"
        )
        == "Cal"
    )
    # Apple's native energy rows already say 'Cal'; it passes through.
    assert (
        health_schema.friendly_unit_label(
            "HKQuantityTypeIdentifierActiveEnergyBurned", "Cal"
        )
        == "Cal"
    )


# --- Day view: heart card ------------------------------------------------------


HR_TYPE = "HKQuantityTypeIdentifierHeartRate"
RESTING_HR_TYPE = "HKQuantityTypeIdentifierRestingHeartRate"
SPO2_TYPE = "HKQuantityTypeIdentifierOxygenSaturation"
RESP_TYPE = "HKQuantityTypeIdentifierRespiratoryRate"
HRV_TYPE = "HKQuantityTypeIdentifierHeartRateVariabilitySDNN"
BP_SYS_TYPE = "HKQuantityTypeIdentifierBloodPressureSystolic"
BP_DIA_TYPE = "HKQuantityTypeIdentifierBloodPressureDiastolic"


def test_day_api_heart_card_ranges_and_friendly_units(body_env):
    env = body_env()
    rows = [
        _row(HR_TYPE, "2026-07-15T06:00:00-06:00", value="55", unit="count/min"),
        _row(HR_TYPE, "2026-07-15T12:00:00-06:00", value="72", unit="count/min"),
        _row(HR_TYPE, "2026-07-15T18:00:00-06:00", value="142", unit="count/min"),
        _row(
            RESTING_HR_TYPE, "2026-07-15T07:00:00-06:00", value="54", unit="count/min"
        ),
        _row(
            RESTING_HR_TYPE, "2026-07-15T22:00:00-06:00", value="52", unit="count/min"
        ),
        _row(SPO2_TYPE, "2026-07-15T03:00:00-06:00", value="0.97", unit="%"),
        _row(SPO2_TYPE, "2026-07-15T04:00:00-06:00", value="0.99", unit="%"),
        _row(RESP_TYPE, "2026-07-15T03:30:00-06:00", value="14.5", unit="count/min"),
        _row(RESP_TYPE, "2026-07-15T04:30:00-06:00", value="16", unit="count/min"),
        _row(HRV_TYPE, "2026-07-15T05:00:00-06:00", value="45", unit="ms"),
        _row(HRV_TYPE, "2026-07-15T06:30:00-06:00", value="60", unit="ms"),
    ]
    _seed_import(env.journal, "20260901_100000", rows)

    heart = env.client.get("/app/body/api/day/20260715").get_json()["heart"]

    # Heart rate: full-day range in bpm with its reading count.
    assert heart["heart_rate"]["summary"] == "55–142 bpm · 3 readings"
    assert heart["heart_rate"]["min"] == 55.0
    assert heart["heart_rate"]["max"] == 142.0
    facts = {fact["label"]: fact["value"] for fact in heart["facts"]}
    # Resting heart rate keeps its latest value even on multi-reading days.
    assert facts["Resting heart rate"] == "52 bpm"
    # Multi-reading signals summarize as min–max in friendly units.
    assert facts["Blood oxygen"] == "97–99%"
    assert facts["Respiratory rate"] == "14.5–16 breaths/min"
    assert facts["Heart rate variability"] == "45–60 ms"


def test_day_api_single_blood_oxygen_reading_renders_as_percent(body_env):
    env = body_env()
    rows = [_row(SPO2_TYPE, "2026-07-16T03:00:00-06:00", value="0.98", unit="%")]
    _seed_import(env.journal, "20260901_110000", rows)

    payload = env.client.get("/app/body/api/day/20260716").get_json()

    facts = {fact["label"]: fact["value"] for fact in payload["heart"]["facts"]}
    assert facts["Blood oxygen"] == "98%"

    html = env.client.get("/app/body/20260716").get_data(as_text=True)
    assert "98%" in html
    assert "1.0 %" not in html


def test_day_api_blood_pressure_pairs_by_start_time(body_env):
    env = body_env()
    rows = [
        _row(BP_SYS_TYPE, "2026-07-17T08:30:00-06:00", value="122", unit="mmHg"),
        _row(BP_DIA_TYPE, "2026-07-17T08:30:00-06:00", value="78", unit="mmHg"),
        _row(BP_SYS_TYPE, "2026-07-17T21:05:00-06:00", value="118", unit="mmHg"),
        _row(BP_DIA_TYPE, "2026-07-17T21:05:00-06:00", value="76", unit="mmHg"),
        # An unpaired systolic row never fabricates a reading.
        _row(BP_SYS_TYPE, "2026-07-17T12:00:00-06:00", value="130", unit="mmHg"),
    ]
    _seed_import(env.journal, "20260901_120000", rows)

    heart = env.client.get("/app/body/api/day/20260717").get_json()["heart"]

    bp = heart["blood_pressure"]
    assert bp["mode"] == "readings"
    assert bp["count"] == 2
    assert bp["readings"] == [
        {"time": "8:30 AM", "label": "122/78 mmHg"},
        {"time": "9:05 PM", "label": "118/76 mmHg"},
    ]
    # The paired card replaces the two count-only fact rows.
    labels = [fact["label"] for fact in heart["facts"]]
    assert "Blood pressure (systolic)" not in labels
    assert "Blood pressure (diastolic)" not in labels

    html = env.client.get("/app/body/20260717").get_data(as_text=True)
    assert "Blood pressure" in html
    assert "122/78 mmHg" in html


def test_day_api_blood_pressure_compresses_to_ranges_when_many_readings(body_env):
    env = body_env()
    rows = []
    for hour, (systolic, diastolic) in enumerate(
        [(110, 70), (112, 72), (115, 74), (120, 76), (124, 78), (126, 80), (128, 82)],
        start=8,
    ):
        start = f"2026-07-18T{hour:02d}:00:00-06:00"
        rows.append(_row(BP_SYS_TYPE, start, value=str(systolic), unit="mmHg"))
        rows.append(_row(BP_DIA_TYPE, start, value=str(diastolic), unit="mmHg"))
    _seed_import(env.journal, "20260901_130000", rows)

    bp = env.client.get("/app/body/api/day/20260718").get_json()["heart"][
        "blood_pressure"
    ]

    assert bp["mode"] == "range"
    assert bp["count"] == 7
    assert bp["readings"] == []
    assert bp["range_label"] == "systolic 110–128 mmHg · diastolic 70–82 mmHg"


# --- Day view: heart-rate day curve -------------------------------------------


def _hr_rows(day: str, times_values: list[tuple[str, int]]) -> list[dict]:
    return [
        _row(
            HR_TYPE,
            f"{day}T{clock}:00-06:00",
            value=str(value),
            unit="count/min",
            source="Synthetic Watch",
        )
        for clock, value in times_values
    ]


def test_day_api_heart_rate_series_buckets_band_and_gap_split(body_env):
    env = body_env()
    # Two adjacent 5-minute buckets in the morning, then a >45-minute gap
    # to an isolated bucket: the curve must split and the tail render as
    # a dot.
    readings = [
        ("06:00", 60),
        ("06:01", 100),
        ("06:02", 62),
        ("06:03", 58),
        ("06:04", 90),
        ("06:05", 64),
        ("06:06", 66),
        ("06:07", 68),
        ("06:08", 70),
        ("06:09", 72),
        ("08:00", 80),
        ("08:01", 82),
        ("08:02", 84),
        ("08:03", 86),
        ("08:04", 88),
    ]
    _seed_import(env.journal, "20260910_000000", _hr_rows("2026-08-10", readings))

    payload = env.client.get("/app/body/api/day/20260810").get_json()

    series = payload["heart"]["series"]
    assert series is not None
    assert series["count"] == 15
    assert series["unit"] == "count/min"
    # Friendly unit only — the raw count/min never reaches the owner.
    assert series["unit_label"] == "bpm"
    assert series["bucket_minutes"] == 5
    # Bucket medians at bucket-center minutes-of-day.
    assert series["points"] == [
        [362.5, 62.0],
        [367.5, 68.0],
        [482.5, 84.0],
    ]
    # Per-bucket min–max carries the honest instantaneous variability.
    assert series["bands"] == [
        [362.5, 58.0, 100.0],
        [367.5, 64.0, 72.0],
        [482.5, 80.0, 88.0],
    ]
    # The 06:07→08:02 bucket gap (115 min) splits the curve: one median
    # path with its band polygon, plus the isolated bucket as a dot.
    assert len(series["svg"]["paths"]) == 1
    assert series["svg"]["paths"][0].startswith("M362.5 ")
    assert "L367.5 " in series["svg"]["paths"][0]
    assert len(series["svg"]["band_paths"]) == 1
    band = series["svg"]["band_paths"][0]
    assert band.startswith("M")
    assert band.endswith(" Z")
    # Closed polygon: maxima out, minima back — four points for two buckets.
    assert band.count("L") == 3
    assert len(series["svg"]["dots"]) == 1
    assert series["svg"]["dots"][0][0] == 482.5


def test_day_api_heart_rate_series_absent_below_threshold(body_env):
    env = body_env()
    readings = [(f"06:{5 * i:02d}", 60 + i) for i in range(11)]
    _seed_import(env.journal, "20260910_010000", _hr_rows("2026-08-11", readings))

    payload = env.client.get("/app/body/api/day/20260811").get_json()

    # Eleven readings are below the curve threshold: the payload keeps
    # the key additively but the card stays a text-only range row.
    heart = payload["heart"]
    assert heart["series"] is None
    assert heart["heart_rate"]["summary"] == "60–70 bpm · 11 readings"

    html = env.client.get("/app/body/20260811").get_data(as_text=True)
    assert "60–70 bpm · 11 readings" in html
    assert "Heart rate through the day" not in html
    assert 'class="body-curve-band"' not in html


def test_day_api_heart_series_y_axis_labels_match_padded_domain(body_env):
    env = body_env()
    values = [55, 60, 70, 80, 90, 100, 110, 120, 125, 130, 135, 142]
    readings = [(f"06:{5 * i:02d}", value) for i, value in enumerate(values)]
    _seed_import(env.journal, "20260910_020000", _hr_rows("2026-08-12", readings))

    series = env.client.get("/app/body/api/day/20260812").get_json()["heart"]["series"]

    # Same axis convention as glucose: readings span 55–142, the domain
    # pads by 8% (6.96) and rounds outward to whole numbers the labels
    # state.
    assert series["min"] == 55.0
    assert series["max"] == 142.0
    assert series["svg"]["y_min_label"] == "48"
    assert series["svg"]["y_max_label"] == "149"


def test_day_page_renders_heart_curve_under_range_row(body_env):
    env = body_env()
    readings = [(f"07:{i:02d}", 62 + (i % 7)) for i in range(20)]
    rows = _hr_rows("2026-08-13", readings) + [
        _row(
            RESP_TYPE,
            "2026-08-13T03:30:00-06:00",
            value="14.5",
            unit="count/min",
            source="Synthetic Watch",
        ),
    ]
    _seed_import(env.journal, "20260910_030000", rows)

    html = env.client.get("/app/body/20260813").get_data(as_text=True)

    # Range row, then the curve with its band, then the other facts.
    assert "62–68 bpm · 20 readings" in html
    assert 'aria-label="Heart rate through the day"' in html
    assert 'class="body-curve-band"' in html
    assert "Respiratory rate" in html
    curve_at = html.index('aria-label="Heart rate through the day"')
    assert html.index("62–68 bpm") < curve_at
    assert curve_at < html.index("Respiratory rate")
    assert "count/min" not in html


def test_day_api_payload_keys_stay_additive_with_heart_series(body_env):
    env = body_env()
    readings = [(f"09:{i:02d}", 70 + i) for i in range(12)]
    rows = _hr_rows("2026-08-14", readings) + [
        _row(
            GLUCOSE_TYPE,
            "2026-08-14T08:00:00-06:00",
            value="100",
            unit="mg/dL",
            source="Synthetic Stelo",
        ),
        _row(
            GLUCOSE_TYPE,
            "2026-08-14T08:05:00-06:00",
            value="105",
            unit="mg/dL",
            source="Synthetic Stelo",
        ),
    ]
    _seed_import(env.journal, "20260910_040000", rows)

    payload = env.client.get("/app/body/api/day/20260814").get_json()

    # Existing top-level keys stay intact alongside the new series.
    assert {
        "day",
        "date_label",
        "summary_markdown",
        "glucose",
        "entry_total",
        "has_data",
        "lede",
        "sleep",
        "glucose_series",
        "activity",
        "heart",
        "mind_sound",
        "walking",
        "body_measurements",
        "other_signals",
        "sources",
        "prompts",
        "audit",
        "nearest",
    } <= set(payload)
    assert {"heart_rate", "series", "blood_pressure", "facts"} <= set(payload["heart"])
    # The glucose curve payload keeps its shape — the band is HR-only.
    glucose_svg = payload["glucose_series"][0]["svg"]
    assert {"width", "height", "paths", "dots", "y_min_label", "y_max_label"} <= set(
        glucose_svg
    )
    assert "band_paths" not in glucose_svg
    series_svg = payload["heart"]["series"]["svg"]
    assert {"paths", "band_paths", "dots", "y_min_label", "y_max_label"} <= set(
        series_svg
    )


# --- Day view: steps primary source ---------------------------------------------


def test_day_api_steps_pick_primary_source_by_coverage(body_env):
    env = body_env()
    rows = [
        _row(
            STEP_TYPE,
            "2026-07-19T08:00:00-06:00",
            "2026-07-19T09:00:00-06:00",
            value="2000",
            unit="count",
            source="Synthetic Ring",
        ),
        _row(
            STEP_TYPE,
            "2026-07-19T10:00:00-06:00",
            "2026-07-19T11:00:00-06:00",
            value="3000",
            unit="count",
            source="Synthetic Ring",
        ),
        _row(
            STEP_TYPE,
            "2026-07-19T12:00:00-06:00",
            "2026-07-19T13:00:00-06:00",
            value="1412",
            unit="count",
            source="Synthetic Ring",
        ),
        _row(
            STEP_TYPE,
            "2026-07-19T08:10:00-06:00",
            "2026-07-19T08:20:00-06:00",
            value="800",
            unit="count",
            source="Synthetic Phone",
        ),
        _row(
            STEP_TYPE,
            "2026-07-19T12:10:00-06:00",
            "2026-07-19T12:20:00-06:00",
            value="400",
            unit="count",
            source="Synthetic Phone",
        ),
    ]
    _seed_import(env.journal, "20260901_140000", rows)

    steps = env.client.get("/app/body/api/day/20260719").get_json()["activity"]["steps"]

    # The largest-coverage source's total wins; the other source is only
    # named, never summed into the figure.
    assert steps["mode"] == "total"
    assert steps["total"] == 6412
    assert steps["total_label"] == "6,412"
    assert steps["source"] == "Synthetic Ring"
    assert steps["others"] == ["Synthetic Phone"]
    assert steps["others_label"] == "Synthetic Phone also contributed"

    html = env.client.get("/app/body/20260719").get_data(as_text=True)
    assert "6,412" in html
    assert "Synthetic Phone also contributed" in html


# --- Day view: asleep vs in-bed --------------------------------------------------


def test_day_api_sleep_splits_asleep_from_in_bed_when_stages_exist(body_env):
    env = body_env()
    rows = [
        _row(
            SLEEP_TYPE,
            "2026-07-20T22:58:00-06:00",
            "2026-07-21T02:00:00-06:00",
            value="HKCategoryValueSleepAnalysisAsleepCore",
            source="Synthetic Ring",
        ),
        _row(
            SLEEP_TYPE,
            "2026-07-21T02:00:00-06:00",
            "2026-07-21T02:30:00-06:00",
            value="HKCategoryValueSleepAnalysisAwake",
            source="Synthetic Ring",
        ),
        _row(
            SLEEP_TYPE,
            "2026-07-21T02:30:00-06:00",
            "2026-07-21T07:08:00-06:00",
            value="HKCategoryValueSleepAnalysisAsleepDeep",
            source="Synthetic Ring",
        ),
    ]
    _seed_import(env.journal, "20260901_150000", rows)

    payload = env.client.get("/app/body/api/day/20260721").get_json()

    sleep = payload["sleep"]
    assert sleep["has_stage_detail"] is True
    # The awake gap stays inside the merged in-bed span but out of asleep.
    assert sleep["in_bed_duration"] == "8h 10m"
    assert sleep["asleep_duration"] == "7h 40m"
    # ``duration`` keeps the merged-span figure the importer's day card
    # states, so the two surfaces stay on one number.
    assert sleep["duration"] == "8h 10m"
    assert payload["lede"].startswith("Slept 7h 40m (in bed 8h 10m)")

    html = env.client.get("/app/body/20260721").get_data(as_text=True)
    assert "asleep" in html
    assert "in bed" in html

    rail = {
        item["day"]: item
        for item in env.client.get("/app/body/api/status").get_json()["archive"][
            "recent_days"
        ]
    }
    assert rail["20260721"]["sleep_duration"] == "7h 40m"
    assert rail["20260721"]["sleep_in_bed"] == "8h 10m"


def test_day_api_sleep_without_stage_detail_keeps_single_figure(body_env):
    env = body_env()
    rows = [
        _row(
            SLEEP_TYPE,
            "2026-07-22T23:00:00-06:00",
            "2026-07-23T06:30:00-06:00",
            value="HKCategoryValueSleepAnalysisInBed",
            source="Synthetic Wrist",
        ),
    ]
    _seed_import(env.journal, "20260901_160000", rows)

    sleep = env.client.get("/app/body/api/day/20260723").get_json()["sleep"]

    # All rows in-bed/unspecified: asleep falls back to the merged span
    # and the card keeps the single pre-stage figure.
    assert sleep["has_stage_detail"] is False
    assert sleep["asleep_duration"] is None
    assert sleep["in_bed_duration"] == "7h 30m"
    assert sleep["duration"] == "7h 30m"


# --- Day view: workout aggregation ------------------------------------------------


def test_day_api_workout_summary_aggregates_kinds(body_env):
    env = body_env()
    cycling = "HKWorkoutActivityTypeCycling"
    walking = "HKWorkoutActivityTypeWalking"
    rows = [
        _row(cycling, "2026-07-25T06:00:00-06:00", source="S", kind="workout"),
        _row(cycling, "2026-07-25T17:00:00-06:00", source="S", kind="workout"),
        _row(walking, "2026-07-25T12:00:00-06:00", source="S", kind="workout"),
    ]
    more = [
        _row(cycling, "2026-07-26T06:00:00-06:00", source="S", kind="workout"),
        _row(cycling, "2026-07-26T17:00:00-06:00", source="S", kind="workout"),
        _row(
            "HKWorkoutActivityTypeHiking",
            "2026-07-26T08:00:00-06:00",
            source="S",
            kind="workout",
        ),
        _row(
            "HKWorkoutActivityTypeRunning",
            "2026-07-26T10:00:00-06:00",
            source="S",
            kind="workout",
        ),
        _row(walking, "2026-07-26T12:00:00-06:00", source="S", kind="workout"),
    ]
    _seed_import(env.journal, "20260901_170000", rows + more)

    day_one = env.client.get("/app/body/api/day/20260725").get_json()["activity"]
    day_two = env.client.get("/app/body/api/day/20260726").get_json()["activity"]

    assert day_one["workout_summary"] == "Cycling ×2 · Walking"
    # More than two kinds compress to "+N more"; the detail list keeps all.
    assert day_two["workout_summary"] == "Cycling ×2 · Hiking +2 more"
    assert len(day_two["workouts"]) == 5

    html = env.client.get("/app/body/20260725").get_data(as_text=True)
    assert "Cycling ×2 · Walking" in html


def test_day_api_workouts_show_recovered_distance_and_energy(body_env):
    env = body_env()
    rows = [
        _row(
            "HKWorkoutActivityTypeCycling",
            "2026-07-27T07:00:00-06:00",
            "2026-07-27T07:45:00-06:00",
            source="Synthetic Watch",
            kind="workout",
            metadata={
                "duration": "45",
                "durationUnit": "min",
                "totalDistance": "12.4",
                "totalDistanceUnit": "km",
                "totalDistanceType": "HKQuantityTypeIdentifierDistanceCycling",
                "totalEnergyBurned": "321.5",
                "totalEnergyBurnedUnit": "Cal",
                "totalEnergyBurnedType": ("HKQuantityTypeIdentifierActiveEnergyBurned"),
            },
        ),
        _row(
            "HKWorkoutActivityTypeWalking",
            "2026-07-27T18:00:00-06:00",
            "2026-07-27T18:20:00-06:00",
            source="Synthetic Watch",
            kind="workout",
        ),
    ]
    _seed_import(env.journal, "20260901_171500", rows)

    workouts = env.client.get("/app/body/api/day/20260727").get_json()["activity"][
        "workouts"
    ]

    assert workouts[0]["name"] == "Cycling"
    assert workouts[0]["distance"]["label"] == "12.4 km"
    assert workouts[0]["energy"]["label"] == "322 Cal"
    assert workouts[0]["metric_labels"] == ["12.4 km", "322 Cal"]
    assert workouts[0]["metrics_label"] == "12.4 km · 322 Cal"
    assert workouts[1]["distance"] is None
    assert workouts[1]["energy"] is None
    assert workouts[1]["metric_labels"] == []
    assert workouts[1]["metrics_label"] is None

    html = env.client.get("/app/body/20260727").get_data(as_text=True)
    assert "7:00 AM" in html
    assert "45m" in html
    assert "12.4 km" in html
    assert "322 Cal" in html
    assert "None" not in html


# --- Day view: running dynamics ---------------------------------------------------


def test_day_api_running_dynamics_summarize_instead_of_counting(body_env):
    env = body_env()
    power = "HKQuantityTypeIdentifierRunningPower"
    speed = "HKQuantityTypeIdentifierRunningSpeed"
    contact = "HKQuantityTypeIdentifierRunningGroundContactTime"
    osc = "HKQuantityTypeIdentifierRunningVerticalOscillation"
    rows = [
        _row(power, "2026-07-30T06:00:00-06:00", value="240", unit="W"),
        _row(power, "2026-07-30T06:01:00-06:00", value="250", unit="W"),
        _row(power, "2026-07-30T06:02:00-06:00", value="260", unit="W"),
        _row(speed, "2026-07-30T06:00:00-06:00", value="2.5", unit="m/s"),
        _row(speed, "2026-07-30T06:01:00-06:00", value="3.125", unit="m/s"),
        _row(contact, "2026-07-30T06:00:00-06:00", value="240", unit="ms"),
        _row(osc, "2026-07-30T06:00:00-06:00", value="8.1", unit="cm"),
        _row(osc, "2026-07-30T06:01:00-06:00", value="9.3", unit="cm"),
    ]
    _seed_import(env.journal, "20260901_180000", rows)

    activity = env.client.get("/app/body/api/day/20260730").get_json()["activity"]

    running = {item["label"]: item["summary"] for item in activity["running"]}
    assert running["Running power"] == "240–260 W · avg 250"
    # Speed converts to pace when the unit permits; fastest pace first.
    assert running["Running speed"] == "5:20–6:40 /km · avg 5:56 /km"
    assert running["Running ground contact time"] == "240 ms"
    assert running["Running vertical oscillation"] == "8.1–9.3 cm · avg 8.7"
    # Running dynamics leave the raw entry-count list.
    counter_labels = [item["label"] for item in activity["counters"]]
    assert "Running power" not in counter_labels

    html = env.client.get("/app/body/20260730").get_data(as_text=True)
    assert "Running dynamics" in html
    assert "240–260 W · avg 250" in html


# --- Day view: summable single-source totals --------------------------------------


def test_day_api_summable_quantities_total_when_single_source(body_env):
    env = body_env()
    flights = "HKQuantityTypeIdentifierFlightsClimbed"
    exercise = "HKQuantityTypeIdentifierAppleExerciseTime"
    daylight = "HKQuantityTypeIdentifierTimeInDaylight"
    mindful = "HKCategoryTypeIdentifierMindfulSession"
    rows = [
        _row(flights, "2026-07-31T08:00:00-06:00", value="9", unit="count"),
        _row(flights, "2026-07-31T15:00:00-06:00", value="14", unit="count"),
        _row(exercise, "2026-07-31T08:00:00-06:00", value="22", unit="min"),
        _row(exercise, "2026-07-31T18:00:00-06:00", value="20", unit="min"),
        _row(daylight, "2026-07-31T12:00:00-06:00", value="38", unit="min"),
        _row(
            mindful,
            "2026-07-31T07:00:00-06:00",
            "2026-07-31T07:10:00-06:00",
            source="Synthetic Phone",
        ),
        _row(
            mindful,
            "2026-07-31T21:00:00-06:00",
            "2026-07-31T21:05:00-06:00",
            source="Synthetic Phone",
        ),
    ]
    _seed_import(env.journal, "20260901_190000", rows)

    payload = env.client.get("/app/body/api/day/20260731").get_json()

    counters = {
        item["label"]: item["value"] for item in payload["activity"]["counters"]
    }
    assert counters["Flights climbed"] == "23"
    assert counters["Exercise minutes"] == "42 min"
    assert counters["Time in daylight"] == "38 min"
    mind_facts = {
        fact["label"]: fact["value"] for fact in payload["mind_sound"]["facts"]
    }
    # Mindful sessions sum to minutes of session time.
    assert mind_facts["Mindful sessions"] == "15m"


# --- Day view: activity value parity ---------------------------------------------


ACTIVE_ENERGY_TYPE = "HKQuantityTypeIdentifierActiveEnergyBurned"
DISTANCE_TYPE = "HKQuantityTypeIdentifierDistanceWalkingRunning"
STAND_HOUR_TYPE = "HKCategoryTypeIdentifierAppleStandHour"
WALK_SPEED_TYPE = "HKQuantityTypeIdentifierWalkingSpeed"
STEP_LENGTH_TYPE = "HKQuantityTypeIdentifierWalkingStepLength"
DOUBLE_SUPPORT_TYPE = "HKQuantityTypeIdentifierWalkingDoubleSupportPercentage"
ASYMMETRY_TYPE = "HKQuantityTypeIdentifierWalkingAsymmetryPercentage"


def test_day_api_energy_and_distance_pick_primary_source_totals(body_env):
    env = body_env()
    rows = [
        _row(
            ACTIVE_ENERGY_TYPE,
            "2026-08-20T08:00:00-06:00",
            "2026-08-20T09:00:00-06:00",
            value="300.5",
            unit="Cal",
            source="Synthetic Ring",
        ),
        _row(
            ACTIVE_ENERGY_TYPE,
            "2026-08-20T10:00:00-06:00",
            "2026-08-20T11:00:00-06:00",
            value="311.6",
            unit="Cal",
            source="Synthetic Ring",
        ),
        _row(
            ACTIVE_ENERGY_TYPE,
            "2026-08-20T08:05:00-06:00",
            "2026-08-20T08:10:00-06:00",
            value="120",
            unit="Cal",
            source="Synthetic Phone",
        ),
        _row(
            DISTANCE_TYPE,
            "2026-08-20T08:00:00-06:00",
            "2026-08-20T09:00:00-06:00",
            value="2.13",
            unit="mi",
            source="Synthetic Ring",
        ),
        _row(
            DISTANCE_TYPE,
            "2026-08-20T10:00:00-06:00",
            "2026-08-20T11:00:00-06:00",
            value="2.11",
            unit="mi",
            source="Synthetic Ring",
        ),
    ]
    _seed_import(env.journal, "20260910_060000", rows)

    payload = env.client.get("/app/body/api/day/20260820").get_json()
    counters = {
        item["label"]: item["value"] for item in payload["activity"]["counters"]
    }

    # The largest-coverage source's sum wins; the other source is only
    # named, never summed into the figure. Energy reads as whole Cal.
    assert (
        counters["Active energy"]
        == "612 Cal · Synthetic Ring — Synthetic Phone also contributed"
    )
    # Distance totals keep one decimal in the rows' own unit.
    assert counters["Walking + running distance"] == "4.2 mi"

    html = env.client.get("/app/body/20260820").get_data(as_text=True)
    assert "612 Cal · Synthetic Ring — Synthetic Phone also contributed" in html
    assert "4.2 mi" in html


def test_day_api_zero_summable_totals_fall_back_to_entry_counts(body_env):
    env = body_env()
    rows = [
        _row(
            ACTIVE_ENERGY_TYPE,
            "2026-08-22T08:00:00-06:00",
            "2026-08-22T08:05:00-06:00",
            value="0",
            unit="Cal",
            source="Synthetic Phone",
        ),
        _row(
            DISTANCE_TYPE,
            "2026-08-22T08:00:00-06:00",
            "2026-08-22T08:05:00-06:00",
            value="0.04",
            unit="mi",
            source="Synthetic Phone",
        ),
        _row(
            "HKQuantityTypeIdentifierFlightsClimbed",
            "2026-08-22T09:00:00-06:00",
            value="3",
            unit="count",
            source="Synthetic Phone",
        ),
    ]
    _seed_import(env.journal, "20260910_130000", rows)

    payload = env.client.get("/app/body/api/day/20260822").get_json()
    counters = {item["label"]: item for item in payload["activity"]["counters"]}

    # A zero-reading day must not manufacture a '0 Cal' health fact — the
    # card falls back to the factual entry count.
    assert counters["Active energy"]["value"] is None
    assert counters["Active energy"]["count"] == 1
    # A distance summing to 0.0 at display precision falls back the same way.
    assert counters["Walking + running distance"]["value"] is None
    # A real total still reads as its value.
    assert counters["Flights climbed"]["value"] == "3"

    html = env.client.get("/app/body/20260822").get_data(as_text=True)
    assert "0 Cal" not in html
    assert "0.0 mi" not in html
    # The count fallback pluralizes honestly: one row reads '1 entry'.
    assert ">entry<" in html


def test_day_api_stand_hours_count_distinct_stood_hours(body_env):
    env = body_env()
    rows = [
        _row(
            STAND_HOUR_TYPE,
            "2026-08-21T08:00:00-06:00",
            value="HKCategoryValueAppleStandHourStood",
            source="Synthetic Watch",
        ),
        _row(
            STAND_HOUR_TYPE,
            "2026-08-21T09:00:00-06:00",
            value="HKCategoryValueAppleStandHourStood",
            source="Synthetic Watch",
        ),
        _row(
            STAND_HOUR_TYPE,
            "2026-08-21T10:00:00-06:00",
            value="HKCategoryValueAppleStandHourIdle",
            source="Synthetic Watch",
        ),
        # The phone repeating an hour the watch already stood must not
        # double-count; a phone-only stood hour still counts.
        _row(
            STAND_HOUR_TYPE,
            "2026-08-21T09:00:00-06:00",
            value="HKCategoryValueAppleStandHourStood",
            source="Synthetic Phone",
        ),
        _row(
            STAND_HOUR_TYPE,
            "2026-08-21T14:00:00-06:00",
            value="HKCategoryValueAppleStandHourStood",
            source="Synthetic Phone",
        ),
    ]
    _seed_import(env.journal, "20260910_070000", rows)

    counters = {
        item["label"]: item["value"]
        for item in env.client.get("/app/body/api/day/20260821").get_json()["activity"][
            "counters"
        ]
    }

    # Distinct stood hours: 8 AM, 9 AM (once), 2 PM. Idle hours are not
    # stand hours; the label stays the factual count, no goal framing.
    assert counters["Stand hours"] == "3"


# --- Day view: walking metric value summaries --------------------------------------


def test_day_api_walking_metrics_summarize_values(body_env):
    env = body_env()
    rows = [
        _row(WALK_SPEED_TYPE, "2026-08-22T08:00:00-06:00", value="2.1", unit="mi/hr"),
        _row(WALK_SPEED_TYPE, "2026-08-22T12:00:00-06:00", value="3.4", unit="mi/hr"),
        _row(WALK_SPEED_TYPE, "2026-08-22T16:00:00-06:00", value="2.9", unit="mi/hr"),
        _row(STEP_LENGTH_TYPE, "2026-08-22T08:00:00-06:00", value="27.2", unit="in"),
        _row(STEP_LENGTH_TYPE, "2026-08-22T12:00:00-06:00", value="28.4", unit="in"),
        _row(DOUBLE_SUPPORT_TYPE, "2026-08-22T08:00:00-06:00", value="0.281", unit="%"),
        _row(DOUBLE_SUPPORT_TYPE, "2026-08-22T12:00:00-06:00", value="0.285", unit="%"),
        _row(ASYMMETRY_TYPE, "2026-08-22T08:00:00-06:00", value="0.02", unit="%"),
        _row(ASYMMETRY_TYPE, "2026-08-22T12:00:00-06:00", value="0.04", unit="%"),
    ]
    _seed_import(env.journal, "20260910_080000", rows)

    payload = env.client.get("/app/body/api/day/20260822").get_json()
    walking = {item["label"]: item for item in payload["walking"]["facts"]}

    # Speed: min–max plus average in the friendly mph label.
    assert walking["Walking speed"]["summary"] == "2.1–3.4 mph · avg 2.8"
    assert walking["Walking speed"]["count_label"] == "3"
    # Step length: average with its unit.
    assert walking["Walking step length"]["summary"] == "avg 27.8 in"
    # Fraction-percent rows scale through the shared normalizers —
    # 0.283 renders as 28.3%, never "0.3 %".
    assert walking["Walking double support percentage"]["summary"] == "avg 28.3%"
    assert walking["Walking asymmetry percentage"]["summary"] == "avg 3%"

    html = env.client.get("/app/body/20260822").get_data(as_text=True)
    assert "2.1–3.4 mph · avg 2.8" in html
    assert "avg 28.3%" in html
    # Entry counts stay secondary next to the value.
    assert "3 entries" in html


# --- Day view: source chip counts ---------------------------------------------------


def test_day_page_source_chips_carry_entry_counts(body_env):
    env = body_env()
    rows = [
        _row(
            HR_TYPE,
            "2026-08-23T08:00:00-06:00",
            value="70",
            unit="count/min",
            source="Synthetic Watch",
        ),
        _row(
            HR_TYPE,
            "2026-08-23T09:00:00-06:00",
            value="80",
            unit="count/min",
            source="Synthetic Watch",
        ),
        _row(
            GLUCOSE_TYPE,
            "2026-08-23T08:00:00-06:00",
            value="100",
            unit="mg/dL",
            source="Synthetic Stelo",
        ),
    ]
    _seed_import(env.journal, "20260910_090000", rows)

    sources = env.client.get("/app/body/api/day/20260823").get_json()["sources"]

    chips = {chip["name"]: chip for chip in sources["chips"]}
    assert chips["Synthetic Watch"]["count"] == 2
    assert chips["Synthetic Watch"]["entries_label"] == "2 entries"
    assert chips["Synthetic Stelo"]["entries_label"] == "1 entry"
    # The footer total is unchanged alongside the per-chip counts.
    assert sources["entry_total_label"] == "3"

    html = env.client.get("/app/body/20260823").get_data(as_text=True)
    assert "2 entries" in html
    assert "1 entry" in html
    assert "entries observed" in html
    # The sources highlight pluralizes by count.
    assert "2 sources" in html


def test_day_page_single_source_highlight_reads_singular(body_env):
    env = body_env()
    rows = [
        _row(
            HR_TYPE,
            "2026-08-25T08:00:00-06:00",
            value="70",
            unit="count/min",
            source="Synthetic Watch",
        ),
    ]
    _seed_import(env.journal, "20260910_150000", rows)

    html = env.client.get("/app/body/20260825").get_data(as_text=True)
    assert "1 source" in html
    assert "1 sources" not in html


# --- Day view: raw units never reach the page ---------------------------------------


def test_day_page_html_carries_no_raw_unit_strings(body_env):
    env = body_env()
    rows = [
        _row(HR_TYPE, "2026-08-24T09:00:00-06:00", value="70", unit="count/min"),
        _row(HR_TYPE, "2026-08-24T12:00:00-06:00", value="88", unit="count/min"),
        _row(
            HEADPHONE_AUDIO_TYPE,
            "2026-08-24T10:00:00-06:00",
            value="52.1",
            unit="dBASPL",
        ),
        _row(
            HEADPHONE_AUDIO_TYPE,
            "2026-08-24T11:00:00-06:00",
            value="63.9",
            unit="dBASPL",
        ),
        _row(WALK_SPEED_TYPE, "2026-08-24T08:00:00-06:00", value="2.5", unit="mi/hr"),
        _row(
            ACTIVE_ENERGY_TYPE,
            "2026-08-24T08:00:00-06:00",
            "2026-08-24T09:00:00-06:00",
            value="512.4",
            unit="Cal",
        ),
        _row(
            "HKQuantityTypeIdentifierAppleExerciseTime",
            "2026-08-24T08:00:00-06:00",
            value="22",
            unit="min",
        ),
        _row(
            STAND_HOUR_TYPE,
            "2026-08-24T08:00:00-06:00",
            value="HKCategoryValueAppleStandHourStood",
        ),
    ]
    _seed_import(env.journal, "20260910_100000", rows)

    html = env.client.get("/app/body/20260824").get_data(as_text=True)

    # Raw exporter unit strings never reach the page — the shared
    # normalizers relabel them ('bpm', 'dB', 'mph', 'Cal').
    for raw in ("count/min", "dBASPL", "kcal", "mi/hr"):
        assert raw not in html, f"raw unit string leaked into HTML: {raw}"
    assert "bpm" in html
    assert "dB" in html
    assert "mph" in html
    assert "512 Cal" in html


# --- Day view: workout ordering and sub-minute durations -----------------------------


def test_day_api_workouts_sort_by_start_and_never_render_zero_minutes(body_env):
    env = body_env()
    rows = [
        _row(
            "HKWorkoutActivityTypeWalking",
            "2026-08-25T13:05:00-06:00",
            "2026-08-25T13:35:00-06:00",
            source="S",
            kind="workout",
        ),
        _row(
            "HKWorkoutActivityTypeRunning",
            "2026-08-25T00:03:00-06:00",
            "2026-08-25T00:03:20-06:00",
            source="S",
            kind="workout",
        ),
        _row(
            "HKWorkoutActivityTypeYoga",
            "2026-08-25T06:00:00-06:00",
            "2026-08-25T06:00:00-06:00",
            source="S",
            kind="workout",
        ),
    ]
    _seed_import(env.journal, "20260910_110000", rows)

    workouts = env.client.get("/app/body/api/day/20260825").get_json()["activity"][
        "workouts"
    ]

    # Strict start-time order: the after-midnight workout leads the list.
    assert [w["start"] for w in workouts] == ["12:03 AM", "6:00 AM", "1:05 PM"]
    # A 20-second workout reads '<1m', never '0m'; a zero-length row
    # carries no duration at all.
    assert workouts[0]["duration"] == "<1m"
    assert workouts[1]["duration"] is None
    assert workouts[2]["duration"] == "30m"

    html = env.client.get("/app/body/20260825").get_data(as_text=True)
    assert "&lt;1m" in html
    assert "&middot; 0m" not in html


# --- Day view: audio-level summaries ------------------------------------------


HEADPHONE_AUDIO_TYPE = "HKQuantityTypeIdentifierHeadphoneAudioExposure"
ENVIRONMENTAL_AUDIO_TYPE = "HKQuantityTypeIdentifierEnvironmentalAudioExposure"


def test_day_api_audio_levels_summarize_factual_range(body_env):
    env = body_env()
    rows = [
        _row(
            HEADPHONE_AUDIO_TYPE,
            "2026-08-06T09:00:00-06:00",
            value="52.1",
            unit="dBASPL",
            source="Synthetic Buds",
        ),
        _row(
            HEADPHONE_AUDIO_TYPE,
            "2026-08-06T13:00:00-06:00",
            value="78",
            unit="dBASPL",
            source="Synthetic Buds",
        ),
        _row(
            HEADPHONE_AUDIO_TYPE,
            "2026-08-06T17:00:00-06:00",
            value="60.5",
            unit="dBASPL",
            source="Synthetic Buds",
        ),
        _row(
            ENVIRONMENTAL_AUDIO_TYPE,
            "2026-08-06T10:00:00-06:00",
            value="61.7",
            unit="dBASPL",
            source="Synthetic Watch",
        ),
        _row(
            ENVIRONMENTAL_AUDIO_TYPE,
            "2026-08-06T15:00:00-06:00",
            value="84.6",
            unit="dBASPL",
            source="Synthetic Watch",
        ),
    ]
    _seed_import(env.journal, "20260910_050000", rows)

    payload = env.client.get("/app/body/api/day/20260806").get_json()

    facts = {fact["label"]: fact["value"] for fact in payload["mind_sound"]["facts"]}
    # Entry count plus the day's factual level range in the friendly
    # 'dB' label — no exposure judgments.
    assert facts["Headphone audio level"] == "3 entries · 52.1–78 dB"
    assert facts["Environmental audio level"] == "2 entries · 61.7–84.6 dB"

    html = env.client.get("/app/body/20260806").get_data(as_text=True)
    assert "Mind &amp; sound" in html
    assert "3 entries · 52.1–78 dB" in html
    # Factual range only inside the card — no exposure judgments.
    card = html[html.index("Mind &amp; sound") : html.index("Sources this day")]
    lowered = card.lower()
    assert "loud" not in lowered
    assert "warning" not in lowered
    assert "exposure" not in lowered


# --- Day view: new cards -----------------------------------------------------------


def test_day_api_body_measurements_and_other_signals_cards(body_env):
    env = body_env()
    rows = [
        _row(
            "HKQuantityTypeIdentifierBodyMass",
            "2026-08-01T07:00:00-06:00",
            value="185.2",
            unit="lb",
        ),
        _row(
            "HKQuantityTypeIdentifierBodyFatPercentage",
            "2026-08-01T07:00:30-06:00",
            value="0.223",
            unit="%",
        ),
        _row(
            "HKQuantityTypeIdentifierBodyMassIndex",
            "2026-08-01T07:01:00-06:00",
            value="24.1",
            unit="count",
        ),
        _row(
            "HKQuantityTypeIdentifierHeight",
            "2026-08-01T07:02:00-06:00",
            value="70",
            unit="in",
        ),
        _row(
            "HKQuantityTypeIdentifierAppleSleepingWristTemperature",
            "2026-08-01T03:00:00-06:00",
            value="96.53",
            unit="degF",
        ),
        _row(
            "HKQuantityTypeIdentifierNumberOfTimesFallen",
            "2026-08-01T10:00:00-06:00",
            value="1",
            unit="count",
        ),
    ]
    _seed_import(env.journal, "20260901_200000", rows)

    payload = env.client.get("/app/body/api/day/20260801").get_json()

    body_facts = {
        fact["label"]: fact["value"] for fact in payload["body_measurements"]["facts"]
    }
    assert body_facts["Body mass"] == "185.2 lb"
    # Fraction-percent fix: 0.223 renders as 22.3%, never "0.2 %".
    assert body_facts["Body fat"] == "22.3%"
    assert body_facts["Body mass index"] == "24.1"
    assert body_facts["Height"] == "70 in"
    other_facts = {
        fact["label"]: fact["value"] for fact in payload["other_signals"]["facts"]
    }
    assert other_facts["Wrist temperature"] == "96.5 degF"
    assert other_facts["Number of times fallen"] == "1"

    html = env.client.get("/app/body/20260801").get_data(as_text=True)
    assert "Body measurements" in html
    assert "Other signals" in html
    assert "22.3%" in html


def test_day_api_multi_row_body_measurements_show_latest_value(body_env):
    env = body_env()
    mass = "HKQuantityTypeIdentifierBodyMass"
    fat = "HKQuantityTypeIdentifierBodyFatPercentage"
    rows = [
        _row(
            mass,
            "2026-05-08T07:00:00-06:00",
            value="173.1",
            unit="lb",
            source="Synthetic Scale",
        ),
        # The latest reading is picked by start time, not row order.
        _row(
            mass,
            "2026-05-08T21:00:00-06:00",
            value="172.4",
            unit="lb",
            source="Synthetic Scale",
        ),
        _row(
            mass,
            "2026-05-08T12:00:00-06:00",
            value="172.9",
            unit="lb",
            source="Synthetic Scale",
        ),
        _row(
            fat,
            "2026-05-08T07:00:00-06:00",
            value="0.231",
            unit="%",
            source="Synthetic Scale",
        ),
        _row(
            fat,
            "2026-05-08T21:00:00-06:00",
            value="0.223",
            unit="%",
            source="Synthetic Scale",
        ),
    ]
    _seed_import(env.journal, "20260910_140000", rows)

    payload = env.client.get("/app/body/api/day/20260508").get_json()
    facts = {
        fact["label"]: fact["value"] for fact in payload["body_measurements"]["facts"]
    }

    # Multi-entry measurement types headline the day's latest reading
    # through the shared normalizers (body fat's 0–1 fraction reads as a
    # percent), with the factual entry count alongside.
    assert facts["Body mass"] == "latest 172.4 lb · 3 entries"
    assert facts["Body fat"] == "latest 22.3% · 2 entries"

    html = env.client.get("/app/body/20260508").get_data(as_text=True)
    assert "latest 172.4 lb · 3 entries" in html


# --- Day view: prompt gating --------------------------------------------------------


def test_day_prompts_gate_journal_references_on_chronicle_day(body_env):
    env = body_env()
    rows = [
        _row(
            "HKWorkoutActivityTypeCycling",
            "2026-08-02T06:00:00-06:00",
            source="S",
            kind="workout",
        ),
    ]
    _seed_import(env.journal, "20260901_210000", rows)

    without_chronicle = env.client.get("/app/body/api/day/20260802").get_json()
    assert without_chronicle["prompts"]
    assert not any("journal" in prompt for prompt in without_chronicle["prompts"])

    (env.journal / "chronicle" / "20260802").mkdir(parents=True)
    with_chronicle = env.client.get("/app/body/api/day/20260802").get_json()
    assert any("journal" in prompt for prompt in with_chronicle["prompts"])


# --- Day view: sparse lede + glucose axis --------------------------------------------


def test_day_api_sparse_lede_names_families(body_env):
    env = body_env()
    rows = [
        _row(HR_TYPE, "2026-08-03T06:00:00-06:00", value="60", unit="count/min"),
        _row(HR_TYPE, "2026-08-03T07:00:00-06:00", value="62", unit="count/min"),
        _row(HR_TYPE, "2026-08-03T08:00:00-06:00", value="64", unit="count/min"),
        _row(STEP_TYPE, "2026-08-03T09:00:00-06:00", value="100", unit="count"),
        _row(STEP_TYPE, "2026-08-03T10:00:00-06:00", value="200", unit="count"),
        _row(
            "HKQuantityTypeIdentifierBodyMass",
            "2026-08-03T07:00:00-06:00",
            value="185",
            unit="lb",
        ),
    ]
    _seed_import(env.journal, "20260901_220000", rows)

    lede = env.client.get("/app/body/api/day/20260803").get_json()["lede"]

    assert lede == "6 entries across Heart, Activity, and 1 more area."


def test_day_api_glucose_axis_labels_match_padded_domain(body_env):
    env = body_env()
    _seed_health_import(env.journal)

    curve = env.client.get("/app/body/api/day/20260703").get_json()["glucose_series"][0]

    # Readings span 100–140; the axis pads by 8% (3.2) and rounds outward,
    # and the labels state that actual rendered domain.
    assert curve["min"] == 100.0
    assert curve["max"] == 140.0
    assert curve["svg"]["y_min_label"] == "96"
    assert curve["svg"]["y_max_label"] == "144"


# --- Day view: evening nap bar sliver -------------------------------------------------


def test_day_api_post_6pm_doze_drops_bar_and_label_together(body_env):
    env = body_env()
    rows = [
        _row(
            SLEEP_TYPE,
            "2026-08-04T23:00:00-06:00",
            "2026-08-05T06:30:00-06:00",
            value="HKCategoryValueSleepAnalysisAsleepCore",
            source="Synthetic Ring",
        ),
        # An evening doze past the 6 PM axis end that never merged into a
        # following night: it has no honest place inside the 6 PM – 6 PM
        # axis, so the bar and its nap label drop together — never a
        # clamped sliver at the edge under an orphaned label.
        _row(
            SLEEP_TYPE,
            "2026-08-05T19:00:00-06:00",
            "2026-08-05T19:40:00-06:00",
            value="HKCategoryValueSleepAnalysisAsleepCore",
            source="Synthetic Ring",
        ),
    ]
    _seed_import(env.journal, "20260901_230000", rows)

    sleep = env.client.get("/app/body/api/day/20260805").get_json()["sleep"]

    assert [segment["kind"] for segment in sleep["bar"]["segments"]] == ["main"]
    assert sleep["naps"] == []
    # The main night stays the headline either way.
    assert sleep["window"] == "11:00 PM – 6:30 AM"


def test_day_api_nap_straddling_axis_end_keeps_visible_bar_and_label(body_env):
    env = body_env()
    rows = [
        _row(
            SLEEP_TYPE,
            "2026-08-06T23:00:00-06:00",
            "2026-08-07T06:30:00-06:00",
            value="HKCategoryValueSleepAnalysisAsleepCore",
            source="Synthetic Ring",
        ),
        # A doze straddling the 6 PM axis end keeps its list label and its
        # bar clips to a visible minimum width inside the axis.
        _row(
            SLEEP_TYPE,
            "2026-08-07T17:58:00-06:00",
            "2026-08-07T18:30:00-06:00",
            value="HKCategoryValueSleepAnalysisAsleepCore",
            source="Synthetic Ring",
        ),
    ]
    _seed_import(env.journal, "20260901_233000", rows)

    sleep = env.client.get("/app/body/api/day/20260807").get_json()["sleep"]

    nap_segments = [
        segment for segment in sleep["bar"]["segments"] if segment["kind"] == "nap"
    ]
    assert len(nap_segments) == 1
    assert nap_segments[0]["width"] >= 4.0
    assert nap_segments[0]["x"] + nap_segments[0]["width"] <= 1440.0
    assert sleep["naps"] == [{"window": "5:58 PM – 6:30 PM", "duration": "32m"}]


# --- Overview: month qualifiers, import list, audit bundles ---------------------------


def test_overview_titles_carry_sources_month_qualifier(body_env):
    env = body_env()
    _seed_health_import(env.journal)

    status = env.client.get("/app/body/api/status").get_json()
    assert status["sources_month_label"] == "July 2026"

    html = env.client.get("/app/body/").get_data(as_text=True)
    assert "Sources represented &middot; July 2026" in html
    assert "By source &middot; July 2026" in html


def test_status_api_import_months_render_as_range_label(body_env):
    env = body_env()
    december = [
        _row(
            GLUCOSE_TYPE,
            "2025-12-30T08:00:00-06:00",
            value="100",
            unit="mg/dL",
            source="Synthetic Stelo",
        )
    ]
    july = [
        _row(
            GLUCOSE_TYPE,
            "2026-07-03T08:00:00-06:00",
            value="100",
            unit="mg/dL",
            source="Synthetic Stelo",
        )
    ]
    _seed_import(env.journal, "20260902_000000", december + july)
    # A manifest with no days and no normalized shards renders honestly.
    _write_json(
        env.journal / "imports" / "20260902_010000" / "manifest.json",
        {
            "import_id": "20260902_010000",
            "source_type": "apple_health",
            "source_hash": "sha256:empty",
            "entry_count": 0,
            "days_affected": [],
            "files_created": [],
            "imported_at": "2026-09-02T01:00:00",
            "imported_via": "test",
        },
    )

    status = env.client.get("/app/body/api/status").get_json()

    by_id = {item["import_id"]: item for item in status["imports"]}
    assert (
        by_id["20260902_000000"]["normalized_months_label"]
        == "2025-12 – 2026-07 · 2 months"
    )
    assert by_id["20260902_010000"]["normalized_months_label"] == "—"

    html = env.client.get("/app/body/").get_data(as_text=True)
    assert "2025-12 – 2026-07 · 2 months" in html
    assert "&mdash;" in html


def test_day_audit_lists_every_bundle_containing_the_day(body_env):
    env = body_env()
    rows = [
        _row(
            GLUCOSE_TYPE,
            "2026-08-05T08:00:00-06:00",
            value="100",
            unit="mg/dL",
            source="Synthetic Stelo",
        )
    ]
    _seed_import(env.journal, "20260903_000000", rows)
    # A second bundle imported the same entries (same dedupe keys) with
    # its own import id — the audit drawer must name both bundles.
    first_shard = (
        env.journal / "imports" / "20260903_000000" / "normalized" / "2026-08.jsonl"
    )
    second_shard = (
        env.journal / "imports" / "20260903_010000" / "normalized" / "2026-08.jsonl"
    )
    second_shard.parent.mkdir(parents=True)
    second_shard.write_text(
        first_shard.read_text(encoding="utf-8").replace(
            "20260903_000000", "20260903_010000"
        ),
        encoding="utf-8",
    )
    _write_json(
        env.journal / "imports" / "20260903_010000" / "manifest.json",
        {
            "import_id": "20260903_010000",
            "source_type": "apple_health",
            "source_hash": "sha256:overlap",
            "entry_count": 1,
            "days_affected": ["20260805"],
            "files_created": [],
            "imported_at": "2026-09-03T01:00:00",
            "imported_via": "test",
        },
    )

    payload = env.client.get("/app/body/api/day/20260805").get_json()

    assert payload["entry_total"] == 1
    assert payload["audit"]["import_ids"] == [
        "20260903_000000",
        "20260903_010000",
    ]
