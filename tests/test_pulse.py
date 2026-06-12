# SPDX-License-Identifier: AGPL-3.0-only
# Copyright (c) 2026 sol pbc

from __future__ import annotations

import json

import pytest

from solstone.talent import pulse
from solstone.think.day_accumulator import read_latest


@pytest.fixture
def journal(tmp_path, monkeypatch):
    monkeypatch.setenv("SOLSTONE_JOURNAL", str(tmp_path))
    return tmp_path


def _pulse_payload(**overrides):
    payload = {
        "title": "Focused morning",
        "one_sentence": "The morning is centered on a launch review.",
        "full_details": "The owner has a coherent launch-review block in motion.",
        "needs_you": ["Review the launch checklist."],
    }
    payload.update(overrides)
    return payload


def test_post_process_persists_valid_pulse_record(journal):
    config = {
        "day": "20260611",
        "model": "test-model",
        "_pulse_window_note": {
            "segments": 1,
            "activities": 0,
            "input_segments": 1,
            "input_activities": 0,
            "since_ms": 123,
            "gaps": [],
        },
    }

    returned = pulse.post_process(json.dumps(_pulse_payload()), config)

    summary = json.loads(returned)
    assert summary == _pulse_payload()
    record = read_latest("20260611", "pulse", lookback_days=0)
    assert record is not None
    for key, value in _pulse_payload().items():
        assert record[key] == value
    assert record["model"] == "test-model"
    assert record["generated_at"].endswith("Z")
    assert isinstance(record["ts"], int)
    assert record["window"] == config["_pulse_window_note"]


@pytest.mark.parametrize("result", ["not json", json.dumps({"title": "Incomplete"})])
def test_post_process_falls_back_for_malformed_model_output(journal, result):
    default = _pulse_payload(
        title="Fallback",
        one_sentence="Fallback sentence.",
        full_details="Fallback details.",
        needs_you=["Use the deterministic fallback."],
    )
    config = {"day": "20260611", "model": "test-model", "_pulse_default": default}

    returned = pulse.post_process(result, config)

    summary = json.loads(returned)
    assert summary == default
    record = read_latest("20260611", "pulse", lookback_days=0)
    assert record is not None
    for key, value in default.items():
        assert record[key] == value
    assert record["model"] == "test-model"
    assert "generated_at" in record
    assert "ts" in record
    assert "window" in record


def test_normalize_pulse_coerces_and_clamps_fields():
    default = _pulse_payload(
        title="Fallback",
        one_sentence="Fallback sentence.",
        full_details="Fallback details.",
        needs_you=["Fallback need."],
    )
    raw = {
        "title": "T" * 100,
        "one_sentence": "S" * 260,
        "full_details": "D" * 1900,
        "needs_you": [
            "one",
            42,
            None,
            "",
            "x" * 300,
            "five",
            "six",
            "seven",
            "eight",
            "nine",
        ],
        "ignored": "dropped",
    }

    summary = pulse._normalize_pulse(raw, default)

    assert set(summary) == {"title", "one_sentence", "full_details", "needs_you"}
    assert summary["title"] == "T" * pulse._TITLE_MAX
    assert summary["one_sentence"] == "S" * pulse._SENTENCE_MAX
    assert summary["full_details"] == "D" * pulse._DETAILS_MAX
    assert summary["needs_you"] == [
        "one",
        "42",
        "x" * pulse._NEED_MAX,
        "five",
        "six",
        "seven",
        "eight",
    ]

    assert (
        pulse._normalize_pulse(
            {
                "title": "",
                "one_sentence": " ",
                "full_details": "",
                "needs_you": ["ignored"],
            },
            default,
        )
        == default
    )


def test_pre_process_includes_segment_timeline_and_missing_gap(journal, monkeypatch):
    day = "20260611"
    segment = "101500_300"
    seg_dir = journal / "chronicle" / day / "desktop" / segment
    seg_dir.mkdir(parents=True)
    (seg_dir / "timeline.json").write_text(
        json.dumps(
            {
                "title": "Launch review",
                "description": "The segment focused on launch readiness.",
            }
        ),
        encoding="utf-8",
    )
    (journal / "identity").mkdir()
    (journal / "identity" / "partner.md").write_text(
        "Partner context", encoding="utf-8"
    )

    monkeypatch.setattr(pulse, "get_current", lambda: {"attention": "clear"})
    monkeypatch.setattr(pulse, "get_imports", lambda: {"pending": []})
    monkeypatch.setattr(pulse, "get_facets", lambda: [])
    monkeypatch.setattr(pulse, "load_recent_entity_names", lambda limit=12: ["Alice"])

    result = pulse.pre_process(
        {
            "day": day,
            "cadence_window": {
                "since_ms": 1000,
                "segments": [
                    {"stream": "desktop", "segment": segment, "ts": 2000},
                    {"stream": None, "segment": "missing_300", "ts": 1000},
                ],
                "activities": [],
            },
        }
    )

    assert result is not None
    template_vars = result["template_vars"]
    assert "Launch review" in template_vars["completed_since"]
    assert (
        "The segment focused on launch readiness." in template_vars["completed_since"]
    )
    assert "no timeline.json found for segment missing_300" in template_vars["gaps"]
    assert "Partner context" == template_vars["partner_profile"]
    assert pulse._compact_json(["Alice"]) == template_vars["recent_entities"]


def test_pre_process_total_failure_returns_skip_reason(monkeypatch):
    monkeypatch.setattr(
        pulse,
        "_completed_since",
        lambda day, config, gaps: (_ for _ in ()).throw(RuntimeError("boom")),
    )

    result = pulse.pre_process({"day": "20260611"})

    assert result is not None
    assert result["skip_reason"] == "pulse pre-hook failed: boom"


def test_accumulate_suppresses_single_file_output_path(journal):
    from solstone.think.talent import get_talent, get_talent_configs
    from solstone.think.talents import prepare_config
    from solstone.think.thinking import _apply_output_persistence

    raw_config = get_talent_configs()["pulse"]
    pulse_config = get_talent("pulse")
    assert raw_config["output"] == "json"
    assert raw_config["schema"] == "pulse.schema.json"
    assert pulse_config["output"] == "json"
    assert pulse_config["json_schema"]["required"] == [
        "title",
        "one_sentence",
        "full_details",
        "needs_you",
    ]
    assert pulse_config["accumulate"] is True

    request_config = {}
    _apply_output_persistence(request_config, pulse_config, force_refresh=False)

    assert "output" not in request_config
    assert "refresh" not in request_config

    prepared = prepare_config({"name": "pulse", "day": "20260611"})
    assert "output_path" not in prepared
