# SPDX-License-Identifier: AGPL-3.0-only
# Copyright (c) 2026 sol pbc

"""Tests for segment-aware day completion."""

from __future__ import annotations

import importlib
import json
import logging
from pathlib import Path

import pytest

from solstone.observe.processing_record import (
    REASON_NO_DECODABLE_FRAMES,
    STATE_EMPTY,
    build_processing_record,
)
from solstone.think.cluster import cluster_segments
from solstone.think.pipeline_health import (
    CAP,
    MIN_SPAN_MS,
    SEGMENT_FLOOR_TALENTS,
    SEGMENT_NONGATING_TALENTS,
    SegmentProgress,
    classify_segment_completion,
    lookup_segment_progress,
    read_segment_backlog,
    read_segment_progress,
    segment_fully_sensed,
    segment_fully_thought,
    segment_requires_processing,
)
from solstone.think.utils import updated_days

DAY = "20990401"
STREAM = "default"
SEGMENT = "090000_300"
SEGMENT_B = "091000_300"
SEGMENT_C = "092000_300"
SEGMENT_D = "093000_300"
SEGMENT_E = "094000_300"


@pytest.fixture
def segment_journal(tmp_path, monkeypatch):
    journal = tmp_path / "journal"
    journal.mkdir()
    monkeypatch.setenv("SOLSTONE_JOURNAL", str(journal))
    monkeypatch.setenv("SOL_SKIP_SUPERVISOR_CHECK", "1")
    return journal


def _write_jsonl(path: Path, events: list[dict]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("w", encoding="utf-8") as handle:
        for event in events:
            handle.write(json.dumps(event) + "\n")


def _daily_complete(name: str = "alpha", ts: int = 1) -> dict:
    return {"event": "talent.complete", "ts": ts, "mode": "daily", "name": name}


def _segment_event(
    event: str,
    segment: str,
    name: str | None = None,
    ts: int = 1,
    **extra,
) -> dict:
    record = {"event": event, "ts": ts, "mode": "segment", "segment": segment}
    if name is not None:
        record["name"] = name
    record.update(extra)
    return record


def _dispatch(
    segment: str,
    name: str,
    ts: int = 1,
    *,
    stream: str | None = None,
) -> dict:
    return _segment_event(
        "talent.dispatch",
        segment,
        name,
        ts,
        **({"stream": stream} if stream else {}),
    )


def _complete(
    segment: str,
    name: str,
    ts: int = 1,
    *,
    stream: str | None = None,
) -> dict:
    return _segment_event(
        "talent.complete",
        segment,
        name,
        ts,
        state="finish",
        **({"stream": stream} if stream else {}),
    )


def _fail(
    segment: str,
    name: str,
    ts: int = 1,
    *,
    stream: str | None = None,
) -> dict:
    return _segment_event(
        "talent.fail",
        segment,
        name,
        ts,
        state="error",
        **({"stream": stream} if stream else {}),
    )


def _skip(
    segment: str,
    name: str,
    reason: str,
    ts: int = 1,
    *,
    stream: str | None = None,
) -> dict:
    return _segment_event(
        "talent.skip",
        segment,
        name,
        ts,
        reason=reason,
        **({"stream": stream} if stream else {}),
    )


def _sense_complete(
    segment: str,
    density: str = "active",
    ts: int = 1,
    *,
    stream: str | None = None,
) -> dict:
    return _segment_event(
        "sense.complete",
        segment,
        ts=ts,
        density=density,
        **({"stream": stream} if stream else {}),
    )


def _complete_segment_events(
    segment: str,
    density: str = "active",
    *,
    stream: str | None = None,
) -> list[dict]:
    events = [
        _dispatch(segment, "sense", 10, stream=stream),
        _complete(segment, "sense", 11, stream=stream),
        _sense_complete(segment, density, 12, stream=stream),
    ]
    if density != "idle":
        events.extend(
            [
                _dispatch(segment, "documents", 13, stream=stream),
                _complete(segment, "documents", 14, stream=stream),
            ]
        )
    return events


def _seed_segment(
    journal: Path,
    day: str,
    segment: str,
    *,
    state: str = "analyzed",
    stream: str | None = STREAM,
) -> Path:
    if stream is None:
        segment_dir = journal / "chronicle" / day / segment
    else:
        segment_dir = journal / "chronicle" / day / stream / segment
    segment_dir.mkdir(parents=True, exist_ok=True)
    if state == "dropped":
        return segment_dir

    (segment_dir / "screen.webm").write_bytes(b"raw")
    if state == "analyzed":
        (segment_dir / "screen.jsonl").write_text(
            json.dumps({"raw": "screen.webm", "type": "screencast"})
            + "\n"
            + json.dumps({"timestamp": 0, "content": {}})
            + "\n",
            encoding="utf-8",
        )
    elif state == "empty":
        record = build_processing_record(
            state=STATE_EMPTY,
            reason_code=REASON_NO_DECODABLE_FRAMES,
            handler="describe",
            input_size=0,
        )
        (segment_dir / "screen.jsonl").write_text(
            json.dumps(
                {
                    "raw": "screen.webm",
                    "type": "screencast",
                    "_solstone_processing": record,
                }
            )
            + "\n",
            encoding="utf-8",
        )
    else:
        (segment_dir / "screen.jsonl").write_text(
            json.dumps({"raw": "screen.webm", "type": "screencast"}) + "\n",
            encoding="utf-8",
        )
        if state == "failed":
            (segment_dir / ".analyze_failed_screen").write_text(
                json.dumps({"reason": "fixture_failure"}) + "\n",
                encoding="utf-8",
            )
        elif state == "analyzing":
            (segment_dir / ".analyzing_screen").write_text(
                json.dumps({"modality": "screen"}) + "\n",
                encoding="utf-8",
            )
    return segment_dir


def _seed_markdown_import_segment(
    journal: Path,
    day: str = DAY,
    segment: str = SEGMENT,
    *,
    stream: str = "import.apple_health",
) -> Path:
    segment_dir = journal / "chronicle" / day / stream / segment
    segment_dir.mkdir(parents=True, exist_ok=True)
    (segment_dir / "day_summary_transcript.md").write_text(
        "# Daily Health\n\nSynthetic summary.\n",
        encoding="utf-8",
    )
    return segment_dir


def _write_health(journal: Path, day: str, filename: str, events: list[dict]) -> Path:
    path = journal / "chronicle" / day / "health" / filename
    _write_jsonl(path, events)
    return path


def _patch_daily_main(monkeypatch, mod, applicable_units=None) -> None:
    if applicable_units is None:
        applicable_units = {("alpha", None)}

    monkeypatch.setattr(mod, "run_command", lambda cmd, day: True)
    monkeypatch.setattr(mod, "run_queued_command", lambda cmd, day, timeout=600: True)
    monkeypatch.setattr(
        mod,
        "run_daily_prompts",
        lambda **kwargs: (len(applicable_units), 0, [], applicable_units),
    )


def _run_daily_gate(journal: Path, day: str, monkeypatch) -> Path:
    mod = importlib.import_module("solstone.think.thinking")
    _patch_daily_main(monkeypatch, mod)
    monkeypatch.setattr("sys.argv", ["sol think", "--day", day])
    mod.main()
    return journal / "chronicle" / day / "health"


def _build_all_gate_states(journal: Path, day: str) -> None:
    _seed_segment(journal, day, SEGMENT)
    _seed_segment(journal, day, SEGMENT_B, state="pending")
    _seed_segment(journal, day, SEGMENT_C)
    _seed_segment(journal, day, SEGMENT_D)
    _seed_segment(journal, day, SEGMENT_E)
    _write_health(journal, day, "001_daily.jsonl", [_daily_complete()])
    _write_health(
        journal,
        day,
        "002_segment.jsonl",
        _complete_segment_events(SEGMENT)
        + _complete_segment_events(SEGMENT_B)
        + [_sense_complete(SEGMENT_C, "active", 20)]
        + _complete_segment_events(SEGMENT_D)
        + [_dispatch(SEGMENT_D, "screen", 30)],
    )


def test_read_segment_progress_folds_latest_terminal_and_segments(
    segment_journal,
):
    _write_health(
        segment_journal,
        DAY,
        "001_segment.jsonl",
        [
            _sense_complete(SEGMENT, "active", 1),
            _dispatch(SEGMENT, "documents", 2),
            _complete(SEGMENT, "documents", 3),
            _skip(SEGMENT, "documents", "not_recommended", 4),
            _fail(SEGMENT, "documents", 5),
            _sense_complete(SEGMENT_B, "active", 1),
            _dispatch(SEGMENT_B, "documents", 2),
            _complete(SEGMENT_B, "documents", 3),
        ],
    )

    progress = read_segment_progress(DAY)

    assert progress[(None, SEGMENT)].sensed is True
    assert progress[(None, SEGMENT)].density == "active"
    assert "documents" not in progress[(None, SEGMENT)].completed
    assert progress[(None, SEGMENT)].dispatched == frozenset({"documents"})
    assert progress[(None, SEGMENT_B)].completed == frozenset({"documents"})


def test_read_segment_progress_tracks_latest_sense_density(segment_journal):
    _write_health(
        segment_journal,
        DAY,
        "001_segment.jsonl",
        [
            _sense_complete(SEGMENT, "active", 1),
            _sense_complete(SEGMENT, "idle", 2),
        ],
    )

    assert read_segment_progress(DAY)[(None, SEGMENT)].density == "idle"


def test_read_segment_progress_captures_change_class(segment_journal):
    _write_health(
        segment_journal,
        DAY,
        "001_segment.jsonl",
        [
            _sense_complete(SEGMENT, "active", 1, stream=STREAM),
            _segment_event(
                "sense.change_detect",
                SEGMENT,
                ts=2,
                stream=STREAM,
                change_class="redundant",
            ),
        ],
    )

    progress = read_segment_progress(DAY)[(STREAM, SEGMENT)]

    assert progress.change_class == "redundant"
    assert segment_fully_thought(progress) == (True, None)


def test_stream_keyed_progress_separates_duplicate_segment_ids(segment_journal):
    day = "20990408"
    _seed_segment(segment_journal, day, SEGMENT, stream="alpha")
    _seed_segment(segment_journal, day, SEGMENT, stream="beta")
    _write_health(
        segment_journal,
        day,
        "001_segment.jsonl",
        _complete_segment_events(SEGMENT, stream="alpha")
        + [
            _dispatch(SEGMENT, "sense", 20, stream="beta"),
            _complete(SEGMENT, "sense", 21, stream="beta"),
            _sense_complete(SEGMENT, "active", 22, stream="beta"),
        ],
    )

    progress = read_segment_progress(day)

    assert ("alpha", SEGMENT) in progress
    assert ("beta", SEGMENT) in progress
    assert progress[("alpha", SEGMENT)].completed == frozenset({"sense", "documents"})
    assert progress[("beta", SEGMENT)].completed == frozenset({"sense"})
    assert segment_fully_thought(
        lookup_segment_progress(progress, "alpha", SEGMENT)
    ) == (True, None)
    assert segment_fully_thought(
        lookup_segment_progress(progress, "beta", SEGMENT)
    ) == (False, "floor:documents")

    completion = classify_segment_completion(cluster_segments(day), progress)
    assert completion.not_thought == 1
    assert completion.total == 2


def test_stream_lookup_does_not_borrow_from_other_stream(segment_journal):
    day = "20990409"
    _seed_segment(segment_journal, day, SEGMENT, stream="alpha")
    _seed_segment(segment_journal, day, SEGMENT, stream="beta")
    _write_health(
        segment_journal,
        day,
        "001_segment.jsonl",
        _complete_segment_events(SEGMENT, stream="alpha")
        + [_sense_complete(SEGMENT, "active", 20, stream="beta")],
    )

    progress = read_segment_progress(day)

    assert segment_fully_thought(
        lookup_segment_progress(progress, "alpha", SEGMENT)
    ) == (True, None)
    assert segment_fully_thought(
        lookup_segment_progress(progress, "beta", SEGMENT)
    ) == (False, "floor:documents")

    completion = classify_segment_completion(cluster_segments(day), progress)
    assert completion.not_thought == 1
    assert completion.total == 2


def test_markdown_only_health_segment_has_no_completion_blocker(segment_journal):
    day = "20990412"
    stream = "import.apple_health"
    _seed_markdown_import_segment(segment_journal, day, SEGMENT, stream=stream)

    segments = cluster_segments(day)
    completion = classify_segment_completion(segments, read_segment_progress(day))

    assert segments == [
        {
            "key": SEGMENT,
            "start": "09:00",
            "end": "09:05",
            "types": ["markdown"],
            "stream": stream,
            "data_state": {"markdown": "analyzed"},
        }
    ]
    assert completion.blockers == []
    assert completion.not_sensed == 0
    assert completion.not_thought == 0
    assert completion.total == 1


def test_markdown_only_health_segment_is_not_selected_for_repair(segment_journal):
    day = "20990413"
    stream = "import.apple_health"
    _seed_markdown_import_segment(segment_journal, day, SEGMENT, stream=stream)
    thinking = importlib.import_module("solstone.think.thinking")

    selected, counts = thinking._select_segment_repair_targets(
        day,
        cluster_segments(day),
        force_all=False,
    )

    assert selected == []
    assert counts == {
        "total": 1,
        "selected": 0,
        "complete": 1,
        "raw_blocked": 0,
    }


def test_markdown_only_owner_import_segment_is_selected_for_repair(segment_journal):
    day = "20990414"
    stream = "import.obsidian"
    _seed_markdown_import_segment(segment_journal, day, SEGMENT, stream=stream)
    thinking = importlib.import_module("solstone.think.thinking")

    segments = cluster_segments(day)
    selected, counts = thinking._select_segment_repair_targets(
        day,
        segments,
        force_all=False,
    )

    # Derived health cards stay out of think, but owner note imports are real
    # source content that must produce sense/entities/facets.
    assert segment_requires_processing(segments[0]) is True
    assert selected == segments
    assert counts == {
        "total": 1,
        "selected": 1,
        "complete": 0,
        "raw_blocked": 0,
    }


def test_stream_keyed_no_config_floor_allows_completion(segment_journal):
    day = "20990410"
    _seed_segment(segment_journal, day, SEGMENT, stream="gamma")
    _write_health(
        segment_journal,
        day,
        "001_segment.jsonl",
        [
            _sense_complete(SEGMENT, "active", 1, stream="gamma"),
            _skip(SEGMENT, "documents", "no_config", 3, stream="gamma"),
        ],
    )

    progress = read_segment_progress(day)

    assert progress[("gamma", SEGMENT)].unconfigured == frozenset({"documents"})
    assert segment_fully_thought(
        lookup_segment_progress(progress, "gamma", SEGMENT)
    ) == (True, None)


def test_stream_keyed_dispatch_blocks_without_terminal(segment_journal):
    day = "20990411"
    _seed_segment(segment_journal, day, SEGMENT, stream="delta")
    _write_health(
        segment_journal,
        day,
        "001_segment.jsonl",
        _complete_segment_events(SEGMENT, stream="delta")
        + [_dispatch(SEGMENT, "screen", 30, stream="delta")],
    )

    progress = read_segment_progress(day)

    assert "screen" in progress[("delta", SEGMENT)].dispatched
    assert "screen" not in progress[("delta", SEGMENT)].completed
    assert segment_fully_thought(
        lookup_segment_progress(progress, "delta", SEGMENT)
    ) == (False, "dispatched:screen")


@pytest.mark.parametrize("emit_fail", [False, True])
def test_stream_keyed_detection_dispatch_is_nongating(segment_journal, emit_fail):
    day = "20990414"
    stream = "delta"
    _seed_segment(segment_journal, day, SEGMENT, stream=stream)
    events = _complete_segment_events(SEGMENT, stream=stream) + [
        _dispatch(SEGMENT, "entities:detection", 30, stream=stream)
    ]
    if emit_fail:
        events.append(_fail(SEGMENT, "entities:detection", 31, stream=stream))
    _write_health(segment_journal, day, "001_segment.jsonl", events)

    progress = read_segment_progress(day)

    assert "entities:detection" in progress[(stream, SEGMENT)].dispatched
    assert "entities:detection" not in progress[(stream, SEGMENT)].completed
    assert segment_fully_thought(
        lookup_segment_progress(progress, stream, SEGMENT)
    ) == (True, None)


def test_primary_segment_uses_legacy_progress_fallback(segment_journal):
    day = "20990412"
    _seed_segment(segment_journal, day, SEGMENT, stream=None)
    _write_health(
        segment_journal,
        day,
        "001_segment.jsonl",
        _complete_segment_events(SEGMENT),
    )

    progress = read_segment_progress(day)
    segments = cluster_segments(day)
    completion = classify_segment_completion(segments, progress)

    assert (None, SEGMENT) in progress
    assert ("_default", SEGMENT) not in progress
    assert segments[0]["stream"] == "_default"
    assert completion.not_thought == 0
    assert completion.total == 1


def test_read_segment_progress_fail_closed_on_unexpected_error(
    monkeypatch,
    caplog,
):
    from solstone.think import pipeline_health

    def fail_day_path(*_args, **_kwargs):
        raise OSError("unreadable")

    monkeypatch.setattr(pipeline_health, "day_path", fail_day_path)
    caplog.set_level(logging.WARNING)

    assert pipeline_health.read_segment_progress(DAY) == {}
    assert "unexpected error reading segment progress" in caplog.text


@pytest.mark.parametrize("state", ["pending", "failed", "analyzing"])
def test_segment_fully_sensed_rejects_unfinished_states(state):
    assert segment_fully_sensed({"screen": state}) is False


def test_segment_fully_sensed_accepts_done_states():
    assert segment_fully_sensed({"screen": "analyzed", "audio": "purged"}) is True
    assert segment_fully_sensed({"screen": "empty"}) is True


def test_segment_fully_thought_idle_short_circuits():
    progress = SegmentProgress(
        sensed=True,
        density="idle",
        change_class=None,
        dispatched=frozenset({"sense"}),
        completed=frozenset({"sense"}),
        unconfigured=frozenset(),
        capped=frozenset(),
    )

    assert segment_fully_thought(progress) == (True, None)


def test_segment_fully_thought_redundant_short_circuits():
    progress = SegmentProgress(
        sensed=True,
        density="active",
        change_class="redundant",
        dispatched=frozenset({"sense"}),
        completed=frozenset({"sense"}),
        unconfigured=frozenset(),
        capped=frozenset(),
    )

    assert segment_fully_thought(progress) == (True, None)


def test_segment_fully_thought_requires_floor_after_sense():
    progress = SegmentProgress(
        sensed=True,
        density="active",
        change_class=None,
        dispatched=frozenset({"sense"}),
        completed=frozenset({"sense"}),
        unconfigured=frozenset(),
        capped=frozenset(),
    )

    assert segment_fully_thought(progress) == (False, "floor:documents")


def test_segment_floor_and_nongating_talents_are_disjoint():
    assert set(SEGMENT_NONGATING_TALENTS).isdisjoint(SEGMENT_FLOOR_TALENTS)


def test_segment_fully_thought_ignores_skipped_not_dispatched_conditionals(
    segment_journal,
):
    _write_health(
        segment_journal,
        DAY,
        "001_segment.jsonl",
        _complete_segment_events(SEGMENT)
        + [_skip(SEGMENT, "speaker_attribution", "not_recommended", 30)],
    )

    progress = read_segment_progress(DAY)[(None, SEGMENT)]

    assert "speaker_attribution" not in progress.dispatched
    assert segment_fully_thought(progress) == (True, None)


def test_segment_fully_thought_does_not_require_detection_before_activation():
    progress = SegmentProgress(
        sensed=True,
        density="active",
        change_class=None,
        dispatched=frozenset({"sense", "documents"}),
        completed=frozenset({"sense", "documents"}),
        unconfigured=frozenset(),
        capped=frozenset(),
    )

    assert segment_fully_thought(progress) == (True, None)


def test_segment_fully_thought_does_not_require_rolling_talents():
    progress = SegmentProgress(
        sensed=True,
        density="active",
        change_class=None,
        dispatched=frozenset({"sense", "documents"}),
        completed=frozenset({"sense", "documents"}),
        unconfigured=frozenset(),
        capped=frozenset(),
    )

    assert segment_fully_thought(progress) == (True, None)


def test_segment_fully_thought_allows_unconfigured_floor_talent():
    progress = SegmentProgress(
        sensed=True,
        density="active",
        change_class=None,
        dispatched=frozenset({"sense"}),
        completed=frozenset({"sense"}),
        unconfigured=frozenset({"documents"}),
        capped=frozenset(),
    )

    assert segment_fully_thought(progress) == (True, None)


def test_segment_fully_thought_requires_dispatched_completion():
    progress = SegmentProgress(
        sensed=True,
        density="active",
        change_class=None,
        dispatched=frozenset({"sense", "documents", "screen"}),
        completed=frozenset({"sense", "documents"}),
        unconfigured=frozenset(),
        capped=frozenset(),
    )

    assert segment_fully_thought(progress) == (False, "dispatched:screen")


def test_segment_fully_thought_allows_nongating_detection_without_completion():
    progress = SegmentProgress(
        sensed=True,
        density="active",
        change_class=None,
        dispatched=frozenset({"sense", "documents", "entities:detection"}),
        completed=frozenset({"sense", "documents"}),
        unconfigured=frozenset(),
        capped=frozenset(),
    )

    assert segment_fully_thought(progress) == (True, None)


def test_classifier_stats_and_gate_agree_on_all_gate_states(
    segment_journal,
    monkeypatch,
    caplog,
):
    from solstone.think.journal_stats import JournalStats

    day = "20990402"
    _build_all_gate_states(segment_journal, day)

    completion = classify_segment_completion(
        cluster_segments(day),
        read_segment_progress(day),
    )

    assert completion.not_thought == 3
    assert completion.not_sensed == 1
    assert completion.total == 5
    assert completion.blockers == [
        {
            "segment": SEGMENT_B,
            "dimension": "not_sensed",
            "detail": "screen=pending",
        },
        {
            "segment": SEGMENT_C,
            "dimension": "not_thought",
            "detail": "floor:documents",
        },
        {
            "segment": SEGMENT_D,
            "dimension": "not_thought",
            "detail": "dispatched:screen",
        },
        {
            "segment": SEGMENT_E,
            "dimension": "not_thought",
            "detail": "no_sense_complete",
        },
    ]

    stats = JournalStats().scan_day(
        day,
        str(segment_journal / "chronicle" / day),
    )
    assert stats["stats"]["segments_pending_think"] == completion.not_thought

    caplog.set_level(logging.INFO)
    health = _run_daily_gate(segment_journal, day, monkeypatch)

    assert not (health / "daily.updated").exists()
    assert str(completion.blockers) in caplog.text


def test_media_terminal_without_sense_complete_is_no_sense_complete(
    segment_journal,
):
    day = "20990415"
    _seed_segment(segment_journal, day, SEGMENT, state="analyzed")

    completion = classify_segment_completion(
        cluster_segments(day),
        read_segment_progress(day),
    )

    assert completion.not_sensed == 0
    assert completion.not_thought == 1
    assert completion.blockers == [
        {
            "segment": SEGMENT,
            "dimension": "not_thought",
            "detail": "no_sense_complete",
        }
    ]


def test_classify_segment_completion_latest_terminal_wins(segment_journal):
    day = "20990403"
    fail_then_complete = SEGMENT
    complete_then_fail = SEGMENT_B
    _seed_segment(segment_journal, day, fail_then_complete)
    _seed_segment(segment_journal, day, complete_then_fail)
    _write_health(
        segment_journal,
        day,
        "001_segment.jsonl",
        [
            _sense_complete(fail_then_complete, "active", 1),
            _dispatch(fail_then_complete, "documents", 4),
            _fail(fail_then_complete, "documents", 5),
            _complete(fail_then_complete, "documents", 6),
            _sense_complete(complete_then_fail, "active", 1),
            _dispatch(complete_then_fail, "documents", 4),
            _complete(complete_then_fail, "documents", 5),
            _fail(complete_then_fail, "documents", 6),
        ],
    )

    completion = classify_segment_completion(
        cluster_segments(day),
        read_segment_progress(day),
    )

    assert completion.not_thought == 1
    assert completion.blockers == [
        {
            "segment": complete_then_fail,
            "dimension": "not_thought",
            "detail": "floor:documents",
        }
    ]


def test_capped_floor_talent_unblocks_segment_completion(segment_journal):
    day = "20990420"
    _seed_segment(segment_journal, day, SEGMENT)
    first_ts = 1_000
    fail_events = [
        _fail(
            SEGMENT,
            "documents",
            first_ts + idx * (MIN_SPAN_MS // (CAP - 1)),
            stream=STREAM,
        )
        for idx in range(CAP)
    ]
    _write_health(
        segment_journal,
        day,
        "001_segment.jsonl",
        [
            _sense_complete(SEGMENT, "active", 10, stream=STREAM),
            _dispatch(SEGMENT, "documents", 20, stream=STREAM),
            *fail_events,
            _skip(
                SEGMENT,
                "documents",
                "capped",
                first_ts + MIN_SPAN_MS + 1,
                stream=STREAM,
            ),
        ],
    )

    progress = read_segment_progress(day)
    segment_progress = progress[(STREAM, SEGMENT)]
    completion = classify_segment_completion(cluster_segments(day), progress)

    assert segment_progress.capped == frozenset({"documents"})
    assert segment_fully_thought(segment_progress) == (True, None)
    assert completion.blockers == []
    assert completion.capped == 1


def test_capped_fold_resets_with_later_terminal_events(segment_journal):
    day = "20990421"
    _seed_segment(segment_journal, day, SEGMENT)
    _write_health(
        segment_journal,
        day,
        "001_segment.jsonl",
        [
            _sense_complete(SEGMENT, "active", 10, stream=STREAM),
            _dispatch(SEGMENT, "documents", 20, stream=STREAM),
            _skip(SEGMENT, "documents", "capped", 30, stream=STREAM),
        ],
    )

    capped_progress = read_segment_progress(day)[(STREAM, SEGMENT)]
    assert capped_progress.capped == frozenset({"documents"})
    assert segment_fully_thought(capped_progress) == (True, None)

    _write_health(
        segment_journal,
        day,
        "002_segment.jsonl",
        [_complete(SEGMENT, "documents", 40, stream=STREAM)],
    )
    complete_progress = read_segment_progress(day)[(STREAM, SEGMENT)]
    assert complete_progress.capped == frozenset()
    assert "documents" in complete_progress.completed
    assert segment_fully_thought(complete_progress) == (True, None)

    _write_health(
        segment_journal,
        day,
        "003_segment.jsonl",
        [_fail(SEGMENT, "documents", 50, stream=STREAM)],
    )
    failed_progress = read_segment_progress(day)[(STREAM, SEGMENT)]
    assert failed_progress.capped == frozenset()
    assert "documents" not in failed_progress.completed
    assert segment_fully_thought(failed_progress) == (False, "floor:documents")


def test_dropped_empty_modality_segment_is_not_counted(segment_journal):
    day = "20990404"
    _seed_segment(segment_journal, day, SEGMENT)
    _seed_segment(segment_journal, day, SEGMENT_B, state="dropped")
    _write_health(
        segment_journal,
        day,
        "001_segment.jsonl",
        _complete_segment_events(SEGMENT),
    )

    completion = classify_segment_completion(
        cluster_segments(day),
        read_segment_progress(day),
    )

    assert completion.total == 1
    assert completion.blockers == []


def test_classify_segment_completion_ignores_missing_pre_activation_detection(
    segment_journal,
):
    day = "20990415"
    _seed_segment(segment_journal, day, SEGMENT)
    _write_health(
        segment_journal,
        day,
        "001_segment.jsonl",
        _complete_segment_events(SEGMENT),
    )

    completion = classify_segment_completion(
        cluster_segments(day),
        read_segment_progress(day),
    )

    assert completion.not_thought == 0
    assert completion.blockers == []


def test_read_segment_backlog_sums_updated_days(segment_journal):
    day_one = "20990405"
    day_two = "20990406"
    day_three = "20990407"

    _seed_segment(segment_journal, day_one, SEGMENT)
    _seed_segment(segment_journal, day_one, SEGMENT_B, state="pending")
    _write_health(
        segment_journal,
        day_one,
        "001_segment.jsonl",
        [_sense_complete(SEGMENT, "active", 1)],
    )

    _seed_segment(segment_journal, day_two, SEGMENT)
    _seed_segment(segment_journal, day_two, SEGMENT_B)
    _write_health(
        segment_journal,
        day_two,
        "001_segment.jsonl",
        _complete_segment_events(SEGMENT),
    )

    _seed_segment(segment_journal, day_three, SEGMENT)
    _write_health(
        segment_journal,
        day_three,
        "001_segment.jsonl",
        [_sense_complete(SEGMENT, "active", 1)],
    )

    for day in (day_one, day_two):
        health = segment_journal / "chronicle" / day / "health"
        health.mkdir(parents=True, exist_ok=True)
        (health / "stream.updated").touch()

    bound = tuple(updated_days())
    backlog = read_segment_backlog()

    assert backlog.days == bound
    assert backlog.days == (day_one, day_two)
    assert set(backlog.per_day) == {day_one, day_two}
    assert backlog.errors == ()
    assert backlog.not_thought == sum(
        completion.not_thought for completion in backlog.per_day.values()
    )
    assert backlog.not_sensed == sum(
        completion.not_sensed for completion in backlog.per_day.values()
    )
    assert backlog.total == sum(
        completion.total for completion in backlog.per_day.values()
    )
    assert backlog.not_thought == 2
    assert backlog.not_sensed == 1
    assert backlog.total == 4


def test_daily_marker_written_when_daily_and_segments_complete(
    segment_journal,
    monkeypatch,
):
    _seed_segment(segment_journal, DAY, SEGMENT)
    _write_health(segment_journal, DAY, "001_daily.jsonl", [_daily_complete()])
    _write_health(
        segment_journal, DAY, "002_segment.jsonl", _complete_segment_events(SEGMENT)
    )

    health = _run_daily_gate(segment_journal, DAY, monkeypatch)

    assert (health / "daily.updated").exists()


def test_daily_marker_written_when_floor_talent_capped(segment_journal, monkeypatch):
    _seed_segment(segment_journal, DAY, SEGMENT)
    _write_health(segment_journal, DAY, "001_daily.jsonl", [_daily_complete()])
    _write_health(
        segment_journal,
        DAY,
        "002_segment.jsonl",
        [
            _sense_complete(SEGMENT, "active", 10, stream=STREAM),
            _dispatch(SEGMENT, "documents", 20, stream=STREAM),
            _skip(SEGMENT, "documents", "capped", 30, stream=STREAM),
        ],
    )

    health = _run_daily_gate(segment_journal, DAY, monkeypatch)

    assert (health / "daily.updated").exists()


def test_downstream_failure_withholds_until_later_complete(
    segment_journal,
    monkeypatch,
):
    _seed_segment(segment_journal, DAY, SEGMENT)
    _write_health(segment_journal, DAY, "001_daily.jsonl", [_daily_complete()])
    events = _complete_segment_events(SEGMENT) + [
        _dispatch(SEGMENT, "screen", 20),
        _fail(SEGMENT, "screen", 21),
    ]
    _write_health(segment_journal, DAY, "002_segment.jsonl", events)

    health = _run_daily_gate(segment_journal, DAY, monkeypatch)
    assert not (health / "daily.updated").exists()

    _write_health(
        segment_journal,
        DAY,
        "003_segment.jsonl",
        [_complete(SEGMENT, "screen", 22)],
    )
    health = _run_daily_gate(segment_journal, DAY, monkeypatch)
    assert (health / "daily.updated").exists()


def test_unterminated_downstream_withholds(segment_journal, monkeypatch):
    _seed_segment(segment_journal, DAY, SEGMENT)
    _write_health(segment_journal, DAY, "001_daily.jsonl", [_daily_complete()])
    events = _complete_segment_events(SEGMENT) + [_dispatch(SEGMENT, "screen", 20)]
    _write_health(segment_journal, DAY, "002_segment.jsonl", events)

    health = _run_daily_gate(segment_journal, DAY, monkeypatch)

    assert not (health / "daily.updated").exists()


def test_not_fully_sensed_segment_withholds(segment_journal, monkeypatch, caplog):
    _seed_segment(segment_journal, DAY, SEGMENT, state="pending")
    _write_health(segment_journal, DAY, "001_daily.jsonl", [_daily_complete()])
    _write_health(
        segment_journal, DAY, "002_segment.jsonl", _complete_segment_events(SEGMENT)
    )
    caplog.set_level(logging.INFO)

    health = _run_daily_gate(segment_journal, DAY, monkeypatch)

    assert not (health / "daily.updated").exists()
    assert "not_sensed" in caplog.text
    assert "screen=pending" in caplog.text


def test_empty_segment_is_not_counted_not_sensed(segment_journal):
    day = "20990416"
    _seed_segment(segment_journal, day, SEGMENT, state="empty")

    completion = classify_segment_completion(
        cluster_segments(day),
        read_segment_progress(day),
    )

    assert completion.not_sensed == 0
    assert all(blocker["dimension"] != "not_sensed" for blocker in completion.blockers)


def test_idle_segment_does_not_block_day(segment_journal, monkeypatch):
    _seed_segment(segment_journal, DAY, SEGMENT)
    _write_health(segment_journal, DAY, "001_daily.jsonl", [_daily_complete()])
    _write_health(
        segment_journal,
        DAY,
        "002_segment.jsonl",
        _complete_segment_events(SEGMENT, density="idle"),
    )

    health = _run_daily_gate(segment_journal, DAY, monkeypatch)

    assert (health / "daily.updated").exists()


def test_empty_idle_segment_allows_daily_gate(
    segment_journal,
    monkeypatch,
):
    day = "20990417"
    _seed_segment(segment_journal, day, SEGMENT, state="empty")
    _write_health(segment_journal, day, "001_daily.jsonl", [_daily_complete()])
    _write_health(
        segment_journal,
        day,
        "002_segment.jsonl",
        _complete_segment_events(SEGMENT, density="idle"),
    )

    completion = classify_segment_completion(
        cluster_segments(day),
        read_segment_progress(day),
    )
    assert completion.blockers == []

    health = _run_daily_gate(segment_journal, day, monkeypatch)

    assert (health / "daily.updated").exists()


def test_backfill_unblocks_stuck_empty_day(segment_journal, monkeypatch):
    day = "20990419"
    _seed_segment(segment_journal, day, SEGMENT, state="pending")
    _write_health(segment_journal, day, "001_daily.jsonl", [_daily_complete()])
    _write_health(
        segment_journal,
        day,
        "002_segment.jsonl",
        _complete_segment_events(SEGMENT, density="idle"),
    )

    health = _run_daily_gate(segment_journal, day, monkeypatch)
    assert not (health / "daily.updated").exists()

    from solstone.think.backfill_processing_records import run_backfill

    run_backfill(day, commit=True)

    health = _run_daily_gate(segment_journal, day, monkeypatch)
    assert (health / "daily.updated").exists()


@pytest.mark.parametrize("state", ["pending", "analyzing"])
def test_unfinished_sensing_states_still_block_daily_gate(
    segment_journal,
    monkeypatch,
    state,
):
    day = "20990418"
    _seed_segment(segment_journal, day, SEGMENT, state=state)
    _write_health(segment_journal, day, "001_daily.jsonl", [_daily_complete()])

    segments = cluster_segments(day)
    assert segments[0]["data_state"] == {"screen": state}

    health = _run_daily_gate(segment_journal, day, monkeypatch)

    assert not (health / "daily.updated").exists()


def test_dropped_segment_directory_is_not_required(segment_journal, monkeypatch):
    _seed_segment(segment_journal, DAY, SEGMENT)
    _seed_segment(segment_journal, DAY, SEGMENT_B, state="dropped")
    _write_health(segment_journal, DAY, "001_daily.jsonl", [_daily_complete()])
    _write_health(
        segment_journal, DAY, "002_segment.jsonl", _complete_segment_events(SEGMENT)
    )

    health = _run_daily_gate(segment_journal, DAY, monkeypatch)

    assert (health / "daily.updated").exists()


def test_all_skip_rerun_writes_marker_and_leaves_updated_days(
    segment_journal,
    monkeypatch,
):
    _seed_segment(segment_journal, DAY, SEGMENT)
    health = segment_journal / "chronicle" / DAY / "health"
    _write_health(
        segment_journal,
        DAY,
        "001_daily.jsonl",
        [
            _daily_complete(ts=1),
            {"event": "talent.skip", "ts": 2, "mode": "daily", "name": "alpha"},
        ],
    )
    _write_health(
        segment_journal,
        DAY,
        "002_segment.jsonl",
        _complete_segment_events(SEGMENT)
        + [
            _skip(SEGMENT, "documents", "already_complete", 31),
        ],
    )
    health.mkdir(parents=True, exist_ok=True)
    (health / "stream.updated").touch()
    assert DAY in updated_days()

    health = _run_daily_gate(segment_journal, DAY, monkeypatch)

    assert (health / "daily.updated").exists()
    assert DAY not in updated_days()
    assert (health / "daily.updated").stat().st_mtime_ns >= (
        health / "stream.updated"
    ).stat().st_mtime_ns


def test_empty_segment_progress_withholds_and_logs_blocker(
    segment_journal,
    monkeypatch,
    caplog,
):
    mod = importlib.import_module("solstone.think.thinking")
    _seed_segment(segment_journal, DAY, SEGMENT)
    _write_health(segment_journal, DAY, "001_daily.jsonl", [_daily_complete()])
    monkeypatch.setattr(mod, "read_segment_progress", lambda day: {})
    _patch_daily_main(monkeypatch, mod)
    monkeypatch.setattr("sys.argv", ["sol think", "--day", DAY])
    caplog.set_level(logging.INFO)

    mod.main()
    health = segment_journal / "chronicle" / DAY / "health"

    assert not (health / "daily.updated").exists()
    assert SEGMENT in caplog.text
    assert "not_thought" in caplog.text
    assert "no_sense_complete" in caplog.text


def test_segment_fully_thought_allows_superseded_entities_after_detection_completion():
    progress = SegmentProgress(
        sensed=True,
        density="active",
        change_class=None,
        dispatched=frozenset({"sense", "documents", "entities"}),
        completed=frozenset({"sense", "documents", "entities:detection"}),
        unconfigured=frozenset(),
        capped=frozenset(),
    )

    assert segment_fully_thought(progress) == (True, None)


def test_segment_fully_thought_blocks_superseded_entities_without_detection_completion():
    progress = SegmentProgress(
        sensed=True,
        density="active",
        change_class=None,
        dispatched=frozenset({"sense", "documents", "entities"}),
        completed=frozenset({"sense", "documents"}),
        unconfigured=frozenset(),
        capped=frozenset(),
    )

    assert segment_fully_thought(progress) == (False, "dispatched:entities")


def test_stream_keyed_superseded_entities_detection_completion_unblocks(
    segment_journal,
):
    day = "20990422"
    stream = "delta"
    _seed_segment(segment_journal, day, SEGMENT, stream=stream)
    _write_health(
        segment_journal,
        day,
        "001_segment.jsonl",
        _complete_segment_events(SEGMENT, stream=stream)
        + [
            _dispatch(SEGMENT, "entities", 30, stream=stream),
            _fail(SEGMENT, "entities", 31, stream=stream),
            _dispatch(SEGMENT, "entities:detection", 32, stream=stream),
            _complete(SEGMENT, "entities:detection", 33, stream=stream),
        ],
    )

    progress = read_segment_progress(day)
    segment_progress = progress[(stream, SEGMENT)]

    assert "entities" in segment_progress.dispatched
    assert "entities" not in segment_progress.completed
    assert "entities:detection" in segment_progress.completed
    assert segment_fully_thought(
        lookup_segment_progress(progress, stream, SEGMENT)
    ) == (True, None)

    completion = classify_segment_completion(cluster_segments(day), progress)
    assert completion.not_thought == 0
    assert all(
        blocker["detail"] != "dispatched:entities" for blocker in completion.blockers
    )
