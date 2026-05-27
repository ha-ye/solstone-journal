# SPDX-License-Identifier: AGPL-3.0-only
# Copyright (c) 2026 sol pbc

import json
import logging
from pathlib import Path


def _write_detected_entities(
    tmp_path: Path, facet: str, day: str, rows: list[dict]
) -> None:
    entities_path = tmp_path / "facets" / facet / "entities" / f"{day}.jsonl"
    entities_path.parent.mkdir(parents=True, exist_ok=True)
    entities_path.write_text(
        "".join(json.dumps(row, ensure_ascii=False) + "\n" for row in rows),
        encoding="utf-8",
    )


def _write_segment_talent_files(
    journal: Path,
    day: str,
    segment: str,
    *,
    sense: dict | None = None,
    speaker_labels: dict | None = None,
    speakers: list[str] | None = None,
) -> Path:
    stream_dir = journal / "chronicle" / day / "default" / segment / "talents"
    stream_dir.mkdir(parents=True, exist_ok=True)
    if sense is not None:
        (stream_dir / "sense.json").write_text(json.dumps(sense))
    if speaker_labels is not None:
        (stream_dir / "speaker_labels.json").write_text(json.dumps(speaker_labels))
    if speakers is not None:
        (stream_dir / "speakers.json").write_text(json.dumps(speakers))
    return stream_dir


def _sense_payload() -> dict:
    return {"meeting_detected": True}


def _activity_record(segments: list[str]) -> dict:
    return {
        "id": "meeting_090000_300",
        "activity": "meeting",
        "segments": segments,
        "level_avg": 1.0,
        "description": "Team sync",
        "active_entities": ["Mike"],
        "created_at": 1,
    }


def _participation_result(
    *,
    name: str = "Mike",
    role: str = "attendee",
    source: str = "voice",
) -> str:
    return json.dumps(
        {
            "participation": [
                {
                    "name": name,
                    "role": role,
                    "source": source,
                    "confidence": 0.91,
                    "context": "Spoke during the meeting",
                    "entity_id": None,
                }
            ]
        }
    )


def _seed_mike(tmp_path: Path, facet: str, day: str) -> None:
    _write_detected_entities(
        tmp_path,
        facet,
        day,
        [
            {
                "id": "ent-mike",
                "type": "Person",
                "name": "Mike Smith",
                "aka": ["Mike"],
            }
        ],
    )


def _run_participation(
    tmp_path: Path,
    monkeypatch,
    *,
    segments: list[str],
    result: str,
    seed_entities: bool = True,
):
    from solstone.talent.participation import post_process
    from solstone.think.activities import append_activity_record, load_activity_records

    facet = "work"
    day = "20260418"
    monkeypatch.setenv("SOLSTONE_JOURNAL", str(tmp_path))
    if seed_entities:
        _seed_mike(tmp_path, facet, day)
    activity = _activity_record(segments)
    append_activity_record(facet, day, activity)
    post_process(result, {"activity": activity, "facet": facet, "day": day})
    return load_activity_records(facet, day)[0]


def test_ac1_demotes_voice_attendee_without_evidence(tmp_path, monkeypatch):
    segment = "090000_300"
    _write_segment_talent_files(tmp_path, "20260418", segment, sense=_sense_payload())

    record = _run_participation(
        tmp_path,
        monkeypatch,
        segments=[segment],
        result=_participation_result(source="voice"),
    )

    assert record["participation"][0]["role"] == "mentioned"


def test_ac2_demotes_speaker_label_attendee_without_evidence(tmp_path, monkeypatch):
    segment = "090000_300"
    _write_segment_talent_files(tmp_path, "20260418", segment, sense=_sense_payload())

    record = _run_participation(
        tmp_path,
        monkeypatch,
        segments=[segment],
        result=_participation_result(source="speaker_label"),
    )

    assert record["participation"][0]["role"] == "mentioned"


def test_ac3_preserves_speaker_label_entity_id_match(tmp_path, monkeypatch):
    segment = "090000_300"
    _write_segment_talent_files(
        tmp_path,
        "20260418",
        segment,
        sense=_sense_payload(),
        speaker_labels={
            "labels": [
                {
                    "sentence_id": 1,
                    "speaker": "ent-mike",
                    "confidence": "high",
                    "method": "acoustic",
                }
            ]
        },
    )

    record = _run_participation(
        tmp_path,
        monkeypatch,
        segments=[segment],
        result=_participation_result(source="speaker_label"),
    )

    assert record["participation"][0]["role"] == "attendee"


def test_ac4_preserves_speakers_json_entity_match(tmp_path, monkeypatch):
    segment = "090000_300"
    _write_segment_talent_files(
        tmp_path,
        "20260418",
        segment,
        sense=_sense_payload(),
        speakers=["Mike Smith"],
    )

    record = _run_participation(
        tmp_path,
        monkeypatch,
        segments=[segment],
        result=_participation_result(source="voice"),
    )

    assert record["participation"][0]["role"] == "attendee"


def test_ac5_preserves_unresolved_casefold_name_match(tmp_path, monkeypatch):
    segment = "090000_300"
    _write_segment_talent_files(
        tmp_path,
        "20260418",
        segment,
        sense=_sense_payload(),
        speakers=["mystery guest"],
    )

    record = _run_participation(
        tmp_path,
        monkeypatch,
        segments=[segment],
        result=_participation_result(name="Mystery Guest", source="voice"),
        seed_entities=False,
    )

    assert record["participation"][0]["entity_id"] is None
    assert record["participation"][0]["role"] == "attendee"


def test_ac6_ignores_transcript_attendee(tmp_path, monkeypatch):
    segment = "090000_300"
    _write_segment_talent_files(tmp_path, "20260418", segment, sense=_sense_payload())

    record = _run_participation(
        tmp_path,
        monkeypatch,
        segments=[segment],
        result=_participation_result(source="transcript"),
    )

    assert record["participation"][0]["role"] == "attendee"


def test_ac7_ignores_screen_attendee(tmp_path, monkeypatch):
    segment = "090000_300"
    _write_segment_talent_files(tmp_path, "20260418", segment, sense=_sense_payload())

    record = _run_participation(
        tmp_path,
        monkeypatch,
        segments=[segment],
        result=_participation_result(source="screen"),
    )

    assert record["participation"][0]["role"] == "attendee"


def test_ac8_ignores_existing_mentioned_entry(tmp_path, monkeypatch):
    segment = "090000_300"
    _write_segment_talent_files(tmp_path, "20260418", segment, sense=_sense_payload())

    record = _run_participation(
        tmp_path,
        monkeypatch,
        segments=[segment],
        result=_participation_result(role="mentioned", source="voice"),
    )

    assert record["participation"][0]["role"] == "mentioned"


def test_ac9_fail_soft_missing_invalid_files(tmp_path, monkeypatch):
    segments = ["090000_300", "090500_300"]
    talents_dir = _write_segment_talent_files(
        tmp_path,
        "20260418",
        segments[1],
        sense=_sense_payload(),
    )
    (talents_dir / "speaker_labels.json").write_text(
        "{not valid json", encoding="utf-8"
    )

    record = _run_participation(
        tmp_path,
        monkeypatch,
        segments=segments,
        result=_participation_result(source="voice"),
    )

    assert record["participation"][0]["role"] == "mentioned"


def test_ac10_logs_once_with_distinct_reason(tmp_path, monkeypatch, caplog):
    segment = "090000_300"
    _write_segment_talent_files(tmp_path, "20260418", segment, sense=_sense_payload())

    with caplog.at_level(logging.WARNING, logger="solstone.talent.participation"):
        _run_participation(
            tmp_path,
            monkeypatch,
            segments=[segment],
            result=_participation_result(source="voice"),
        )

    warnings = [record.getMessage() for record in caplog.records]
    matching = [
        message
        for message in warnings
        if "no corroborating speaker evidence across activity segments" in message
    ]
    assert len(matching) == 1
    assert len(warnings) == 1


def test_ac11_second_pass_is_idempotent(tmp_path, monkeypatch, caplog):
    from solstone.talent.participation import post_process
    from solstone.think.activities import append_activity_record, load_activity_records

    facet = "work"
    day = "20260418"
    segment = "090000_300"
    monkeypatch.setenv("SOLSTONE_JOURNAL", str(tmp_path))
    _seed_mike(tmp_path, facet, day)
    _write_segment_talent_files(tmp_path, day, segment, sense=_sense_payload())
    activity = _activity_record([segment])
    append_activity_record(facet, day, activity)

    with caplog.at_level(logging.WARNING, logger="solstone.talent.participation"):
        post_process(
            _participation_result(source="voice"),
            {"activity": activity, "facet": facet, "day": day},
        )

    first = load_activity_records(facet, day)[0]
    caplog.clear()

    with caplog.at_level(logging.WARNING, logger="solstone.talent.participation"):
        post_process(
            json.dumps({"participation": first["participation"]}),
            {"activity": first, "facet": facet, "day": day},
        )

    second = load_activity_records(facet, day)[0]
    assert second["participation"] == first["participation"]
    assert caplog.records == []
