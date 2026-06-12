# SPDX-License-Identifier: AGPL-3.0-only
# Copyright (c) 2026 sol pbc

"""Tests for the awareness system."""

import json
import re
import unittest.mock

import pytest


@pytest.fixture(autouse=True)
def _temp_journal(monkeypatch, tmp_path):
    """Isolate all tests to a temporary journal."""
    monkeypatch.setenv("SOLSTONE_JOURNAL", str(tmp_path))


def _read_identity_history(journal_path):
    path = journal_path / "identity" / "history.jsonl"
    return [json.loads(line) for line in path.read_text().splitlines()]


def _assert_identity_history(record, *, file_name, actor, op, section, reason):
    assert list(record) == [
        "ts",
        "file",
        "actor",
        "op",
        "section",
        "reason",
        "before_hash",
        "after_hash",
        "bytes_before",
        "bytes_after",
    ]
    assert re.fullmatch(r"\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}\.\d{3}Z", record["ts"])
    assert record["file"] == file_name
    assert record["actor"] == actor
    assert record["op"] == op
    assert record["section"] == section
    assert record["reason"] == reason


class TestCurrentState:
    def test_empty_state_returns_empty_dict(self):
        from solstone.think.awareness import get_current

        assert get_current() == {}

    def test_update_state_creates_section(self):
        from solstone.think.awareness import get_current, update_state

        update_state("onboarding", {"path": "a", "status": "observing"})

        state = get_current()
        assert state["onboarding"]["path"] == "a"
        assert state["onboarding"]["status"] == "observing"

    def test_update_state_merges_into_existing(self):
        from solstone.think.awareness import get_current, update_state

        update_state("onboarding", {"path": "a", "status": "observing"})
        update_state("onboarding", {"observation_count": 5})

        state = get_current()
        assert state["onboarding"]["path"] == "a"
        assert state["onboarding"]["observation_count"] == 5

    def test_update_state_multiple_sections(self):
        from solstone.think.awareness import get_current, update_state

        update_state("onboarding", {"status": "complete"})
        update_state("preferences", {"nudge_frequency": "low"})

        state = get_current()
        assert state["onboarding"]["status"] == "complete"
        assert state["preferences"]["nudge_frequency"] == "low"

    def test_current_json_written_atomically(self, tmp_path):
        from solstone.think.awareness import _awareness_dir, update_state

        update_state("test", {"key": "value"})

        path = _awareness_dir() / "current.json"
        assert path.exists()
        data = json.loads(path.read_text())
        assert data["test"]["key"] == "value"


class TestDailyLog:
    def test_append_log_creates_file(self, tmp_path):
        from solstone.think.awareness import _awareness_dir, _today, append_log

        entry = append_log("state", key="test.started", message="café")

        log_path = _awareness_dir() / f"{_today()}.jsonl"
        assert log_path.exists()
        assert entry["kind"] == "state"
        assert entry["key"] == "test.started"
        assert entry["message"] == "café"
        assert "ts" in entry
        raw = log_path.read_bytes()
        assert b"caf\\u00e9" in raw
        assert b"caf\xc3\xa9" not in raw
        assert raw.endswith(b"\n")
        assert raw.count(b"\n") == 1

    def test_append_log_appends_multiple(self):
        from solstone.think.awareness import _today, append_log, read_log

        append_log("state", key="a")
        append_log("observation", message="saw something")
        append_log("nudge", message="hey")

        entries = read_log(_today())
        assert len(entries) == 3
        assert entries[0]["kind"] == "state"
        assert entries[1]["kind"] == "observation"
        assert entries[2]["kind"] == "nudge"

    def test_read_log_empty_returns_empty_list(self):
        from solstone.think.awareness import read_log

        assert read_log("20990101") == []

    def test_append_log_with_data(self):
        from solstone.think.awareness import _today, append_log, read_log

        append_log("observation", data={"meetings": 2, "entities": ["Alice"]})

        entries = read_log(_today())
        assert entries[0]["data"]["meetings"] == 2

    def test_append_log_with_extra_fields(self):
        from solstone.think.awareness import _today, append_log, read_log

        append_log("observation", segment="123456_300", detail="meeting detected")

        entries = read_log(_today())
        assert entries[0]["segment"] == "123456_300"
        assert entries[0]["detail"] == "meeting detected"


class TestJournalState:
    def test_first_daily_ready_via_update_state(self):
        from solstone.think.awareness import get_current, update_state

        update_state(
            "journal",
            {"first_daily_ready": True, "first_daily_ready_at": "20260308T14:00:00"},
        )

        state = get_current()
        assert state["journal"]["first_daily_ready"] is True
        assert state["journal"]["first_daily_ready_at"] == "20260308T14:00:00"


class TestOwnerDetectionReady:
    """Tests for owner_detection_ready()."""

    def test_not_ready_when_centroid_exists(self):
        """Returns not ready when owner centroid already exists."""
        from solstone.think.awareness import owner_detection_ready

        with unittest.mock.patch(
            "solstone.apps.speakers.owner.load_owner_centroid",
            return_value=("centroid", 0.82),
        ):
            result = owner_detection_ready()

        assert result["ready"] is False
        assert result["reason"] == "centroid_exists"

    def test_not_ready_during_cooldown(self):
        """Returns not ready when rejection was within 14 days."""
        from datetime import datetime

        from solstone.think.awareness import owner_detection_ready, update_state

        update_state("voiceprint", {"rejected_at": datetime.now().isoformat()})

        with unittest.mock.patch(
            "solstone.apps.speakers.owner.load_owner_centroid", return_value=None
        ):
            result = owner_detection_ready()

        assert result["ready"] is False
        assert result["reason"] == "cooldown"
        assert result["days_remaining"] == 14

    def test_ready_when_candidate_found(self):
        """Returns ready when detect_owner_candidate returns positive."""
        from solstone.think.awareness import owner_detection_ready

        mock_detection = {
            "status": "candidate",
            "recommendation": "ready",
            "cluster_size": 88,
            "streams_represented": 2,
            "samples": [{"day": "20240101"}],
        }

        with unittest.mock.patch(
            "solstone.apps.speakers.owner.load_owner_centroid", return_value=None
        ):
            with unittest.mock.patch(
                "solstone.apps.speakers.owner.detect_owner_candidate",
                return_value=mock_detection,
            ):
                result = owner_detection_ready()

        assert result["ready"] is True
        assert result["reason"] == "candidate_found"
        assert result["cluster_size"] == 88
        assert result["streams_represented"] == 2

    def test_not_ready_low_data(self):
        """Returns not ready when detection has insufficient data."""
        from solstone.think.awareness import owner_detection_ready

        mock_detection = {
            "status": "low_data",
            "recommendation": "low_data",
        }

        with unittest.mock.patch(
            "solstone.apps.speakers.owner.load_owner_centroid", return_value=None
        ):
            with unittest.mock.patch(
                "solstone.apps.speakers.owner.detect_owner_candidate",
                return_value=mock_detection,
            ):
                result = owner_detection_ready()

        assert result["ready"] is False
        assert result["reason"] == "low_data"

    def test_not_ready_single_stream(self):
        """Returns not ready when candidate is single_stream (not 'ready')."""
        from solstone.think.awareness import owner_detection_ready

        mock_detection = {
            "status": "candidate",
            "recommendation": "single_stream",
            "cluster_size": 60,
        }

        with unittest.mock.patch(
            "solstone.apps.speakers.owner.load_owner_centroid", return_value=None
        ):
            with unittest.mock.patch(
                "solstone.apps.speakers.owner.detect_owner_candidate",
                return_value=mock_detection,
            ):
                result = owner_detection_ready()

        assert result["ready"] is False
        assert result["reason"] == "single_stream"

    def test_cooldown_expires_after_14_days(self):
        """Cooldown no longer blocks after 14 days."""
        from datetime import datetime, timedelta

        from solstone.think.awareness import owner_detection_ready, update_state

        old_rejection = (datetime.now() - timedelta(days=15)).isoformat()
        update_state("voiceprint", {"rejected_at": old_rejection})

        mock_detection = {
            "status": "candidate",
            "recommendation": "ready",
            "cluster_size": 100,
            "streams_represented": 3,
            "samples": [],
        }

        with unittest.mock.patch(
            "solstone.apps.speakers.owner.load_owner_centroid", return_value=None
        ):
            with unittest.mock.patch(
                "solstone.apps.speakers.owner.detect_owner_candidate",
                return_value=mock_detection,
            ):
                result = owner_detection_ready()

        assert result["ready"] is True


class TestEnsureIdentityDirectory:
    """Tests for ensure_identity_directory()."""

    def test_creates_default_templates(self, tmp_path):
        from solstone.think.identity import ensure_identity_directory

        identity_dir = ensure_identity_directory()
        assert identity_dir == tmp_path / "identity"
        assert not (identity_dir / ("self" + ".md")).exists()
        assert not (identity_dir / "agency.md").exists()

        assert not (identity_dir / "digest.md").exists()
        assert (identity_dir / "health.md").exists()

    def test_idempotent_does_not_overwrite(self, tmp_path):
        from solstone.think.identity import ensure_identity_directory

        identity_dir = ensure_identity_directory()
        partner_path = identity_dir / "partner.md"
        partner_path.write_text("custom content", encoding="utf-8")

        # Call again — should NOT overwrite
        ensure_identity_directory()
        assert partner_path.read_text() == "custom content"

    def test_creates_partner_md(self, tmp_path):
        from solstone.think.identity import ensure_identity_directory

        identity_dir = ensure_identity_directory()
        partner_path = identity_dir / "partner.md"
        assert partner_path.exists()
        content = partner_path.read_text()
        assert "# partner" in content
        assert "## work patterns" in content
        assert "## communication style" in content
        assert "## relationship priorities" in content
        assert "## decision style" in content
        assert "## expertise domains" in content

    def test_does_not_overwrite_existing_partner_md(self, tmp_path):
        from solstone.think.identity import ensure_identity_directory

        identity_dir = tmp_path / "identity"
        identity_dir.mkdir()
        custom = "# partner\n\n## work patterns\nCustom content.\n"
        (identity_dir / "partner.md").write_text(custom)

        ensure_identity_directory()
        assert (identity_dir / "partner.md").read_text() == custom


class TestUpdateIdentitySection:
    """Tests for update_identity_section generic helper."""

    def test_update_partner_section(self, tmp_path):
        from solstone.think.identity import update_identity_section

        partner_md = "# partner\n\n## work patterns\n[observing]\n\n## communication style\n[observing]\n"
        (tmp_path / "identity").mkdir(exist_ok=True)
        (tmp_path / "identity" / "partner.md").write_text(partner_md)

        result = update_identity_section(
            "partner.md",
            "work patterns",
            "Prefers mornings",
            actor="test update identity section",
            reason="test",
        )
        assert result is True

        content = (tmp_path / "identity" / "partner.md").read_text()
        assert "Prefers mornings" in content
        assert "## communication style" in content
        assert "[observing]" in content  # other section preserved

    def test_update_nonexistent_file_returns_false(self, tmp_path):
        from solstone.think.identity import update_identity_section

        (tmp_path / "identity").mkdir(exist_ok=True)
        result = update_identity_section(
            "nonexistent.md",
            "heading",
            "content",
            actor="test update identity section",
            reason="test",
        )
        assert result is False

    def test_partner_update_prunes_getting_started(self, tmp_path):
        from solstone.think.identity import update_identity_section

        partner_md = (
            "# partner\n\n"
            "## getting started\n\nOnboarding guidance here.\n\n"
            "## work patterns\n[not yet observed]\n\n"
            "## communication style\n[not yet observed]\n"
        )
        (tmp_path / "identity").mkdir(exist_ok=True)
        (tmp_path / "identity" / "partner.md").write_text(partner_md)

        result = update_identity_section(
            "partner.md",
            "work patterns",
            "Prefers mornings",
            actor="test update identity section",
            reason="test",
        )
        assert result is True

        content = (tmp_path / "identity" / "partner.md").read_text()
        assert "Prefers mornings" in content
        assert "## communication style" in content
        assert "## getting started" not in content
        assert "Onboarding guidance" not in content
