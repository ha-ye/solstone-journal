# SPDX-License-Identifier: AGPL-3.0-only
# Copyright (c) 2026 sol pbc

from __future__ import annotations

import json
from pathlib import Path

from solstone.convey.sol_initiated.copy import KIND_SOL_CHAT_REQUEST
from solstone.think.push import runtime, triggers


def _read_log(journal: Path) -> list[dict[str, object]]:
    path = journal / "push" / "nudge_log.jsonl"
    return [json.loads(line) for line in path.read_text(encoding="utf-8").splitlines()]


def test_push_runtime_relay_dispatch_and_log(journal_copy, monkeypatch):
    monkeypatch.setattr(triggers, "push_relay_token", lambda: "tok")
    monkeypatch.setattr(
        triggers,
        "load_devices",
        lambda: [
            {
                "fingerprint": "fp-1",
                "token": "a" * 64,
                "bundle_id": "org.solpbc.solstone-swift",
                "environment": "development",
                "platform": "ios",
                "registered_at": 1,
            }
        ],
    )
    calls: list[dict[str, str]] = []
    monkeypatch.setattr(
        triggers,
        "dispatch_via_portal",
        lambda **kwargs: calls.append(kwargs) or {"ok": True},
    )

    runtime._on_callosum_message(
        {
            "tract": "chat",
            "event": KIND_SOL_CHAT_REQUEST,
            "request_id": "req-1",
            "summary": "relay me",
            "category": "notice",
        }
    )

    assert calls == [
        {"request_id": "req-1", "summary": "relay me", "category": "notice"}
    ]
    rows = _read_log(journal_copy)
    assert rows == [
        {
            "ts": rows[0]["ts"],
            "kind": f"{KIND_SOL_CHAT_REQUEST}_push",
            "dedupe_key": "req-1",
            "category": "notice",
            "outcome": "dispatched",
            "via": "portal",
        }
    ]
