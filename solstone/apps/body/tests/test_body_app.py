# SPDX-License-Identifier: AGPL-3.0-only
# Copyright (c) 2026 sol pbc

from __future__ import annotations

import json
import sqlite3
from pathlib import Path

from solstone.apps.body import routes as body_routes

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


# --- Archive: heat strip, families, rail --------------------------------------


def test_status_api_heat_strip_spans_gap_months_log_scaled(body_env):
    env = body_env()
    march = [
        _row(
            GLUCOSE_TYPE,
            f"2026-03-15T0{i}:00:00-06:00",
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
    _seed_import(env.journal, "20260804_000000", march + july)

    archive = env.client.get("/app/body/api/status").get_json()["archive"]

    heat = archive["heat"]
    assert [cell["month"] for cell in heat] == [
        "2026-03",
        "2026-04",
        "2026-05",
        "2026-06",
        "2026-07",
    ]
    assert heat[0]["count"] == 2
    assert 0.0 < heat[0]["intensity"] < 1.0
    assert heat[0]["first_day"] == "20260315"
    for cell in heat[1:4]:
        assert cell["count"] == 0
        assert cell["intensity"] == 0.0
        assert cell["first_day"] is None
    assert heat[-1]["intensity"] == 1.0
    assert archive["months_observed"] == 2
    assert archive["coverage"]["range_label"] == "Mar 2026 – Jul 2026"


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
    glucose_day = rail[1]
    assert glucose_day["glucose_label"] == "100–140 mg/dL · avg 120"
    assert glucose_day["workout_count"] == 0
    assert glucose_day["source_count"] == 2
    workout_day = rail[0]
    assert workout_day["workout_count"] == 1
    assert workout_day["sleep_duration"] is None


def test_status_page_renders_archive_sections(body_env):
    env = body_env()
    _seed_health_import(env.journal)

    html = env.client.get("/app/body/").get_data(as_text=True)

    assert "Body archive" in html
    assert "Recent body days" in html
    assert "Coverage areas" in html
    assert "Sources represented" in html
    assert "body-heat-cell" in html
    assert "months observed" in html
