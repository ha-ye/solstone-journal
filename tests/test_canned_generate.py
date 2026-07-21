# SPDX-License-Identifier: AGPL-3.0-only
# Copyright (c) 2026 sol pbc

from __future__ import annotations

import pytest

from solstone.think.providers.shared import classify_canned_generate


@pytest.mark.parametrize(
    ("result", "expected"),
    [
        ({"text": "OK", "finish_reason": "max_tokens"}, "starved"),
        ({"text": "OK", "finish_reason": "stop"}, "pass"),
        ({"text": "OK"}, "pass"),
        ({"text": "", "finish_reason": "stop"}, "invalid"),
        ({"text": "   ", "finish_reason": "stop"}, "invalid"),
        ({"text": 123, "finish_reason": "stop"}, "invalid"),
        (
            {
                "text": "",
                "finish_reason": "stop",
                "usage": {"reasoning_tokens": 4},
            },
            "starved",
        ),
        (
            {
                "text": "",
                "finish_reason": "stop",
                "thinking": [{"summary": "reasoned"}],
            },
            "starved",
        ),
        ({"text": "", "finish_reason": None}, "starved"),
        ({"text": "   "}, "starved"),
        ({"text": "", "finish_reason": "unknown"}, "starved"),
    ],
)
def test_classify_canned_generate_branches(result, expected):
    assert classify_canned_generate(result) == expected
