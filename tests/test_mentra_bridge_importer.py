# SPDX-License-Identifier: AGPL-3.0-only
# Copyright (c) 2026 sol pbc

import importlib
import json

from solstone.think.importers.file_importer import FILE_IMPORTER_REGISTRY


def _write_sample_bridge(root):
    bridge = root / "solstone-bridge-demo"
    segment = bridge / "chronicle" / "20260614" / "mentra-live" / "144500_60"
    segment.mkdir(parents=True)
    (bridge / "manifest.jsonl").write_text(
        json.dumps(
            {
                "type": "transcript",
                "day": "20260614",
                "stream": "mentra-live",
                "segment": "144500_60",
                "files": ["audio.jsonl"],
            }
        )
        + "\n",
        encoding="utf-8",
    )
    (segment / "audio.jsonl").write_text(
        json.dumps({"source": "mentra-notes-solstone"})
        + "\n"
        + json.dumps(
            {
                "start": "00:00:01",
                "speaker": "speaker",
                "text": "Testing Mentra to Solstone.",
            }
        )
        + "\n",
        encoding="utf-8",
    )
    (segment / "signals.jsonl").write_text(
        json.dumps({"source": "mentra-notes-solstone", "modality": "signal"})
        + "\n"
        + json.dumps(
            {
                "timestamp": "2026-06-14T20:45:00Z",
                "event_type": "location_update",
                "payload": {"lat": 39.7392, "lng": -104.9903},
            }
        )
        + "\n",
        encoding="utf-8",
    )
    (segment / "mentra-photo-1.jpg").write_bytes(b"fake-jpeg")
    return bridge


def test_mentra_importer_registered():
    assert FILE_IMPORTER_REGISTRY["mentra"] == "solstone.think.importers.mentra_bridge"


def test_detect_and_preview_bridge(tmp_path):
    bridge = _write_sample_bridge(tmp_path)
    mod = importlib.import_module("solstone.think.importers.mentra_bridge")

    assert mod.importer.detect(bridge) is True
    assert mod.importer.detect(tmp_path) is True

    preview = mod.importer.preview(bridge)

    assert preview.date_range == ("20260614", "20260614")
    assert preview.item_count == 1
    assert preview.entity_count == 0
    assert "1 segment" in preview.summary
    assert "3 file" in preview.summary


def test_process_bridge_into_temp_journal(tmp_path):
    bridge = _write_sample_bridge(tmp_path)
    journal_root = tmp_path / "journal"
    mod = importlib.import_module("solstone.think.importers.mentra_bridge")

    result = mod.importer.process(
        bridge,
        journal_root,
        import_id="20260614_144500",
    )

    target = journal_root / "chronicle" / "20260614" / "import.mentra" / "144500_60"
    assert result.errors == []
    assert result.entries_written == 1
    assert result.segments == [("20260614", "144500_60")]
    assert (target / "audio.jsonl").exists()
    assert (target / "signals.jsonl").exists()
    assert (target / "mentra-photo-1.jpg").exists()
    assert (target / "mentra_bridge.json").exists()
    assert (
        journal_root
        / "imports"
        / "20260614_144500"
        / "mentra_bridge"
        / "chronicle"
        / "20260614"
        / "mentra-live"
        / "144500_60"
        / "audio.jsonl"
    ).exists()
