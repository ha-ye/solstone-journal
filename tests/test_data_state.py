# SPDX-License-Identifier: AGPL-3.0-only
# Copyright (c) 2026 sol pbc

import json
import os
import time

from solstone.observe.processing_record import (
    FAILED_ATTEMPT_BOUND,
    HANDLER_DESCRIBE,
    HANDLER_TRANSCRIBE,
    REASON_ANALYSIS_FAILED,
    REASON_CORRUPT_INPUT,
    SCREEN_ANALYSIS_ROW_KEY,
    STATE_ANALYZED,
    STATE_EMPTY,
    STATE_FAILED,
    is_failure_exhausted,
    record_attempts,
    should_reenter_analysis_output,
)
from solstone.think.data_state import (
    DataState,
    derive_modality_state,
    read_processing_record,
)


def test_read_processing_record_returns_header_record() -> None:
    record = {"state": STATE_EMPTY}

    assert read_processing_record([{"_solstone_processing": record}]) is record


def test_read_processing_record_rejects_missing_or_invalid_header() -> None:
    assert read_processing_record([]) is None
    assert read_processing_record(["header"]) is None
    assert read_processing_record([{"_solstone_processing": STATE_EMPTY}]) is None
    assert (
        read_processing_record([{"raw": "screen.webm"}, {"_solstone_processing": {}}])
        is None
    )


def test_is_failure_exhausted_uses_corrupt_input_or_attempt_bound() -> None:
    assert not is_failure_exhausted(None)
    assert not is_failure_exhausted({"state": STATE_EMPTY})
    assert not is_failure_exhausted({"state": STATE_FAILED})
    assert not is_failure_exhausted(
        {
            "state": STATE_FAILED,
            "reason_code": REASON_ANALYSIS_FAILED,
            "attempts": FAILED_ATTEMPT_BOUND - 1,
        }
    )
    assert is_failure_exhausted(
        {"state": STATE_FAILED, "reason_code": REASON_CORRUPT_INPUT}
    )
    assert is_failure_exhausted(
        {
            "state": STATE_FAILED,
            "reason_code": REASON_ANALYSIS_FAILED,
            "attempts": FAILED_ATTEMPT_BOUND,
        }
    )


def test_record_attempts_coerces_absent_or_malformed_to_zero() -> None:
    assert record_attempts(None) == 0
    assert record_attempts({}) == 0
    assert record_attempts({"attempts": 2}) == 2
    assert record_attempts({"attempts": None}) == 0
    assert record_attempts({"attempts": "3"}) == 0
    assert record_attempts({"attempts": True}) == 0


def test_ac1_should_reenter_analysis_output_table(tmp_path) -> None:
    output_path = tmp_path / "screen.jsonl"

    def write_output(record: dict | None, rows: list[dict] | None = None) -> None:
        header = {"raw": "screen.webm"}
        if record is not None:
            header["_solstone_processing"] = record
        lines = [json.dumps(header)]
        if rows:
            lines.extend(json.dumps(row) for row in rows)
        output_path.write_text("\n".join(lines) + "\n", encoding="utf-8")

    cases = [
        (
            "retryable_describe_failure",
            {
                "state": STATE_FAILED,
                "handler": HANDLER_DESCRIBE,
                "reason_code": REASON_ANALYSIS_FAILED,
                "attempts": FAILED_ATTEMPT_BOUND - 1,
            },
            None,
            HANDLER_DESCRIBE,
            True,
        ),
        (
            "corrupt_describe_failure",
            {
                "state": STATE_FAILED,
                "handler": HANDLER_DESCRIBE,
                "reason_code": REASON_CORRUPT_INPUT,
            },
            None,
            HANDLER_DESCRIBE,
            False,
        ),
        (
            "exhausted_describe_failure",
            {
                "state": STATE_FAILED,
                "handler": HANDLER_DESCRIBE,
                "reason_code": REASON_ANALYSIS_FAILED,
                "attempts": FAILED_ATTEMPT_BOUND,
            },
            None,
            HANDLER_DESCRIBE,
            False,
        ),
        (
            "transcribe_failure",
            {
                "state": STATE_FAILED,
                "handler": HANDLER_TRANSCRIBE,
                "reason_code": REASON_ANALYSIS_FAILED,
                "attempts": 1,
            },
            None,
            HANDLER_TRANSCRIBE,
            False,
        ),
        ("recordless_screen_no_rows", None, None, HANDLER_DESCRIBE, True),
        (
            "recordless_screen_with_rows",
            None,
            [{"frame_id": 1, SCREEN_ANALYSIS_ROW_KEY: 0.0}],
            HANDLER_DESCRIBE,
            False,
        ),
        ("recordless_audio_no_rows", None, None, HANDLER_TRANSCRIBE, False),
    ]

    for _name, record, rows, handler, expected in cases:
        write_output(record, rows)
        assert (
            should_reenter_analysis_output(
                record=record,
                output_path=output_path,
                handler=handler,
            )
            is expected
        )


def test_ac8_screen_disagreement_table_has_no_silent_nonterminal_wedge(
    tmp_path,
) -> None:
    terminal_states = {
        DataState.ANALYZED.value,
        DataState.EMPTY.value,
        DataState.FAILED_FINAL.value,
    }
    retryable_record = {
        "state": STATE_FAILED,
        "handler": HANDLER_DESCRIBE,
        "reason_code": REASON_ANALYSIS_FAILED,
        "attempts": 1,
    }
    final_record = {
        "state": STATE_FAILED,
        "handler": HANDLER_DESCRIBE,
        "reason_code": REASON_CORRUPT_INPUT,
    }
    empty_record = {"state": STATE_EMPTY, "handler": HANDLER_DESCRIBE}
    analyzed_record = {"state": STATE_ANALYZED, "handler": HANDLER_DESCRIBE}
    row = {"frame_id": 1, SCREEN_ANALYSIS_ROW_KEY: 0.0}
    cases = [
        {
            "name": "no_jsonl_pending_open_row",
            "jsonl": False,
            "record": None,
            "rows": False,
            "marker": False,
            "state": DataState.PENDING.value,
            "reentry": True,
        },
        {
            "name": "no_jsonl_failed_marker_open_row",
            "jsonl": False,
            "record": None,
            "rows": False,
            "marker": True,
            "state": DataState.FAILED.value,
            "reentry": True,
        },
        {
            "name": "no_jsonl_rows_impossible",
            "jsonl": False,
            "record": None,
            "rows": True,
            "marker": False,
            "impossible": "analyzed rows require a JSONL file",
        },
        {
            "name": "no_jsonl_rows_marker_impossible",
            "jsonl": False,
            "record": None,
            "rows": True,
            "marker": True,
            "impossible": "analyzed rows require a JSONL file",
        },
        {
            "name": "no_jsonl_record_impossible",
            "jsonl": False,
            "record": final_record,
            "rows": False,
            "marker": False,
            "impossible": "a processing record requires a JSONL header",
        },
        {
            "name": "no_jsonl_record_marker_impossible",
            "jsonl": False,
            "record": final_record,
            "rows": False,
            "marker": True,
            "impossible": "a processing record requires a JSONL header",
        },
        {
            "name": "no_jsonl_record_rows_impossible",
            "jsonl": False,
            "record": final_record,
            "rows": True,
            "marker": False,
            "impossible": "recorded analyzed rows require a JSONL file",
        },
        {
            "name": "no_jsonl_record_rows_marker_impossible",
            "jsonl": False,
            "record": final_record,
            "rows": True,
            "marker": True,
            "impossible": "recorded analyzed rows require a JSONL file",
        },
        {
            "name": "header_only_pending",
            "jsonl": True,
            "record": None,
            "rows": False,
            "marker": False,
            "state": DataState.PENDING.value,
            "reentry": True,
        },
        {
            "name": "header_only_failed_marker",
            "jsonl": True,
            "record": None,
            "rows": False,
            "marker": True,
            "state": DataState.FAILED.value,
            "reentry": True,
        },
        {
            "name": "recordless_rows_analyzed",
            "jsonl": True,
            "record": None,
            "rows": True,
            "marker": False,
            "state": DataState.ANALYZED.value,
            "reentry": False,
        },
        {
            "name": "recordless_rows_marker_analyzed",
            "jsonl": True,
            "record": None,
            "rows": True,
            "marker": True,
            "state": DataState.ANALYZED.value,
            "reentry": False,
        },
        {
            "name": "retryable_record_no_rows",
            "jsonl": True,
            "record": retryable_record,
            "rows": False,
            "marker": False,
            "state": DataState.FAILED.value,
            "reentry": True,
        },
        {
            "name": "retryable_record_marker_no_rows",
            "jsonl": True,
            "record": retryable_record,
            "rows": False,
            "marker": True,
            "state": DataState.FAILED.value,
            "reentry": True,
        },
        {
            "name": "retryable_record_rows",
            "jsonl": True,
            "record": retryable_record,
            "rows": True,
            "marker": False,
            "state": DataState.FAILED.value,
            "reentry": True,
        },
        {
            "name": "retryable_record_failed_marker_analysis_rows",
            "jsonl": True,
            "record": retryable_record,
            "rows": True,
            "marker": True,
            "state": DataState.FAILED.value,
            "reentry": True,
        },
        {
            "name": "final_record_no_rows",
            "jsonl": True,
            "record": final_record,
            "rows": False,
            "marker": False,
            "state": DataState.FAILED_FINAL.value,
            "reentry": False,
        },
        {
            "name": "final_record_marker_no_rows",
            "jsonl": True,
            "record": final_record,
            "rows": False,
            "marker": True,
            "state": DataState.FAILED_FINAL.value,
            "reentry": False,
        },
        {
            "name": "empty_record_no_rows",
            "jsonl": True,
            "record": empty_record,
            "rows": False,
            "marker": False,
            "state": DataState.EMPTY.value,
            "reentry": False,
        },
        {
            "name": "empty_record_marker_no_rows",
            "jsonl": True,
            "record": empty_record,
            "rows": False,
            "marker": True,
            "state": DataState.EMPTY.value,
            "reentry": False,
        },
        {
            "name": "empty_record_rows_impossible",
            "jsonl": True,
            "record": empty_record,
            "rows": True,
            "marker": False,
            "impossible": "empty verdicts contain no analyzed screen rows",
        },
        {
            "name": "analyzed_record_rows",
            "jsonl": True,
            "record": analyzed_record,
            "rows": True,
            "marker": False,
            "state": DataState.ANALYZED.value,
            "reentry": False,
        },
        {
            "name": "analyzed_record_failed_marker_analysis_rows",
            "jsonl": True,
            "record": analyzed_record,
            "rows": True,
            "marker": True,
            "state": DataState.ANALYZED.value,
            "reentry": False,
        },
        {
            "name": "analyzed_record_no_rows_impossible",
            "jsonl": True,
            "record": analyzed_record,
            "rows": False,
            "marker": False,
            "impossible": "analyzed verdicts are emitted with analyzed rows",
        },
    ]

    for index, case in enumerate(cases):
        if case.get("impossible"):
            assert isinstance(case["impossible"], str)
            assert case["impossible"].strip()
            assert "state" not in case
            assert "reentry" not in case
            continue
        assert case["jsonl"] or not case["record"]

        segment = tmp_path / f"0900{index:02d}_300"
        segment.mkdir()
        output_path = segment / "screen.jsonl"
        if case["marker"]:
            (segment / ".analyze_failed_screen").write_text("{}\n", encoding="utf-8")
        if case["jsonl"]:
            header = {"raw": "screen.webm"}
            record = case["record"]
            if record is not None:
                header["_solstone_processing"] = record
            lines = [json.dumps(header)]
            if case["rows"]:
                lines.append(json.dumps(row))
            output_path.write_text("\n".join(lines) + "\n", encoding="utf-8")

        record = case["record"]
        state = derive_modality_state(
            segment,
            "screen",
            has_chunks=bool(case["rows"]),
            has_jsonl=bool(case["jsonl"]),
            has_raw=True,
            record=record,
        )
        assert state == case["state"], case["name"]
        if case["jsonl"]:
            reentry = should_reenter_analysis_output(
                record=record,
                output_path=output_path,
                handler=HANDLER_DESCRIBE,
            )
        else:
            # no JSONL + handler exits nonzero without output -> non-terminal but
            # re-attempted each cycle, a visible blocker rather than a silent wedge.
            reentry = True
        assert reentry is case["reentry"], case["name"]
        assert not (state not in terminal_states and not reentry), case["name"]


def test_derive_chunks_win_beats_processing_record(tmp_path) -> None:
    segment = tmp_path / "090000_300"
    segment.mkdir()

    state = derive_modality_state(
        segment,
        "screen",
        has_chunks=True,
        has_jsonl=True,
        has_raw=True,
        record={"state": STATE_EMPTY},
    )

    assert state == DataState.ANALYZED.value


def test_derive_failed_record_beats_chunk_rows(tmp_path) -> None:
    segment = tmp_path / "090000_300"
    segment.mkdir()

    state = derive_modality_state(
        segment,
        "screen",
        has_chunks=True,
        has_jsonl=True,
        has_raw=True,
        record={"state": STATE_FAILED},
    )

    assert state == DataState.FAILED.value


def test_derive_chunk_rows_without_record_are_analyzed(tmp_path) -> None:
    segment = tmp_path / "090000_300"
    segment.mkdir()

    state = derive_modality_state(
        segment,
        "screen",
        has_chunks=True,
        has_jsonl=True,
        has_raw=True,
    )

    assert state == DataState.ANALYZED.value


def test_derive_empty_record_beats_failed_marker(tmp_path) -> None:
    segment = tmp_path / "090000_300"
    segment.mkdir()
    (segment / ".analyze_failed_screen").write_text("{}\n", encoding="utf-8")

    state = derive_modality_state(
        segment,
        "screen",
        has_chunks=False,
        has_jsonl=True,
        has_raw=True,
        record={"state": STATE_EMPTY},
    )

    assert state == DataState.EMPTY.value


def test_derive_empty_record_beats_stale_marker(tmp_path) -> None:
    segment = tmp_path / "090000_300"
    segment.mkdir()
    marker = segment / ".analyzing_screen"
    marker.write_text(
        '{"started_at": "2026-05-20T09:00:00Z", "modality": "screen"}\n',
        encoding="utf-8",
    )
    old_time = time.time() - 2000
    os.utime(marker, (old_time, old_time))

    state = derive_modality_state(
        segment,
        "screen",
        has_chunks=False,
        has_jsonl=True,
        has_raw=True,
        record={"state": STATE_EMPTY},
    )

    assert state == DataState.EMPTY.value
    assert marker.exists()
    assert not (segment / ".analyze_failed_screen").exists()


def test_derive_empty_record_beats_corrupt_marker(tmp_path) -> None:
    segment = tmp_path / "090000_300"
    segment.mkdir()
    marker = segment / ".analyzing_screen"
    marker.write_text("{not json", encoding="utf-8")

    state = derive_modality_state(
        segment,
        "screen",
        has_chunks=False,
        has_jsonl=True,
        has_raw=True,
        record={"state": STATE_EMPTY},
    )

    assert state == DataState.EMPTY.value
    assert marker.exists()
    assert not (segment / ".analyze_failed_screen").exists()


def test_derive_failed_record_maps_to_failed(tmp_path) -> None:
    segment = tmp_path / "090000_300"
    segment.mkdir()

    state = derive_modality_state(
        segment,
        "audio",
        has_chunks=False,
        has_jsonl=True,
        has_raw=True,
        record={"state": STATE_FAILED},
    )

    assert state == DataState.FAILED.value


def test_derive_corrupt_input_failed_record_maps_to_failed_final(tmp_path) -> None:
    segment = tmp_path / "090000_300"
    segment.mkdir()

    for modality in ("audio", "screen"):
        state = derive_modality_state(
            segment,
            modality,
            has_chunks=False,
            has_jsonl=True,
            has_raw=True,
            record={"state": STATE_FAILED, "reason_code": REASON_CORRUPT_INPUT},
        )

        assert state == DataState.FAILED_FINAL.value


def test_derive_attempt_bound_failed_record_maps_to_failed_final(tmp_path) -> None:
    segment = tmp_path / "090000_300"
    segment.mkdir()

    state = derive_modality_state(
        segment,
        "screen",
        has_chunks=False,
        has_jsonl=True,
        has_raw=True,
        record={
            "state": STATE_FAILED,
            "reason_code": REASON_ANALYSIS_FAILED,
            "attempts": FAILED_ATTEMPT_BOUND,
        },
    )

    assert state == DataState.FAILED_FINAL.value


def test_derive_unexhausted_failed_record_stays_failed(tmp_path) -> None:
    segment = tmp_path / "090000_300"
    segment.mkdir()

    state = derive_modality_state(
        segment,
        "screen",
        has_chunks=False,
        has_jsonl=True,
        has_raw=True,
        record={
            "state": STATE_FAILED,
            "reason_code": REASON_ANALYSIS_FAILED,
            "attempts": FAILED_ATTEMPT_BOUND - 1,
        },
    )

    assert state == DataState.FAILED.value


def test_derive_ignores_missing_or_unknown_record_state(tmp_path) -> None:
    segment = tmp_path / "090000_300"
    segment.mkdir()

    for record in ({}, {"state": "surprise"}):
        assert (
            derive_modality_state(
                segment,
                "audio",
                has_chunks=False,
                has_jsonl=True,
                has_raw=False,
                record=record,
            )
            == DataState.PENDING.value
        )


def test_derive_no_record_preserves_legacy_result(tmp_path) -> None:
    segment = tmp_path / "090000_300"
    segment.mkdir()

    assert (
        derive_modality_state(
            segment,
            "audio",
            has_chunks=False,
            has_jsonl=True,
            has_raw=False,
        )
        == DataState.PENDING.value
    )
