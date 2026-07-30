# SPDX-License-Identifier: AGPL-3.0-only
# Copyright (c) 2026 sol pbc

from __future__ import annotations

import json
from pathlib import Path

import numpy as np

from solstone.apps.speakers import suggest as suggest_module
from solstone.apps.speakers.discovery import SpeakerDiscoveryKernelError
from solstone.apps.speakers.suggest import (
    _parse_meetings,
    format_suggestions,
    suggest_opportunities,
)
from solstone.think.speaker_candidate_pair_review_candidates import (
    record_candidate_pair,
)
from solstone.think.speaker_keep_separate import record_keep_separate_assertion


def create_meetings_md(env, day: str, content: str) -> Path:
    chronicle_day = env.journal / "chronicle" / day
    chronicle_day.mkdir(parents=True, exist_ok=True)
    flat_day = env.journal / day
    if not flat_day.exists():
        flat_day.symlink_to(chronicle_day, target_is_directory=True)
    meetings_path = chronicle_day / "talents" / "meetings.md"
    meetings_path.parent.mkdir(parents=True, exist_ok=True)
    meetings_path.write_text(content, encoding="utf-8")
    return meetings_path


def _write_voiceprints(entity_dir: Path, embeddings: list[np.ndarray]) -> None:
    metadata = np.array(
        [
            json.dumps(
                {
                    "day": "20240101",
                    "segment_key": f"10000{i}_300",
                    "source": "mic_audio",
                    "sentence_id": i + 1,
                    "added_at": 1700000000000,
                }
            )
            for i in range(len(embeddings))
        ],
        dtype=str,
    )
    np.savez_compressed(
        entity_dir / "voiceprints.npz",
        embeddings=np.array(embeddings, dtype=np.float32),
        metadata=metadata,
    )


def test_suggest_empty_journal(speakers_env):
    speakers_env()

    assert suggest_opportunities() == {
        "status": "ok",
        "items": [],
        "issues": [],
        "markdown": "No speaker curation suggestions found.",
    }


def test_suggest_low_confidence_review(speakers_env):
    env = speakers_env()
    for idx in range(2):
        segment_key = f"1000{idx:02d}_300"
        env.create_segment("20240101", segment_key, ["mic_audio"])
        labels = []
        for sid in range(1, 13):
            labels.append(
                {
                    "sentence_id": sid,
                    "speaker": "alice_test" if sid % 2 == 0 else None,
                    "confidence": "medium" if sid % 2 == 0 else None,
                    "method": "voiceprint" if sid % 2 == 0 else None,
                }
            )
        env.create_speaker_labels("20240101", segment_key, labels)

    result = suggest_opportunities()
    results = result["items"]

    assert set(result) == {"status", "items", "issues", "markdown"}
    assert result["status"] == "ok"
    assert result["issues"] == []
    low_conf = [item for item in results if item["type"] == "low_confidence_review"]
    assert len(low_conf) == 2
    for suggestion in low_conf:
        assert suggestion["day"] == "20240101"
        assert suggestion["medium_or_null_count"] == 12
        assert suggestion["total_labels"] == 12
        assert "segment_key" in suggestion
        assert "null_proportion" in suggestion


def test_suggest_low_confidence_below_threshold(speakers_env):
    env = speakers_env()
    for idx in range(2):
        segment_key = f"1100{idx:02d}_300"
        env.create_segment("20240101", segment_key, ["mic_audio"])
        env.create_speaker_labels(
            "20240101",
            segment_key,
            [
                {
                    "sentence_id": 1,
                    "speaker": "alice_test",
                    "confidence": "medium",
                    "method": "voiceprint",
                },
                {
                    "sentence_id": 2,
                    "speaker": None,
                    "confidence": None,
                    "method": None,
                },
            ],
        )

    result = suggest_opportunities()
    results = result["items"]

    assert result["status"] == "ok"
    assert result["issues"] == []
    assert all(item["type"] != "low_confidence_review" for item in results)


def test_suggest_name_variant(speakers_env):
    env = speakers_env()
    alice_dir = env.create_entity("Alice")
    alice_test_dir = env.create_entity("Alice Test")

    base = env.create_embedding([1.0, 0.0, 0.0])
    similar = env.create_embedding([1.0, 0.01, 0.0])
    _write_voiceprints(alice_dir, [base, similar])
    _write_voiceprints(alice_test_dir, [similar, base])

    result = suggest_opportunities()
    results = result["items"]

    assert result["status"] == "ok"
    assert result["issues"] == []
    suggestion = next(item for item in results if item["type"] == "name_variant")
    assert suggestion["source"] == {"id": "alice", "name": "Alice"}
    assert suggestion["target"] == {"id": "alice_test", "name": "Alice Test"}
    assert suggestion["similarity"] > 0.90
    assert suggestion["readiness"] == "ready"


def test_suggest_name_variant_respects_keep_separate(speakers_env):
    env = speakers_env()
    alice_dir = env.create_entity("Alice")
    alice_test_dir = env.create_entity("Alice Test")

    base = env.create_embedding([1.0, 0.0, 0.0])
    similar = env.create_embedding([1.0, 0.01, 0.0])
    _write_voiceprints(alice_dir, [base, similar])
    _write_voiceprints(alice_test_dir, [similar, base])
    record_keep_separate_assertion(
        "alice",
        "alice_test",
        source_kind="explicit_create_near_match",
        operation_id="idop_test",
        detection_count=1,
    )

    result = suggest_opportunities()
    results = result["items"]

    assert result["status"] == "ok"
    assert result["issues"] == []
    assert all(item["type"] != "name_variant" for item in results)


def test_suggest_speaker_candidate_pair_and_formats_it(speakers_env):
    speakers_env()
    anchor_a = '["20260101","090000_300","test","mic_audio",1]'
    anchor_b = '["20260102","090000_300","test","mic_audio",2]'
    record_candidate_pair(
        source_anchor=anchor_a,
        target_anchor=anchor_b,
        source_anchors={anchor_a},
        target_anchors={anchor_b},
        similarity=0.62,
        source_intervals=31,
        target_intervals=35,
        source_samples=[],
        target_samples=[],
    )

    result = suggest_opportunities()
    results = result["items"]
    suggestion = next(
        item for item in results if item["type"] == "speaker_candidate_pair"
    )
    rendered = format_suggestions([suggestion])

    assert suggestion["similarity"] == 0.62
    assert suggestion["source_intervals"] == 31
    assert suggestion["target_intervals"] == 35
    assert rendered
    assert "Speaker candidate pair: similarity 0.62 (31 vs 35 intervals)" in rendered


def test_suggest_import_linkable(speakers_env):
    env = speakers_env()
    env.create_entity("Romeo Montague")
    create_meetings_md(
        env,
        "20240101",
        "# Meetings\n\n- 10:00 Strategy Call with Romeo and Juliet\n",
    )

    result = suggest_opportunities()
    results = result["items"]

    assert result["status"] == "ok"
    assert result["issues"] == []
    suggestion = next(item for item in results if item["type"] == "import_linkable")
    assert suggestion["entity_id"] == "romeo_montague"
    assert suggestion["name"] == "Romeo Montague"
    assert suggestion["meetings_mentioned"] == 1
    assert suggestion["meeting_days"] == ["20240101"]


def test_suggest_import_linkable_with_voiceprint_excluded(speakers_env):
    env = speakers_env()
    entity_dir = env.create_entity("Romeo Montague")
    _write_voiceprints(entity_dir, [env.create_embedding([1.0, 0.0, 0.0])])
    create_meetings_md(
        env,
        "20240101",
        "# Meetings\n\n- 10:00 Strategy Call with Romeo and Juliet\n",
    )

    result = suggest_opportunities()
    results = result["items"]

    assert result["status"] == "ok"
    assert result["issues"] == []
    assert all(
        not (
            item["type"] == "import_linkable" and item["entity_id"] == "romeo_montague"
        )
        for item in results
    )


def test_suggest_limit(speakers_env):
    env = speakers_env()
    env.create_entity("Romeo Montague")
    alice_dir = env.create_entity("Alice")
    alice_test_dir = env.create_entity("Alice Test")
    _write_voiceprints(alice_dir, [env.create_embedding([1.0, 0.0, 0.0])])
    _write_voiceprints(alice_test_dir, [env.create_embedding([1.0, 0.01, 0.0])])
    create_meetings_md(
        env,
        "20240101",
        "# Meetings\n\n- 10:00 Strategy Call with Romeo and Juliet\n",
    )
    for idx in range(4):
        segment_key = f"1200{idx:02d}_300"
        env.create_segment("20240101", segment_key, ["mic_audio"])
        env.create_speaker_labels(
            "20240101",
            segment_key,
            [
                {
                    "sentence_id": sid,
                    "speaker": None,
                    "confidence": None,
                    "method": None,
                }
                for sid in range(1, 4)
            ],
        )

    result = suggest_opportunities(limit=1)
    results = result["items"]

    assert set(result) == {"status", "items", "issues", "markdown"}
    assert result["status"] == "ok"
    assert result["issues"] == []
    assert len(results) == 1


def test_suggest_priority_order(speakers_env):
    env = speakers_env()
    env.create_entity("Romeo Montague")
    alice_dir = env.create_entity("Alice")
    alice_test_dir = env.create_entity("Alice Test")
    _write_voiceprints(alice_dir, [env.create_embedding([1.0, 0.0, 0.0])])
    _write_voiceprints(alice_test_dir, [env.create_embedding([1.0, 0.01, 0.0])])
    create_meetings_md(
        env,
        "20240101",
        "# Meetings\n\n- 10:00 Strategy Call with Romeo and Juliet\n",
    )
    for idx in range(4):
        segment_key = f"1300{idx:02d}_300"
        env.create_segment("20240101", segment_key, ["mic_audio"])
        env.create_speaker_labels(
            "20240101",
            segment_key,
            [
                {
                    "sentence_id": sid,
                    "speaker": None,
                    "confidence": None,
                    "method": None,
                }
                for sid in range(1, 13)
            ],
        )

    result = suggest_opportunities(limit=3)
    results = result["items"]

    assert set(result) == {"status", "items", "issues", "markdown"}
    assert result["status"] == "ok"
    assert result["issues"] == []
    assert [item["type"] for item in results] == [
        "import_linkable",
        "name_variant",
        "low_confidence_review",
    ]


def test_parse_meetings_parenthesized(speakers_env):
    env = speakers_env()
    meetings_path = create_meetings_md(
        env,
        "20240101",
        "# Meetings\n\n- 08:30 Pre-Board Meeting Prep (Romeo, Juliet, Benvolio)\n",
    )

    meetings = _parse_meetings(str(meetings_path.parent.parent))

    assert meetings == [
        {
            "time": "08:30",
            "line": "- 08:30 Pre-Board Meeting Prep (Romeo, Juliet, Benvolio)",
            "participants": ["Romeo", "Juliet", "Benvolio"],
        }
    ]


def test_parse_meetings_with_keyword(speakers_env):
    env = speakers_env()
    meetings_path = create_meetings_md(
        env,
        "20240101",
        "# Meetings\n\n- 10:00 Strategy Call with Professor Lawrence, Romeo, and Juliet\n",
    )

    meetings = _parse_meetings(str(meetings_path.parent.parent))

    assert meetings == [
        {
            "time": "10:00",
            "line": "- 10:00 Strategy Call with Professor Lawrence, Romeo, and Juliet",
            "participants": ["Professor Lawrence", "Romeo", "Juliet"],
        }
    ]


def test_parse_meetings_missing_file(tmp_path):
    assert _parse_meetings(str(tmp_path)) == []


def test_format_suggestions_empty():
    assert format_suggestions([]) == "No speaker curation suggestions found."


def test_suggest_partial_generator_failure_with_empty_success_is_degraded(
    speakers_env,
    monkeypatch,
    caplog,
):
    speakers_env()

    def fail_discovery():
        raise RuntimeError("first generator failed")

    def empty_generator():
        return []

    monkeypatch.setattr(suggest_module, "_discovery_helpers", lambda: fail_discovery)
    monkeypatch.setattr(suggest_module, "_import_linkable", empty_generator)
    monkeypatch.setattr(suggest_module, "_name_variant", empty_generator)
    monkeypatch.setattr(suggest_module, "_candidate_pair_review", empty_generator)
    monkeypatch.setattr(suggest_module, "_low_confidence_review", empty_generator)
    caplog.set_level("ERROR")

    result = suggest_opportunities()

    assert result == {
        "status": "degraded",
        "items": [],
        "issues": [
            {
                "reason_code": "speaker_suggestion_generator_failed",
                "generator": "_unknown_recurring",
                "message": "i couldn't finish part of the speaker suggestions.",
            }
        ],
        "markdown": (
            "some speaker suggestions are incomplete:\n"
            "- i couldn't finish part of the speaker suggestions. (_unknown_recurring)"
        ),
    }
    assert "Suggestion generator _unknown_recurring failed" in caplog.text


def test_suggest_unknown_recurring_kernel_failure_uses_discovery_issue(
    speakers_env,
    monkeypatch,
):
    speakers_env()

    def fail_discovery():
        raise SpeakerDiscoveryKernelError(stage="invoke", reason="timeout")

    def empty_generator():
        return []

    monkeypatch.setattr(suggest_module, "_discovery_helpers", lambda: fail_discovery)
    monkeypatch.setattr(suggest_module, "_import_linkable", empty_generator)
    monkeypatch.setattr(suggest_module, "_name_variant", empty_generator)
    monkeypatch.setattr(suggest_module, "_candidate_pair_review", empty_generator)
    monkeypatch.setattr(suggest_module, "_low_confidence_review", empty_generator)

    result = suggest_opportunities()

    assert result["status"] == "degraded"
    assert result["items"] == []
    assert result["issues"] == [
        {
            "reason_code": "speaker_discovery_failed",
            "generator": "_unknown_recurring",
            "message": "i couldn't look for new voices right now.",
        }
    ]
    assert result["markdown"] == (
        "some speaker suggestions are incomplete:\n"
        "- i couldn't look for new voices right now. (_unknown_recurring)"
    )


def test_suggest_every_invoked_generator_failed_returns_failed(
    speakers_env,
    monkeypatch,
):
    speakers_env()

    def fail_generator(name: str):
        def fail():
            raise RuntimeError(f"{name} failed")

        fail.__name__ = name
        return fail

    for name in (
        "_unknown_recurring",
        "_import_linkable",
        "_name_variant",
        "_candidate_pair_review",
        "_low_confidence_review",
    ):
        monkeypatch.setattr(suggest_module, name, fail_generator(name))

    result = suggest_opportunities()

    assert result["status"] == "failed"
    assert result["items"] == []
    assert result["issues"] == [
        {
            "reason_code": "speaker_suggestion_generator_failed",
            "generator": name,
            "message": "i couldn't finish part of the speaker suggestions.",
        }
        for name in (
            "_unknown_recurring",
            "_import_linkable",
            "_name_variant",
            "_candidate_pair_review",
            "_low_confidence_review",
        )
    ]
    assert result["markdown"] == (
        "some speaker suggestions are incomplete:\n"
        "- i couldn't finish part of the speaker suggestions. (_unknown_recurring)\n"
        "- i couldn't finish part of the speaker suggestions. (_import_linkable)\n"
        "- i couldn't finish part of the speaker suggestions. (_name_variant)\n"
        "- i couldn't finish part of the speaker suggestions. (_candidate_pair_review)\n"
        "- i couldn't finish part of the speaker suggestions. (_low_confidence_review)"
    )
