# SPDX-License-Identifier: AGPL-3.0-only
# Copyright (c) 2026 sol pbc

from __future__ import annotations

import json
from pathlib import Path

import pytest
from jsonschema import Draft202012Validator, ValidationError

from solstone.apps.entities.talent import detection
from solstone.think.entities.loading import load_entities
from solstone.think.entities.observations import add_observation
from solstone.think.entities.saving import save_entities, upsert_detection_segment

DAY = "20250101"
STREAM = "default"
SEGMENT = "090000_300"
COMPOSITE = f"{DAY}/{STREAM}/{SEGMENT}"


def _set_journal(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    monkeypatch.setenv("SOLSTONE_JOURNAL", str(tmp_path))
    import solstone.think.utils as think_utils

    think_utils._journal_path_cache = None
    return tmp_path


def _write_facet(root: Path, slug: str, description: str) -> None:
    facet_dir = root / "facets" / slug
    facet_dir.mkdir(parents=True, exist_ok=True)
    (facet_dir / "facet.json").write_text(
        json.dumps(
            {
                "title": slug.title(),
                "description": description,
                "color": "#00695c",
            }
        )
        + "\n",
        encoding="utf-8",
    )


def _sense(
    *,
    facets: list[dict] | None = None,
    entities: list[dict] | None = None,
) -> dict:
    return {
        "facets": facets
        if facets is not None
        else [{"facet": "work", "activity": "planning", "level": "high"}],
        "entities": entities
        if entities is not None
        else [
            {
                "type": "Person",
                "name": "Sarah Chen",
                "role": "attendee",
                "source": "transcript",
                "context": "Sarah reviewed the release plan.",
            }
        ],
    }


def _write_sense(root: Path, sense: dict) -> Path:
    seg_dir = root / "chronicle" / DAY / STREAM / SEGMENT
    talents = seg_dir / "talents"
    talents.mkdir(parents=True, exist_ok=True)
    (talents / "sense.json").write_text(
        json.dumps(sense) + "\n",
        encoding="utf-8",
    )
    return seg_dir


def _context() -> dict:
    return {"day": DAY, "segment": SEGMENT, "stream": STREAM}


def _outcome(seg_dir: Path) -> dict:
    return json.loads((seg_dir / "talents" / "detection_outcome.json").read_text())


def _valid_result(*rows: dict) -> str:
    return json.dumps({"detections": list(rows)})


def test_pre_process_builds_plain_packet_with_active_facet_context(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
):
    root = _set_journal(tmp_path, monkeypatch)
    _write_facet(root, "work", "Professional work")
    _write_facet(root, "personal", "Personal life")
    save_entities(
        "work",
        [{"type": "Person", "name": "Sarah Chen", "description": "Backend lead"}],
        day=None,
    )
    save_entities(
        "personal",
        [
            {
                "type": "Person",
                "name": "Sarah Chen",
                "description": "Family friend",
            }
        ],
        day=None,
    )
    add_observation("work", "sarah_chen", "Prefers concise launch notes", "20240101")
    upsert_detection_segment(
        "work",
        DAY,
        f"{DAY}/{STREAM}/080000_300",
        [
            {
                "name": "Sarah Chen",
                "type": "Person",
                "description": "Sarah reviewed earlier design notes.",
            }
        ],
    )
    _write_sense(
        root,
        _sense(
            facets=[
                {"facet": "work", "activity": "planning", "level": "high"},
                {"facet": "personal", "activity": "call", "level": "low"},
            ],
            entities=[
                {
                    "type": "Person",
                    "name": "Sarah Chen",
                    "role": "attendee",
                    "source": "transcript",
                    "context": "Sarah reviewed the release plan.",
                },
                {
                    "type": "Project",
                    "name": "New Project",
                    "role": "mentioned",
                    "source": "screen",
                    "context": "New Project appeared in notes.",
                },
            ],
        ),
    )

    result = detection.pre_process(_context())

    assert isinstance(result, dict)
    packet = result["template_vars"]["detection_packet"]
    lower_packet = packet.lower()
    for banned in (
        "segment",
        "sense",
        "candidate",
        "attached identity",
        "facet relationship",
        "observation",
        "detection",
        "contribution",
        "level:",
    ):
        assert banned not in lower_packet
    assert COMPOSITE not in packet
    assert "This is a moment from today." in packet
    assert "### work" in packet
    assert "Facet: Professional work" in packet
    assert "What happened here: planning" in packet
    assert "Why it matters: This was a main focus." in packet
    assert "### personal" in packet
    assert "Why it matters: This came up in passing." in packet
    assert "### Sarah Chen" in packet
    assert "Sarah Chen — person" not in packet  # entity type removed from packet
    assert "- In work: Backend lead" in packet
    assert "- In personal: Family friend" in packet
    assert (
        "Summary so far today in work: Sarah reviewed earlier design notes." in packet
    )
    assert "In this moment: Sarah reviewed the release plan." in packet
    assert "How central it was:" not in packet
    assert "### New Project" in packet
    assert "Summary so far today: Nothing saved yet in the active facets." in packet
    assert "Prefers concise launch notes" not in packet


def test_pre_process_surfaces_entity_centrality_cue(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
):
    root = _set_journal(tmp_path, monkeypatch)
    _write_facet(root, "work", "Professional work")
    _write_sense(
        root,
        _sense(
            facets=[{"facet": "work", "activity": "planning", "level": "high"}],
            entities=[
                {
                    "type": "Person",
                    "name": "Sarah Chen",
                    "role": "attendee",
                    "source": "transcript",
                    "context": "Sarah reviewed the release plan.",
                    "level": "high",
                },
                {
                    "type": "Project",
                    "name": "New Project",
                    "role": "mentioned",
                    "source": "screen",
                    "context": "New Project appeared in notes.",
                    "level": "low",
                },
            ],
        ),
    )

    result = detection.pre_process(_context())

    assert isinstance(result, dict)
    packet = result["template_vars"]["detection_packet"]
    assert "How central it was: central to this moment." in packet
    assert "How central it was: a peripheral mention." in packet
    assert "level:" not in packet.lower()


def test_pre_process_skip_taxonomy(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    root = _set_journal(tmp_path, monkeypatch)
    _write_facet(root, "work", "Professional work")

    assert detection.pre_process(_context()) == {"skip_reason": "no_sense"}

    _write_sense(root, _sense(facets=[], entities=[{"name": "Sarah Chen"}]))
    assert detection.pre_process(_context()) == {"skip_reason": "no_facets"}

    seg_dir = _write_sense(
        root,
        _sense(
            facets=[{"facet": "work", "activity": "planning", "level": "high"}],
            entities=[],
        ),
    )
    assert detection.pre_process(_context()) == {"skip_reason": "no_candidates"}
    assert not (seg_dir / "talents" / "detection_outcome.json").exists()


def test_detection_schema_validates_sample_and_rejects_malformed():
    schema_path = Path(__file__).parents[1] / "talent" / "detection.schema.json"
    schema = json.loads(schema_path.read_text())
    schema["properties"]["detections"]["items"]["properties"]["facet"]["enum"] = [
        "work"
    ]
    Draft202012Validator.check_schema(schema)
    validator = Draft202012Validator(schema)

    validator.validate(
        {
            "detections": [
                {
                    "name": "Sarah Chen",
                    "facet": "work",
                    "description": "Sarah reviewed the launch.",
                }
            ]
        }
    )
    with pytest.raises(ValidationError):
        validator.validate(
            {
                "detections": [
                    {
                        "name": "Sarah Chen",
                        "type": "Person",
                        "facet": "work",
                        "contribution": "Sarah reviewed the launch.",
                        "detect": True,
                    }
                ]
            }
        )
    with pytest.raises(ValidationError):
        validator.validate(
            {
                "detections": [
                    {
                        "name": "Sarah Chen",
                        "facet": "work",
                        "description": "Sarah reviewed the launch.",
                        "extra": True,
                    }
                ]
            }
        )


def test_post_process_uses_sense_type_and_writes_same_name_to_two_facets(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
):
    root = _set_journal(tmp_path, monkeypatch)
    _write_facet(root, "work", "Professional work")
    _write_facet(root, "personal", "Personal life")
    seg_dir = _write_sense(
        root,
        _sense(
            facets=[
                {"facet": "work", "activity": "planning", "level": "high"},
                {"facet": "personal", "activity": "call", "level": "medium"},
            ]
        ),
    )

    result = detection.post_process(
        _valid_result(
            {
                "name": "Sarah Chen",
                "type": "Company",
                "facet": "work",
                "description": "Sarah reviewed the release plan.",
            },
            {
                "name": "Sarah Chen",
                "facet": "personal",
                "description": "Sarah coordinated the family call.",
            },
        ),
        _context(),
    )

    assert result is None
    work = load_entities("work", DAY)[0]
    personal = load_entities("personal", DAY)[0]
    assert work["name"] == "Sarah Chen"
    assert work["type"] == "Person"
    assert work["description"] == "Sarah reviewed the release plan."
    assert work["segments"] == [COMPOSITE]
    assert personal["name"] == "Sarah Chen"
    assert personal["type"] == "Person"
    assert personal["description"] == "Sarah coordinated the family call."
    assert personal["segments"] == [COMPOSITE]
    outcome = _outcome(seg_dir)
    assert outcome["wrote"] == 2
    assert outcome["skipped"] == 0
    assert outcome["dropped"] == 0
    assert outcome["errored"] == 0
    assert outcome["error"] is None


def test_post_process_defensively_handles_bad_json(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
):
    root = _set_journal(tmp_path, monkeypatch)
    _write_facet(root, "work", "Professional work")
    seg_dir = _write_sense(root, _sense())

    assert detection.post_process("{bad json", _context()) is None

    outcome = _outcome(seg_dir)
    assert outcome["wrote"] == 0
    assert outcome["skipped"] == 0
    assert outcome["dropped"] == 0
    assert outcome["errored"] == 0


def test_post_process_drops_invalid_and_invented_rows(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
):
    root = _set_journal(tmp_path, monkeypatch)
    _write_facet(root, "work", "Professional work")
    seg_dir = _write_sense(root, _sense())

    detection.post_process(
        _valid_result(
            {
                "name": "Sarah Chen",
                "facet": "personal",
                "description": "Wrong facet.",
            },
            {
                "name": "Sarah Chen",
                "facet": "work",
                "description": 7,
            },
            {
                "name": "",
                "facet": "work",
                "description": "Missing name.",
            },
            {
                "facet": "work",
                "description": "Missing name.",
            },
            {
                "name": "Invented Person",
                "facet": "work",
                "description": "Not in the packet.",
            },
        ),
        _context(),
    )

    outcome = _outcome(seg_dir)
    assert outcome["wrote"] == 0
    assert outcome["skipped"] == 0
    assert outcome["dropped"] == 5
    assert load_entities("work", DAY) == []


def test_post_process_idempotent_reprocess(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
):
    root = _set_journal(tmp_path, monkeypatch)
    _write_facet(root, "work", "Professional work")
    _write_sense(root, _sense())
    payload = _valid_result(
        {
            "name": "Sarah Chen",
            "facet": "work",
            "description": "Sarah reviewed the release plan.",
        }
    )

    detection.post_process(payload, _context())
    detection.post_process(payload, _context())

    entity = load_entities("work", DAY)[0]
    assert entity["description"] == "Sarah reviewed the release plan."
    assert entity["segments"] == [COMPOSITE]


def test_post_process_empty_result_writes_all_zero_outcome_without_upsert(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
):
    root = _set_journal(tmp_path, monkeypatch)
    _write_facet(root, "work", "Professional work")
    seg_dir = _write_sense(root, _sense())

    def fail_upsert(*args, **kwargs):
        raise AssertionError("upsert should not be called")

    monkeypatch.setattr(detection, "upsert_detection_segment", fail_upsert)
    detection.post_process(_valid_result(), _context())

    outcome = _outcome(seg_dir)
    assert outcome["wrote"] == 0
    assert outcome["skipped"] == 0
    assert outcome["dropped"] == 0
    assert outcome["errored"] == 0


def test_post_process_records_substrate_failure(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
):
    root = _set_journal(tmp_path, monkeypatch)
    _write_facet(root, "work", "Professional work")
    seg_dir = _write_sense(root, _sense())

    def fail_upsert(*args, **kwargs):
        raise RuntimeError("boom")

    monkeypatch.setattr(detection, "upsert_detection_segment", fail_upsert)
    detection.post_process(
        _valid_result(
            {
                "name": "Sarah Chen",
                "facet": "work",
                "description": "Sarah reviewed the release plan.",
            }
        ),
        _context(),
    )

    outcome = _outcome(seg_dir)
    assert outcome["errored"] == 1
    assert "RuntimeError: boom" == outcome["error"]


def test_forward_compat_detection_rows_load(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
):
    root = _set_journal(tmp_path, monkeypatch)
    _write_facet(root, "work", "Professional work")
    path = root / "facets" / "work" / "entities" / f"{DAY}.jsonl"
    path.parent.mkdir(parents=True)
    path.write_text(
        "\n".join(
            [
                json.dumps(
                    {
                        "id": "sarah_chen",
                        "type": "Person",
                        "name": "Sarah Chen",
                        "description": "Sarah reviewed the release plan.",
                        "segments": [COMPOSITE],
                        "updated_at": 123,
                    }
                ),
                json.dumps(
                    {
                        "type": "Project",
                        "name": "Legacy Project",
                        "description": "Legacy project row.",
                    }
                ),
            ]
        )
        + "\n",
        encoding="utf-8",
    )

    rows = load_entities("work", DAY)
    assert rows[0]["segments"] == [COMPOSITE]
    assert rows[0]["updated_at"] == 123
    assert any(row["name"] == "Legacy Project" for row in rows)
