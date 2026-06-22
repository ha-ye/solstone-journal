# SPDX-License-Identifier: AGPL-3.0-only
# Copyright (c) 2026 sol pbc

from __future__ import annotations

import pytest

from solstone.convey import backlog_copy
from solstone.convey.backlog_view import stuck_rows, verdict


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
        ("catchup_backoff", backlog_copy.BACKLOG_REASON_FAILING_STEP),
        ("totally_made_up_code", backlog_copy.BACKLOG_REASON_FAILING_STEP),
    ],
)
def test_stuck_rows_maps_reason_categories_to_backlog_copy(
    reason_code: str, expected: str
):
    _assert_single_stuck_reason(reason_code, expected)


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


def test_verdict_pending_only_copy_does_not_claim_caught_up():
    assert (
        verdict({"pending_days": 1, "stuck_days": 0}) == "1 day is still catching up."
    )
    assert (
        verdict({"pending_days": 3, "stuck_days": 0}) == "3 days are still catching up."
    )


def test_verdict_mixed_copy_uses_independent_arms():
    assert (
        verdict({"pending_days": 1, "stuck_days": 1})
        == "1 day needs a hand — 1 more day is still catching up."
    )
    assert (
        verdict({"pending_days": 1, "stuck_days": 2})
        == "2 days need a hand — 1 more day is still catching up."
    )
    assert (
        verdict({"pending_days": 3, "stuck_days": 1})
        == "1 day needs a hand — 3 more days are still catching up."
    )
    assert (
        verdict({"pending_days": 3, "stuck_days": 2})
        == "2 days need a hand — 3 more days are still catching up."
    )
