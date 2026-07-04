# SPDX-License-Identifier: AGPL-3.0-only
# Copyright (c) 2026 sol pbc

import datetime as dt
import importlib
import importlib.util
import json
import logging
import re
from collections import Counter
from pathlib import Path

import pytest

from solstone.apps.body import routes as body_routes
from solstone.think.importers import apple_health
from solstone.think.importers.apple_health import (
    AppleHealthImporter,
)
from solstone.think.importers.health_dedupe import get_health_dedupe_record
from solstone.think.importers.health_schema import (
    merge_sleep_sessions,
    pick_day_sleep,
    pick_main_session,
)

FIXTURE_ROOT = (
    Path(__file__).parent
    / "fixtures"
    / "importers"
    / "health"
    / "apple_health_synthetic"
)
ZIP_FIXTURE = (
    Path(__file__).parent
    / "fixtures"
    / "importers"
    / "health"
    / "apple_health_synthetic.zip"
)
DTD_FIXTURE_ROOT = (
    Path(__file__).parent
    / "fixtures"
    / "importers"
    / "health"
    / "apple_health_synthetic_dtd"
)
REGENERATE_SCRIPT = (
    Path(__file__).parent.parent / "scripts" / "regenerate_health_day_summaries.py"
)

# The fixture's only sleep entry starts Jan 2 at 10:30 PM and ends Jan 3 at
# 6:30 AM. Under the canonical cross-midnight rule that night belongs to the
# day it ended (Jan 3, which has no records and hence no card), so the Jan 2
# card carries no Sleep line — exactly like the body day page for Jan 2.
EXPECTED_FIXTURE_DAY_CARD = """\
# Body · January 2, 2026

Glucose 105 mg/dL, 1 workout.

**Glucose** 105 mg/dL · 1 reading

**Workouts** Running · 1 workout

**Signals**

- Glucose: 1
- Resting heart rate: 1
- Sleep: 1

*Sources: Synthetic Ring Mirror, Synthetic Stelo, Synthetic Watch \
· 4 records summarized · brought in via Apple Health · import 20260103_120000*
"""


def _record_row(
    record_type: str,
    *,
    value: str | None = None,
    unit: str | None = None,
    source: str = "Synthetic Watch",
    start: str | None = None,
    end: str | None = None,
) -> dict:
    row: dict = {"kind": "record", "record_type": record_type, "source_name": source}
    if value is not None:
        row["value"] = value
    if unit is not None:
        row["unit"] = unit
    if start is not None:
        row["start_date"] = start
    if end is not None:
        row["end_date"] = end
    return row


def _workout_row(activity_type: str, *, source: str = "Synthetic Watch") -> dict:
    return {"kind": "workout", "record_type": activity_type, "source_name": source}


def _rich_day_summary() -> apple_health._DaySummary:
    summary = apple_health._DaySummary(day="20260701")
    rows = [
        _record_row(
            "HKCategoryTypeIdentifierSleepAnalysis",
            start="2026-07-01 00:01:00 -0700",
            end="2026-07-01 03:00:00 -0700",
        ),
        _record_row(
            "HKCategoryTypeIdentifierSleepAnalysis",
            source="Synthetic Ring Mirror",
            start="2026-07-01 03:10:00 -0700",
            end="2026-07-01 08:05:00 -0700",
        ),
        _record_row(
            "HKQuantityTypeIdentifierBloodGlucose",
            value="77",
            unit="mg/dL",
            source="Synthetic Stelo",
        ),
        _record_row(
            "HKQuantityTypeIdentifierBloodGlucose",
            value="104",
            unit="mg/dL",
            source="Synthetic Stelo",
        ),
        _record_row(
            "HKQuantityTypeIdentifierBloodGlucose",
            value="84",
            unit="mg/dL",
            source="Synthetic Stelo",
        ),
        _record_row("HKQuantityTypeIdentifierStepCount", value="500", unit="count"),
        _record_row("HKQuantityTypeIdentifierStepCount", value="250", unit="count"),
        _record_row("HKQuantityTypeIdentifierStepCount", value="125", unit="count"),
        _record_row("HKQuantityTypeIdentifierWalkingStepLength", value="0.7", unit="m"),
        _workout_row("HKWorkoutActivityTypeRunning"),
        _workout_row("HKWorkoutActivityTypeRunning"),
        _workout_row("HKWorkoutActivityTypeRunning"),
        _workout_row("HKWorkoutActivityTypeHighIntensityIntervalTraining"),
    ]
    for row in rows:
        apple_health._add_to_day_summary(summary, row)
    apple_health._attach_night_sleep({summary.day: summary})
    return summary


def _load_regenerate_script_module():
    spec = importlib.util.spec_from_file_location(
        "regenerate_health_day_summaries", REGENERATE_SCRIPT
    )
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def test_apple_health_registered_after_pre_save_gate():
    file_importer = importlib.import_module("solstone.think.importers.file_importer")

    assert "apple_health" in file_importer.FILE_IMPORTER_REGISTRY
    assert file_importer.get_file_importer("apple_health") is not None


def test_detects_synthetic_export_directory():
    importer = AppleHealthImporter()

    assert importer.detect(FIXTURE_ROOT) is True
    assert importer.detect(FIXTURE_ROOT / "apple_health_export") is True
    assert importer.detect(Path(__file__)) is False


def test_preview_synthetic_export_directory():
    preview = AppleHealthImporter().preview(FIXTURE_ROOT)

    assert preview.date_range == ("20260101", "20260102")
    assert preview.item_count == 7
    assert preview.entity_count == 0
    assert "records=5" in preview.summary
    assert "workouts=1" in preview.summary
    assert "routes=1" in preview.summary
    assert "glucose=1" in preview.summary


def test_preview_filters_synthetic_export_by_inclusive_date_window():
    preview = AppleHealthImporter().preview(
        FIXTURE_ROOT,
        date_from="2026-01-02",
        date_to="2026-01-02",
    )

    assert preview.date_range == ("20260102", "20260102")
    assert preview.item_count == 4
    assert "records=3" in preview.summary
    assert "workouts=1" in preview.summary
    assert "routes=0" in preview.summary
    assert "glucose=1" in preview.summary


def test_preview_parses_synthetic_export_with_internal_dtd_subset():
    preview = AppleHealthImporter().preview(DTD_FIXTURE_ROOT)

    assert preview.date_range == ("20260410", "20260411")
    assert preview.item_count == 3
    assert "records=2" in preview.summary
    assert "workouts=1" in preview.summary
    assert "export_cda=present" in preview.summary
    assert "electrocardiograms=2" in preview.summary


def test_preview_reports_cda_and_ecg_files_by_name_only():
    preview = AppleHealthImporter().preview(DTD_FIXTURE_ROOT)

    assert "export_cda=present" in preview.summary
    assert "electrocardiograms=2" in preview.summary


def test_dry_run_process_returns_preview_without_files(tmp_path: Path):
    result = AppleHealthImporter().process(
        FIXTURE_ROOT,
        tmp_path,
        import_id="20260102_123000",
        dry_run=True,
    )

    assert result.entries_written == 0
    assert result.entities_seeded == 0
    assert result.files_created == []
    assert result.date_range == ("20260101", "20260102")
    assert "Dry run only" in result.summary
    assert not (tmp_path / "imports").exists()


def test_save_mode_writes_raw_source_normalized_rows_and_dedupe_to_journal_root(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
):
    live_journal = tmp_path / "live-journal"
    journal = tmp_path / "synthetic-journal"
    monkeypatch.setenv("SOLSTONE_JOURNAL", str(live_journal))

    result = AppleHealthImporter().process(
        FIXTURE_ROOT,
        journal,
        import_id="20260103_120000",
        dry_run=False,
        date_from="20260102",
        date_to="20260102",
    )

    raw_export = (
        journal
        / "imports"
        / "20260103_120000"
        / "raw"
        / "apple_health_export"
        / "export.xml"
    )
    normalized = (
        journal / "imports" / "20260103_120000" / "normalized" / "2026-01.jsonl"
    )
    rows = _read_jsonl(normalized)
    glucose_row = next(
        row
        for row in rows
        if row["record_type"] == "HKQuantityTypeIdentifierBloodGlucose"
    )
    dedupe_row = get_health_dedupe_record(journal, glucose_row["dedupe_key"])

    assert result.entries_written == 4
    assert result.entities_seeded == 0
    assert result.files_created == []
    assert result.segments is None
    assert result.date_range == ("20260102", "20260102")
    assert raw_export.read_text(encoding="utf-8").startswith("<?xml")
    assert {row["day"] for row in rows} == {"20260102"}
    assert {row["kind"] for row in rows} == {"record", "workout"}
    assert all(row["import_id"] == "20260103_120000" for row in rows)
    assert dedupe_row is not None
    assert dedupe_row["last_seen_import_id"] == "20260103_120000"
    assert dedupe_row["normalized_ref"] == glucose_row["normalized_ref"]
    assert dedupe_row["raw_ref"] == glucose_row["raw_ref"]
    assert not live_journal.exists()


def test_save_mode_writes_opt_in_day_summary_files_only_in_files_created(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
):
    monkeypatch.setenv("SOLSTONE_JOURNAL", str(tmp_path))

    result = AppleHealthImporter().process(
        FIXTURE_ROOT,
        tmp_path,
        import_id="20260103_120000",
        dry_run=False,
        date_from="2026-01-02",
        date_to="2026-01-02",
        with_day_summaries=True,
    )

    assert len(result.files_created) == 1
    summary_path = Path(result.files_created[0])
    assert summary_path == (
        tmp_path
        / "chronicle"
        / "20260102"
        / "import.apple_health"
        / "000000_300"
        / "day_summary_transcript.md"
    )
    assert result.segments == [("20260102", "000000_300")]

    from solstone.think.cluster import cluster_segments
    from solstone.think.pipeline_health import (
        classify_segment_completion,
        read_segment_progress,
    )

    clustered = cluster_segments("20260102")
    assert clustered == [
        {
            "key": "000000_300",
            "start": "00:00",
            "end": "00:05",
            "types": ["markdown"],
            "stream": "import.apple_health",
            "data_state": {"markdown": "analyzed"},
        }
    ]
    completion = classify_segment_completion(
        clustered, read_segment_progress("20260102")
    )
    assert completion.blockers == []
    assert completion.not_sensed == 0
    assert completion.not_thought == 0

    summary = summary_path.read_text(encoding="utf-8")
    assert summary == EXPECTED_FIXTURE_DAY_CARD


def test_day_summary_card_has_lede_and_provenance_footer(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
):
    monkeypatch.setenv("SOLSTONE_JOURNAL", str(tmp_path))

    AppleHealthImporter().process(
        FIXTURE_ROOT,
        tmp_path,
        import_id="20260103_120000",
        dry_run=False,
        date_from="2026-01-02",
        date_to="2026-01-02",
        with_day_summaries=True,
    )

    summary_path = (
        tmp_path
        / "chronicle"
        / "20260102"
        / "import.apple_health"
        / "000000_300"
        / "day_summary_transcript.md"
    )
    lines = summary_path.read_text(encoding="utf-8").splitlines()

    assert lines[0] == "# Body · January 2, 2026"
    assert lines[2] == "Glucose 105 mg/dL, 1 workout."
    footer = lines[-1]
    assert "import 20260103_120000" in footer
    assert "brought in via Apple Health" in footer
    assert "4 records summarized" in footer


def test_render_day_summary_owner_card_structure():
    # Two sources cover the night: Synthetic Watch 12:01–3:00 AM and
    # Synthetic Ring Mirror 3:10–8:05 AM. The canonical rule never sums
    # sources — the longest-coverage source (Ring Mirror) is the day's
    # sleep, matching what the body day page shows for the same rows.
    rendered = apple_health._render_day_summary(
        _rich_day_summary(), import_id="20260704_090000"
    )

    assert rendered == (
        "# Body · July 1, 2026\n"
        "\n"
        "Slept 3:10 AM – 8:05 AM, glucose 77–104 mg/dL (avg 88.3), 4 workouts.\n"
        "\n"
        "**Sleep** 3:10 AM – 8:05 AM · 4h 55m · 1 sleep entry\n"
        "\n"
        "**Glucose** 77–104 mg/dL · avg 88.3 · 3 readings\n"
        "\n"
        "**Workouts** High intensity interval training, Running · 4 workouts\n"
        "\n"
        "**Signals**\n"
        "\n"
        "- Glucose: 3\n"
        "- Step count: 3\n"
        "- Sleep: 2\n"
        "- Walking step length: 1\n"
        "\n"
        "*Sources: Synthetic Ring Mirror, Synthetic Stelo, Synthetic Watch"
        " · 13 records summarized · brought in via Apple Health"
        " · import 20260704_090000*"
    )


def test_render_day_summary_uses_friendly_names_never_raw_identifiers():
    rendered = apple_health._render_day_summary(
        _rich_day_summary(), import_id="20260704_090000"
    )

    assert "HKQuantityTypeIdentifier" not in rendered
    assert "HKCategoryTypeIdentifier" not in rendered
    assert "HKWorkoutActivityType" not in rendered
    assert "- Glucose: 3" in rendered
    assert "- Step count: 3" in rendered
    assert "- Sleep: 2" in rendered
    assert "- Walking step length: 1" in rendered
    assert "High intensity interval training" in rendered


def test_render_day_summary_avoids_surveillance_words():
    rendered = apple_health._render_day_summary(
        _rich_day_summary(), import_id="20260704_090000"
    ).lower()

    for banned in ("capture", "track", "monitor", "collect"):
        assert banned not in rendered
    assert re.search(r"\brecorded\b", rendered) is None


def test_render_day_summary_signals_only_day_uses_entry_count_lede():
    summary = apple_health._DaySummary(day="20260101")
    apple_health._add_to_day_summary(
        summary,
        _record_row("HKQuantityTypeIdentifierStepCount", value="500", unit="count"),
    )
    apple_health._add_to_day_summary(
        summary,
        _record_row("HKQuantityTypeIdentifierHeartRate", value="62", unit="count/min"),
    )

    rendered = apple_health._render_day_summary(summary, import_id="20260103_120000")
    lines = rendered.splitlines()

    assert lines[0] == "# Body · January 1, 2026"
    assert lines[2] == "2 entries across 2 signals."
    assert "**Sleep**" not in rendered
    assert "**Glucose**" not in rendered
    assert "**Workouts**" not in rendered
    assert "- Heart rate: 1" in rendered
    assert "- Step count: 1" in rendered


def test_render_day_summary_trims_signals_to_top_six_with_more_line():
    summary = apple_health._DaySummary(day="20260701")
    summary.record_count = 45
    summary.type_counts = Counter(
        {
            "HKQuantityTypeIdentifierStepCount": 9,
            "HKQuantityTypeIdentifierHeartRate": 8,
            "HKQuantityTypeIdentifierRespiratoryRate": 7,
            "HKQuantityTypeIdentifierOxygenSaturation": 6,
            "HKQuantityTypeIdentifierWalkingStepLength": 5,
            "HKQuantityTypeIdentifierFlightsClimbed": 4,
            "HKQuantityTypeIdentifierBodyMass": 3,
            "HKQuantityTypeIdentifierHeight": 2,
            "HKQuantityTypeIdentifierVO2Max": 1,
        }
    )

    rendered = apple_health._render_day_summary(summary, import_id="20260704_090000")
    bullets = [line for line in rendered.splitlines() if line.startswith("- ")]

    assert bullets == [
        "- Step count: 9",
        "- Heart rate: 8",
        "- Respiratory rate: 7",
        "- Blood oxygen: 6",
        "- Walking step length: 5",
        "- Flights climbed: 4",
    ]
    assert "…and 3 more signals" in rendered
    assert "Body mass" not in rendered
    assert "Height" not in rendered
    assert "VO2 max" not in rendered


def test_render_day_summary_more_line_singular_when_one_signal_hidden():
    summary = apple_health._DaySummary(day="20260701")
    summary.record_count = 28
    summary.type_counts = Counter(
        {
            "HKQuantityTypeIdentifierStepCount": 7,
            "HKQuantityTypeIdentifierHeartRate": 6,
            "HKQuantityTypeIdentifierRespiratoryRate": 5,
            "HKQuantityTypeIdentifierOxygenSaturation": 4,
            "HKQuantityTypeIdentifierWalkingStepLength": 3,
            "HKQuantityTypeIdentifierFlightsClimbed": 2,
            "HKQuantityTypeIdentifierBodyMass": 1,
        }
    )

    rendered = apple_health._render_day_summary(summary, import_id="20260704_090000")

    assert "…and 1 more signal" in rendered
    assert "more signals" not in rendered


def test_render_day_summary_no_more_line_when_six_or_fewer_signals():
    rendered = apple_health._render_day_summary(
        _rich_day_summary(), import_id="20260704_090000"
    )

    # Four signal types on this day — every one lists, no trim line.
    assert "- Walking step length: 1" in rendered
    assert "more signal" not in rendered


def test_render_day_summary_formats_counts_with_thousands_separators():
    summary = apple_health._DaySummary(day="20260701")
    summary.record_count = 1234
    summary.type_counts = Counter({"HKQuantityTypeIdentifierStepCount": 1234})

    rendered = apple_health._render_day_summary(summary, import_id="20260704_090000")

    assert "- Step count: 1,234" in rendered
    assert "1,234 records summarized" in rendered


# --- Canonical sleep: shared helpers, card == day page ------------------------


TZ = dt.timezone(dt.timedelta(hours=-6))


def _at(month: int, day: int, hour: int, minute: int = 0) -> dt.datetime:
    return dt.datetime(2026, month, day, hour, minute, tzinfo=TZ)


CROSS_MIDNIGHT_EXPORT_RECORDS = """
  <Record type="HKCategoryTypeIdentifierSleepAnalysis" sourceName="Synthetic Ring" sourceVersion="1.0" creationDate="2026-07-01 07:10:00 -0600" startDate="2026-06-30 22:58:00 -0600" endDate="2026-07-01 02:00:00 -0600" value="HKCategoryValueSleepAnalysisAsleepCore"/>
  <Record type="HKCategoryTypeIdentifierSleepAnalysis" sourceName="Synthetic Ring" sourceVersion="1.0" creationDate="2026-07-01 07:10:00 -0600" startDate="2026-07-01 02:30:00 -0600" endDate="2026-07-01 07:08:00 -0600" value="HKCategoryValueSleepAnalysisAsleepDeep"/>
  <Record type="HKCategoryTypeIdentifierSleepAnalysis" sourceName="Synthetic Wrist" sourceVersion="1.0" creationDate="2026-07-01 06:35:00 -0600" startDate="2026-06-30 23:30:00 -0600" endDate="2026-07-01 06:30:00 -0600" value="HKCategoryValueSleepAnalysisAsleepUnspecified"/>
  <Record type="HKQuantityTypeIdentifierHeartRate" sourceName="Synthetic Watch" sourceVersion="1.0" unit="count/min" creationDate="2026-07-01 09:00:00 -0600" startDate="2026-07-01 09:00:00 -0600" endDate="2026-07-01 09:00:00 -0600" value="61"/>
"""


def _write_cross_midnight_export(root: Path) -> Path:
    export_root = root / "apple_health_export"
    export_root.mkdir(parents=True)
    (export_root / "export.xml").write_text(
        '<?xml version="1.0" encoding="UTF-8"?>\n'
        "<!DOCTYPE HealthData>\n"
        f'<HealthData locale="en_US">{CROSS_MIDNIGHT_EXPORT_RECORDS}</HealthData>\n',
        encoding="utf-8",
    )
    return root


def test_merge_sleep_sessions_joins_gaps_up_to_the_limit():
    merged = merge_sleep_sessions(
        [
            (_at(7, 1, 2, 30), _at(7, 1, 7, 8)),  # unsorted on purpose
            (_at(6, 30, 22, 58), _at(7, 1, 2, 0)),  # 30-minute wake gap
            (_at(7, 1, 14, 0), _at(7, 1, 14, 45)),  # afternoon nap, far apart
        ]
    )

    assert merged == [
        (_at(6, 30, 22, 58), _at(7, 1, 7, 8)),
        (_at(7, 1, 14, 0), _at(7, 1, 14, 45)),
    ]


def test_merge_sleep_sessions_keeps_gaps_beyond_the_limit_apart():
    merged = merge_sleep_sessions(
        [
            (_at(7, 1, 22, 0), _at(7, 1, 23, 0)),
            (_at(7, 2, 0, 1), _at(7, 2, 6, 0)),  # 61-minute gap
        ]
    )

    assert merged == [
        (_at(7, 1, 22, 0), _at(7, 1, 23, 0)),
        (_at(7, 2, 0, 1), _at(7, 2, 6, 0)),
    ]


def test_pick_main_session_applies_noon_rule_and_naps():
    day = dt.date(2026, 7, 1)
    night = (_at(6, 30, 22, 58), _at(7, 1, 7, 8))
    nap = (_at(7, 1, 14, 0), _at(7, 1, 14, 45))
    tonight = (_at(7, 1, 23, 0), _at(7, 2, 6, 0))  # ends tomorrow: not today's

    main, naps = pick_main_session([nap, night, tonight], day)

    assert main == night
    assert naps == [nap]


def test_pick_day_sleep_prefers_longest_coverage_source():
    sleep = pick_day_sleep(
        {
            "Synthetic Wrist": [(_at(6, 30, 23, 30), _at(7, 1, 6, 30))],
            "Synthetic Ring": [(_at(6, 30, 22, 58), _at(7, 1, 7, 8))],
        },
        dt.date(2026, 7, 1),
    )

    assert sleep is not None
    assert sleep.source == "Synthetic Ring"
    assert sleep.other_sources == ("Synthetic Wrist",)
    assert sleep.main == (_at(6, 30, 22, 58), _at(7, 1, 7, 8))
    assert sleep.naps == ()


def test_day_card_sleep_matches_body_day_page_for_cross_midnight_session(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
):
    journal = tmp_path / "journal"
    journal.mkdir()
    monkeypatch.setenv("SOLSTONE_JOURNAL", str(journal))
    export = _write_cross_midnight_export(tmp_path / "source")

    AppleHealthImporter().process(
        export,
        journal,
        import_id="20260702_080000",
        dry_run=False,
        with_day_summaries=True,
    )

    day_page = body_routes._build_health_day(journal, "20260701")
    sleep = day_page["sleep"]
    card = (
        journal
        / "chronicle"
        / "20260701"
        / "import.apple_health"
        / "000000_300"
        / "day_summary_transcript.md"
    ).read_text(encoding="utf-8")

    # The canonical rule merges the two Ring intervals across the 30-minute
    # wake gap into the night ending July 1 morning; Ring outlasts Wrist.
    assert sleep["source"] == "Synthetic Ring"
    assert sleep["window"] == "10:58 PM – 7:08 AM"
    assert sleep["duration"] == "8h 10m"
    # Card and day page answer with the SAME window and duration.
    assert (
        f"**Sleep** {sleep['window']} · {sleep['duration']} · 2 sleep entries" in card
    )
    assert card.splitlines()[2].startswith(f"Slept {sleep['window']}")

    # The night is not double-attributed: June 30 shows no sleep on either
    # surface (its card exists — the night's first interval starts there).
    prev_card = (
        journal
        / "chronicle"
        / "20260630"
        / "import.apple_health"
        / "000000_300"
        / "day_summary_transcript.md"
    ).read_text(encoding="utf-8")
    assert "**Sleep**" not in prev_card
    assert body_routes._build_health_day(journal, "20260630")["sleep"] is None


def test_regenerate_script_feeds_prev_day_sleep_into_cross_midnight_cards(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture,
):
    journal = tmp_path / "journal"
    journal.mkdir()
    monkeypatch.setenv("SOLSTONE_JOURNAL", str(journal))
    export = _write_cross_midnight_export(tmp_path / "source")
    AppleHealthImporter().process(
        export,
        journal,
        import_id="20260702_080000",
        dry_run=False,
        with_day_summaries=True,
    )
    card_path = (
        journal
        / "chronicle"
        / "20260701"
        / "import.apple_health"
        / "000000_300"
        / "day_summary_transcript.md"
    )
    original = card_path.read_text(encoding="utf-8")
    card_path.write_text("# Apple Health Summary\n\nstale\n", encoding="utf-8")

    module = _load_regenerate_script_module()
    exit_code = module.main([str(journal), "--apply"])
    output = capsys.readouterr().out
    regenerated = card_path.read_text(encoding="utf-8")

    # July 1's rebuild pulls the prior day's sleep rows — which live in the
    # prior month's shard — back in, landing byte-identical to the import.
    assert exit_code == 0
    assert "1 rewritten" in output
    assert regenerated == original
    assert "**Sleep** 10:58 PM – 7:08 AM · 8h 10m · 2 sleep entries" in regenerated


def test_regenerate_script_dry_run_reports_diff_without_writing(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture,
):
    monkeypatch.setenv("SOLSTONE_JOURNAL", str(tmp_path))
    AppleHealthImporter().process(
        FIXTURE_ROOT,
        tmp_path,
        import_id="20260103_120000",
        dry_run=False,
        with_day_summaries=True,
    )
    stale_path = (
        tmp_path
        / "chronicle"
        / "20260102"
        / "import.apple_health"
        / "000000_300"
        / "day_summary_transcript.md"
    )
    stale_content = "# Apple Health Summary\n\nstale debug dump\n"
    stale_path.write_text(stale_content, encoding="utf-8")

    module = _load_regenerate_script_module()
    exit_code = module.main([str(tmp_path)])
    output = capsys.readouterr().out

    assert exit_code == 0
    assert stale_path.read_text(encoding="utf-8") == stale_content
    assert re.search(r"20260102: old \d+ bytes -> new \d+ bytes \(changed\)", output)
    assert re.search(r"20260101: old \d+ bytes -> new \d+ bytes \(unchanged\)", output)
    assert "1 would change" in output
    assert "dry-run" in output


def test_regenerate_script_apply_rewrites_from_rows_deduped_across_bundles(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture,
):
    monkeypatch.setenv("SOLSTONE_JOURNAL", str(tmp_path))
    for import_id in ("20260103_120000", "20260104_090000"):
        AppleHealthImporter().process(
            FIXTURE_ROOT,
            tmp_path,
            import_id=import_id,
            dry_run=False,
            with_day_summaries=True,
        )
    summary_path = (
        tmp_path
        / "chronicle"
        / "20260102"
        / "import.apple_health"
        / "000000_300"
        / "day_summary_transcript.md"
    )
    summary_path.write_text("# Apple Health Summary\n\nstale\n", encoding="utf-8")

    module = _load_regenerate_script_module()
    exit_code = module.main([str(tmp_path), "--apply"])
    output = capsys.readouterr().out
    regenerated = summary_path.read_text(encoding="utf-8")

    assert exit_code == 0
    assert "1 rewritten" in output
    assert regenerated.startswith("# Body · January 2, 2026\n")
    # Rows exist in both import bundles; dedupe_key collapse keeps the count
    # at the fixture day's 4 unique records, attributed to the later bundle.
    assert "4 records summarized" in regenerated
    assert "import 20260104_090000" in regenerated

    rerun_code = module.main([str(tmp_path)])
    rerun_output = capsys.readouterr().out
    assert rerun_code == 0
    assert "0 would change" in rerun_output


def test_regenerate_script_requires_existing_journal_root(tmp_path: Path):
    module = _load_regenerate_script_module()

    with pytest.raises(SystemExit) as excinfo:
        module.main([str(tmp_path / "missing-journal")])

    assert excinfo.value.code == 2


def test_detects_and_previews_synthetic_zip_fixture():
    importer = AppleHealthImporter()

    assert ZIP_FIXTURE.exists()
    assert importer.detect(ZIP_FIXTURE) is True
    assert (
        importer.preview(ZIP_FIXTURE).summary == importer.preview(FIXTURE_ROOT).summary
    )


def test_preview_logs_byte_progress_for_large_xml_reads(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    caplog: pytest.LogCaptureFixture,
):
    export_root = tmp_path / "apple_health_export"
    export_root.mkdir()
    records = "\n".join(
        '<Record type="HKQuantityTypeIdentifierStepCount" '
        'sourceName="Synthetic Watch" startDate="2026-05-01 08:00:00 -0700" '
        'endDate="2026-05-01 08:05:00 -0700" unit="count" value="1"/>'
        for _ in range(3)
    )
    (export_root / "export.xml").write_text(
        f'<?xml version="1.0" encoding="UTF-8"?>\n'
        f"<!DOCTYPE HealthData>\n"
        f'<HealthData locale="en_US">{records}</HealthData>\n',
        encoding="utf-8",
    )
    monkeypatch.setattr(apple_health, "_BYTE_PROGRESS_LOG_INTERVAL", 128)
    caplog.set_level(logging.INFO, logger=apple_health.__name__)

    preview = AppleHealthImporter().preview(tmp_path)

    assert preview.item_count == 3
    assert any(
        "from Apple Health export.xml" in record.message for record in caplog.records
    )


def _read_jsonl(path: Path) -> list[dict]:
    return [
        json.loads(line)
        for line in path.read_text(encoding="utf-8").splitlines()
        if line.strip()
    ]
