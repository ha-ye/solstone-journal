# SPDX-License-Identifier: AGPL-3.0-only
# Copyright (c) 2026 sol pbc

from __future__ import annotations

import io
import json
from pathlib import Path

from solstone.apps.observer.prune import format_result, run_prune
from solstone.apps.observer.utils import (
    append_history_record,
    list_observers,
    load_history,
    save_observer,
)
from solstone.think.link.auth import AuthorizedClients
from solstone.think.link.paths import authorized_clients_path
from solstone.think.streams import read_segment_stream, write_segment_stream

DAY = "20250103"
STREAM = "field"
AUDIO = b"observer prune upload bytes"
KEY = "field-prune-key"
FINGERPRINT = "sha256:" + ("c" * 64)


def _sha(data: bytes) -> str:
    import hashlib

    return hashlib.sha256(data).hexdigest()


def _observer() -> dict:
    AuthorizedClients(authorized_clients_path()).add(
        FINGERPRINT,
        "prune-device",
        "instance-1",
        paired_at="2026-05-20T00:00:00Z",
    )
    return {
        "key": KEY,
        "name": STREAM,
        "stream": STREAM,
        "device_binding": {"device": FINGERPRINT, "kind": "cert"},
        "created_at": 1,
        "last_seen": None,
        "enabled": True,
        "stats": {"segments_received": 10, "bytes_received": 999},
    }


def _write_segment(
    journal: Path,
    segment: str,
    seq: int,
    prev: str | None,
    *,
    audio: bytes = AUDIO,
    marker: bool = True,
) -> Path:
    seg_dir = journal / "chronicle" / DAY / STREAM / segment
    seg_dir.mkdir(parents=True)
    if marker:
        write_segment_stream(seg_dir, STREAM, DAY if prev else None, prev, seq)
    (seg_dir / "audio.flac").write_bytes(audio)
    (seg_dir / "audio.jsonl").write_text(
        json.dumps({"segment": segment}) + "\n",
        encoding="utf-8",
    )
    return seg_dir


def _write_unverifiable_manifest_segment(
    journal: Path, segment: str, seq: int, prev: str | None
) -> Path:
    seg_dir = journal / "chronicle" / DAY / STREAM / segment
    seg_dir.mkdir(parents=True)
    write_segment_stream(seg_dir, STREAM, DAY if prev else None, prev, seq)
    (seg_dir / "ingest.json").write_text(
        json.dumps(
            {
                "schema_version": 1,
                "requested_segment": segment,
                "files": {
                    "audio.flac": {
                        "sha256": _sha(AUDIO),
                        "size": len(AUDIO),
                    }
                },
            }
        )
        + "\n",
        encoding="utf-8",
    )
    return seg_dir


def _upload_history(
    prefix: str,
    segment: str,
    *,
    segment_original: str | None = None,
    audio: bytes = AUDIO,
    record_type: str | None = None,
) -> None:
    record = {
        "ts": 1,
        "segment": segment,
        "stream": STREAM,
        "files": [
            {
                "submitted": "audio.flac",
                "written": "audio.flac",
                "size": len(audio),
                "sha256": _sha(audio),
                "disposition": "written",
            }
        ],
    }
    if segment_original:
        record["segment_original"] = segment_original
    if record_type:
        record["type"] = record_type
    append_history_record(
        prefix,
        DAY,
        record,
    )


def _pruned_history(prefix: str, segment: str) -> list[dict]:
    return [
        record
        for record in load_history(prefix, DAY)
        if record.get("type") == "pruned" and record.get("segment") == segment
    ]


def _append_pruned_history(prefix: str, segment: str, duplicate_of: str) -> None:
    append_history_record(
        prefix,
        DAY,
        {
            "type": "pruned",
            "ts": 1,
            "segment": segment,
            "stream": STREAM,
            "duplicate_of": duplicate_of,
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


def _seg(journal: Path, segment: str) -> Path:
    return journal / "chronicle" / DAY / STREAM / segment


def test_cross_start_execute_deletes_relocated_candidate_and_repairs_chain(
    observer_env,
) -> None:
    """AC2: cross-start execute deletes a proven relocated duplicate."""
    env = observer_env()
    assert save_observer(_observer())
    prefix = KEY[:8]
    origin = "120000_300"
    candidate = "121000_300"
    successor = "122000_300"
    _write_segment(env.journal, origin, 1, None)
    _write_segment(env.journal, candidate, 2, origin)
    _write_segment(env.journal, successor, 3, candidate)
    _upload_history(prefix, candidate, segment_original=origin)

    result = run_prune(days=[DAY], stream=STREAM, execute=True, cross_start=True)

    assert result.refusals == []
    assert [item.analysis.segment for item in result.deleted] == [candidate]
    assert read_segment_stream(_seg(env.journal, successor))["prev_segment"] == origin
    pruned = _pruned_history(prefix, candidate)
    assert len(pruned) == 1
    assert pruned[0]["duplicate_of"] == origin
    assert not _seg(env.journal, candidate).exists()


def test_cross_start_leaves_no_provenance_pair_untouched(observer_env) -> None:
    """AC3: different-start duplicates without segment_original are untouched."""
    env = observer_env()
    assert save_observer(_observer())
    prefix = KEY[:8]
    origin = "123000_300"
    candidate = "124000_300"
    _write_segment(env.journal, origin, 1, None)
    _write_segment(env.journal, candidate, 2, origin)
    _upload_history(prefix, origin)
    _upload_history(prefix, candidate)

    result = run_prune(days=[DAY], stream=STREAM, execute=True, cross_start=True)

    assert result.deleted == []
    assert result.refusals == []
    assert _seg(env.journal, origin).is_dir()
    assert _seg(env.journal, candidate).is_dir()


def test_cross_start_ignores_non_upload_provenance_record(observer_env) -> None:
    """Cross-start only keys on absent/upload records; transferred provenance is ignored."""
    env = observer_env()
    assert save_observer(_observer())
    prefix = KEY[:8]
    origin = "146000_300"
    candidate = "147000_300"
    _write_segment(env.journal, origin, 1, None)
    _write_segment(env.journal, candidate, 2, origin)
    _upload_history(
        prefix,
        candidate,
        segment_original=origin,
        record_type="transferred",
    )

    result = run_prune(days=[DAY], stream=STREAM, execute=True, cross_start=True)

    assert result.deleted == []
    assert result.refusals == []
    assert _seg(env.journal, origin).is_dir()
    assert _seg(env.journal, candidate).is_dir()


def test_cross_start_marker_less_candidate_refuses_chain_identity(observer_env) -> None:
    """A marker-less cross-start candidate reuses the same-start chain-identity gate."""
    env = observer_env()
    assert save_observer(_observer())
    prefix = KEY[:8]
    origin = "148000_300"
    candidate = "149000_300"
    _write_segment(env.journal, origin, 1, None)
    _write_segment(env.journal, candidate, 2, origin, marker=False)
    _upload_history(prefix, candidate, segment_original=origin)

    result = run_prune(days=[DAY], stream=STREAM, execute=True, cross_start=True)

    assert [refusal.gate for refusal in result.refusals] == ["chain-identity"]
    assert result.deleted == []
    assert _seg(env.journal, candidate).is_dir()


def test_cross_start_refuses_conflicting_segment_original(observer_env) -> None:
    """AC4: conflicting server-authored origins refuse the candidate."""
    env = observer_env()
    assert save_observer(_observer())
    prefix = KEY[:8]
    first_origin = "125000_300"
    second_origin = "125500_300"
    candidate = "126000_300"
    _write_segment(env.journal, first_origin, 1, None)
    _write_segment(env.journal, second_origin, 2, first_origin)
    _write_segment(env.journal, candidate, 3, second_origin)
    _upload_history(prefix, candidate, segment_original=first_origin)
    _upload_history(prefix, candidate, segment_original=second_origin)

    result = run_prune(days=[DAY], stream=STREAM, execute=True, cross_start=True)

    assert [refusal.gate for refusal in result.refusals] == ["cross-start-provenance"]
    assert result.deleted == []
    assert _seg(env.journal, candidate).is_dir()


def test_cross_start_resolves_origin_through_pruned_hops(observer_env) -> None:
    """AC5a: origins can resolve through pruned duplicate_of hops."""
    env = observer_env()
    assert save_observer(_observer())
    prefix = KEY[:8]
    canonical = "127000_300"
    first_hop = "127500_300"
    second_hop = "127700_300"
    candidate = "128000_300"
    _write_segment(env.journal, canonical, 1, None)
    _write_segment(env.journal, candidate, 2, canonical)
    _append_pruned_history(prefix, first_hop, canonical)
    _append_pruned_history(prefix, second_hop, first_hop)
    _upload_history(prefix, candidate, segment_original=second_hop)

    result = run_prune(days=[DAY], stream=STREAM, execute=True, cross_start=True)

    assert result.refusals == []
    assert [item.analysis.segment for item in result.deleted] == [candidate]
    assert _pruned_history(prefix, candidate)[0]["duplicate_of"] == canonical


def test_cross_start_refuses_origin_chain_cycle(observer_env) -> None:
    """AC5b: origin chains that cycle refuse with cross-start-origin."""
    env = observer_env()
    assert save_observer(_observer())
    prefix = KEY[:8]
    first_hop = "129000_300"
    second_hop = "129500_300"
    candidate = "130000_300"
    _write_segment(env.journal, candidate, 1, None)
    _append_pruned_history(prefix, first_hop, second_hop)
    _append_pruned_history(prefix, second_hop, first_hop)
    _upload_history(prefix, candidate, segment_original=first_hop)

    result = run_prune(days=[DAY], stream=STREAM, execute=True, cross_start=True)

    assert [refusal.gate for refusal in result.refusals] == ["cross-start-origin"]
    assert result.deleted == []
    assert _seg(env.journal, candidate).is_dir()


def test_cross_start_refuses_origin_dead_end(observer_env) -> None:
    """AC5c: off-disk origins with no pruned record refuse."""
    env = observer_env()
    assert save_observer(_observer())
    prefix = KEY[:8]
    candidate = "131000_300"
    _write_segment(env.journal, candidate, 1, None)
    _upload_history(prefix, candidate, segment_original="130500_300")

    result = run_prune(days=[DAY], stream=STREAM, execute=True, cross_start=True)

    assert [refusal.gate for refusal in result.refusals] == ["cross-start-origin"]
    assert result.deleted == []
    assert _seg(env.journal, candidate).is_dir()


def test_cross_start_refuses_content_mismatch(observer_env) -> None:
    """AC6: cross-start candidates must match canonical content identity."""
    env = observer_env()
    assert save_observer(_observer())
    prefix = KEY[:8]
    origin = "132000_300"
    candidate = "133000_300"
    near_audio = b"near duplicate observer prune bytes"
    _write_segment(env.journal, origin, 1, None)
    _write_segment(env.journal, candidate, 2, origin, audio=near_audio)
    _upload_history(prefix, candidate, segment_original=origin, audio=near_audio)

    result = run_prune(days=[DAY], stream=STREAM, execute=True, cross_start=True)

    assert [refusal.gate for refusal in result.refusals] == ["content-identity"]
    assert result.deleted == []
    assert _seg(env.journal, candidate).is_dir()


def test_cross_start_refuses_proof_absent_origin(observer_env) -> None:
    """AC7: an unverifiable resolved origin refuses as canonical-heldness."""
    env = observer_env()
    assert save_observer(_observer())
    prefix = KEY[:8]
    origin = "134000_300"
    candidate = "135000_300"
    _write_unverifiable_manifest_segment(env.journal, origin, 1, None)
    _write_segment(env.journal, candidate, 2, origin)
    _upload_history(prefix, candidate, segment_original=origin)

    result = run_prune(days=[DAY], stream=STREAM, execute=True, cross_start=True)

    assert [refusal.gate for refusal in result.refusals] == ["canonical-heldness"]
    assert result.deleted == []
    assert _seg(env.journal, candidate).is_dir()


def test_cross_start_cluster_is_all_or_nothing(observer_env) -> None:
    """AC8: one candidate identity error refuses the whole origin cluster."""
    env = observer_env()
    assert save_observer(_observer())
    prefix = KEY[:8]
    origin = "136000_300"
    good = "137000_300"
    bad = "138000_300"
    _write_segment(env.journal, origin, 1, None)
    _write_segment(env.journal, good, 2, origin)
    bad_dir = _write_segment(env.journal, bad, 3, good)
    (bad_dir / "audio.flac").unlink()
    _upload_history(prefix, good, segment_original=origin)
    _upload_history(prefix, bad, segment_original=origin)

    result = run_prune(days=[DAY], stream=STREAM, execute=True, cross_start=True)

    assert [refusal.gate for refusal in result.refusals] == ["canonical-heldness"]
    assert result.deleted == []
    assert _seg(env.journal, good).is_dir()
    assert _seg(env.journal, bad).is_dir()


def test_cross_start_runs_after_same_start_with_no_double_claim(observer_env) -> None:
    """AC9: cross-start sees same-start synthetic pruned claims."""
    env = observer_env()
    assert save_observer(_observer())
    prefix = KEY[:8]
    canonical = "140000_300"
    same_start_candidate = "140000_301"
    cross_candidate = "141000_300"
    _write_segment(env.journal, canonical, 1, None)
    _write_segment(env.journal, same_start_candidate, 2, canonical)
    _write_segment(env.journal, cross_candidate, 3, same_start_candidate)
    _upload_history(prefix, same_start_candidate)
    _upload_history(prefix, cross_candidate, segment_original=same_start_candidate)

    dry = run_prune(days=[DAY], stream=STREAM, execute=False, cross_start=True)
    assert sorted(
        [candidate.analysis.segment for candidate in group.candidates]
        for group in dry.groups
    ) == [[same_start_candidate], [cross_candidate]]

    result = run_prune(days=[DAY], stream=STREAM, execute=True, cross_start=True)

    assert result.refusals == []
    assert sorted(item.analysis.segment for item in result.deleted) == [
        same_start_candidate,
        cross_candidate,
    ]
    assert _seg(env.journal, canonical).is_dir()
    assert not _seg(env.journal, same_start_candidate).exists()
    assert not _seg(env.journal, cross_candidate).exists()
    groups = [
        [candidate.analysis.segment for candidate in group.candidates]
        for group in result.groups
    ]
    assert [same_start_candidate] in groups
    assert [cross_candidate] in groups


def test_cross_start_dry_run_matches_existing_report_shape(observer_env) -> None:
    """AC10: cross-start dry-run uses the normal prune report shape."""
    env = observer_env()
    assert save_observer(_observer())
    prefix = KEY[:8]
    origin = "142000_300"
    candidate = "143000_300"
    _write_segment(env.journal, origin, 1, None)
    _write_segment(env.journal, candidate, 2, origin)
    _upload_history(prefix, candidate, segment_original=origin)

    result = run_prune(days=[DAY], stream=STREAM, execute=False, cross_start=True)
    output = format_result(result)

    assert "observer prune dry-run" in output
    assert "deleted: 0" in output
    assert f"group {DAY}/{STREAM}/142000_*: canonical={origin} candidates=1" in output
    headers = {
        line.split(":", 1)[0]
        for line in output.splitlines()
        if ":" in line and not line.startswith(" ") and not line.startswith("group ")
    }
    assert headers == {
        "groups",
        "candidates",
        "deleted",
        "chain-repaired",
        "last-physical-copy",
        "refusals",
    }
    assert "cross-start" not in output


def test_cross_start_execute_twice_is_noop(observer_env) -> None:
    """Idempotence: a second cross-start execute has no work or refusals."""
    env = observer_env()
    assert save_observer(_observer())
    prefix = KEY[:8]
    origin = "144000_300"
    candidate = "145000_300"
    _write_segment(env.journal, origin, 1, None)
    _write_segment(env.journal, candidate, 2, origin)
    _upload_history(prefix, candidate, segment_original=origin)

    first = run_prune(days=[DAY], stream=STREAM, execute=True, cross_start=True)
    second = run_prune(days=[DAY], stream=STREAM, execute=True, cross_start=True)

    assert [item.analysis.segment for item in first.deleted] == [candidate]
    assert second.deleted == []
    assert second.refusals == []
