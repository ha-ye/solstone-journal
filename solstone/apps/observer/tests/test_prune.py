# SPDX-License-Identifier: AGPL-3.0-only
# Copyright (c) 2026 sol pbc

from __future__ import annotations

import io
import json
from pathlib import Path

from solstone.apps.observer.prune import run_prune
from solstone.apps.observer.utils import (
    append_history_record,
    list_observers,
    save_observer,
)
from solstone.think.streams import write_segment_stream

DAY = "20250103"
STREAM = "field"
AUDIO = b"observer prune upload bytes"
KEY = "field-prune-key"


def _sha(data: bytes) -> str:
    import hashlib

    return hashlib.sha256(data).hexdigest()


def _observer() -> dict:
    return {
        "key": KEY,
        "name": STREAM,
        "stream": STREAM,
        "created_at": 1,
        "last_seen": None,
        "enabled": True,
        "stats": {"segments_received": 10, "bytes_received": 999},
    }


def _write_segment(journal: Path, segment: str, seq: int, prev: str | None) -> Path:
    seg_dir = journal / "chronicle" / DAY / STREAM / segment
    seg_dir.mkdir(parents=True)
    write_segment_stream(seg_dir, STREAM, DAY if prev else None, prev, seq)
    (seg_dir / "audio.flac").write_bytes(AUDIO)
    (seg_dir / "audio.jsonl").write_text(
        json.dumps({"segment": segment}) + "\n",
        encoding="utf-8",
    )
    return seg_dir


def _upload_history(prefix: str, segment: str) -> None:
    append_history_record(
        prefix,
        DAY,
        {
            "ts": 1,
            "segment": segment,
            "stream": STREAM,
            "files": [
                {
                    "submitted": "audio.flac",
                    "written": "audio.flac",
                    "size": len(AUDIO),
                    "sha256": _sha(AUDIO),
                }
            ],
        },
    )


def test_pruned_segments_hide_from_listing_and_reupload_resolves_duplicate(
    observer_env,
) -> None:
    env = observer_env()
    assert save_observer(_observer())
    prefix = KEY[:8]
    _write_segment(env.journal, "120000_300", 1, None)
    _write_segment(env.journal, "120000_301", 2, "120000_300")
    _upload_history(prefix, "120000_300")
    _upload_history(prefix, "120000_301")
    _upload_history(prefix, "130000_300")

    result = run_prune(days=[DAY], stream=STREAM, execute=True)
    assert result.refusals == []
    assert [candidate.analysis.segment for candidate in result.deleted] == [
        "120000_301"
    ]

    listed = env.client.get(
        f"/app/observer/ingest/segments/{DAY}",
        headers={"Authorization": f"Bearer {KEY}"},
    )
    assert listed.status_code == 200
    payload = listed.get_json()
    items = payload["items"] if isinstance(payload, dict) else payload
    keys = {entry["key"] for entry in items}
    assert "120000_300" in keys
    assert "120000_301" not in keys
    missing = next(entry for entry in items if entry["key"] == "130000_300")
    assert missing["files"][0]["status"] == "missing"

    manifest = env.client.get(
        "/app/observer/ingest/manifest",
        headers={"Authorization": f"Bearer {KEY}"},
    )
    assert manifest.status_code == 200
    assert manifest.get_json()["days"][DAY]["segments"] == 2

    reupload = env.client.post(
        "/app/observer/ingest",
        headers={"Authorization": f"Bearer {KEY}"},
        data={
            "day": DAY,
            "segment": "120000_301",
            "files": [(io.BytesIO(AUDIO), "audio.flac")],
        },
    )
    assert reupload.status_code == 200
    body = reupload.get_json()
    assert body["status"] == "duplicate"
    assert body["existing_segment"] == "120000_300"

    records = list_observers()
    stats = records[0]["stats"]
    assert stats["segments_received"] == 10
    assert stats["bytes_received"] == 999
