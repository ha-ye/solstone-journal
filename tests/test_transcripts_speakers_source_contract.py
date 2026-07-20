# SPDX-License-Identifier: AGPL-3.0-only
# Copyright (c) 2026 sol pbc

from __future__ import annotations

from pathlib import Path

import numpy as np

from solstone.convey import create_app

FIXTURE_DAY = "20260304"
FIXTURE_STREAM = "default"
FIXTURE_SEGMENT = "090000_300"


def _write_embedding_npz(
    segment_dir: Path,
    *,
    source: str = "audio",
    statement_ids: tuple[int, ...] = (1, 4),
) -> None:
    np.savez_compressed(
        segment_dir / f"{source}.npz",
        embeddings=np.ones((len(statement_ids), 256), dtype=np.float32),
        statement_ids=np.array(statement_ids, dtype=np.int64),
        durations_s=np.ones(len(statement_ids), dtype=np.float32),
    )


def test_transcripts_speaker_source_resolves_in_speakers_review_cli(journal_copy):
    segment_dir = (
        journal_copy / "chronicle" / FIXTURE_DAY / FIXTURE_STREAM / FIXTURE_SEGMENT
    )
    _write_embedding_npz(segment_dir)
    app = create_app(str(journal_copy))

    with app.test_client() as client:
        transcripts_response = client.get(
            "/app/transcripts/api/segment/"
            f"{FIXTURE_DAY}/{FIXTURE_STREAM}/{FIXTURE_SEGMENT}"
        )

        assert transcripts_response.status_code == 200
        transcripts_payload = transcripts_response.get_json()
        actionable = [
            chunk
            for chunk in transcripts_payload["chunks"]
            if chunk["type"] == "audio" and chunk["has_embedding"]
        ]
        assert actionable

        for chunk in actionable:
            source = chunk["speaker_source"]
            assert (segment_dir / f"{source}.jsonl").is_file()
            assert (segment_dir / f"{source}.npz").is_file()

            speakers_response = client.get(
                "/app/speakers/api/review-cli/"
                f"{FIXTURE_DAY}/{FIXTURE_STREAM}/{FIXTURE_SEGMENT}/{source}"
            )

            assert speakers_response.status_code == 200
            speakers_payload = speakers_response.get_json()
            assert speakers_payload["source"] == source
            rows_by_id = {
                row["sentence_id"]: row for row in speakers_payload["sentences"]
            }
            assert rows_by_id[chunk["sentence_id"]]["has_embedding"] is True
