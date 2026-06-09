# SPDX-License-Identifier: AGPL-3.0-only
# Copyright (c) 2026 sol pbc

"""Tests for convey app placeholder and attention behavior."""

import pytest
from flask import Flask

from solstone.apps import AppRegistry
from solstone.convey.apps import register_app_context
from solstone.convey.chat_stream import append_chat_event
from solstone.convey.sol_initiated.copy import CATEGORIES, KIND_SOL_CHAT_REQUEST


@pytest.fixture(autouse=True)
def _temp_journal(monkeypatch, tmp_path):
    """Ensure journaling defaults remain isolated from developer data."""
    monkeypatch.setenv("SOLSTONE_JOURNAL", str(tmp_path))
    monkeypatch.setattr("solstone.convey.chat_stream.index_file", lambda *_args: True)


def _context(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path,
    awareness: dict,
    *,
    day_count: int = 5,
) -> dict:
    import solstone.convey.state as convey_state

    app = Flask(__name__)
    registry = AppRegistry()
    monkeypatch.setattr(convey_state, "journal_root", str(tmp_path))
    monkeypatch.setattr("solstone.convey.apps._get_facets_data", lambda: [])
    monkeypatch.setattr("solstone.convey.apps._get_selected_facet", lambda: None)
    monkeypatch.setattr("solstone.think.awareness.get_current", lambda: awareness)
    monkeypatch.setattr(
        "solstone.think.utils.day_dirs",
        lambda: {str(index): str(index) for index in range(day_count)},
    )
    register_app_context(app, registry)
    with app.test_request_context("/"):
        context: dict = {}
        app.update_template_context(context)
    return context


def _append_request(request_id: str = "req", *, ts: int | None = None) -> None:
    fields = {
        "request_id": request_id,
        "summary": "Notice this",
        "message": None,
        "category": CATEGORIES[0],
        "dedupe": request_id,
        "dedupe_window": "24h",
        "since_ts": 1,
        "trigger_talent": "reflection",
    }
    if ts is not None:
        fields["ts"] = ts
    append_chat_event(KIND_SOL_CHAT_REQUEST, **fields)


# --- Placeholder resolution ---


class TestPlaceholderResolution:
    def test_no_imports_young(self):
        from solstone.convey.apps import _resolve_placeholder

        result = _resolve_placeholder({}, 0)
        assert "Bring in past conversations" in result

    def test_no_daily(self):
        from solstone.convey.apps import _resolve_placeholder

        current = {"imports": {"has_imported": True}}
        result = _resolve_placeholder(current, 0)
        assert "observing" in result

    def test_first_daily_young(self):
        from solstone.convey.apps import _resolve_placeholder

        current = {
            "imports": {"has_imported": True},
            "journal": {"first_daily_ready": True},
        }
        result = _resolve_placeholder(current, 1)
        assert "first daily analysis is ready" in result

    def test_first_daily_mid(self):
        from solstone.convey.apps import _resolve_placeholder

        current = {"journal": {"first_daily_ready": True}}
        result = _resolve_placeholder(current, 3)
        assert "daily analysis is ready" in result
        assert "first" not in result

    def test_first_daily_mature(self):
        from solstone.convey.apps import _resolve_placeholder

        current = {"journal": {"first_daily_ready": True}}
        result = _resolve_placeholder(current, 10)
        assert "Ask me about your day" in result

    def test_default_fallback(self):
        from solstone.convey.apps import _resolve_placeholder

        result = _resolve_placeholder({}, 5)
        assert "observing" in result


class TestInjectedChatBarContext:
    def test_no_attention_or_sol_request_uses_fallback_context(
        self, monkeypatch, tmp_path
    ):
        context = _context(monkeypatch, tmp_path, {"imports": {"has_imported": True}})

        assert context["chat_bar_placeholder"] == (
            "observing — your first daily analysis will be ready soon..."
        )
        assert context["chat_bar_attention"] is None
        assert context["chat_bar_sol_request"] is None

    def test_attention_surfaces_structured_copy_and_keeps_fallback_placeholder(
        self, monkeypatch, tmp_path
    ):
        from datetime import datetime

        context = _context(
            monkeypatch,
            tmp_path,
            {
                "imports": {
                    "has_imported": True,
                    "last_completed": datetime.now().isoformat(),
                    "last_result_summary": "142 Calendar events",
                }
            },
        )

        assert context["chat_bar_attention"] == {
            "placeholder_text": "Import complete: 142 Calendar events — ask me about it"
        }
        assert context["chat_bar_sol_request"] is None
        assert context["chat_bar_placeholder"] == (
            "observing — your first daily analysis will be ready soon..."
        )

    def test_sol_request_surfaces_structured_state(self, monkeypatch, tmp_path):
        from datetime import date

        _append_request("req")

        context = _context(monkeypatch, tmp_path, {"imports": {"has_imported": True}})

        assert context["chat_bar_sol_request"]["request_id"] == "req"
        assert context["chat_bar_sol_request"]["summary"] == "Notice this"
        assert isinstance(context["chat_bar_sol_request"]["ts"], int)
        assert context["chat_bar_sol_request"]["event_index"] == 0
        assert context["chat_bar_sol_request"]["day"] == date.today().strftime("%Y%m%d")
        assert set(context["chat_bar_sol_request"]) == {
            "request_id",
            "summary",
            "ts",
            "event_index",
            "day",
        }
        assert context["chat_bar_attention"] is None
        assert context["chat_bar_placeholder"] == (
            "observing — your first daily analysis will be ready soon..."
        )

    def test_past_day_request_does_not_surface(self, monkeypatch, tmp_path):
        from datetime import date, datetime, time, timedelta

        from solstone.think.utils import get_owner_timezone

        yesterday = date.today() - timedelta(days=1)
        yesterday_dt = datetime.combine(
            yesterday,
            time(hour=12),
            tzinfo=get_owner_timezone(),
        )
        _append_request("past", ts=int(yesterday_dt.timestamp() * 1000))

        context = _context(monkeypatch, tmp_path, {"imports": {"has_imported": True}})

        assert context["chat_bar_sol_request"] is None


class TestAttentionResolution:
    """Tests for _resolve_attention() and attention-aware placeholder resolution."""

    def test_no_attention_returns_none(self):
        from solstone.convey.apps import _resolve_attention

        assert _resolve_attention({}) is None

    def test_no_attention_empty_sections(self):
        from solstone.convey.apps import _resolve_attention

        current = {"imports": {"has_imported": True}, "journal": {}}
        assert _resolve_attention(current) is None

    def test_p1_recent_import(self):
        from datetime import datetime

        from solstone.convey.apps import _resolve_attention

        current = {
            "imports": {
                "has_imported": True,
                "last_completed": datetime.now().isoformat(),
                "last_result_summary": "142 Calendar events",
            }
        }
        result = _resolve_attention(current)
        assert result is not None
        assert "import" in result.placeholder_text.lower()
        assert len(result.placeholder_text) <= 90

    def test_p2_old_import_no_attention(self):
        from datetime import datetime, timedelta

        from solstone.convey.apps import _resolve_attention

        old_time = (datetime.now() - timedelta(hours=2)).isoformat()
        current = {
            "imports": {
                "has_imported": True,
                "last_completed": old_time,
                "last_result_summary": "142 Calendar events",
            }
        }
        assert _resolve_attention(current) is None

    def test_p0_cortex_errors(self, tmp_path, monkeypatch):
        """Cortex errors are P0 — highest priority."""
        import json
        from datetime import datetime

        from solstone.convey.apps import _resolve_attention

        monkeypatch.setenv("SOLSTONE_JOURNAL", str(tmp_path))

        today = datetime.now().strftime("%Y%m%d")
        agents_dir = tmp_path / "talents"
        agents_dir.mkdir()
        day_index = agents_dir / f"{today}.jsonl"
        day_index.write_text(
            json.dumps(
                {
                    "use_id": "1",
                    "name": "flow",
                    "day": today,
                    "ts": 1000,
                    "status": "error",
                }
            )
            + "\n"
            + json.dumps(
                {
                    "use_id": "2",
                    "name": "meetings",
                    "day": today,
                    "ts": 1001,
                    "status": "completed",
                }
            )
            + "\n"
        )

        result = _resolve_attention({})
        assert result is not None
        assert result.placeholder_text == "1 agent error today — ask what happened"
        assert "error" in result.placeholder_text.lower()
        assert "1" in result.placeholder_text
        assert len(result.placeholder_text) <= 90

    def test_p0_readiness_error_prefers_setup_guidance(self, tmp_path, monkeypatch):
        """Readiness blockers get setup guidance instead of generic error copy."""
        import json
        from datetime import datetime

        from solstone.convey.apps import _resolve_attention

        monkeypatch.setenv("SOLSTONE_JOURNAL", str(tmp_path))

        today = datetime.now().strftime("%Y%m%d")
        agents_dir = tmp_path / "talents"
        agents_dir.mkdir()
        day_index = agents_dir / f"{today}.jsonl"
        day_index.write_text(
            json.dumps(
                {
                    "use_id": "1",
                    "name": "flow",
                    "day": today,
                    "ts": 1000,
                    "status": "error",
                    "reason_code": "provider_key_missing",
                    "provider": "anthropic",
                    "model": "claude-test",
                }
            )
            + "\n"
        )

        result = _resolve_attention({})

        assert result is not None
        assert "agent error" not in result.placeholder_text
        assert "Anthropic needs credentials" in result.placeholder_text
        assert len(result.placeholder_text) <= 90
        assert any("provider setup" in line for line in result.context_lines)
        assert any(
            "reason_code=provider_key_missing" in line for line in result.context_lines
        )
        assert any("provider=anthropic" in line for line in result.context_lines)
        assert any("model=claude-test" in line for line in result.context_lines)

    def test_p0_self_healing(self, tmp_path, monkeypatch):
        """An error followed by a success for the same agent is resolved."""
        import json
        from datetime import datetime

        from solstone.convey.apps import _resolve_attention

        monkeypatch.setenv("SOLSTONE_JOURNAL", str(tmp_path))

        today = datetime.now().strftime("%Y%m%d")
        agents_dir = tmp_path / "talents"
        agents_dir.mkdir()
        day_index = agents_dir / f"{today}.jsonl"
        day_index.write_text(
            json.dumps(
                {
                    "use_id": "1",
                    "name": "flow",
                    "day": today,
                    "ts": 1000,
                    "status": "error",
                    "reason_code": "provider_key_missing",
                    "provider": "anthropic",
                    "model": "claude-test",
                }
            )
            + "\n"
            + json.dumps(
                {
                    "use_id": "3",
                    "name": "flow",
                    "day": today,
                    "ts": 2000,
                    "status": "completed",
                }
            )
            + "\n"
        )

        result = _resolve_attention({})
        assert result is None

    def test_p0_counts_unresolved_occurrences_not_distinct_names(
        self, tmp_path, monkeypatch
    ):
        """Multiple unresolved errors for one agent count as multiple occurrences."""
        import json
        from datetime import datetime

        from solstone.convey.apps import _resolve_attention

        monkeypatch.setenv("SOLSTONE_JOURNAL", str(tmp_path))

        today = datetime.now().strftime("%Y%m%d")
        agents_dir = tmp_path / "talents"
        agents_dir.mkdir()
        day_index = agents_dir / f"{today}.jsonl"
        day_index.write_text(
            json.dumps(
                {
                    "use_id": "1",
                    "name": "flow",
                    "day": today,
                    "ts": 1000,
                    "status": "error",
                }
            )
            + "\n"
            + json.dumps(
                {
                    "use_id": "2",
                    "name": "flow",
                    "day": today,
                    "ts": 2000,
                    "status": "error",
                }
            )
            + "\n"
        )

        result = _resolve_attention({})
        assert result is not None
        assert result.placeholder_text == "2 agent errors today — ask what happened"
        assert result.context_lines == [
            "System health: 2 unresolved agent error(s) today: flow. If user asks "
            "what needs attention, summarize which agents failed."
        ]

    def test_p0_later_success_resolves_earlier_occurrences_only(
        self, tmp_path, monkeypatch
    ):
        """Later same-agent errors after a success remain unresolved."""
        import json
        from datetime import datetime

        from solstone.convey.apps import _resolve_attention

        monkeypatch.setenv("SOLSTONE_JOURNAL", str(tmp_path))

        today = datetime.now().strftime("%Y%m%d")
        agents_dir = tmp_path / "talents"
        agents_dir.mkdir()
        day_index = agents_dir / f"{today}.jsonl"
        day_index.write_text(
            json.dumps(
                {
                    "use_id": "1",
                    "name": "flow",
                    "day": today,
                    "ts": 1000,
                    "status": "error",
                }
            )
            + "\n"
            + json.dumps(
                {
                    "use_id": "2",
                    "name": "flow",
                    "day": today,
                    "ts": 2000,
                    "status": "completed",
                }
            )
            + "\n"
            + json.dumps(
                {
                    "use_id": "3",
                    "name": "flow",
                    "day": today,
                    "ts": 3000,
                    "status": "error",
                }
            )
            + "\n"
        )

        result = _resolve_attention({})
        assert result is not None
        assert result.placeholder_text == "1 agent error today — ask what happened"

    def test_p0_home_attention_count_matches_health_seed_count(
        self, tmp_path, monkeypatch
    ):
        """Home attention and health seed use the same occurrence count."""
        import json
        import time
        from datetime import datetime

        from solstone.apps.health.routes import _build_agent_error_seed
        from solstone.convey.apps import _resolve_attention
        from solstone.think.talent_runs import read_unresolved_agent_failures

        monkeypatch.setenv("SOLSTONE_JOURNAL", str(tmp_path))

        today = datetime.now().strftime("%Y%m%d")
        now_ms = int(time.time() * 1000)
        agents_dir = tmp_path / "talents"
        agents_dir.mkdir()
        day_index = agents_dir / f"{today}.jsonl"
        day_index.write_text(
            json.dumps(
                {
                    "use_id": "1",
                    "name": "flow",
                    "day": today,
                    "ts": now_ms,
                    "status": "error",
                }
            )
            + "\n"
            + json.dumps(
                {
                    "use_id": "2",
                    "name": "flow",
                    "day": today,
                    "ts": now_ms + 1,
                    "status": "error",
                }
            )
            + "\n"
            + json.dumps(
                {
                    "use_id": "3",
                    "name": "meetings",
                    "day": today,
                    "ts": now_ms + 2,
                    "status": "error",
                }
            )
            + "\n"
        )

        scan = read_unresolved_agent_failures()
        attention = _resolve_attention({})

        assert attention is not None
        assert attention.placeholder_text == "3 agent errors today — ask what happened"
        home_count = int(attention.placeholder_text.split(" ", 1)[0])
        assert (
            home_count == len(_build_agent_error_seed(scan)) == len(scan.failures) == 3
        )

    def test_p0_readiness_branch_uses_latest_error_per_name(
        self, tmp_path, monkeypatch
    ):
        """An older blocker does not mask a later unresolved non-blocking error."""
        import json
        from datetime import datetime

        from solstone.convey.apps import _resolve_attention

        monkeypatch.setenv("SOLSTONE_JOURNAL", str(tmp_path))

        today = datetime.now().strftime("%Y%m%d")
        agents_dir = tmp_path / "talents"
        agents_dir.mkdir()
        day_index = agents_dir / f"{today}.jsonl"
        day_index.write_text(
            json.dumps(
                {
                    "use_id": "1",
                    "name": "flow",
                    "day": today,
                    "ts": 1000,
                    "status": "error",
                    "reason_code": "provider_key_missing",
                    "provider": "anthropic",
                }
            )
            + "\n"
            + json.dumps(
                {
                    "use_id": "2",
                    "name": "flow",
                    "day": today,
                    "ts": 2000,
                    "status": "error",
                    "reason_code": "no_output",
                }
            )
            + "\n"
        )

        result = _resolve_attention({})
        assert result is not None
        assert result.placeholder_text == "2 agent errors today — ask what happened"

    def test_priority_p0_over_p1_imports(self, tmp_path, monkeypatch):
        """P0 (cortex errors) takes priority over P1 (recent import)."""
        import json
        from datetime import datetime

        from solstone.convey.apps import _resolve_attention

        monkeypatch.setenv("SOLSTONE_JOURNAL", str(tmp_path))

        today = datetime.now().strftime("%Y%m%d")
        agents_dir = tmp_path / "talents"
        agents_dir.mkdir()
        day_index = agents_dir / f"{today}.jsonl"
        day_index.write_text(
            json.dumps(
                {
                    "use_id": "1",
                    "name": "flow",
                    "day": today,
                    "ts": 1000,
                    "status": "error",
                }
            )
            + "\n"
        )

        current = {
            "imports": {
                "has_imported": True,
                "last_completed": datetime.now().isoformat(),
                "last_result_summary": "10 items",
            }
        }
        result = _resolve_attention(current)
        assert result is not None
        assert "error" in result.placeholder_text.lower()

    def test_placeholder_no_attention_preserves_behavior(self):
        """When no attention items, existing placeholder logic unchanged."""
        from solstone.convey.apps import _resolve_placeholder

        current = {"journal": {"first_daily_ready": True}}
        result = _resolve_placeholder(current, 10)
        assert "Ask me about your day" in result

    def test_all_placeholder_texts_under_90_chars(self, tmp_path, monkeypatch):
        """All attention placeholder texts must be <=90 characters."""
        import json
        from datetime import datetime

        from solstone.convey.apps import _resolve_attention

        monkeypatch.setenv("SOLSTONE_JOURNAL", str(tmp_path))

        today = datetime.now().strftime("%Y%m%d")
        agents_dir = tmp_path / "talents"
        agents_dir.mkdir()
        day_index = agents_dir / f"{today}.jsonl"
        day_index.write_text(
            json.dumps({"use_id": "1", "name": "flow", "ts": 1000, "status": "error"})
            + "\n"
        )
        result = _resolve_attention({})
        assert result is not None
        assert len(result.placeholder_text) <= 90

        day_index.unlink()
        agents_dir.rmdir()
        result = _resolve_attention(
            {
                "imports": {
                    "last_completed": datetime.now().isoformat(),
                    "last_result_summary": "142 Calendar events",
                }
            }
        )
        assert result is not None
        assert len(result.placeholder_text) <= 90

    def test_p3_daily_analysis(self, tmp_path, monkeypatch):
        """P3: daily analysis outputs available."""
        from datetime import datetime

        from solstone.convey.apps import _resolve_attention

        monkeypatch.setenv("SOLSTONE_JOURNAL", str(tmp_path))

        today = datetime.now().strftime("%Y%m%d")
        agents_dir = tmp_path / today / "talents"
        agents_dir.mkdir(parents=True)
        (agents_dir / "flow.md").write_text("# Flow")
        (agents_dir / "meetings.md").write_text("# Meetings")

        current = {"journal": {"first_daily_ready": True}}
        result = _resolve_attention(current)
        assert result is not None
        assert "2" in result.placeholder_text
        assert "report" in result.placeholder_text.lower()
        assert len(result.placeholder_text) <= 90
