# SPDX-License-Identifier: AGPL-3.0-only
# Copyright (c) 2026 sol pbc

import importlib.util
import json
import re
import sys
from datetime import datetime
from pathlib import Path

from solstone.convey.chat_stream import append_chat_event
from solstone.convey.sol_initiated.copy import KIND_SOL_CHAT_REQUEST
from solstone.think.day_accumulator import append_record

TEMPLATE_VAR_KEYS = {
    "active_talents",
    "trigger_kind",
    "trigger_context",
    "summary",
    "message",
    "category",
    "since_ts",
    "trigger_talent",
    "location",
    "situational",
}


def _load_chat_context_module():
    path = (
        Path(__file__).resolve().parents[1] / "solstone" / "talent" / "chat_context.py"
    )
    spec = importlib.util.spec_from_file_location("test_chat_context_local", path)
    assert spec is not None
    assert spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def _assert_template_vars_result(result):
    assert isinstance(result, dict)
    assert "template_vars" in result
    assert "user_instruction" not in result
    assert set(result["template_vars"]) == TEMPLATE_VAR_KEYS
    return result["template_vars"]


def _write_journal_config(journal: Path, data: dict) -> None:
    config_dir = journal / "config"
    config_dir.mkdir(parents=True, exist_ok=True)
    (config_dir / "journal.json").write_text(
        json.dumps(data, indent=2),
        encoding="utf-8",
    )


def _ts(hour: int, minute: int, second: int = 0) -> int:
    return int(datetime(2026, 4, 20, hour, minute, second).timestamp() * 1000)


def _append_owner_message(text: str, ts: int, **extra) -> None:
    append_chat_event(
        "owner_message",
        ts=ts,
        text=text,
        app=extra.pop("app", "home"),
        path=extra.pop("path", "/app/home"),
        facet=extra.pop("facet", "work"),
        **extra,
    )


def _chat_prompt_path() -> Path:
    return Path(__file__).resolve().parents[1] / "solstone" / "talent" / "chat.md"


def _chat_prompt_text() -> str:
    return _chat_prompt_path().read_text(encoding="utf-8")


def _chat_prompt_frontmatter() -> dict:
    text = _chat_prompt_text()
    metadata, end = json.JSONDecoder().raw_decode(text)
    assert isinstance(metadata, dict)
    assert text[end:].startswith("\n\n")
    return metadata


def test_chat_context_injects_tail_trigger_location(monkeypatch, tmp_path):
    journal = tmp_path / "journal"
    monkeypatch.setenv("SOLSTONE_JOURNAL", str(journal))
    _write_journal_config(
        journal,
        {
            "identity": {"preferred": "Alice"},
            "agent": {"name": "Sol-agent", "name_status": "custom"},
        },
    )

    owner_ts = _ts(9, 0)
    append_chat_event(
        "owner_message",
        ts=owner_ts,
        text="Please brief me for my meeting",
        app="home",
        path="/app/home",
        facet="work",
    )
    append_chat_event(
        "sol_message",
        ts=_ts(9, 1),
        use_id="use-chat-1",
        text="I can help with that.",
        notes="Responded directly.",
        requested_target=None,
        requested_task=None,
    )
    append_chat_event(
        "talent_spawned",
        ts=_ts(9, 2),
        use_id="use-exec-1",
        name="exec",
        task="Prepare the meeting brief",
        started_at=_ts(9, 2),
    )

    result = _load_chat_context_module().pre_process(
        {
            "prompt": "Please brief me for my meeting",
            "facet": "work",
            "day": "20260420",
            "app": "home",
            "path": "/app/home",
            "trigger": {
                "type": "owner_message",
                "message": "Please brief me for my meeting",
                "ts": owner_ts,
            },
        }
    )

    template_vars = _assert_template_vars_result(result)
    assert result["messages"] == [
        {"role": "user", "content": "Please brief me for my meeting"},
        {"role": "assistant", "content": "I can help with that."},
        {"role": "user", "content": "Please brief me for my meeting"},
    ]
    assert all("exec spawned" not in msg["content"] for msg in result["messages"])
    assert "## Active Talents" in template_vars["active_talents"]
    assert "Prepare the meeting brief" in template_vars["active_talents"]
    assert template_vars["trigger_context"] == ""
    assert "## Location" in template_vars["location"]
    assert "/app/home" in template_vars["location"]
    assert "work" in template_vars["location"]


def test_chat_prompt_has_no_digest_slot():
    prompt = _chat_prompt_text()

    assert "$digest_contents" not in prompt
    assert "digest" not in prompt.lower()


def test_render_situational_uses_pulse_record_with_cold_open_lookback(
    monkeypatch, tmp_path
):
    journal = tmp_path / "journal"
    monkeypatch.setenv("SOLSTONE_JOURNAL", str(journal))
    module = _load_chat_context_module()

    assert module._render_situational("20260420") == ""

    append_record(
        "20260419",
        "pulse",
        {
            "title": "Yesterday's read",
            "one_sentence": "Yesterday still has the freshest context.",
            "full_details": "The prior day ended with a useful shape.",
            "needs_you": ["Review the carryover note."],
            "ts": 100,
        },
    )

    cold_open = module._render_situational("20260420")
    assert "Yesterday's read" in cold_open
    assert "Yesterday still has the freshest context." in cold_open
    assert "- Review the carryover note." in cold_open

    append_record(
        "20260420",
        "pulse",
        {
            "title": "Today's read",
            "one_sentence": "Today now has a current pulse.",
            "full_details": "The current day has taken over the situational read.",
            "needs_you": ["Prep the current meeting."],
            "ts": 200,
        },
    )

    current = module._render_situational("20260420")
    assert "Today's read" in current
    assert "Today now has a current pulse." in current
    assert "- Prep the current meeting." in current
    assert "Yesterday's read" not in current


def test_chat_context_owner_message_anchors_empty_tail(monkeypatch, tmp_path):
    journal = tmp_path / "journal"
    monkeypatch.setenv("SOLSTONE_JOURNAL", str(journal))

    result = _load_chat_context_module().pre_process(
        {
            "prompt": "what's my name?",
            "day": "20260420",
            "trigger": {
                "type": "owner_message",
                "message": "what's my name?",
                "ts": _ts(8, 0),
            },
        }
    )

    assert result["messages"] == [{"role": "user", "content": "what's my name?"}]


def test_chat_context_owner_message_anchors_after_sol_message(monkeypatch, tmp_path):
    journal = tmp_path / "journal"
    monkeypatch.setenv("SOLSTONE_JOURNAL", str(journal))

    _append_owner_message("thanks", _ts(8, 0))
    append_chat_event(
        "sol_message",
        ts=_ts(8, 1),
        use_id="use-chat-anchor",
        text="Anytime!",
        notes="Responded directly.",
        requested_target=None,
        requested_task=None,
    )

    result = _load_chat_context_module().pre_process(
        {
            "day": "20260420",
            "trigger": {
                "type": "owner_message",
                "message": "what happened?",
                "ts": _ts(8, 2),
            },
        }
    )

    assert result["messages"] == [
        {"role": "user", "content": "thanks"},
        {"role": "assistant", "content": "Anytime!"},
        {"role": "user", "content": "what happened?"},
    ]


def test_chat_context_owner_message_anchor_is_idempotent(monkeypatch, tmp_path):
    journal = tmp_path / "journal"
    monkeypatch.setenv("SOLSTONE_JOURNAL", str(journal))

    _append_owner_message("what's my name?", _ts(8, 0))

    result = _load_chat_context_module().pre_process(
        {
            "day": "20260420",
            "trigger": {
                "type": "owner_message",
                "message": "what's my name?",
                "ts": _ts(8, 0),
            },
        }
    )

    assert result["messages"] == [{"role": "user", "content": "what's my name?"}]


def test_chat_context_owner_message_anchors_after_different_user(monkeypatch, tmp_path):
    journal = tmp_path / "journal"
    monkeypatch.setenv("SOLSTONE_JOURNAL", str(journal))

    _append_owner_message("thanks", _ts(8, 0))

    result = _load_chat_context_module().pre_process(
        {
            "day": "20260420",
            "trigger": {
                "type": "owner_message",
                "message": "what's my name?",
                "ts": _ts(8, 1),
            },
        }
    )

    assert result["messages"] == [
        {"role": "user", "content": "thanks"},
        {"role": "user", "content": "what's my name?"},
    ]


def test_chat_context_prompt_only_owner_message_anchors(monkeypatch, tmp_path):
    journal = tmp_path / "journal"
    monkeypatch.setenv("SOLSTONE_JOURNAL", str(journal))

    result = _load_chat_context_module().pre_process(
        {"prompt": "what's my name?", "day": "20260420"}
    )

    assert result["messages"] == [{"role": "user", "content": "what's my name?"}]


def test_chat_context_owner_message_anchors_when_tail_read_fails(monkeypatch, tmp_path):
    journal = tmp_path / "journal"
    monkeypatch.setenv("SOLSTONE_JOURNAL", str(journal))
    module = _load_chat_context_module()

    def _boom(*_args, **_kwargs):
        raise RuntimeError("boom")

    monkeypatch.setattr(module, "read_chat_tail", _boom)

    result = module.pre_process(
        {
            "prompt": "what's my name?",
            "day": "20260420",
            "trigger": {
                "type": "owner_message",
                "message": "what's my name?",
                "ts": _ts(8, 0),
            },
        }
    )

    assert result["messages"] == [{"role": "user", "content": "what's my name?"}]


def test_chat_context_talent_finished_does_not_owner_anchor(monkeypatch, tmp_path):
    journal = tmp_path / "journal"
    monkeypatch.setenv("SOLSTONE_JOURNAL", str(journal))

    _append_owner_message("What happened?", _ts(8, 0))
    append_chat_event(
        "sol_message",
        ts=_ts(8, 1),
        use_id="use-chat-finished-anchor",
        text="Looking into it.",
        notes="Acknowledged request.",
        requested_target=None,
        requested_task=None,
    )

    result = _load_chat_context_module().pre_process(
        {
            "day": "20260420",
            "trigger": {
                "type": "talent_finished",
                "name": "exec",
                "summary": "Found the latest notes.",
            },
        }
    )

    assert len(result["messages"]) == 3
    assert result["messages"][1] == {
        "role": "assistant",
        "content": "Looking into it.",
    }
    assert result["messages"][-1]["role"] == "user"
    assert result["messages"][-1]["content"].startswith("[internal follow-up:")


def test_chat_context_talent_errored_does_not_owner_anchor(monkeypatch, tmp_path):
    journal = tmp_path / "journal"
    monkeypatch.setenv("SOLSTONE_JOURNAL", str(journal))

    _append_owner_message("What happened?", _ts(8, 0))
    append_chat_event(
        "sol_message",
        ts=_ts(8, 1),
        use_id="use-chat-errored-anchor",
        text="Looking into it.",
        notes="Acknowledged request.",
        requested_target=None,
        requested_task=None,
    )

    result = _load_chat_context_module().pre_process(
        {
            "day": "20260420",
            "trigger": {
                "type": "talent_errored",
                "name": "exec",
                "reason": "The lookup failed.",
            },
        }
    )

    assert len(result["messages"]) == 3
    assert result["messages"][1] == {
        "role": "assistant",
        "content": "Looking into it.",
    }
    assert result["messages"][-1]["role"] == "user"
    assert result["messages"][-1]["content"].startswith("[internal follow-up:")


def test_chat_context_owner_message_renders_empty_trigger_context(
    monkeypatch, tmp_path
):
    journal = tmp_path / "journal"
    monkeypatch.setenv("SOLSTONE_JOURNAL", str(journal))

    result = _load_chat_context_module().pre_process(
        {
            "prompt": "What is on my calendar today?",
            "day": "20260420",
            "trigger": {
                "type": "owner_message",
                "message": "What is on my calendar today?",
                "ts": _ts(8, 0),
            },
        }
    )

    template_vars = _assert_template_vars_result(result)
    assert template_vars["trigger_context"] == ""


def test_chat_context_sol_initiated_still_renders_trigger_context(
    monkeypatch, tmp_path
):
    journal = tmp_path / "journal"
    monkeypatch.setenv("SOLSTONE_JOURNAL", str(journal))

    result = _load_chat_context_module().pre_process(
        {
            "day": "20260420",
            "trigger": {
                "type": KIND_SOL_CHAT_REQUEST,
                "summary": "Notice this",
                "message": "Here is why.",
                "category": "attention",
                "since_ts": 1_775_000_000_000,
            },
        }
    )

    template_vars = _assert_template_vars_result(result)
    assert "## Trigger Context" in template_vars["trigger_context"]
    assert "- Summary: Notice this" in template_vars["trigger_context"]


def test_chat_context_talent_finished_still_renders_trigger_context(
    monkeypatch, tmp_path
):
    journal = tmp_path / "journal"
    monkeypatch.setenv("SOLSTONE_JOURNAL", str(journal))

    result = _load_chat_context_module().pre_process(
        {
            "day": "20260420",
            "trigger": {
                "type": "talent_finished",
                "name": "exec",
                "summary": "Found the latest notes.",
            },
        }
    )

    template_vars = _assert_template_vars_result(result)
    assert "## Trigger Context" in template_vars["trigger_context"]
    assert "- Talent: exec" in template_vars["trigger_context"]


def test_chat_context_talent_errored_still_renders_trigger_context(
    monkeypatch, tmp_path
):
    journal = tmp_path / "journal"
    monkeypatch.setenv("SOLSTONE_JOURNAL", str(journal))

    result = _load_chat_context_module().pre_process(
        {
            "day": "20260420",
            "trigger": {
                "type": "talent_errored",
                "name": "exec",
                "reason": "The lookup failed.",
            },
        }
    )

    template_vars = _assert_template_vars_result(result)
    assert "## Trigger Context" in template_vars["trigger_context"]
    assert "- Talent: exec" in template_vars["trigger_context"]


def test_chat_context_owner_message_needs_you_source_becomes_trigger_context(
    monkeypatch, tmp_path
):
    journal = tmp_path / "journal"
    monkeypatch.setenv("SOLSTONE_JOURNAL", str(journal))
    source = {"kind": "needs_you", "item_text": "Review the launch checklist"}

    _append_owner_message("Review the launch checklist", _ts(8, 0), source=source)

    result = _load_chat_context_module().pre_process(
        {
            "prompt": "Review the launch checklist",
            "day": "20260420",
            "trigger": {
                "type": "owner_message",
                "message": "Review the launch checklist",
                "ts": _ts(8, 0),
            },
        }
    )

    expected = (
        "The owner reached this conversation from their Needs You tile: "
        '"Review the launch checklist". Be useful on this topic -- no need '
        "to call out where it came from."
    )
    assert result["template_vars"]["trigger_context"] == expected
    assert result["template_vars"]["trigger_context"].startswith(
        "The owner reached this conversation from their Needs You tile"
    )
    assert "## Trigger Context" not in result["template_vars"]["trigger_context"]


def test_chat_context_talent_finished_marks_stop_and_report(monkeypatch, tmp_path):
    journal = tmp_path / "journal"
    monkeypatch.setenv("SOLSTONE_JOURNAL", str(journal))

    append_chat_event(
        "owner_message",
        ts=_ts(10, 0),
        text="What happened?",
        app="home",
        path="/app/home",
        facet="work",
    )
    append_chat_event(
        "sol_message",
        ts=_ts(10, 1),
        use_id="use-chat-2",
        text="Looking into it.",
        notes="Acknowledged request.",
        requested_target=None,
        requested_task=None,
    )
    append_chat_event(
        "talent_finished",
        ts=_ts(10, 2),
        use_id="use-exec-2",
        name="exec",
        summary="Found the latest notes.",
    )

    result = _load_chat_context_module().pre_process(
        {
            "day": "20260420",
            "trigger": {
                "type": "talent_finished",
                "name": "exec",
                "summary": "Found the latest notes.",
            },
        }
    )

    template_vars = _assert_template_vars_result(result)
    assert (
        "Instruction: This is a stop-and-report turn, not a dispatch turn. "
        "Do not retry this task or request another talent for it. Stop here "
        "and report to the owner directly using the result below."
    ) in template_vars["trigger_context"]
    assert result["messages"] == [
        {"role": "user", "content": "What happened?"},
        {"role": "assistant", "content": "Looking into it."},
        {
            "role": "user",
            "content": (
                "[internal follow-up: talent exec finished. This is a "
                "stop-and-report turn, not a dispatch turn. Do not retry "
                "this task or request another talent for it. Stop here and "
                "report to the owner directly using the result below. "
                "Result: Found the latest notes.]"
            ),
        },
    ]


def test_chat_context_talent_errored_marks_stop_and_report(monkeypatch, tmp_path):
    journal = tmp_path / "journal"
    monkeypatch.setenv("SOLSTONE_JOURNAL", str(journal))

    append_chat_event(
        "owner_message",
        ts=_ts(10, 0),
        text="What happened?",
        app="home",
        path="/app/home",
        facet="work",
    )
    append_chat_event(
        "sol_message",
        ts=_ts(10, 1),
        use_id="use-chat-3",
        text="Looking into it.",
        notes="Acknowledged request.",
        requested_target=None,
        requested_task=None,
    )
    append_chat_event(
        "talent_errored",
        ts=_ts(10, 2),
        use_id="use-exec-3",
        name="exec",
        reason="The lookup failed.",
    )

    result = _load_chat_context_module().pre_process(
        {
            "day": "20260420",
            "trigger": {
                "type": "talent_errored",
                "name": "exec",
                "reason": "The lookup failed.",
            },
        }
    )

    template_vars = _assert_template_vars_result(result)
    assert (
        "Instruction: This is a stop-and-report turn, not a dispatch turn. "
        "Do not retry this task or request another talent for it. Stop here "
        "and report to the owner directly using the reason below."
    ) in template_vars["trigger_context"]
    assert result["messages"] == [
        {"role": "user", "content": "What happened?"},
        {"role": "assistant", "content": "Looking into it."},
        {
            "role": "user",
            "content": (
                "[internal follow-up: talent exec errored. This is a "
                "stop-and-report turn, not a dispatch turn. Do not retry "
                "this task or request another talent for it. Stop here and "
                "report to the owner directly using the reason below. "
                "Reason: The lookup failed.]"
            ),
        },
    ]


def test_chat_context_talent_followups_are_observably_distinct(monkeypatch, tmp_path):
    journal = tmp_path / "journal"
    monkeypatch.setenv("SOLSTONE_JOURNAL", str(journal))

    append_chat_event(
        "owner_message",
        ts=_ts(10, 0),
        text="What happened?",
        app="home",
        path="/app/home",
        facet="work",
    )
    append_chat_event(
        "sol_message",
        ts=_ts(10, 1),
        use_id="use-chat-4",
        text="Looking into it.",
        notes="Acknowledged request.",
        requested_target=None,
        requested_task=None,
    )

    module = _load_chat_context_module()
    finished = module.pre_process(
        {
            "day": "20260420",
            "trigger": {
                "type": "talent_finished",
                "name": "exec",
                "summary": "Found the latest notes.",
            },
        }
    )
    errored = module.pre_process(
        {
            "day": "20260420",
            "trigger": {
                "type": "talent_errored",
                "name": "exec",
                "reason": "The lookup failed.",
            },
        }
    )

    finished_vars = _assert_template_vars_result(finished)
    errored_vars = _assert_template_vars_result(errored)

    finished_message = finished["messages"][-1]["content"]
    errored_message = errored["messages"][-1]["content"]

    assert finished_message == (
        "[internal follow-up: talent exec finished. This is a stop-and-report "
        "turn, not a dispatch turn. Do not retry this task or request another "
        "talent for it. Stop here and report to the owner directly using the "
        "result below. Result: Found the latest notes.]"
    )
    assert errored_message == (
        "[internal follow-up: talent exec errored. This is a stop-and-report "
        "turn, not a dispatch turn. Do not retry this task or request another "
        "talent for it. Stop here and report to the owner directly using the "
        "reason below. Reason: The lookup failed.]"
    )
    stop_and_report = (
        "stop-and-report turn, not a dispatch turn. Do not retry this task "
        "or request another talent for it. Stop here and report to the owner "
        "directly using the"
    )
    assert stop_and_report in finished_message
    assert stop_and_report in errored_message
    assert "using the result below. Result:" in finished_message
    assert "using the reason below. Reason:" in errored_message
    assert "Do not retry this task or request another talent for it." in errored_message
    assert (
        "Do not retry this task or request another talent for it." in finished_message
    )

    finished_instruction = (
        "Instruction: This is a stop-and-report turn, not a dispatch turn. "
        "Do not retry this task or request another talent for it. Stop here "
        "and report to the owner directly using the result below."
    )
    errored_instruction = (
        "Instruction: This is a stop-and-report turn, not a dispatch turn. "
        "Do not retry this task or request another talent for it. Stop here "
        "and report to the owner directly using the reason below."
    )
    assert finished_instruction in finished_vars["trigger_context"]
    assert errored_instruction in errored_vars["trigger_context"]
    assert "- Result: Found the latest notes." in finished_vars["trigger_context"]
    assert "- Reason: The lookup failed." in errored_vars["trigger_context"]


def test_chat_context_omits_messages_when_empty(monkeypatch, tmp_path):
    journal = tmp_path / "journal"
    monkeypatch.setenv("SOLSTONE_JOURNAL", str(journal))

    result = _load_chat_context_module().pre_process({"day": "20260420"})

    template_vars = _assert_template_vars_result(result)
    assert template_vars["active_talents"] == ""
    assert "messages" not in result


def test_pre_process_exposes_latest_owner_message_source(monkeypatch, tmp_path):
    journal = tmp_path / "journal"
    monkeypatch.setenv("SOLSTONE_JOURNAL", str(journal))
    source = {"kind": "needs_you", "item_text": "Review the launch checklist"}
    append_chat_event(
        "owner_message",
        ts=_ts(10, 0),
        text="First message",
        app="home",
        path="/app/home",
        facet="work",
    )
    append_chat_event(
        "owner_message",
        ts=_ts(10, 1),
        text="let's dig into Review the launch checklist",
        app="home",
        path="/app/home",
        facet="work",
        source=source,
    )

    result = _load_chat_context_module().pre_process({"day": "20260420"})

    template_vars = result["template_vars"]
    assert template_vars["source"] == source
    assert "Needs You tile" in template_vars["trigger_context"]
    assert "Review the launch checklist" in template_vars["trigger_context"]

    empty_journal = tmp_path / "empty-journal"
    monkeypatch.setenv("SOLSTONE_JOURNAL", str(empty_journal))
    append_chat_event(
        "owner_message",
        ts=_ts(10, 2),
        text="No source here",
        app="home",
        path="/app/home",
        facet="work",
    )

    result_without_source = _load_chat_context_module().pre_process({"day": "20260420"})

    assert "source" not in result_without_source["template_vars"]


def test_chat_context_enrichment_errors_are_graceful(monkeypatch, tmp_path):
    journal = tmp_path / "journal"
    monkeypatch.setenv("SOLSTONE_JOURNAL", str(journal))

    module = _load_chat_context_module()

    def _boom(*_args, **_kwargs):
        raise RuntimeError("boom")

    monkeypatch.setattr(module, "read_chat_tail", _boom)
    monkeypatch.setattr(module, "reduce_chat_state", _boom)

    result = module.pre_process(
        {
            "prompt": "What is on my calendar today?",
            "path": "/app/home",
            "trigger": {
                "type": "owner_message",
                "message": "What is on my calendar today?",
                "path": "/app/home",
                "ts": _ts(12, 0),
            },
        }
    )

    template_vars = _assert_template_vars_result(result)
    assert template_vars["active_talents"] == ""
    assert template_vars["trigger_context"] == ""
    assert "/app/home" in template_vars["location"]
    assert result["messages"] == [
        {"role": "user", "content": "What is on my calendar today?"}
    ]


def test_chat_context_drops_legacy_memory_imports(monkeypatch):
    legacy_module = "think" + ".con" + "versation"
    legacy_memory = "conversation_" + "memory"
    source = (
        Path(__file__).resolve().parents[1] / "solstone" / "talent" / "chat_context.py"
    ).read_text(encoding="utf-8")
    assert legacy_module not in source
    assert legacy_memory not in source

    sys.modules.pop(legacy_module, None)
    _load_chat_context_module()

    assert legacy_module not in sys.modules


def test_chat_prompt_v4_sentinels_are_present():
    prompt_text = _chat_prompt_text()

    assert (
        "The latest user message in the conversation below is what you must answer"
        in prompt_text
    )
    assert not re.search(r"^##\s+Trigger Context", prompt_text, re.MULTILINE)
    assert not re.search(r"^##\s+Location", prompt_text, re.MULTILINE)
    assert "$trigger_context" in prompt_text
    assert "## Stop-And-Report Contract" in prompt_text


def test_chat_prompt_frontmatter_pins_generation_budget():
    metadata = _chat_prompt_frontmatter()

    assert metadata["thinking_budget"] == 4096
    assert metadata["max_output_tokens"] == 2048
