# SPDX-License-Identifier: AGPL-3.0-only
# Copyright (c) 2026 sol pbc

"""Tests for entity hook behavior across schedules."""

from __future__ import annotations

import json
import logging


def _read_jsonl(path):
    return [
        json.loads(line)
        for line in path.read_text(encoding="utf-8").splitlines()
        if line.strip()
    ]


def test_entities_post_process_writes_deduped_jsonl(tmp_path, caplog):
    from solstone.talent.entities import post_process

    caplog.set_level(logging.INFO)
    output_path = (
        tmp_path / "chronicle/20240115/default/120000_300/talents/entities.json"
    )
    result = json.dumps(
        {
            "entities": [
                {
                    "type": "Person",
                    "name": "Alice Smith",
                    "description": "desc1",
                },
                {
                    "type": "Person",
                    "name": "alice smith",
                    "description": "DUP",
                },
                {
                    "type": "Tool",
                    "name": "Grafana",
                    "description": "desc2",
                },
            ]
        }
    )

    post_process(result, {"output_path": str(output_path)})

    entities_path = output_path.parent / "entities.jsonl"
    assert entities_path.exists()
    assert _read_jsonl(entities_path) == [
        {
            "type": "Person",
            "name": "Alice Smith",
            "description": "desc1",
        },
        {"type": "Tool", "name": "Grafana", "description": "desc2"},
    ]


def test_entities_post_process_requires_output_path(caplog):
    from solstone.talent.entities import post_process

    caplog.set_level(logging.INFO)
    post_process(
        json.dumps(
            {
                "entities": [
                    {
                        "type": "Person",
                        "name": "Alice Smith",
                        "description": "Mentioned in the meeting",
                    }
                ]
            }
        ),
        {},
    )

    assert "missing output_path" in caplog.text


def test_entities_post_process_drops_malformed_items_with_warning(tmp_path, caplog):
    from solstone.talent.entities import post_process

    caplog.set_level(logging.INFO)
    output_path = (
        tmp_path / "chronicle/20240115/default/120000_300/talents/entities.json"
    )
    result = json.dumps(
        {
            "entities": [
                {
                    "type": "Project",
                    "name": "Zephyr Quartz",
                    "description": "Valid project",
                },
                {
                    "type": "Person",
                    "name": "",
                    "description": "Missing name",
                },
                {
                    "type": "person",
                    "name": "Alice Smith",
                    "description": "Invalid type casing",
                },
            ]
        }
    )

    post_process(result, {"output_path": str(output_path)})

    assert any(
        record.levelno == logging.WARNING
        and "dropped 2 malformed entity items" in record.message
        for record in caplog.records
    )
    assert _read_jsonl(output_path.parent / "entities.jsonl") == [
        {
            "type": "Project",
            "name": "Zephyr Quartz",
            "description": "Valid project",
        }
    ]


def test_entities_post_process_malformed_json_warns_without_write(tmp_path, caplog):
    from solstone.talent.entities import post_process

    caplog.set_level(logging.INFO)
    output_path = (
        tmp_path / "chronicle/20240115/default/120000_300/talents/entities.json"
    )

    post_process("not json at all", {"output_path": str(output_path)})

    assert any(
        record.levelno == logging.WARNING and "malformed payload" in record.message
        for record in caplog.records
    )
    assert not (output_path.parent / "entities.jsonl").exists()


def test_entities_post_process_empty_is_info_not_warning(tmp_path, caplog):
    from solstone.talent.entities import post_process

    caplog.set_level(logging.INFO)
    output_path = (
        tmp_path / "chronicle/20240115/default/120000_300/talents/entities.json"
    )

    post_process('{"entities": []}', {"output_path": str(output_path)})

    assert any(
        record.levelno == logging.INFO and "no entities extracted" in record.message
        for record in caplog.records
    )
    assert not any(record.levelno >= logging.WARNING for record in caplog.records)
    assert not (output_path.parent / "entities.jsonl").exists()


def test_entities_post_process_indexes_sidecar_under_journal(tmp_path, monkeypatch):
    from solstone.talent.entities import post_process
    from solstone.think.indexer.journal import search_journal

    journal = tmp_path / "journal"
    monkeypatch.setenv("SOLSTONE_JOURNAL", str(journal))
    output_path = (
        journal / "chronicle/20240115/default/120000_300/talents/entities.json"
    )
    result = json.dumps(
        {
            "entities": [
                {
                    "type": "Project",
                    "name": "Zephyr Quartz Index",
                    "description": "unique freshness seed",
                }
            ]
        }
    )

    post_process(result, {"output_path": str(output_path)})

    total, results = search_journal("Zephyr Quartz Index")
    assert total >= 1
    assert any(
        result["metadata"]["path"]
        == "20240115/default/120000_300/talents/entities.jsonl"
        for result in results
    )


def test_entities_talent_loads_schema():
    from solstone.think.talent import get_talent

    config = get_talent("entities")

    assert config.get("output") == "json"
    assert "json_schema" in config


def test_entities_talent_is_segment_scheduled():
    from solstone.think.talent import get_talent_configs

    segment_prompts = get_talent_configs(schedule="segment")

    assert "entities" in segment_prompts
