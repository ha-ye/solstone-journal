# SPDX-License-Identifier: AGPL-3.0-only
# Copyright (c) 2026 sol pbc

from __future__ import annotations

import pytest

from solstone.convey import backlog_copy
from solstone.convey.backlog_view import stuck_day_rows, stuck_rows, verdict


def _assert_single_stuck_reason(reason_code: str, expected: str) -> None:
    rows = stuck_rows(
        {
            "days": [
                {
                    "day": "20260601",
                    "state": "stuck",
                    "segments": 1,
                    "units": 0,
                    "reason": "failing_step",
                    "reason_code": reason_code,
                }
            ],
            "errors": [],
        }
    )

    assert rows[0]["reason"] == expected


@pytest.mark.parametrize(
    ("reason_code", "expected"),
    [
        ("local_model_installing", backlog_copy.BACKLOG_REASON_PROVIDER_DOWN),
        ("local_model_loading", backlog_copy.BACKLOG_REASON_PROVIDER_DOWN),
        ("local_model_not_ready", backlog_copy.BACKLOG_REASON_PROVIDER_DOWN),
        ("provider_key_missing", backlog_copy.BACKLOG_REASON_MISSING_CONFIG),
        ("local_model_missing", backlog_copy.BACKLOG_REASON_MISSING_CONFIG),
        ("unsupported_platform", backlog_copy.BACKLOG_REASON_MISSING_CONFIG),
        ("gpu_probe_failed", backlog_copy.BACKLOG_REASON_MISSING_CONFIG),
        ("provider_unavailable", backlog_copy.BACKLOG_REASON_PROVIDER_DOWN),
        ("local_server_unhealthy", backlog_copy.BACKLOG_REASON_PROVIDER_DOWN),
        ("local_endpoint_unreachable", backlog_copy.BACKLOG_REASON_PROVIDER_DOWN),
        ("provider_quota_exceeded", backlog_copy.BACKLOG_REASON_PROVIDER_DOWN),
        ("provider_key_invalid", backlog_copy.BACKLOG_REASON_PROVIDER_DOWN),
        ("provider_request_rejected", backlog_copy.BACKLOG_REASON_PROVIDER_REFUSED),
        ("catchup_backoff", backlog_copy.BACKLOG_REASON_FAILING_STEP),
        ("totally_made_up_code", backlog_copy.BACKLOG_REASON_FAILING_STEP),
    ],
)
def test_stuck_rows_maps_reason_categories_to_backlog_copy(
    reason_code: str, expected: str
):
    _assert_single_stuck_reason(reason_code, expected)


def test_stuck_rows_provider_request_rejected_copy_is_distinct_and_actionable():
    _assert_single_stuck_reason(
        "provider_request_rejected",
        backlog_copy.BACKLOG_REASON_PROVIDER_REFUSED,
    )

    reason = backlog_copy.BACKLOG_REASON_PROVIDER_REFUSED
    assert "try again" not in reason
    assert "unreachable" not in reason
    assert reason not in {
        backlog_copy.BACKLOG_REASON_CORRUPT_RAW,
        backlog_copy.BACKLOG_REASON_FAILING_STEP,
        backlog_copy.BACKLOG_REASON_MISSING_CONFIG,
        backlog_copy.BACKLOG_REASON_PROVIDER_DOWN,
    }


def test_stuck_rows_maps_readiness_reasons_and_carries_operator_fields():
    backlog = {
        "days": [
            {
                "day": "20260601",
                "state": "stuck",
                "segments": 1,
                "units": 0,
                "reason": "failing_step",
                "reason_code": "provider_key_missing",
                "provider": "anthropic",
                "model": "claude-test",
            },
            {
                "day": "20260602",
                "state": "stuck",
                "segments": 0,
                "units": 2,
                "reason": "provider_unavailable",
                "reason_code": "provider_unavailable",
                "provider": "openai",
                "model": "gpt-test",
            },
            {
                "day": "20260603",
                "state": "stuck",
                "segments": 0,
                "units": 3,
                "reason": "failing_step",
                "reason_code": "provider_quota_exceeded",
                "provider": "anthropic",
                "model": "claude-test",
            },
            {
                "day": "20260604",
                "state": "stuck",
                "segments": 0,
                "units": 1,
                "reason": "failing_step",
                "reason_code": "gpu_unavailable",
                "provider": "local",
                "model": "local/qwen3.5-4b",
            },
            {
                "day": "20260605",
                "state": "stuck",
                "segments": 0,
                "units": 1,
                "reason": "corrupt_raw",
            },
        ],
        "errors": [],
    }

    rows = stuck_rows(backlog)

    assert rows == [
        {
            "day": "20260601",
            "reason": backlog_copy.BACKLOG_REASON_MISSING_CONFIG,
            "depth": 1,
            "reason_code": "provider_key_missing",
            "provider": "anthropic",
            "model": "claude-test",
        },
        {
            "day": "20260602",
            "reason": backlog_copy.BACKLOG_REASON_PROVIDER_DOWN,
            "depth": 2,
            "reason_code": "provider_unavailable",
            "provider": "openai",
            "model": "gpt-test",
        },
        {
            "day": "20260603",
            "reason": backlog_copy.BACKLOG_REASON_PROVIDER_DOWN,
            "depth": 3,
            "reason_code": "provider_quota_exceeded",
            "provider": "anthropic",
            "model": "claude-test",
        },
        {
            "day": "20260604",
            "reason": backlog_copy.BACKLOG_REASON_MISSING_CONFIG,
            "depth": 1,
            "reason_code": "gpu_unavailable",
            "provider": "local",
            "model": "local/qwen3.5-4b",
        },
        {
            "day": "20260605",
            "reason": backlog_copy.BACKLOG_REASON_CORRUPT_RAW,
            "depth": 1,
        },
    ]


def test_stuck_day_rows_excludes_pending_error_rows_without_changing_row_shape():
    backlog = {
        "days": [
            {
                "day": "20260601",
                "state": "pending",
                "error": {"reason_code": "provider_request_rejected"},
                "reason_code": "provider_request_rejected",
                "provider": "google",
            },
            {
                "day": "20260602",
                "state": "stuck",
                "segments": 1,
                "reason_code": "provider_request_rejected",
                "provider": "google",
            },
        ],
        "errors": [],
    }

    assert stuck_rows(backlog) == [
        {
            "day": "20260601",
            "reason": backlog_copy.BACKLOG_REASON_PROVIDER_REFUSED,
            "depth": None,
            "reason_code": "provider_request_rejected",
            "provider": "google",
        },
        {
            "day": "20260602",
            "reason": backlog_copy.BACKLOG_REASON_PROVIDER_REFUSED,
            "depth": 1,
            "reason_code": "provider_request_rejected",
            "provider": "google",
        },
    ]
    assert stuck_day_rows(backlog) == [
        {
            "day": "20260602",
            "reason": backlog_copy.BACKLOG_REASON_PROVIDER_REFUSED,
            "depth": 1,
            "reason_code": "provider_request_rejected",
            "provider": "google",
        }
    ]


def test_verdict_pending_only_copy_does_not_claim_caught_up():
    assert (
        verdict({"pending_days": 1, "stuck_days": 0}) == "1 day is still catching up."
    )
    assert (
        verdict({"pending_days": 3, "stuck_days": 0}) == "3 days are still catching up."
    )


def test_verdict_degraded_backlog_does_not_claim_caught_up():
    assert (
        verdict({"pending_days": 0, "stuck_days": 0, "degraded": True})
        != backlog_copy.BACKLOG_VERDICT_CAUGHT_UP
    )


def test_stuck_rows_includes_segment_repair_stuck_day():
    rows = stuck_rows(
        {
            "days": [
                {
                    "day": "20260601",
                    "state": "stuck",
                    "segments": 0,
                    "units": 0,
                    "reason": "segment_repair_stuck",
                    "reason_code": "segment_repair_stuck",
                    "segment_repair_status": "stuck",
                    "segment_repair_attempts": 3,
                    "segment_repair_consecutive_non_completion": 3,
                    "segment_repair_last_outcome": "timeout",
                    "segment_repair_next_retry_at": 1600.0,
                    "segment_repair_reason_code": "wall_clock_exceeded",
                    "segment_repair_timeout_seconds": 300,
                    "segment_repair_bounded": True,
                }
            ],
            "errors": [],
        }
    )

    assert rows == [
        {
            "day": "20260601",
            "reason": backlog_copy.BACKLOG_REASON_FAILING_STEP,
            "depth": None,
            "reason_code": "segment_repair_stuck",
        }
    ]


def test_verdict_mixed_copy_uses_independent_arms():
    assert (
        verdict({"pending_days": 1, "stuck_days": 1})
        == "1 day needs a hand. 1 more day is still catching up."
    )
    assert (
        verdict({"pending_days": 1, "stuck_days": 2})
        == "2 days need a hand. 1 more day is still catching up."
    )
    assert (
        verdict({"pending_days": 3, "stuck_days": 1})
        == "1 day needs a hand. 3 more days are still catching up."
    )
    assert (
        verdict({"pending_days": 3, "stuck_days": 2})
        == "2 days need a hand. 3 more days are still catching up."
    )
