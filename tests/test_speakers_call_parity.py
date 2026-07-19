# SPDX-License-Identifier: AGPL-3.0-only
# Copyright (c) 2026 sol pbc

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import pytest
import requests
from typer.testing import CliRunner

import solstone.apps.speakers.call as speakers_call
import solstone.apps.speakers.routes as speakers_routes
from solstone.apps.speakers.call import app
from solstone.apps.speakers.encoder_config import OWNER_BOOTSTRAP_MIN_STMTS
from solstone.convey.reasons import (
    ENTITY_BLOCKED,
    INVALID_SEGMENT_OR_STREAM,
    SPEAKER_LABELS_BUSY,
    SPEAKER_OWNER_IDENTITY_REQUIRED,
    SPEAKER_SENTENCE_MISSING,
    SPEAKER_VOICEPRINT_BUSY,
)
from solstone.think.convey_client import ConveyClient
from solstone.think.journal_io import LockTimeout
from tests._baseline_harness import make_test_client, mark_setup_complete


@pytest.fixture
def journal(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    monkeypatch.setenv("SOLSTONE_JOURNAL", str(tmp_path))
    mark_setup_complete(tmp_path)
    return tmp_path


@pytest.fixture
def runner(journal: Path, monkeypatch: pytest.MonkeyPatch) -> CliRunner:
    client = ConveyClient(session=make_test_client(journal), base_url="")
    monkeypatch.setattr(speakers_call, "get_client", lambda: client)
    return CliRunner()


def _bootstrap_stats() -> dict[str, Any]:
    return {
        "segments_scanned": 5,
        "single_speaker_segments": 3,
        "speakers_found": {"Alice": 2, "Bob": 1},
        "entities_created": 1,
        "embeddings_saved": 3,
        "embeddings_skipped_owner": 1,
        "embeddings_skipped_duplicate": 2,
        "errors": [],
    }


def _resolve_stats() -> dict[str, Any]:
    return {
        "entities_with_voiceprints": 3,
        "pairs_compared": 2,
        "matches_found": [{"alias": "Al", "canonical": "Alice"}],
        "auto_merged": [{"alias": "Al", "canonical": "Alice", "similarity": 0.93}],
        "ambiguous": [
            {
                "name": "Bob",
                "candidates": [{"name": "Bobby", "similarity": 0.91}],
            }
        ],
        "errors": [],
    }


def _attribute_result() -> dict[str, Any]:
    return {
        "labels": [
            {"sentence_id": 1, "speaker": "alice", "method": "owner"},
            {"sentence_id": 2, "speaker": None, "method": None},
            {"sentence_id": 3, "speaker": "bob", "method": "acoustic"},
        ],
        "unmatched": [{"sentence_id": 2}],
        "source": "mic",
        "metadata": {"model": "test"},
    }


def _correct_result() -> dict[str, Any]:
    return {
        "status": "corrected",
        "old_speaker": "alice",
        "new_speaker": "bob",
        "voiceprint_removal": {
            "outcome": "rewritten",
            "entity_id": "alice",
            "keys_removed": ["20260101/120000_10/mic_audio#2"],
            "file_deleted": False,
            "path": "entities/alice/voiceprints.npz",
        },
        "propagation_offer": {
            "available": True,
            "statement_count": 3,
            "segment_count": 2,
            "route": "/app/speakers/api/propagate-correction",
            "request": {
                "old_speaker": "alice",
                "new_speaker": "bob",
                "commit": False,
            },
        },
    }


def _propagate_result(*, commit: bool) -> dict[str, Any]:
    return {
        "status": "applied" if commit else "preview",
        "commit": commit,
        "old_speaker": "alice",
        "new_speaker": "bob",
        "segments_scanned": 4,
        "segments_considered": 4,
        "segment_count": 2,
        "statement_count": 3,
        "changes": [],
        "segments": [],
        "errors": [],
        "reversal": {
            "verb": "speakers propagate-correction",
            "old_speaker": "bob",
            "new_speaker": "alice",
            "bounded_to": "segments where these two appear",
        },
    }


def _backfill_stats() -> dict[str, Any]:
    return {
        "total_segments": 10,
        "total_eligible": 7,
        "skipped_no_embed": 3,
        "already_labeled": 2,
        "processed": 5,
        "speakers_seen": {"alice": 3, "bob": 1},
        "errors": [],
    }


def _backfill_last_seen_stats() -> dict[str, Any]:
    return {
        "labels_read": 2,
        "entities_seen": 2,
        "rows_scanned": 5,
        "rows_pending": 2,
        "rows_written": 0,
        "pending": {"alice": {"rows": 2}},
        "errors": [],
    }


def _wipe_report() -> dict[str, Any]:
    return {
        "segment_embeddings": {"count": 1, "bytes": 10, "paths": ["a.npz"]},
        "speaker_labels": {"count": 2, "bytes": 20, "paths": ["labels.json"]},
        "speaker_corrections": {"count": 3, "bytes": 30, "paths": []},
        "entity_voiceprints": {"count": 4, "bytes": 40, "paths": []},
        "owner_centroids": {"count": 5, "bytes": 50, "paths": []},
        "owner_candidate": {"count": 6, "bytes": 60, "paths": []},
        "total_files": 21,
        "total_bytes": 210,
    }


def _seed_stats() -> dict[str, Any]:
    return {
        "segments_scanned": 4,
        "segments_with_speakers": 3,
        "speakers_found": {"Alice": 3, "Bob": 1},
        "embeddings_saved": 4,
        "embeddings_skipped_owner": 1,
        "embeddings_skipped_duplicate": 2,
        "speakers_unmatched": ["Unknown"],
        "errors": [],
    }


def _assert_json_stdout(result, expected: Any) -> None:
    assert result.exit_code == 0
    assert json.loads(result.stdout) == expected
    assert result.stderr == ""


def _owner_tag_guidance(manual_tags_count: int) -> str:
    needed = OWNER_BOOTSTRAP_MIN_STMTS - manual_tags_count
    return (
        "Use sol call speakers tag-owner <day> <stream> <segment> "
        "<source> <sentence-id> on owner sentences in raw media until "
        f"you have {OWNER_BOOTSTRAP_MIN_STMTS} validated owner tags; "
        f"{needed} more needed. Then run sol call speakers build-from-tags."
    )


def test_status_full_section_and_unknown(
    runner: CliRunner, monkeypatch: pytest.MonkeyPatch
) -> None:
    status = {
        "embeddings": {"total": 1},
        "owner": {"status": "confirmed"},
        "speakers": [{"name": "Alice"}],
        "pool": {"candidate_count": 2},
        "clusters": {"count": 0},
        "imports": {"names": []},
        "attribution": {"labels": 2},
    }
    monkeypatch.setattr(speakers_routes, "get_speakers_status", lambda section: status)

    full = runner.invoke(app, ["status"])
    speakers = runner.invoke(app, ["status", "speakers"])
    pool = runner.invoke(app, ["status", "pool"])
    unknown = runner.invoke(app, ["status", "nope"])

    _assert_json_stdout(full, status)
    _assert_json_stdout(speakers, [{"name": "Alice"}])
    _assert_json_stdout(pool, {"candidate_count": 2})
    _assert_json_stdout(
        unknown,
        {
            "error": (
                "Unknown section 'nope'. Valid: embeddings, owner, speakers, "
                "pool, clusters, imports, attribution"
            )
        },
    )


def test_bootstrap_text_commit_json_and_owner_error(
    runner: CliRunner, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(
        speakers_routes, "bootstrap_voiceprints", lambda dry_run: _bootstrap_stats()
    )

    dry = runner.invoke(app, ["bootstrap"])
    commit = runner.invoke(app, ["bootstrap", "--commit"])
    json_result = runner.invoke(app, ["bootstrap", "--json"])

    assert dry.exit_code == 0
    assert dry.stderr == ""
    assert dry.stdout == (
        "REPORT ONLY — pass --commit to persist.\n"
        "\n"
        "Bootstrapping voiceprints from single-speaker segments...\n"
        "\n"
        "Segments scanned: 5\n"
        "Single-speaker segments: 3\n"
        "Unique speakers: 2\n"
        "Entities created: 1\n"
        "Embeddings saved: 3\n"
        "Embeddings skipped (owner): 1\n"
        "Embeddings skipped (duplicate): 2\n"
        "\n"
        "Top speakers by embedding count:\n"
        "  Alice: 2\n"
        "  Bob: 1\n"
    )
    assert commit.exit_code == 0
    assert commit.stderr == ""
    assert commit.stdout == dry.stdout.removeprefix(
        "REPORT ONLY — pass --commit to persist.\n\n"
    )
    _assert_json_stdout(json_result, _bootstrap_stats())

    monkeypatch.setattr(
        speakers_routes,
        "bootstrap_voiceprints",
        lambda dry_run: {
            "error": "No confirmed owner centroid. Run owner detection first."
        },
    )
    error = runner.invoke(app, ["bootstrap"])

    assert error.exit_code == 1
    assert error.stdout == (
        "REPORT ONLY — pass --commit to persist.\n"
        "\n"
        "Bootstrapping voiceprints from single-speaker segments...\n"
    )
    assert (
        error.stderr
        == "Error: No confirmed owner centroid. Run owner detection first.\n"
    )


def test_resolve_names_text_and_json(
    runner: CliRunner, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(
        speakers_routes, "resolve_name_variants", lambda dry_run: _resolve_stats()
    )

    text = runner.invoke(app, ["resolve-names"])
    json_result = runner.invoke(app, ["resolve-names", "--json"])

    assert text.exit_code == 0
    assert text.stderr == ""
    assert text.stdout == (
        "REPORT ONLY — pass --commit to persist.\n"
        "\n"
        "Resolving speaker name variants...\n"
        "\n"
        "Entities with voiceprints: 3\n"
        "Pairs compared: 2\n"
        "High-similarity pairs: 1\n"
        "\n"
        "Auto-merged (1):\n"
        "  Al -> Alice (0.93)\n"
        "\n"
        "Ambiguous (1):\n"
        "  Bob: Bobby (0.91)\n"
    )
    _assert_json_stdout(json_result, _resolve_stats())


def test_attribute_segment_text_json_error_and_commit_outputs(
    runner: CliRunner, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(
        speakers_routes,
        "attribute_segment",
        lambda day, stream, segment: _attribute_result(),
    )
    monkeypatch.setattr(
        speakers_routes,
        "save_speaker_labels",
        lambda seg_dir, labels, metadata: Path("/tmp/speaker_labels.json"),
    )
    monkeypatch.setattr(
        speakers_routes,
        "accumulate_voiceprints",
        lambda day, stream, segment, labels, source: {"alice": 2},
    )

    text = runner.invoke(app, ["attribute-segment", "20260101", "mic", "120000_10"])
    json_result = runner.invoke(
        app, ["attribute-segment", "20260101", "mic", "120000_10", "--json"]
    )
    commit = runner.invoke(
        app, ["attribute-segment", "20260101", "mic", "120000_10", "--commit"]
    )

    assert text.exit_code == 0
    assert text.stderr == ""
    assert text.stdout == (
        "REPORT ONLY — pass --commit to persist.\n"
        "\n"
        "Sentences: 3\n"
        "Resolved:  2\n"
        "Unmatched: 1\n"
        "\n"
        "By method:\n"
        "  acoustic: 1\n"
        "  owner: 1\n"
        "  unmatched: 1\n"
    )
    _assert_json_stdout(json_result, _attribute_result())
    assert commit.exit_code == 0
    assert commit.stderr == ""
    assert commit.stdout == (
        "Sentences: 3\n"
        "Resolved:  2\n"
        "Unmatched: 1\n"
        "\n"
        "By method:\n"
        "  acoustic: 1\n"
        "  owner: 1\n"
        "  unmatched: 1\n"
        "\n"
        "Wrote: /tmp/speaker_labels.json\n"
        "\n"
        "Accumulated voiceprints:\n"
        "  alice: 2 embeddings\n"
    )

    monkeypatch.setattr(
        speakers_routes,
        "attribute_segment",
        lambda day, stream, segment: {"error": "no_owner_centroid"},
    )
    error = runner.invoke(app, ["attribute-segment", "20260101", "mic", "120000_10"])

    assert error.exit_code == 1
    assert error.stdout == "REPORT ONLY — pass --commit to persist.\n\n"
    assert error.stderr == "Error: no_owner_centroid\n"


def test_correct_text_and_json_use_correction_route(
    runner: CliRunner,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    calls: list[tuple[str, str, dict[str, Any] | None]] = []

    def fake_request(
        method: str,
        path: str,
        *,
        params: dict[str, Any] | None = None,
        json_body: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        calls.append((method, path, json_body))
        assert params is None
        return _correct_result()

    monkeypatch.setattr(speakers_call, "_request", fake_request)

    text = runner.invoke(
        app,
        [
            "correct",
            "20260101",
            "mic",
            "120000_10",
            "mic_audio",
            "2",
            "bob",
        ],
    )
    json_result = runner.invoke(
        app,
        [
            "correct",
            "20260101",
            "mic",
            "120000_10",
            "mic_audio",
            "2",
            "bob",
            "--json",
        ],
    )

    assert text.exit_code == 0
    assert text.stderr == ""
    assert text.stdout == (
        "Corrected 20260101/mic/120000_10 #2: alice -> bob\n"
        "Voiceprint removal: rewritten\n"
        "Propagation available: 3 statements in 2 segments would change\n"
        "Preview with: sol call speakers propagate-correction alice bob\n"
    )
    _assert_json_stdout(json_result, _correct_result())
    assert calls == [
        (
            "POST",
            "/app/speakers/api/correct-attribution",
            {
                "day": "20260101",
                "stream": "mic",
                "segment_key": "120000_10",
                "source": "mic_audio",
                "sentence_id": 2,
                "new_speaker": "bob",
            },
        ),
        (
            "POST",
            "/app/speakers/api/correct-attribution",
            {
                "day": "20260101",
                "stream": "mic",
                "segment_key": "120000_10",
                "source": "mic_audio",
                "sentence_id": 2,
                "new_speaker": "bob",
            },
        ),
    ]


def test_propagate_correction_previews_by_default_and_commits(
    runner: CliRunner,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    calls: list[tuple[str, str, dict[str, Any] | None]] = []

    def fake_request(
        method: str,
        path: str,
        *,
        params: dict[str, Any] | None = None,
        json_body: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        calls.append((method, path, json_body))
        assert params is None
        return _propagate_result(commit=bool(json_body and json_body.get("commit")))

    monkeypatch.setattr(speakers_call, "_request", fake_request)

    preview = runner.invoke(app, ["propagate-correction", "alice", "bob"])
    commit = runner.invoke(
        app,
        ["propagate-correction", "alice", "bob", "--commit"],
    )
    json_result = runner.invoke(
        app,
        ["propagate-correction", "alice", "bob", "--json"],
    )

    assert preview.exit_code == 0
    assert preview.stderr == ""
    assert preview.stdout == (
        "REPORT ONLY — pass --commit to persist.\n"
        "\n"
        "Would change: 3 statements in 2 segments\n"
    )
    assert commit.exit_code == 0
    assert commit.stderr == ""
    assert commit.stdout == (
        "Applied: 3 statements in 2 segments\n"
        "Reverse with: sol call speakers propagate-correction bob alice --commit\n"
    )
    _assert_json_stdout(json_result, _propagate_result(commit=False))
    assert calls == [
        (
            "POST",
            "/app/speakers/api/propagate-correction",
            {"old_speaker": "alice", "new_speaker": "bob", "commit": False},
        ),
        (
            "POST",
            "/app/speakers/api/propagate-correction",
            {"old_speaker": "alice", "new_speaker": "bob", "commit": True},
        ),
        (
            "POST",
            "/app/speakers/api/propagate-correction",
            {"old_speaker": "alice", "new_speaker": "bob", "commit": False},
        ),
    ]


def test_backfill_text_commit_without_progress_and_json(
    runner: CliRunner, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(
        speakers_routes,
        "backfill_segments",
        lambda dry_run, progress_callback: _backfill_stats(),
    )
    monkeypatch.setattr(speakers_call.time, "monotonic", lambda: 0.0)

    dry = runner.invoke(app, ["backfill"])
    commit = runner.invoke(app, ["backfill", "--commit"])
    json_result = runner.invoke(app, ["backfill", "--json"])

    dry_expected = (
        "REPORT ONLY — pass --commit to persist.\n"
        "\n"
        "Scanning journal for segments with embeddings...\n"
        "\n"
        "\n"
        "Total segments scanned:    10\n"
        "With embeddings:           7\n"
        "Without embeddings:        3\n"
        "Already labeled (skipped): 2\n"
        "Processed this run:        5\n"
        "Elapsed:                   0.0s\n"
        "\n"
        "Speakers identified (2):\n"
        "  alice: 3 attributions\n"
        "  bob: 1 attributions\n"
    )
    assert dry.exit_code == 0
    assert dry.stderr == ""
    assert dry.stdout == dry_expected
    assert commit.exit_code == 0
    assert commit.stderr == ""
    assert commit.stdout == dry_expected.removeprefix(
        "REPORT ONLY — pass --commit to persist.\n\n"
    )
    assert "\n  202" not in commit.stdout
    _assert_json_stdout(json_result, _backfill_stats())


def test_backfill_last_seen_text_and_json(
    runner: CliRunner, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(
        speakers_routes,
        "backfill_last_seen",
        lambda dry_run: _backfill_last_seen_stats(),
    )

    text = runner.invoke(app, ["backfill-last-seen"])
    json_result = runner.invoke(app, ["backfill-last-seen", "--json"])

    assert text.exit_code == 0
    assert text.stderr == ""
    assert text.stdout == (
        "REPORT ONLY — pass --commit to persist.\n"
        "\n"
        "Speaker label files read: 2\n"
        "Entities seen:            2\n"
        "Voiceprint rows scanned:  5\n"
        "Rows pending:             2\n"
        "Rows written:             0\n"
        "\n"
        "Pending by entity:\n"
        "  alice: 2\n"
    )
    _assert_json_stdout(json_result, _backfill_last_seen_stats())


def test_wipe_text_and_json(runner: CliRunner, monkeypatch: pytest.MonkeyPatch) -> None:
    class Report:
        def to_dict(self) -> dict[str, Any]:
            return _wipe_report()

    monkeypatch.setattr(
        speakers_routes, "wipe_speaker_artifacts", lambda dry_run: Report()
    )

    text = runner.invoke(app, ["wipe"])
    json_result = runner.invoke(app, ["wipe", "--json"])

    assert text.exit_code == 0
    assert text.stderr == ""
    assert text.stdout == (
        "REPORT ONLY — pass --commit to persist.\n"
        "\n"
        "segment_embeddings : 1 files (10 B)\n"
        "speaker_labels     : 2 files (20 B)\n"
        "speaker_corrections: 3 files (30 B)\n"
        "entity_voiceprints : 4 files (40 B)\n"
        "owner_centroids    : 5 files (50 B)\n"
        "owner_candidate    : 6 files (60 B)\n"
        "total              : 21 files (210 B)\n"
    )
    _assert_json_stdout(json_result, _wipe_report())


def test_discover_text_no_clusters_clusters_and_json(
    runner: CliRunner, monkeypatch: pytest.MonkeyPatch
) -> None:
    cluster_result = {
        "clusters": [
            {
                "cluster_id": 1,
                "size": 2,
                "segment_count": 1,
                "samples": [
                    {
                        "day": "20260101",
                        "stream": "mic",
                        "segment_key": "120000_10",
                        "sentence_id": 7,
                        "text": "hello from an unknown speaker",
                    }
                ],
            }
        ]
    }

    monkeypatch.setattr(
        speakers_routes, "discover_unknown_speakers", lambda: {"clusters": []}
    )
    empty = runner.invoke(app, ["discover"])

    assert empty.exit_code == 0
    assert empty.stdout == "No recurring unknown speakers found.\n"
    assert empty.stderr == ""

    monkeypatch.setattr(
        speakers_routes, "discover_unknown_speakers", lambda: cluster_result
    )
    text = runner.invoke(app, ["discover"])
    json_result = runner.invoke(app, ["discover", "--json"])

    assert text.exit_code == 0
    assert text.stderr == ""
    assert text.stdout == (
        "Found 1 unknown speaker cluster(s):\n"
        "\n"
        "  Cluster 1: 2 samples across 1 segments\n"
        "    - 20260101/mic/120000_10 sid=7: hello from an unknown speaker\n"
        "\n"
    )
    _assert_json_stdout(json_result, cluster_result)


def test_identify_success_forwards_entity_id_and_family2_error(
    runner: CliRunner, monkeypatch: pytest.MonkeyPatch
) -> None:
    seen: dict[str, Any] = {}

    def identify_cluster(cluster_id: int, name: str, entity_id: str | None = None):
        seen.update({"cluster_id": cluster_id, "name": name, "entity_id": entity_id})
        return {"status": "identified", "entity_id": entity_id}

    monkeypatch.setattr(speakers_routes, "identify_cluster", identify_cluster)
    success = runner.invoke(
        app, ["identify", "3", "Alice", "--entity-id", "person-alice"]
    )

    _assert_json_stdout(success, {"status": "identified", "entity_id": "person-alice"})
    assert seen == {"cluster_id": 3, "name": "Alice", "entity_id": "person-alice"}

    error_payload = {"error": "No discovery scan results. Run scan first."}
    monkeypatch.setattr(
        speakers_routes,
        "identify_cluster",
        lambda cluster_id, name, entity_id=None: error_payload,
    )
    error = runner.invoke(app, ["identify", "3", "Alice"])

    assert error.exit_code == 1
    assert error.stdout == ""
    assert error.stderr == json.dumps(error_payload, indent=2, default=str) + "\n"


def test_merge_names_success_simple_error_and_multi_key_error(
    runner: CliRunner, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(
        speakers_routes,
        "merge_names",
        lambda alias, canonical: {
            "status": "merged",
            "alias": alias,
            "canonical": canonical,
        },
    )
    success = runner.invoke(app, ["merge-names", "Al", "Alice"])
    _assert_json_stdout(
        success, {"status": "merged", "alias": "Al", "canonical": "Alice"}
    )

    simple_error = {"error": "Alias entity not found"}
    monkeypatch.setattr(
        speakers_routes, "merge_names", lambda alias, canonical: simple_error
    )
    simple = runner.invoke(app, ["merge-names", "Al", "Alice"])
    assert simple.exit_code == 1
    assert simple.stdout == ""
    assert simple.stderr == json.dumps(simple_error, indent=2, default=str) + "\n"

    multi_error = {
        "error": "Merge failed",
        "failed_phase": "labels",
        "recovery": "Retry after fixing labels",
    }
    monkeypatch.setattr(
        speakers_routes, "merge_names", lambda alias, canonical: multi_error
    )
    multi = runner.invoke(app, ["merge-names", "Al", "Alice"])
    assert multi.exit_code == 1
    assert multi.stdout == ""
    assert multi.stderr == json.dumps(multi_error, indent=2, default=str) + "\n"


def test_link_import_success_and_entity_not_found_error(
    runner: CliRunner, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(
        speakers_routes,
        "link_import",
        lambda name, entity_id: {
            "status": "linked",
            "name": name,
            "entity_id": entity_id,
        },
    )
    success = runner.invoke(
        app, ["link-import", "Alice", "--entity-id", "person-alice"]
    )
    _assert_json_stdout(
        success,
        {"status": "linked", "name": "Alice", "entity_id": "person-alice"},
    )

    error_payload = {"error": "Entity not found: person-missing"}
    monkeypatch.setattr(
        speakers_routes, "link_import", lambda name, entity_id: error_payload
    )
    error = runner.invoke(
        app, ["link-import", "Alice", "--entity-id", "person-missing"]
    )
    assert error.exit_code == 1
    assert error.stdout == ""
    assert error.stderr == json.dumps(error_payload, indent=2, default=str) + "\n"


def test_seed_from_imports_text_and_owner_error(
    runner: CliRunner, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(
        speakers_routes, "seed_from_imports", lambda dry_run: _seed_stats()
    )

    text = runner.invoke(app, ["seed-from-imports"])

    assert text.exit_code == 0
    assert text.stderr == ""
    assert text.stdout == (
        "REPORT ONLY — pass --commit to persist.\n"
        "\n"
        "Seeding voiceprints from import segments...\n"
        "\n"
        "Segments scanned: 4\n"
        "Segments with speakers: 3\n"
        "Unique speakers: 2\n"
        "Embeddings saved: 4\n"
        "Embeddings skipped (owner): 1\n"
        "Embeddings skipped (duplicate): 2\n"
        "\n"
        "Speakers by embedding count:\n"
        "  Alice: 3\n"
        "  Bob: 1\n"
        "\n"
        "Unmatched speakers (1):\n"
        "  Unknown\n"
    )

    monkeypatch.setattr(
        speakers_routes,
        "seed_from_imports",
        lambda dry_run: {
            "error": "No confirmed owner centroid. Run owner detection first."
        },
    )
    error = runner.invoke(app, ["seed-from-imports"])

    assert error.exit_code == 1
    assert error.stdout == (
        "REPORT ONLY — pass --commit to persist.\n"
        "\n"
        "Seeding voiceprints from import segments...\n"
    )
    assert (
        error.stderr
        == "Error: No confirmed owner centroid. Run owner detection first.\n"
    )


def test_suggest_json_is_bare_items_and_text_is_server_markdown(
    runner: CliRunner, monkeypatch: pytest.MonkeyPatch
) -> None:
    items = [{"kind": "name_variant", "name": "Al"}]
    monkeypatch.setattr(speakers_routes, "suggest_opportunities", lambda limit: items)
    monkeypatch.setattr(
        speakers_routes, "format_suggestions", lambda results: "server markdown"
    )

    text = runner.invoke(app, ["suggest", "--limit", "9"])
    json_result = runner.invoke(app, ["suggest", "--json"])

    assert text.exit_code == 0
    assert text.stdout == "server markdown\n"
    assert text.stderr == ""
    _assert_json_stdout(json_result, items)


def test_detect_success_json_and_busy_owner_voice(
    runner: CliRunner, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(
        speakers_routes,
        "detect_owner_candidate",
        lambda: {"status": "candidate", "cluster_size": 4},
    )
    success = runner.invoke(app, ["detect"])
    _assert_json_stdout(success, {"status": "candidate", "cluster_size": 4})

    monkeypatch.setattr(
        speakers_routes,
        "detect_owner_candidate",
        lambda: {"error_kind": "voiceprint_busy", "error": "busy"},
    )
    busy = runner.invoke(app, ["detect"])

    assert busy.exit_code == 1
    assert busy.stdout == ""
    assert busy.stderr == f"{SPEAKER_VOICEPRINT_BUSY.message}\n"


def test_detect_help_has_no_json_option(runner: CliRunner) -> None:
    result = runner.invoke(app, ["detect", "--help"])

    assert result.exit_code == 0
    assert "--json" not in result.stdout


def test_build_from_tags_low_quality_json_and_busy(
    runner: CliRunner, monkeypatch: pytest.MonkeyPatch
) -> None:
    manual_tags_count = 2
    low_quality = {
        "status": "low_quality",
        "source": "manual_tags",
        "recommendation": "low_quality",
        "segments_available": manual_tags_count,
        "embeddings_available": manual_tags_count,
        "low_quality_reason": "too_few_stmts",
        "observed_value": float(manual_tags_count),
        "threshold_value": float(OWNER_BOOTSTRAP_MIN_STMTS),
        "manual_tags_count": manual_tags_count,
        "can_build_from_tags": False,
        "next_step": "seed_manual_tags",
        "guidance": _owner_tag_guidance(manual_tags_count),
    }
    monkeypatch.setattr(
        speakers_routes,
        "bootstrap_owner_from_manual_tags",
        lambda: low_quality,
    )

    text = runner.invoke(app, ["build-from-tags"])
    json_result = runner.invoke(app, ["build-from-tags", "--json"])

    assert text.exit_code == 0
    assert text.stderr == ""
    assert text.stdout == (
        "Owner manual tags are not ready.\n"
        "Reason: too_few_stmts\n"
        f"Observed: {float(manual_tags_count)}\n"
        f"Threshold: {float(OWNER_BOOTSTRAP_MIN_STMTS)}\n"
        f"Manual tags: {manual_tags_count}\n"
        "Can build from tags: False\n"
        "Next step: seed_manual_tags\n"
        f"Guidance: {_owner_tag_guidance(manual_tags_count)}\n"
    )
    _assert_json_stdout(json_result, low_quality)

    monkeypatch.setattr(
        speakers_routes,
        "bootstrap_owner_from_manual_tags",
        lambda: {"error_kind": "voiceprint_busy", "error": "locked"},
    )
    busy = runner.invoke(app, ["build-from-tags"])

    assert busy.exit_code == 1
    assert busy.stdout == ""
    assert busy.stderr == f"{SPEAKER_VOICEPRINT_BUSY.message}\n"


def test_rebuild_owner_cli_posts_to_http_and_formats_json(
    runner: CliRunner, monkeypatch: pytest.MonkeyPatch
) -> None:
    calls: list[bool] = []
    payload = {
        "status": "rebuilt",
        "principal_id": "jer",
        "cluster_size": 30,
        "override_applied": True,
        "next_step": "none",
        "guidance": "",
    }

    def fake_rebuild_owner_centroid(*, override: bool = False) -> dict[str, Any]:
        calls.append(override)
        return dict(payload)

    monkeypatch.setattr(
        speakers_routes,
        "rebuild_owner_centroid",
        fake_rebuild_owner_centroid,
    )

    text = runner.invoke(app, ["rebuild-owner", "--override"])
    json_result = runner.invoke(app, ["rebuild-owner", "--override", "--json"])

    assert text.exit_code == 0
    assert text.stderr == ""
    assert text.stdout == (
        "Owner centroid rebuilt (principal: jer, cluster_size: 30)\n"
        "Override applied: true\n"
    )
    _assert_json_stdout(json_result, payload)
    assert calls == [True, True]


def test_rebuild_owner_cli_honest_refusal_next_step_guidance(
    runner: CliRunner, monkeypatch: pytest.MonkeyPatch
) -> None:
    refusal = {
        "status": "rejected_regression",
        "reason": "centroid_agreement_too_low",
        "next_step": "review_or_override",
        "guidance": "Review the manual owner tags.",
    }
    monkeypatch.setattr(
        speakers_routes,
        "rebuild_owner_centroid",
        lambda *, override=False: refusal,
    )

    result = runner.invoke(app, ["rebuild-owner"])

    assert result.exit_code == 0
    assert result.stderr == ""
    assert result.stdout == (
        "Owner centroid rebuild did not write.\n"
        "Reason: centroid_agreement_too_low\n"
        "Next step: review_or_override\n"
        "Guidance: Review the manual owner tags.\n"
    )


def test_tag_owner_success_retry_safe_actionable_errors_and_busy(
    runner: CliRunner, monkeypatch: pytest.MonkeyPatch
) -> None:
    args = ["tag-owner", "20260101", "mic", "120000_10", "mic_audio", "1"]
    monkeypatch.setattr(speakers_routes, "_principal_id_or_none", lambda: "jer")
    monkeypatch.setattr(
        speakers_routes,
        "_assign_attribution_impl",
        lambda *_unused: speakers_routes.success_response({"status": "assigned"}),
    )

    assigned = runner.invoke(app, args)

    assert assigned.exit_code == 0
    assert assigned.stderr == ""
    assert assigned.stdout == ("Tagged owner sentence: 20260101/mic/120000_10 #1\n")

    monkeypatch.setattr(
        speakers_routes,
        "_assign_attribution_impl",
        lambda *_unused: speakers_routes.success_response(
            {"status": "already_assigned"}
        ),
    )
    already = runner.invoke(app, args)

    assert already.exit_code == 0
    assert already.stderr == ""
    assert already.stdout == (
        "Owner sentence already tagged: 20260101/mic/120000_10 #1\n"
    )

    cases = [
        (
            "sentence missing",
            SPEAKER_SENTENCE_MISSING,
            "Pick a different sentence with an embedding.",
        ),
        (
            "no embedding",
            SPEAKER_SENTENCE_MISSING,
            "Pick a different sentence with an embedding.",
        ),
        (
            "invalid segment",
            INVALID_SEGMENT_OR_STREAM,
            "Use a valid day, stream, and segment, then pick a sentence.",
        ),
        ("blocked entity", ENTITY_BLOCKED, "Choose an unblocked speaker."),
    ]
    for _case, reason, detail in cases:

        def attribution_error(*_unused, reason=reason, detail=detail):
            return speakers_routes.error_response(reason, detail=detail)

        monkeypatch.setattr(
            speakers_routes,
            "_assign_attribution_impl",
            attribution_error,
        )
        result = runner.invoke(app, args)

        assert result.exit_code == 1
        assert result.stdout == ""
        assert result.stderr == f"{detail}\n"

    monkeypatch.setattr(
        speakers_routes,
        "_assign_attribution_impl",
        lambda *_unused: speakers_routes.error_response(
            SPEAKER_VOICEPRINT_BUSY, detail="locked"
        ),
    )
    voice_busy = runner.invoke(app, args)

    assert voice_busy.exit_code == 1
    assert voice_busy.stdout == ""
    assert voice_busy.stderr == f"{SPEAKER_VOICEPRINT_BUSY.message}\n"

    monkeypatch.setattr(
        speakers_routes,
        "_assign_attribution_impl",
        lambda *_unused: speakers_routes.error_response(
            SPEAKER_LABELS_BUSY, detail="locked"
        ),
    )
    labels_busy = runner.invoke(app, args)

    assert labels_busy.exit_code == 1
    assert labels_busy.stdout == ""
    assert labels_busy.stderr == f"{SPEAKER_LABELS_BUSY.message}\n"


def test_tag_owner_identity_required_error(
    runner: CliRunner, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(speakers_routes, "_principal_id_or_none", lambda: None)
    monkeypatch.setattr(speakers_routes, "principal_identity_or_none", lambda: None)

    result = runner.invoke(
        app, ["tag-owner", "20260101", "mic", "120000_10", "mic_audio", "1"]
    )

    assert result.exit_code == 1
    assert result.stdout == ""
    assert result.stderr == f"{SPEAKER_OWNER_IDENTITY_REQUIRED.message}\n"


def test_sentences_projects_sentence_id_text_and_json(
    runner: CliRunner, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(
        speakers_routes,
        "_load_sentences",
        lambda day, segment_key, source, stream=None: (
            [
                {"id": 1, "text": "hello owner sentence", "has_embedding": True},
                {"id": 2, "text": "missing embedding", "has_embedding": False},
            ],
            None,
        ),
    )
    monkeypatch.setattr(
        speakers_routes,
        "_load_speaker_labels",
        lambda _segment_dir: {
            "labels": [],
            "skipped": True,
            "reason": "pre-bootstrap owner",
        },
    )
    stub_json_result = runner.invoke(
        app, ["sentences", "20260101", "mic", "120000_10", "mic_audio", "--json"]
    )

    _assert_json_stdout(
        stub_json_result,
        {
            "success": True,
            "day": "20260101",
            "stream": "mic",
            "segment_key": "120000_10",
            "source": "mic_audio",
            "sentences": [
                {
                    "sentence_id": 1,
                    "text": "hello owner sentence",
                    "has_embedding": True,
                    "speaker": None,
                    "confidence": None,
                    "method": None,
                    "needs_review": True,
                },
                {
                    "sentence_id": 2,
                    "text": "missing embedding",
                    "has_embedding": False,
                    "speaker": None,
                    "confidence": None,
                    "method": None,
                    "needs_review": True,
                },
            ],
        },
    )

    monkeypatch.setattr(
        speakers_routes,
        "_load_speaker_labels",
        lambda _segment_dir: {
            "labels": [
                {
                    "sentence_id": 1,
                    "speaker": "jer",
                    "confidence": "high",
                    "method": "user_assigned",
                }
            ]
        },
    )

    text = runner.invoke(
        app, ["sentences", "20260101", "mic", "120000_10", "mic_audio"]
    )
    json_result = runner.invoke(
        app, ["sentences", "20260101", "mic", "120000_10", "mic_audio", "--json"]
    )

    assert text.exit_code == 0
    assert text.stderr == ""
    assert text.stdout == (
        "Sentences for 20260101/mic/120000_10/mic_audio:\n"
        "* 1: hello owner sentence\n"
        "- 2: missing embedding\n"
    )
    _assert_json_stdout(
        json_result,
        {
            "success": True,
            "day": "20260101",
            "stream": "mic",
            "segment_key": "120000_10",
            "source": "mic_audio",
            "sentences": [
                {
                    "sentence_id": 1,
                    "text": "hello owner sentence",
                    "has_embedding": True,
                    "speaker": "jer",
                    "confidence": "high",
                    "method": "user_assigned",
                    "needs_review": False,
                },
                {
                    "sentence_id": 2,
                    "text": "missing embedding",
                    "has_embedding": False,
                    "speaker": None,
                    "confidence": None,
                    "method": None,
                    "needs_review": True,
                },
            ],
        },
    )


def test_day_segments_reports_total_when_limited(
    runner: CliRunner, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(
        speakers_routes,
        "_scan_segment_embeddings",
        lambda day: [
            {"stream": "sys", "key": "100000_60", "sources": ["sys_audio"]},
            {"stream": "mic", "key": "090000_60", "sources": ["mic_audio"]},
        ],
    )

    text = runner.invoke(app, ["day-segments", "20260101", "--limit", "1"])
    json_result = runner.invoke(
        app, ["day-segments", "20260101", "--limit", "1", "--json"]
    )

    assert text.exit_code == 0
    assert text.stderr == ""
    assert text.stdout == (
        "Showing 1 of 2 segments (limit 1)\nmic/090000_60: mic_audio\n"
    )
    _assert_json_stdout(
        json_result,
        {
            "success": True,
            "day": "20260101",
            "segments": [
                {"stream": "mic", "key": "090000_60", "sources": ["mic_audio"]}
            ],
            "returned": 1,
            "limit": 1,
            "total": 2,
        },
    )


def test_confirm_owner_text_backfill_json_and_family2_error(
    runner: CliRunner, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(
        speakers_routes,
        "confirm_owner_candidate",
        lambda: {"status": "confirmed", "principal_id": "jer", "cluster_size": 4},
    )
    monkeypatch.setattr(
        speakers_routes,
        "backfill_segments",
        lambda dry_run, progress_callback: _backfill_stats(),
    )

    no_backfill = runner.invoke(app, ["confirm-owner", "--no-backfill"])
    default = runner.invoke(app, ["confirm-owner"])
    json_result = runner.invoke(app, ["confirm-owner", "--json"])

    assert no_backfill.exit_code == 0
    assert no_backfill.stderr == ""
    assert no_backfill.stdout == (
        "Owner centroid confirmed (principal: jer, cluster_size: 4)\n"
    )
    assert default.exit_code == 0
    assert default.stderr == ""
    assert default.stdout == (
        "Owner centroid confirmed (principal: jer, cluster_size: 4)\n"
        "Running attribution backfill...\n"
        "Backfill complete: 5 segments processed, 2 already labeled\n"
    )
    _assert_json_stdout(
        json_result,
        {
            "status": "confirmed",
            "principal_id": "jer",
            "cluster_size": 4,
            "backfill": _backfill_stats(),
        },
    )

    error_payload = {"error": "No candidate available"}
    monkeypatch.setattr(
        speakers_routes, "confirm_owner_candidate", lambda: error_payload
    )
    error = runner.invoke(app, ["confirm-owner", "--no-backfill"])

    assert error.exit_code == 1
    assert error.stdout == ""
    assert error.stderr == json.dumps(error_payload, indent=2, default=str) + "\n"


def test_reject_owner_and_owner_ready_json(
    runner: CliRunner, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(
        speakers_routes, "reject_owner_candidate", lambda: {"status": "rejected"}
    )
    monkeypatch.setattr(
        speakers_routes,
        "owner_detection_ready",
        lambda: {"ready": True, "reason": "enough_segments"},
    )

    reject = runner.invoke(app, ["reject-owner"])
    ready = runner.invoke(app, ["owner-ready"])

    _assert_json_stdout(reject, {"status": "rejected"})
    _assert_json_stdout(ready, {"ready": True, "reason": "enough_segments"})


def test_convey_down_prints_require_solstone_message(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    class DownSession:
        def get(self, _url):
            raise requests.exceptions.ConnectionError()

        def post(self, _url, json=None):
            raise requests.exceptions.ConnectionError()

    client = ConveyClient(session=DownSession(), base_url="http://localhost:5015")
    monkeypatch.setattr(speakers_call, "get_client", lambda: client)
    monkeypatch.delenv("SOL_SKIP_SUPERVISOR_CHECK", raising=False)
    monkeypatch.delenv("SOL_SUPERVISOR_SPAWNED", raising=False)

    result = CliRunner().invoke(app, ["status"])

    assert result.exit_code == 1
    assert (
        result.stderr
        == "sol: solstone isn't running. Start it with 'journal up' and retry.\n"
    )
    assert result.stdout == ""


def test_attribute_save_busy_prints_owner_voice_error(
    runner: CliRunner, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(
        speakers_routes,
        "attribute_segment",
        lambda day, stream, segment: _attribute_result(),
    )

    def save_speaker_labels(_seg_dir, _labels, _metadata):
        raise LockTimeout(Path("speaker_labels.json"), 0.01)

    monkeypatch.setattr(speakers_routes, "save_speaker_labels", save_speaker_labels)

    result = runner.invoke(
        app,
        [
            "attribute-segment",
            "20260101",
            "mic",
            "120000_10",
            "--commit",
            "--no-accumulate",
        ],
    )

    assert result.exit_code == 1
    assert result.stdout == ""
    assert result.stderr == f"{SPEAKER_LABELS_BUSY.message}\n"
