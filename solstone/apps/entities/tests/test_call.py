# SPDX-License-Identifier: AGPL-3.0-only
# Copyright (c) 2026 sol pbc

"""Tests for entities CLI commands (sol call entities ...)."""

import json
from pathlib import Path

from typer.testing import CliRunner

from solstone.think.call import call_app
from solstone.think.entities.core import entity_slug
from solstone.think.entities.journal import save_journal_entity
from solstone.think.entities.review_candidates import save_candidates

runner = CliRunner()


class TestEntitiesList:
    def test_list_attached(self, entity_env):
        entity_env(
            attached=[
                {
                    "type": "Person",
                    "name": "Alice Johnson",
                    "description": "Friend",
                    "attached_at": 1000,
                    "updated_at": 1000,
                },
                {
                    "type": "Company",
                    "name": "Acme Corp",
                    "description": "Client",
                    "attached_at": 1001,
                    "updated_at": 1001,
                },
            ]
        )

        result = runner.invoke(call_app, ["entities", "list", "personal"])

        assert result.exit_code == 0
        assert "2 attached entities" in result.output
        assert "Alice Johnson" in result.output
        assert "Acme Corp" in result.output

    def test_list_detected(self, entity_env):
        entity_env(
            detected=[
                {
                    "type": "Person",
                    "name": "Alice",
                    "description": "Met at conference",
                },
                {
                    "type": "Tool",
                    "name": "pytest",
                    "description": "Testing tool",
                },
            ],
            day="20240101",
        )

        result = runner.invoke(
            call_app, ["entities", "list", "personal", "--day", "20240101"]
        )

        assert result.exit_code == 0
        assert "Alice" in result.output
        assert "pytest" in result.output

    def test_list_empty(self, entity_env):
        entity_env()

        result = runner.invoke(call_app, ["entities", "list", "personal"])

        assert result.exit_code == 0
        assert "No entities found" in result.output


class TestEntitiesDetect:
    def test_detect_new(self, entity_env):
        entity_env()

        result = runner.invoke(
            call_app,
            [
                "entities",
                "detect",
                "Person",
                "Alice",
                "Met at conference",
                "--facet",
                "personal",
                "--day",
                "20240101",
            ],
        )

        assert result.exit_code == 0
        assert "detected" in result.output

    def test_detect_duplicate(self, entity_env):
        entity_env(
            detected=[
                {"type": "Person", "name": "Alice", "description": "First"},
            ],
            day="20240101",
        )

        result = runner.invoke(
            call_app,
            [
                "entities",
                "detect",
                "Person",
                "Alice",
                "Second",
                "--facet",
                "personal",
                "--day",
                "20240101",
            ],
        )

        assert result.exit_code == 1
        assert "already detected" in result.output

    def test_detect_invalid_type(self, entity_env):
        entity_env()

        result = runner.invoke(
            call_app,
            [
                "entities",
                "detect",
                "AB",
                "Alice",
                "Met at conference",
                "--facet",
                "personal",
                "--day",
                "20240101",
            ],
        )

        assert result.exit_code == 1
        assert "Invalid" in result.output


class TestEntitiesAttach:
    def test_attach_new(self, entity_env):
        entity_env()

        result = runner.invoke(
            call_app,
            [
                "entities",
                "attach",
                "Person",
                "Alice Johnson",
                "Friend",
                "-f",
                "personal",
            ],
        )

        assert result.exit_code == 0
        assert "attached" in result.output

    def test_attach_existing(self, entity_env):
        entity_env(
            attached=[
                {
                    "type": "Person",
                    "name": "Alice Johnson",
                    "description": "Friend",
                    "attached_at": 1000,
                    "updated_at": 1000,
                }
            ]
        )

        result = runner.invoke(
            call_app,
            [
                "entities",
                "attach",
                "Person",
                "Alice Johnson",
                "Friend",
                "-f",
                "personal",
            ],
        )

        assert result.exit_code == 0
        assert "already attached" in result.output

    def test_attach_invalid_type(self, entity_env):
        entity_env()

        result = runner.invoke(
            call_app,
            ["entities", "attach", "AB", "Alice Johnson", "Friend", "-f", "personal"],
        )

        assert result.exit_code == 1
        assert "Invalid" in result.output


class TestEntitiesUpdate:
    def test_update_attached(self, entity_env):
        entity_env(
            attached=[
                {
                    "type": "Person",
                    "name": "Alice Johnson",
                    "description": "Old",
                    "attached_at": 1000,
                    "updated_at": 1000,
                }
            ]
        )

        result = runner.invoke(
            call_app,
            [
                "entities",
                "update",
                "Alice Johnson",
                "New description",
                "-f",
                "personal",
            ],
        )
        verify = runner.invoke(call_app, ["entities", "list", "personal"])

        assert result.exit_code == 0
        assert "updated" in result.output
        assert "New description" in verify.output

    def test_update_detected(self, entity_env):
        entity_env(
            detected=[
                {"type": "Person", "name": "Alice", "description": "Old"},
            ],
            day="20240101",
        )

        result = runner.invoke(
            call_app,
            [
                "entities",
                "update",
                "Alice",
                "New desc",
                "-f",
                "personal",
                "--day",
                "20240101",
            ],
        )

        assert result.exit_code == 0
        assert "updated" in result.output

    def test_update_not_found(self, entity_env):
        entity_env()

        result = runner.invoke(
            call_app,
            ["entities", "update", "Missing", "New description", "-f", "personal"],
        )

        assert result.exit_code == 1
        assert "not found" in result.output


class TestEntitiesMove:
    def test_move_entity(self, entity_move_env):
        journal, src_facet, dst_facet, entity_name = entity_move_env()
        slug = entity_slug(entity_name)

        result = runner.invoke(
            call_app,
            [
                "entities",
                "move",
                entity_name,
                "--from",
                src_facet,
                "--to",
                dst_facet,
            ],
        )

        assert result.exit_code == 0
        src_dir = journal / "facets" / src_facet / "entities" / slug
        dst_dir = journal / "facets" / dst_facet / "entities" / slug
        assert not src_dir.exists()
        assert dst_dir.exists()

    def test_move_entity_already_exists_no_merge(self, entity_move_env):
        _, src_facet, dst_facet, entity_name = entity_move_env(create_dst_entity=True)

        result = runner.invoke(
            call_app,
            [
                "entities",
                "move",
                entity_name,
                "--from",
                src_facet,
                "--to",
                dst_facet,
            ],
        )

        assert result.exit_code == 1
        assert "Use --merge" in result.output

    def test_move_entity_merge(self, entity_move_env):
        journal, src_facet, dst_facet, entity_name = entity_move_env(
            src_observations=[
                {
                    "content": "Prefers async communication",
                    "observed_at": 1000,
                    "source_day": "20240101",
                },
                {"content": "Uses Vim", "observed_at": 1001, "source_day": "20240102"},
            ],
            dst_observations=[
                {
                    "content": "Prefers async communication",
                    "observed_at": 1000,
                    "source_day": "20240101",
                },
                {"content": "Likes tea", "observed_at": 1002, "source_day": "20240103"},
            ],
            create_dst_entity=True,
        )
        slug = entity_slug(entity_name)

        result = runner.invoke(
            call_app,
            [
                "entities",
                "move",
                entity_name,
                "--from",
                src_facet,
                "--to",
                dst_facet,
                "--merge",
            ],
        )

        assert result.exit_code == 0
        src_dir = journal / "facets" / src_facet / "entities" / slug
        dst_obs_path = (
            journal / "facets" / dst_facet / "entities" / slug / "observations.jsonl"
        )
        observations = [
            json.loads(line)
            for line in dst_obs_path.read_text(encoding="utf-8").splitlines()
        ]
        assert not src_dir.exists()
        assert len(observations) == 3

    def test_move_entity_not_found(self, entity_move_env):
        _, src_facet, dst_facet, _ = entity_move_env()

        result = runner.invoke(
            call_app,
            ["entities", "move", "Missing", "--from", src_facet, "--to", dst_facet],
        )

        assert result.exit_code == 1
        assert "not found" in result.output

    def test_move_missing_facet(self, entity_move_env):
        _, src_facet, _, entity_name = entity_move_env()

        result = runner.invoke(
            call_app,
            ["entities", "move", entity_name, "--from", src_facet, "--to", "missing"],
        )

        assert result.exit_code == 1
        assert "does not exist" in result.output


class TestEntitiesAka:
    def test_add_aka(self, entity_env):
        entity_env(
            attached=[
                {
                    "type": "Person",
                    "name": "Alice Johnson",
                    "description": "Friend",
                    "attached_at": 1000,
                    "updated_at": 1000,
                }
            ]
        )

        result = runner.invoke(
            call_app,
            ["entities", "aka", "Alice Johnson", "Ali", "-f", "personal"],
        )

        assert result.exit_code == 0
        assert "Added alias" in result.output

    def test_aka_duplicate(self, entity_env):
        entity_env(
            attached=[
                {
                    "type": "Person",
                    "name": "Alice Johnson",
                    "description": "Friend",
                    "attached_at": 1000,
                    "updated_at": 1000,
                    "aka": ["Ali"],
                }
            ]
        )

        result = runner.invoke(
            call_app,
            ["entities", "aka", "Alice Johnson", "Ali", "-f", "personal"],
        )

        assert result.exit_code == 0
        assert "already exists" in result.output

    def test_aka_first_word(self, entity_env):
        entity_env(
            attached=[
                {
                    "type": "Person",
                    "name": "Alice Johnson",
                    "description": "Friend",
                    "attached_at": 1000,
                    "updated_at": 1000,
                }
            ]
        )

        result = runner.invoke(
            call_app,
            ["entities", "aka", "Alice Johnson", "Alice", "-f", "personal"],
        )

        assert result.exit_code == 0
        assert "first word" in result.output


class TestEntitiesObservations:
    def test_observations_empty(self, entity_env):
        entity_env(
            attached=[
                {
                    "type": "Person",
                    "name": "Alice Johnson",
                    "description": "Friend",
                    "attached_at": 1000,
                    "updated_at": 1000,
                }
            ]
        )

        result = runner.invoke(
            call_app,
            ["entities", "observations", "Alice Johnson", "-f", "personal"],
        )

        assert result.exit_code == 0
        assert "No observations" in result.output

    def test_observations_with_data(self, entity_env):
        entity_env(
            attached=[
                {
                    "type": "Person",
                    "name": "Alice Johnson",
                    "description": "Friend",
                    "attached_at": 1000,
                    "updated_at": 1000,
                }
            ],
            observations=["Likes coffee", "Expert in Python"],
            observation_entity="Alice Johnson",
        )

        result = runner.invoke(
            call_app,
            ["entities", "observations", "Alice Johnson", "-f", "personal"],
        )

        assert result.exit_code == 0
        assert "Likes coffee" in result.output
        assert "Expert in Python" in result.output


class TestEntitiesObserve:
    def test_observe_new(self, entity_env):
        entity_env(
            attached=[
                {
                    "type": "Person",
                    "name": "Alice Johnson",
                    "description": "Friend",
                    "attached_at": 1000,
                    "updated_at": 1000,
                }
            ]
        )

        result = runner.invoke(
            call_app,
            ["entities", "observe", "Alice Johnson", "Likes coffee", "-f", "personal"],
        )

        assert result.exit_code == 0
        assert "Observation added" in result.output

    def test_observe_not_found(self, entity_env):
        entity_env()

        result = runner.invoke(
            call_app,
            ["entities", "observe", "Missing", "Likes coffee", "-f", "personal"],
        )

        assert result.exit_code == 1
        assert "not found" in result.output


class TestSolEnvResolution:
    """Tests for SOL_* env var resolution in entities commands."""

    def test_list_from_sol_facet(self, entity_env, monkeypatch):
        """list with SOL_FACET env instead of positional arg works."""
        entity_env(
            attached=[
                {
                    "type": "Person",
                    "name": "Alice",
                    "description": "Friend",
                    "attached_at": 1000,
                    "updated_at": 1000,
                }
            ]
        )
        monkeypatch.setenv("SOL_FACET", "personal")
        result = runner.invoke(call_app, ["entities", "list"])
        assert result.exit_code == 0
        assert "Alice" in result.output

    def test_detect_from_sol_day_and_facet(self, entity_env, monkeypatch):
        """detect with SOL_DAY + SOL_FACET env works."""
        entity_env()
        monkeypatch.setenv("SOL_DAY", "20240101")
        monkeypatch.setenv("SOL_FACET", "personal")
        result = runner.invoke(
            call_app,
            ["entities", "detect", "Person", "Bob", "Met at party"],
        )
        assert result.exit_code == 0
        assert "detected" in result.output

    def test_detect_arg_overrides_sol_day(self, entity_env, monkeypatch):
        """detect with explicit --day works even with SOL_DAY set."""
        entity_env()
        monkeypatch.setenv("SOL_DAY", "19990101")
        result = runner.invoke(
            call_app,
            [
                "entities",
                "detect",
                "Person",
                "Charlie",
                "Met at office",
                "-f",
                "personal",
                "--day",
                "20240101",
            ],
        )
        assert result.exit_code == 0
        assert "detected" in result.output


class TestEntitiesMergeCandidates:
    def _candidate_rows(self, journal: Path) -> list[dict]:
        path = journal / "entities" / "review-candidates.jsonl"
        return [
            json.loads(line)
            for line in path.read_text(encoding="utf-8").splitlines()
            if line.strip()
        ]

    def _seed_merge_entities(self) -> None:
        save_journal_entity(
            {
                "id": "kognova_inc",
                "name": "Kognova Inc",
                "type": "Company",
                "aka": ["Kognova Incorporated"],
            }
        )
        save_journal_entity(
            {
                "id": "kognova",
                "name": "Kognova",
                "type": "Company",
                "aka": [],
            }
        )

    def _seed_merge_candidate(self) -> None:
        save_candidates(
            [
                {
                    "facet": "work",
                    "source": "Kognova Inc",
                    "source_slug": "kognova_inc",
                    "target": "Kognova",
                    "target_slug": "kognova",
                    "status": "open",
                    "evidence": {
                        "basis": "name-variant",
                        "summary": "Kognova Inc / Kognova",
                        "detection_count": 4,
                        "needs": 0,
                    },
                }
            ]
        )

    def test_record_merge_candidate_creates_one_row(self, entity_env):
        journal = entity_env()

        result = runner.invoke(
            call_app,
            [
                "entities",
                "record-merge-candidate",
                "Kognova Inc",
                "Kognova",
                "--facet",
                "work",
                "--day",
                "20260602",
                "--evidence",
                "Kognova Inc / Kognova — needs 1 more",
                "--detections",
                "3",
                "--needs",
                "1",
            ],
        )

        rows = self._candidate_rows(journal)
        assert result.exit_code == 0
        assert len(rows) == 1
        row = rows[0]
        assert row["source"] == "Kognova Inc"
        assert row["source_slug"] == entity_slug("Kognova Inc")
        assert row["target"] == "Kognova"
        assert row["target_slug"] == entity_slug("Kognova")
        assert row["status"] == "open"
        assert row["evidence"] == {
            "basis": "name-variant",
            "summary": "Kognova Inc / Kognova — needs 1 more",
            "detection_count": 3,
            "needs": 1,
        }
        assert row["first_surfaced"] == "20260602"
        assert row["last_surfaced"] == "20260602"

    def test_record_merge_candidate_upserts_idempotently(self, entity_env):
        journal = entity_env()
        base_args = [
            "entities",
            "record-merge-candidate",
            "Kognova Inc",
            "Kognova",
            "--facet",
            "work",
            "--evidence",
            "Kognova Inc / Kognova — needs 1 more",
        ]

        first = runner.invoke(
            call_app,
            [*base_args, "--day", "20260602", "--detections", "3"],
        )
        second = runner.invoke(
            call_app,
            [*base_args, "--day", "20260603", "--detections", "4"],
        )

        rows = self._candidate_rows(journal)
        assert first.exit_code == 0
        assert second.exit_code == 0
        assert len(rows) == 1
        row = rows[0]
        assert row["status"] == "open"
        assert row["first_surfaced"] == "20260602"
        assert row["last_surfaced"] == "20260603"
        assert row["evidence"]["detection_count"] == 4

    def test_record_merge_candidate_preserves_status_and_unknown_keys(self, entity_env):
        journal = entity_env()
        save_candidates(
            [
                {
                    "facet": "work",
                    "source": "Kognova Inc",
                    "source_slug": entity_slug("Kognova Inc"),
                    "target": "Kognova",
                    "target_slug": entity_slug("Kognova"),
                    "status": "dismissed",
                    "evidence": {
                        "basis": "name-variant",
                        "summary": "old summary",
                        "detection_count": 2,
                        "needs": 1,
                        "review_note": "preserve",
                    },
                    "first_surfaced": "20260602",
                    "last_surfaced": "20260602",
                    "created_at": "2026-06-02T17:30:00Z",
                    "updated_at": "2026-06-02T17:30:00Z",
                    "dismissed_detection_count": 2,
                }
            ]
        )

        result = runner.invoke(
            call_app,
            [
                "entities",
                "record-merge-candidate",
                "Kognova Inc",
                "Kognova",
                "--facet",
                "work",
                "--day",
                "20260603",
                "--evidence",
                "new summary",
                "--detections",
                "4",
            ],
        )

        rows = self._candidate_rows(journal)
        row = rows[0]
        assert result.exit_code == 0
        assert row["status"] == "dismissed"
        assert row["dismissed_detection_count"] == 2
        assert row["evidence"]["summary"] == "new summary"
        assert row["evidence"]["detection_count"] == 4
        assert row["evidence"]["needs"] == 1
        assert row["evidence"]["review_note"] == "preserve"

    def test_record_merge_candidate_same_slug_errors(self, entity_env):
        entity_env()

        result = runner.invoke(
            call_app,
            [
                "entities",
                "record-merge-candidate",
                "Kognova",
                "kognova",
                "--facet",
                "work",
                "--day",
                "20260602",
                "--evidence",
                "same",
            ],
        )

        assert result.exit_code == 1
        assert "same entity" in result.output

    def test_merge_candidates_json_and_filters(self, entity_env):
        entity_env()
        record = runner.invoke(
            call_app,
            [
                "entities",
                "record-merge-candidate",
                "Kognova Inc",
                "Kognova",
                "--facet",
                "work",
                "--day",
                "20260602",
                "--evidence",
                "Kognova Inc / Kognova",
            ],
        )

        all_result = runner.invoke(call_app, ["entities", "merge-candidates", "--json"])
        work_result = runner.invoke(
            call_app, ["entities", "merge-candidates", "--facet", "work", "--json"]
        )
        other_result = runner.invoke(
            call_app, ["entities", "merge-candidates", "--facet", "other", "--json"]
        )
        open_result = runner.invoke(
            call_app, ["entities", "merge-candidates", "--status", "open", "--json"]
        )

        assert record.exit_code == 0
        assert all_result.exit_code == 0
        assert work_result.exit_code == 0
        assert other_result.exit_code == 0
        assert open_result.exit_code == 0
        assert len(json.loads(all_result.output)) == 1
        assert len(json.loads(work_result.output)) == 1
        assert json.loads(other_result.output) == []
        assert len(json.loads(open_result.output)) == 1

    def test_merge_candidates_empty_text(self, entity_env):
        entity_env()

        result = runner.invoke(call_app, ["entities", "merge-candidates"])

        assert result.exit_code == 0
        assert "No merge candidates found." in result.output

    def test_accept_merge_candidate_preview_does_not_change_status(self, entity_env):
        journal = entity_env()
        self._seed_merge_entities()
        self._seed_merge_candidate()

        result = runner.invoke(
            call_app,
            [
                "entities",
                "accept-merge-candidate",
                "kognova_inc",
                "kognova",
                "--facet",
                "work",
            ],
        )

        rows = self._candidate_rows(journal)
        assert result.exit_code == 0
        assert "Merge preview:" in result.output
        assert "aliases added:" in result.output
        assert rows[0]["status"] == "open"

    def test_accept_merge_candidate_commit_marks_accepted(self, entity_env):
        journal = entity_env()
        self._seed_merge_entities()
        self._seed_merge_candidate()

        result = runner.invoke(
            call_app,
            [
                "entities",
                "accept-merge-candidate",
                "kognova_inc",
                "kognova",
                "--facet",
                "work",
                "--commit",
            ],
        )

        rows = self._candidate_rows(journal)
        assert result.exit_code == 0
        assert "Accepted merge candidate" in result.output
        assert rows[0]["status"] == "accepted"

    def test_dismiss_merge_candidate_sets_watermark(self, entity_env):
        journal = entity_env()
        self._seed_merge_candidate()

        result = runner.invoke(
            call_app,
            [
                "entities",
                "dismiss-merge-candidate",
                "kognova_inc",
                "kognova",
                "--facet",
                "work",
            ],
        )

        rows = self._candidate_rows(journal)
        assert result.exit_code == 0
        assert "Dismissed merge candidate" in result.output
        assert rows[0]["status"] == "dismissed"
        assert rows[0]["dismissed_detection_count"] == 4

    def test_accept_merge_candidate_commit_is_idempotent(self, entity_env):
        entity_env()
        self._seed_merge_entities()
        self._seed_merge_candidate()

        first = runner.invoke(
            call_app,
            [
                "entities",
                "accept-merge-candidate",
                "kognova_inc",
                "kognova",
                "--facet",
                "work",
                "--commit",
            ],
        )
        second = runner.invoke(
            call_app,
            [
                "entities",
                "accept-merge-candidate",
                "kognova_inc",
                "kognova",
                "--facet",
                "work",
                "--commit",
            ],
        )

        assert first.exit_code == 0
        assert second.exit_code == 0
        assert "already accepted" in second.output
