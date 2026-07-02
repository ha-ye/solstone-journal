# SPDX-License-Identifier: AGPL-3.0-only
# Copyright (c) 2026 sol pbc

"""Provider/model attribution on talent.fail health records."""

from __future__ import annotations

import json
from pathlib import Path


def _capture_jsonl(monkeypatch, mod):
    records: list[dict] = []

    def log(event: str, **fields) -> None:
        records.append({"event": event, **fields})

    def capture_emit(event_name: str, **kwargs) -> None:
        records.append({"event": f"emit:{event_name}", **kwargs})

    monkeypatch.setattr(mod, "_jsonl_log", log)
    monkeypatch.setattr(mod, "emit", capture_emit)
    monkeypatch.setattr(mod, "_update_status", lambda **kwargs: None)
    monkeypatch.setattr(mod, "day_log", lambda *args, **kwargs: None)
    return records


def _use_log_status(use_id: str) -> str:
    if use_id.endswith("lost"):
        return "not_found"
    if use_id.endswith("running"):
        return "running"
    return "completed"


def _provider_model_reason(use_id: str) -> tuple[str | None, str | None, str | None]:
    if use_id.endswith("lost"):
        return (None, None, None)
    if use_id.endswith("timeout"):
        return ("google", "gemini-2.5-pro", None)
    if use_id.endswith("error"):
        return (None, None, "provider_key_missing")
    return ("openai", "gpt-5", None)


def _write_activity_record(
    journal: Path,
    day: str,
    facet: str,
    activity_id: str,
) -> None:
    path = journal / "facets" / facet / "activities" / f"{day}.jsonl"
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        json.dumps(
            {
                "id": activity_id,
                "activity": "coding",
                "segments": ["100000_300"],
                "description": "Coding",
                "active_entities": [],
            }
        )
        + "\n",
        encoding="utf-8",
    )


def test_priority_fail_records_provider_model_and_null_when_missing(monkeypatch):
    from solstone.think import thinking

    records = _capture_jsonl(monkeypatch, thinking)
    monkeypatch.setattr(thinking, "get_use_log_status", _use_log_status)
    monkeypatch.setattr(
        thinking,
        "read_use_provider_model_reason",
        _provider_model_reason,
    )
    monkeypatch.setattr(
        thinking,
        "wait_for_uses",
        lambda ids, timeout: ({}, ["use-lost", "use-running", "use-completed"]),
    )

    _, _, failed_names = thinking._drain_priority_batch(
        [
            ("use-lost", "lost", {"type": "cogitate"}, None),
            ("use-running", "running", {"type": "cogitate"}, None),
            ("use-completed", "completed", {"type": "cogitate"}, None),
        ],
        "segment",
        "20240101",
        "100000_300",
        stream="default",
    )

    assert "lost (request_lost)" in failed_names
    assert "running (timeout)" in failed_names

    monkeypatch.setattr(
        thinking,
        "wait_for_uses",
        lambda ids, timeout: ({"use-error": "error"}, []),
    )
    thinking._drain_priority_batch(
        [("use-error", "error", {"type": "cogitate"}, None)],
        "segment",
        "20240101",
        "100000_300",
        stream="default",
    )

    failures = {
        record["name"]: record for record in records if record["event"] == "talent.fail"
    }
    assert failures["lost"]["state"] == "request_lost"
    assert failures["lost"]["provider"] is None
    assert failures["lost"]["model"] is None
    assert failures["running"]["state"] == "timeout"
    assert failures["completed"]["state"] == "timeout"
    assert failures["error"]["state"] == "error"
    assert failures["error"]["provider"] is None
    assert failures["error"]["model"] is None
    assert failures["error"]["reason_code"] == "provider_key_missing"

    emits = {
        record["name"]: record
        for record in records
        if record["event"] == "emit:talent_completed"
    }
    assert emits["lost"]["state"] == "request_lost"


def test_activity_fail_records_provider_model_on_timeout_and_terminal(
    tmp_path,
    monkeypatch,
):
    from solstone.think import thinking

    journal = tmp_path / "journal"
    monkeypatch.setenv("SOLSTONE_JOURNAL", str(journal))
    _write_activity_record(journal, "20240101", "work", "coding_100000_300")
    records = _capture_jsonl(monkeypatch, thinking)
    monkeypatch.setattr(thinking, "get_use_log_status", _use_log_status)
    monkeypatch.setattr(
        thinking,
        "read_use_provider_model_reason",
        _provider_model_reason,
    )
    monkeypatch.setattr(
        thinking,
        "get_talent_configs",
        lambda schedule: {
            "lost": {
                "type": "cogitate",
                "priority": 10,
                "activities": ["coding"],
            },
            "running": {
                "type": "cogitate",
                "priority": 10,
                "activities": ["coding"],
            },
            "completed": {
                "type": "cogitate",
                "priority": 10,
                "activities": ["coding"],
            },
            "error": {
                "type": "cogitate",
                "priority": 10,
                "activities": ["coding"],
            },
        },
    )
    monkeypatch.setattr(
        thinking,
        "_cortex_request_with_retry",
        lambda **kwargs: f"use-{kwargs['name']}",
    )
    monkeypatch.setattr(
        thinking,
        "wait_for_uses",
        lambda ids, timeout: (
            {"use-error": "error"},
            ["use-lost", "use-running", "use-completed"],
        ),
    )

    assert (
        thinking.run_activity_prompts(
            day="20240101",
            activity_id="coding_100000_300",
            facet="work",
            max_concurrency=0,
        )
        is False
    )

    failures = {
        record["name"]: record for record in records if record["event"] == "talent.fail"
    }
    assert failures["lost"]["state"] == "request_lost"
    assert failures["lost"]["provider"] is None
    assert failures["lost"]["model"] is None
    assert failures["running"]["state"] == "timeout"
    assert failures["completed"]["state"] == "timeout"
    assert failures["error"]["state"] == "error"
    assert failures["error"]["reason_code"] == "provider_key_missing"

    emits = {
        record["name"]: record
        for record in records
        if record["event"] == "emit:talent_completed"
    }
    assert emits["lost"]["state"] == "request_lost"


def test_flush_fail_records_provider_model_on_timeout_and_terminal(monkeypatch):
    from solstone.think import thinking

    records = _capture_jsonl(monkeypatch, thinking)
    monkeypatch.setattr(thinking, "get_use_log_status", _use_log_status)
    monkeypatch.setattr(
        thinking,
        "read_use_provider_model_reason",
        _provider_model_reason,
    )
    monkeypatch.setattr(
        thinking,
        "get_talent_configs",
        lambda schedule: {
            "lost": {
                "type": "cogitate",
                "priority": 10,
                "hook": {"flush": True},
            },
            "running": {
                "type": "cogitate",
                "priority": 10,
                "hook": {"flush": True},
            },
            "completed": {
                "type": "cogitate",
                "priority": 10,
                "hook": {"flush": True},
            },
            "error": {
                "type": "cogitate",
                "priority": 10,
                "hook": {"flush": True},
            },
        },
    )
    monkeypatch.setattr(
        thinking,
        "_cortex_request_with_retry",
        lambda **kwargs: f"use-{kwargs['name']}",
    )
    monkeypatch.setattr(
        thinking,
        "wait_for_uses",
        lambda ids, timeout: (
            {"use-error": "error"},
            ["use-lost", "use-running", "use-completed"],
        ),
    )

    assert thinking.run_flush_prompts("20240101", "100000_300", False) is False

    failures = {
        record["name"]: record for record in records if record["event"] == "talent.fail"
    }
    assert failures["lost"]["state"] == "request_lost"
    assert failures["lost"]["provider"] is None
    assert failures["lost"]["model"] is None
    assert failures["running"]["state"] == "timeout"
    assert failures["completed"]["state"] == "timeout"
    assert failures["error"]["state"] == "error"
    assert failures["error"]["reason_code"] == "provider_key_missing"
