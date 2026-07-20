# SPDX-License-Identifier: AGPL-3.0-only
# Copyright (c) 2026 sol pbc

"""Tests for local-only speaker quality visibility."""

from __future__ import annotations

import socket
from collections.abc import Iterator
from pathlib import Path

import numpy as np
import pytest
from flask import Flask

from solstone.apps.speakers.quality import (
    SPEAKER_QUALITY_WINDOW_DAYS,
    get_speaker_quality_status,
)

STREAM = "test"
WORKSPACE = Path(__file__).resolve().parents[1] / "workspace.html"


@pytest.fixture(scope="module", autouse=True)
def block_real_network() -> Iterator[None]:
    patch = pytest.MonkeyPatch()

    def blocked_connect(*_args, **_kwargs):
        raise AssertionError("real network disabled in speaker quality tests")

    patch.setattr(socket.socket, "connect", blocked_connect)
    patch.setattr(socket.socket, "connect_ex", blocked_connect)
    yield
    patch.undo()


def _create_labeled_segment(
    env,
    day: str,
    segment_key: str,
    labels: list[dict],
    *,
    metadata: dict | None = None,
) -> Path:
    env.create_segment(day, segment_key, ["audio"], num_sentences=max(1, len(labels)))
    return env.create_speaker_labels(day, segment_key, labels, metadata=metadata)


def _talents_dir(env, day: str, segment_key: str) -> Path:
    return env.journal / "chronicle" / day / STREAM / segment_key / "talents"


def _write_confirmed_owner_centroid(env) -> Path:
    from solstone.apps.speakers.encoder_config import (
        OWNER_BOOTSTRAP_EVIDENCE_TIER_STANDARD,
        OWNER_MARGIN_MIN,
        OWNER_THRESHOLD,
    )

    principal_dir = env.create_entity("Self Person", is_principal=True)
    centroid = np.zeros(256, dtype=np.float32)
    centroid[0] = 1.0
    owner_path = principal_dir / "owner_centroid.npz"
    np.savez_compressed(
        owner_path,
        centroid=centroid,
        cluster_size=np.array(10, dtype=np.int32),
        threshold=np.array(OWNER_THRESHOLD, dtype=np.float32),
        margin=np.array(OWNER_MARGIN_MIN, dtype=np.float32),
        last_refreshed_at=np.array("2026-07-20T00:00:00Z"),
        created_at=np.array("2026-07-20T00:00:00Z"),
        evidence_hash=np.array("quality-fixture"),
        evidence_intra_cosine_p25=np.array(0.5, dtype=np.float32),
        evidence_tier=np.array(OWNER_BOOTSTRAP_EVIDENCE_TIER_STANDARD),
    )
    return owner_path


def _workspace_text() -> str:
    return WORKSPACE.read_text(encoding="utf-8")


def _js_function_block(text: str, name: str) -> str:
    start = text.index(f"function {name}(")
    next_function = text.find("\n  function ", start + 1)
    return text[start:] if next_function == -1 else text[start:next_function]


def test_quality_counts_each_bucket_exactly(speakers_env):
    env = speakers_env()
    day = "20240110"
    _create_labeled_segment(
        env,
        day,
        "090000_300",
        [{"sentence_id": 1, "speaker": "alice", "confidence": "high"}],
    )
    _create_labeled_segment(
        env,
        day,
        "091000_300",
        [
            {
                "sentence_id": 1,
                "speaker": "bob",
                "confidence": "medium",
                "owner_margin_declined": True,
            }
        ],
    )
    _create_labeled_segment(
        env,
        day,
        "092000_300",
        [
            {
                "sentence_id": 1,
                "speaker": None,
                "confidence": None,
                "owner_margin_declined": True,
            }
        ],
    )
    _create_labeled_segment(
        env,
        day,
        "093000_300",
        [{"sentence_id": 1, "speaker": None, "confidence": None}],
    )
    _create_labeled_segment(
        env,
        day,
        "094000_300",
        [],
        metadata={"skipped": True, "reason": "fixture"},
    )
    env.create_segment(day, "095000_300", ["audio"])
    _create_labeled_segment(env, day, "100000_300", [])
    env.create_speaker_corrections(
        day,
        "090000_300",
        [{"sentence_id": 1, "original_speaker": None, "corrected_speaker": "alice"}],
    )

    result = get_speaker_quality_status()

    assert result["quality_window_days"] == SPEAKER_QUALITY_WINDOW_DAYS
    assert result["quality_window_count"] == 1
    assert result["quality_window_error_count"] == 0
    assert result["tier_histogram"] == {
        "high_statements": 1,
        "medium_statements": 1,
        "margin_declined_statements": 1,
        "unlabeled_sentence_statements": 1,
        "skipped_stub_segments": 1,
        "no_labels_file_segments": 1,
    }
    assert result["empty_labels_without_skipped_segments"] == 1
    assert result["demotions_by_class"] == {
        "owner_margin_declined": {
            "high_statements": 0,
            "medium_statements": 1,
            "none_statements": 1,
            "total_statements": 2,
        },
        "acoustic_margin_declined": {
            "high_statements": 0,
            "medium_statements": 0,
            "none_statements": 0,
            "total_statements": 0,
        },
    }
    assert result["corrections_window_count"] == 1


def test_quality_counts_medium_acoustic_demotion_without_double_counting(
    speakers_env,
):
    env = speakers_env()
    _create_labeled_segment(
        env,
        "20240110",
        "090000_300",
        [
            {
                "sentence_id": 1,
                "speaker": "alice",
                "confidence": "medium",
                "acoustic_margin_declined": True,
            }
        ],
    )

    result = get_speaker_quality_status()

    assert result["tier_histogram"]["medium_statements"] == 1
    assert result["tier_histogram"]["margin_declined_statements"] == 0
    assert (
        sum(
            result["tier_histogram"][field]
            for field in (
                "high_statements",
                "medium_statements",
                "margin_declined_statements",
                "unlabeled_sentence_statements",
            )
        )
        == 1
    )
    assert result["demotions_by_class"]["acoustic_margin_declined"] == {
        "high_statements": 0,
        "medium_statements": 1,
        "none_statements": 0,
        "total_statements": 1,
    }
    assert (
        result["demotions_by_class"]["owner_margin_declined"]["total_statements"] == 0
    )


def test_quality_fresh_journal_is_prebootstrap_zero_payload(speakers_env):
    env = speakers_env()
    awareness_dir = env.journal / "awareness"
    assert not awareness_dir.exists()

    fresh = get_speaker_quality_status()

    assert not awareness_dir.exists()
    assert fresh["owner_voice"]["bootstrap_state"] == "pre_bootstrap"
    assert fresh["tier_histogram"] == {
        "high_statements": 0,
        "medium_statements": 0,
        "margin_declined_statements": 0,
        "unlabeled_sentence_statements": 0,
        "skipped_stub_segments": 0,
        "no_labels_file_segments": 0,
    }
    assert fresh["empty_labels_without_skipped_segments"] == 0
    assert fresh["corrections_window_count"] == 0
    assert fresh["unreadable_files"]["total_window_count"] == 0
    assert fresh["quality_window_error_count"] == 0
    assert all(
        value == 0
        for demotion in fresh["demotions_by_class"].values()
        for value in demotion.values()
    )

    _write_confirmed_owner_centroid(env)
    _create_labeled_segment(
        env,
        "20240110",
        "090000_300",
        [{"sentence_id": 1, "speaker": "alice", "confidence": "high"}],
    )
    true_zero = get_speaker_quality_status()
    assert true_zero["owner_voice"]["bootstrap_state"] == "bootstrapped"
    assert true_zero["tier_histogram"]["high_statements"] == 1
    assert true_zero["corrections_window_count"] == 0
    assert true_zero["unreadable_files"]["total_window_count"] == 0

    corrupt_talents = _talents_dir(env, "20240110", "091000_300")
    env.create_segment("20240110", "091000_300", ["audio"])
    corrupt_talents.mkdir(parents=True, exist_ok=True)
    (corrupt_talents / "speaker_labels.json").write_text("{", encoding="utf-8")
    (corrupt_talents / "speaker_corrections.json").write_text("{", encoding="utf-8")
    partially_corrupt = get_speaker_quality_status()
    assert partially_corrupt["owner_voice"]["bootstrap_state"] == "bootstrapped"
    assert partially_corrupt["tier_histogram"]["high_statements"] == 1
    assert partially_corrupt["corrections_window_count"] == 0
    assert partially_corrupt["unreadable_files"]["total_window_count"] == 2

    render_keys = [
        (
            payload["owner_voice"]["bootstrap_state"],
            payload["corrections_window_count"],
            payload["unreadable_files"]["total_window_count"],
        )
        for payload in (fresh, true_zero, partially_corrupt)
    ]
    assert render_keys == [
        ("pre_bootstrap", 0, 0),
        ("bootstrapped", 0, 0),
        ("bootstrapped", 0, 2),
    ]
    assert len(set(render_keys)) == 3


def test_quality_ignores_screen_only_segments_without_labels(speakers_env):
    env = speakers_env()
    day = "20240110"
    env.create_segment(day, "090000_300", ["screen"])
    env.create_segment(day, "091000_300", ["audio"])

    result = get_speaker_quality_status()

    assert result["tier_histogram"]["no_labels_file_segments"] == 1
    assert result["tier_histogram"]["skipped_stub_segments"] == 0
    assert result["quality_window_count"] == 1


def test_quality_uses_sorted_present_day_window(speakers_env):
    env = speakers_env()
    for index in range(SPEAKER_QUALITY_WINDOW_DAYS + 1):
        day = f"202401{index + 1:02d}"
        (env.journal / "chronicle" / day).mkdir(parents=True, exist_ok=True)

    outside_day = "20240101"
    inside_day = "20240102"
    _create_labeled_segment(
        env,
        outside_day,
        "090000_300",
        [{"sentence_id": 1, "speaker": "old", "confidence": "medium"}],
    )
    _create_labeled_segment(
        env,
        inside_day,
        "090000_300",
        [{"sentence_id": 1, "speaker": "inside", "confidence": "high"}],
    )

    result = get_speaker_quality_status()

    assert result["quality_window_count"] == SPEAKER_QUALITY_WINDOW_DAYS
    assert result["tier_histogram"]["high_statements"] == 1
    assert result["tier_histogram"]["medium_statements"] == 0


def test_quality_teaching_copy_is_wired_to_quality_panel() -> None:
    from solstone.apps.speakers.copy import speaker_copy_payload

    text = _workspace_text()
    teaching_block = _js_function_block(text, "qualityTeachingLine")
    ready_block = _js_function_block(text, "renderQualityReady")
    load_block = _js_function_block(text, "loadQuality")

    assert "corrections_window_count" in teaching_block
    assert "COPY.SPK_OVERVIEW_QUALITY_TEACHING_ZERO" in teaching_block
    assert "COPY.SPK_OVERVIEW_QUALITY_TEACHING_LABEL" in teaching_block
    assert "qualityPanel.innerHTML" in ready_block
    assert "${qualityTeachingLine(data)}" in ready_block
    assert "COPY.SPK_OVERVIEW_QUALITY_ERROR_HEADING" in load_block
    assert "Couldn't load voice quality" not in text

    quality_copy = {
        name: value
        for name, value in speaker_copy_payload().items()
        if name.startswith("SPK_OVERVIEW_QUALITY_")
    }
    assert quality_copy["SPK_OVERVIEW_QUALITY_TEACHING_LABEL"] == "teaching changes"
    assert quality_copy["SPK_OVERVIEW_QUALITY_TEACHING_ZERO"] == (
        "no recent teaching changes"
    )
    assert quality_copy["SPK_OVERVIEW_QUALITY_ERROR_HEADING"] == (
        "couldn't load voice quality"
    )
    assert all(
        "error rate" not in str(value).lower() for value in quality_copy.values()
    )


def test_owner_teach_static_source_contracts() -> None:
    text = _workspace_text()
    teach_entry = _js_function_block(text, "ownerStatusStartsTeachSession")
    apply_day_state = _js_function_block(text, "applySpeakersDayState")
    day_candidate = _js_function_block(text, "renderOwnerCandidate")
    overview_candidate = _js_function_block(text, "ownerCandidate")
    teach_render = _js_function_block(text, "renderOwnerTeachSession")
    teach_fetch = _js_function_block(text, "loadOwnerTeachReviews")

    assert "sessionStorage" not in text
    assert "localStorage" not in text
    assert "OWNER_HELP_TOAST" not in text
    assert "spkOwnerGuideToast" not in text
    assert "SPK_OWNER_TEACH_ALREADY_BUILT" not in text
    assert "OWNER_MIN = payload.owner_min_statements || 0" in apply_day_state

    assert "data.status === 'needs_detection'" in teach_entry
    assert "data.status === 'low_quality'" in teach_entry
    assert "data.status === 'none'" in teach_entry
    assert "data.status === 'candidate'" not in teach_entry
    assert "data.status === 'no_cluster'" not in teach_entry
    assert "SPK_OWNER_TEACH_START_LABEL" not in day_candidate
    assert "SPK_OVERVIEW_OWNER_HELP_LABEL" not in overview_candidate

    assert teach_fetch.count("/app/speakers/api/assign-attribution") == 1
    assert "/app/speakers/api/owner/build-from-tags" not in teach_fetch
    assert "/app/speakers/api/owner/detect" not in teach_fetch
    assert "/app/speakers/api/owner/confirm" not in teach_fetch
    assert "/app/speakers/api/owner/reject" not in teach_fetch
    assert "/app/speakers/api/correct-attribution" not in teach_fetch
    assert "/app/speakers/api/propagate-correction" not in teach_fetch
    assert "ownerTeachState.refusalGuidance" in teach_render
    assert "SPK_OWNER_TEACH_REFUSED_TITLE" in teach_render
    assert "SPK_OWNER_TEACH_BUSY" in text
    assert "SPK_OWNER_REVEAL_TITLE" not in teach_render


def test_quality_counts_unreadable_label_and_correction_files(speakers_env):
    env = speakers_env()
    day = "20240110"
    segment_key = "090000_300"
    env.create_segment(day, segment_key, ["audio"])
    talents_dir = _talents_dir(env, day, segment_key)
    talents_dir.mkdir(parents=True, exist_ok=True)
    (talents_dir / "speaker_labels.json").write_text("{", encoding="utf-8")
    (talents_dir / "speaker_corrections.json").write_text("{", encoding="utf-8")

    result = get_speaker_quality_status()

    assert result["quality_window_error_count"] == 2
    assert result["unreadable_files"] == {
        "speaker_labels_window_count": 1,
        "speaker_corrections_window_count": 1,
        "total_window_count": 2,
    }
    assert result["corrections_window_count"] == 0
    assert result["tier_histogram"]["no_labels_file_segments"] == 0


def test_quality_route_matches_status_section(speakers_env):
    from solstone.apps.speakers.routes import speakers_bp
    from solstone.apps.speakers.status import get_speakers_status

    env = speakers_env()
    _create_labeled_segment(
        env,
        "20240110",
        "090000_300",
        [{"sentence_id": 1, "speaker": "alice", "confidence": "high"}],
    )
    app = Flask(__name__)
    app.register_blueprint(speakers_bp)

    with app.test_client() as client:
        response = client.get("/app/speakers/api/quality")

    assert response.status_code == 200
    assert response.get_json() == get_speakers_status(section="quality")


def test_quality_never_opens_files_outside_window(speakers_env, monkeypatch):
    env = speakers_env()
    for index in range(SPEAKER_QUALITY_WINDOW_DAYS + 1):
        day = f"202401{index + 1:02d}"
        (env.journal / "chronicle" / day).mkdir(parents=True, exist_ok=True)

    outside_day = "20240101"
    _create_labeled_segment(
        env,
        outside_day,
        "090000_300",
        [{"sentence_id": 1, "speaker": "old", "confidence": "medium"}],
    )
    env.create_speaker_corrections(
        outside_day,
        "090000_300",
        [{"sentence_id": 1, "original_speaker": None, "corrected_speaker": "old"}],
    )
    _create_labeled_segment(
        env,
        "20240102",
        "090000_300",
        [{"sentence_id": 1, "speaker": "inside", "confidence": "high"}],
    )

    opened: list[Path] = []
    original_open = Path.open

    def recording_open(self, *args, **kwargs):
        opened.append(Path(self))
        return original_open(self, *args, **kwargs)

    monkeypatch.setattr(Path, "open", recording_open)

    result = get_speaker_quality_status()

    assert result["tier_histogram"]["high_statements"] == 1
    assert not any(outside_day in path.parts for path in opened)
