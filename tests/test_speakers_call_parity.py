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
from solstone.convey.reasons import SPEAKER_LABELS_BUSY, SPEAKER_VOICEPRINT_BUSY
from solstone.think.convey_client import ConveyClient
from solstone.think.journal_io import LockTimeout
from tests._baseline_harness import make_logged_in_test_client


@pytest.fixture
def journal(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    monkeypatch.setenv("SOLSTONE_JOURNAL", str(tmp_path))
    return tmp_path


@pytest.fixture
def runner(journal: Path, monkeypatch: pytest.MonkeyPatch) -> CliRunner:
    client = ConveyClient(session=make_logged_in_test_client(journal), base_url="")
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


def test_status_full_section_and_unknown(
    runner: CliRunner, monkeypatch: pytest.MonkeyPatch
) -> None:
    status = {
        "embeddings": {"total": 1},
        "owner": {"status": "confirmed"},
        "speakers": [{"name": "Alice"}],
        "clusters": {"count": 0},
        "imports": {"names": []},
        "attribution": {"labels": 2},
    }
    monkeypatch.setattr(speakers_routes, "get_speakers_status", lambda section: status)

    full = runner.invoke(app, ["status"])
    speakers = runner.invoke(app, ["status", "speakers"])
    unknown = runner.invoke(app, ["status", "nope"])

    _assert_json_stdout(full, status)
    _assert_json_stdout(speakers, [{"name": "Alice"}])
    _assert_json_stdout(
        unknown,
        {
            "error": (
                "Unknown section 'nope'. Valid: embeddings, owner, speakers, "
                "clusters, imports, attribution"
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
