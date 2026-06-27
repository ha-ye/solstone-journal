# SPDX-License-Identifier: AGPL-3.0-only
# Copyright (c) 2026 sol pbc

from __future__ import annotations

import json
from pathlib import Path

import pytest
from jsonschema import Draft202012Validator, ValidationError

from solstone.apps.entities.talent import detection
from solstone.apps.entities.talent.entity_digest import assemble_facet_day_digest
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


def test_pre_process_builds_multi_facet_packet_with_context(
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
    add_observation("work", "sarah_chen", "Prefers concise launch notes", "20240101")
    upsert_detection_segment(
        "work",
        DAY,
        f"{DAY}/{STREAM}/080000_300",
        [
            {
                "name": "Sarah Chen",
                "type": "Person",
                "facet_activity": "planning",
                "level": "medium",
                "contribution": "Sarah reviewed earlier design notes.",
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
    assert f"## Segment: {COMPOSITE}" in packet
    assert "### work - Professional work" in packet
    assert "This segment: planning (level: high)" in packet
    assert "### personal - Personal life" in packet
    assert "### Sarah Chen (Person) - role: attendee" in packet
    assert "Attached identity in work: Sarah Chen" in packet
    assert "Facet relationship: Backend lead" in packet
    assert "Observation (20240101): Prefers concise launch notes" in packet
    assert "Prior contribution: Sarah reviewed earlier design notes." in packet


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
                    "type": "Person",
                    "facet": "work",
                    "contribution": "Sarah reviewed the launch.",
                    "detect": True,
                },
                {
                    "name": "Background Tool",
                    "type": "Tool",
                    "facet": "work",
                    "contribution": "",
                    "detect": False,
                },
            ]
        }
    )
    with pytest.raises(ValidationError):
        validator.validate({"detections": [{"name": "Sarah Chen", "detect": "yes"}]})


def test_post_process_upserts_per_facet_and_writes_outcome(
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
                "type": "Person",
                "facet": "work",
                "contribution": "Sarah reviewed the release plan.",
                "detect": True,
            },
            {
                "name": "Family Call",
                "type": "Project",
                "facet": "personal",
                "contribution": "Family Call was scheduled.",
                "detect": True,
            },
            {
                "name": "Background Tool",
                "type": "Tool",
                "facet": "work",
                "contribution": "",
                "detect": False,
            },
        ),
        _context(),
    )

    assert result is None
    assert load_entities("work", DAY)[0]["name"] == "Sarah Chen"
    assert load_entities("personal", DAY)[0]["name"] == "Family Call"
    outcome = _outcome(seg_dir)
    assert outcome["wrote"] == 2
    assert outcome["skipped"] == 1
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


def test_post_process_drops_invalid_rows(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
):
    root = _set_journal(tmp_path, monkeypatch)
    _write_facet(root, "work", "Professional work")
    seg_dir = _write_sense(root, _sense())

    detection.post_process(
        _valid_result(
            {
                "name": "Sarah Chen",
                "type": "Person",
                "facet": "personal",
                "contribution": "Wrong facet.",
                "detect": True,
            },
            {
                "name": "Bad Bool",
                "type": "Person",
                "facet": "work",
                "contribution": "Invalid detect.",
                "detect": "yes",
            },
            {
                "type": "Person",
                "facet": "work",
                "contribution": "Missing name.",
                "detect": True,
            },
        ),
        _context(),
    )

    outcome = _outcome(seg_dir)
    assert outcome["dropped"] == 3
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
            "type": "Person",
            "facet": "work",
            "contribution": "Sarah reviewed the release plan.",
            "detect": True,
        }
    )

    detection.post_process(payload, _context())
    detection.post_process(payload, _context())

    entity = load_entities("work", DAY)[0]
    assert entity["description"] == "Sarah reviewed the release plan."
    assert len(entity["segments"]) == 1


def test_post_process_retracts_detection(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
):
    root = _set_journal(tmp_path, monkeypatch)
    _write_facet(root, "work", "Professional work")
    seg_dir = _write_sense(root, _sense())

    detection.post_process(
        _valid_result(
            {
                "name": "Sarah Chen",
                "type": "Person",
                "facet": "work",
                "contribution": "Sarah reviewed the release plan.",
                "detect": True,
            }
        ),
        _context(),
    )
    detection.post_process(
        _valid_result(
            {
                "name": "Sarah Chen",
                "type": "Person",
                "facet": "work",
                "contribution": "",
                "detect": False,
            }
        ),
        _context(),
    )

    assert load_entities("work", DAY) == []
    outcome = _outcome(seg_dir)
    assert outcome["wrote"] == 0
    assert outcome["skipped"] == 1
    assert outcome["dropped"] == 0


def test_post_process_quiet_segment_writes_all_zero_outcome_with_skipped(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
):
    root = _set_journal(tmp_path, monkeypatch)
    _write_facet(root, "work", "Professional work")
    seg_dir = _write_sense(root, _sense())

    detection.post_process(
        _valid_result(
            {
                "name": "Sarah Chen",
                "type": "Person",
                "facet": "work",
                "contribution": "",
                "detect": False,
            }
        ),
        _context(),
    )

    outcome = _outcome(seg_dir)
    assert outcome["wrote"] == 0
    assert outcome["skipped"] == 1
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
                "type": "Person",
                "facet": "work",
                "contribution": "Sarah reviewed the release plan.",
                "detect": True,
            }
        ),
        _context(),
    )

    outcome = _outcome(seg_dir)
    assert outcome["errored"] == 1
    assert "RuntimeError: boom" == outcome["error"]


def test_forward_compat_detection_rows_load_and_digest(
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
                        "segments": [
                            {
                                "segment": COMPOSITE,
                                "facet_activity": "planning",
                                "level": "high",
                                "contribution": "Sarah reviewed the release plan.",
                            }
                        ],
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
    assert rows[0]["segments"][0]["segment"] == COMPOSITE
    assert rows[0]["updated_at"] == 123
    assert any(row["name"] == "Legacy Project" for row in rows)

    digest = assemble_facet_day_digest("work", DAY)
    assert "Sarah Chen (Person): Sarah reviewed the release plan." in digest
    assert "Legacy Project (Project): Legacy project row." in digest
