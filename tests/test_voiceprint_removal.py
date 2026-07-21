# SPDX-License-Identifier: AGPL-3.0-only
# Copyright (c) 2026 sol pbc

from __future__ import annotations

import json
from pathlib import Path

import numpy as np
import pytest

from solstone.think.entities.voiceprints import (
    VoiceprintRemovalError,
    remove_voiceprints_by_key,
    save_voiceprints_batch,
)


@pytest.fixture
def voiceprint_journal(monkeypatch, tmp_path):
    monkeypatch.setenv("SOLSTONE_JOURNAL", str(tmp_path))
    import solstone.think.utils as think_utils

    think_utils._journal_path_cache = None
    return Path(tmp_path)


def _embedding(value: float) -> np.ndarray:
    vector = np.zeros(256, dtype=np.float32)
    vector[0] = value
    return vector


def _metadata(sentence_id: int, *, extra: str | None = None) -> dict[str, object]:
    metadata: dict[str, object] = {
        "day": "20260101",
        "segment_key": "090000_300",
        "source": "mic_audio",
        "stream": "test",
        "sentence_id": sentence_id,
        "added_at": 1,
    }
    if extra is not None:
        metadata["extra"] = extra
    return metadata


def _removal(metadata: dict[str, object]) -> dict[str, object]:
    return {
        "key": {
            "day": metadata["day"],
            "segment_key": metadata["segment_key"],
            "source": metadata["source"],
            "sentence_id": metadata["sentence_id"],
        },
        "expected_metadata": metadata,
    }


def _load_metadata(path: Path) -> list[dict[str, object]]:
    with np.load(path, allow_pickle=False) as data:
        return [json.loads(str(item)) for item in data["metadata"]]


def test_remove_one_voiceprint_by_key_and_metadata(voiceprint_journal):
    first = _metadata(1)
    second = _metadata(2)
    save_voiceprints_batch(
        "alice",
        [(_embedding(1.0), first), (_embedding(2.0), second)],
    )
    path = voiceprint_journal / "entities" / "alice" / "voiceprints.npz"

    report = remove_voiceprints_by_key("alice", [_removal(first)])

    assert report == {
        "removed_count": 1,
        "skipped_count": 0,
        "skipped_reasons": {"missing": 0, "metadata_mismatch": 0},
        "file_removed": False,
    }
    assert _load_metadata(path) == [second]


def test_missing_key_is_skipped(voiceprint_journal):
    existing = _metadata(1)
    missing = _metadata(99)
    save_voiceprints_batch("alice", [(_embedding(1.0), existing)])

    report = remove_voiceprints_by_key("alice", [_removal(missing)])

    assert report["removed_count"] == 0
    assert report["skipped_count"] == 1
    assert report["skipped_reasons"] == {"missing": 1, "metadata_mismatch": 0}


def test_metadata_mismatch_is_skipped(voiceprint_journal):
    existing = _metadata(1)
    expected = _metadata(1, extra="different")
    save_voiceprints_batch("alice", [(_embedding(1.0), existing)])

    report = remove_voiceprints_by_key("alice", [_removal(expected)])

    assert report["removed_count"] == 0
    assert report["skipped_count"] == 1
    assert report["skipped_reasons"] == {"missing": 0, "metadata_mismatch": 1}


def test_remove_all_deletes_file(voiceprint_journal):
    existing = _metadata(1)
    save_voiceprints_batch("alice", [(_embedding(1.0), existing)])
    path = voiceprint_journal / "entities" / "alice" / "voiceprints.npz"

    report = remove_voiceprints_by_key("alice", [_removal(existing)])

    assert report["removed_count"] == 1
    assert report["file_removed"] is True
    assert not path.exists()


def test_duplicate_exact_match_raises(voiceprint_journal):
    metadata = _metadata(1)
    path = voiceprint_journal / "entities" / "alice" / "voiceprints.npz"
    path.parent.mkdir(parents=True)
    np.savez_compressed(
        path,
        embeddings=np.stack([_embedding(1.0), _embedding(2.0)]),
        metadata=np.asarray([json.dumps(metadata), json.dumps(metadata)], dtype=str),
    )

    with pytest.raises(VoiceprintRemovalError):
        remove_voiceprints_by_key("alice", [_removal(metadata)])
