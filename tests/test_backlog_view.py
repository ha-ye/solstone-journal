# SPDX-License-Identifier: AGPL-3.0-only
# Copyright (c) 2026 sol pbc

from __future__ import annotations

from solstone.convey import backlog_copy
from solstone.convey.backlog_view import stuck_rows


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
            "reason": backlog_copy.BACKLOG_REASON_CORRUPT_RAW,
            "depth": 1,
        },
    ]
