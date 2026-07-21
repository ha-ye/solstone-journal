# SPDX-License-Identifier: AGPL-3.0-only
# Copyright (c) 2026 sol pbc

from __future__ import annotations

import hashlib
import json
from pathlib import Path

from scripts import build_core_fixtures
from solstone.convey.contract.assemble import CALLOSUM_REGISTRY
from solstone.think import markdown as markdown_formatter
from solstone.think.cogitate_contract import (
    COGITATE_ACCESS_TIERS,
    COGITATE_READ_TOOL_NAMES,
    COGITATE_RUNTIME_PREAMBLE,
    FUTURE_ACCESS_TIERS,
    TALENT_FINALIZATION_MODES,
    capabilities_for_access_tier,
)


def test_callosum_core_fixture_matches_registry() -> None:
    fixture = build_core_fixtures.build_callosum_registry_fixture()

    assert list(fixture["registry"]) == sorted(CALLOSUM_REGISTRY)
    for tract, events in fixture["registry"].items():
        assert events == CALLOSUM_REGISTRY[tract]


def test_cogitate_core_fixture_matches_public_contract() -> None:
    fixture = build_core_fixtures.build_cogitate_contract_fixture()
    preamble_bytes = COGITATE_RUNTIME_PREAMBLE.encode("utf-8")

    assert fixture["access_tiers"] == list(COGITATE_ACCESS_TIERS)
    assert fixture["future_access_tiers"] == list(FUTURE_ACCESS_TIERS)
    assert fixture["read_tools"] == list(COGITATE_READ_TOOL_NAMES)
    assert fixture["finalization_modes"] == list(TALENT_FINALIZATION_MODES)
    assert set(fixture["capabilities"]) == set(COGITATE_ACCESS_TIERS)
    assert not set(fixture["capabilities"]) & set(FUTURE_ACCESS_TIERS)

    for tier in COGITATE_ACCESS_TIERS:
        caps = capabilities_for_access_tier(tier)
        assert fixture["capabilities"][tier] == {
            "sol": caps.sol,
            "reads": caps.reads,
            "submit": caps.submit,
        }

    assert fixture["runtime_preamble"] == {
        "algorithm": "sha256",
        "encoding": "utf-8",
        "digest": hashlib.sha256(preamble_bytes).hexdigest(),
        "byte_length": len(preamble_bytes),
    }


def test_markdown_core_fixture_matches_formatter_contract() -> None:
    fixture = build_core_fixtures.build_markdown_chunks_fixture()

    assert fixture["constraints"]["token_comparison_cases_ascii_only"] is True
    assert {
        case["id"] for case in fixture["cases"] if case.get("token_comparison") is False
    } == {
        "non_ascii_line_under_char_bound_over_byte_bound",
        "non_ascii_line_over_char_bound",
        "non_ascii_chunk_under_char_bound_over_byte_bound",
    }
    for case in fixture["cases"]:
        if case.get("token_comparison") is False:
            assert set(case) == {
                "id",
                "input",
                "chunk_count",
                "warnings",
                "token_comparison",
                "token_comparison_reason",
            }
        else:
            assert case["input"].isascii()
    assert (
        fixture["constraints"]["max_line_chars"] == markdown_formatter._MAX_LINE_CHARS
    )
    assert (
        fixture["constraints"]["max_chunk_chars"] == markdown_formatter._MAX_CHUNK_CHARS
    )
    assert {case["id"] for case in fixture["cases"]} == {
        case["id"] for case in build_core_fixtures._markdown_fixture_cases()
    }


def test_committed_core_fixtures_are_current() -> None:
    for path, expected in build_core_fixtures.expected_outputs().items():
        assert path.read_text(encoding="utf-8") == expected


def test_core_fixtures_check_reports_stale_paths(
    tmp_path: Path,
    monkeypatch,
    capsys,
) -> None:
    root = tmp_path
    fixture_dir = root / "core" / "fixtures"
    callosum_path = fixture_dir / "callosum_registry.json"
    cogitate_path = fixture_dir / "cogitate_contract.json"
    edge_schema_path = fixture_dir / "edge_schema.json"
    markdown_chunks_path = fixture_dir / "markdown_chunks.json"

    monkeypatch.setattr(build_core_fixtures, "ROOT", root)
    monkeypatch.setattr(build_core_fixtures, "FIXTURE_DIR", fixture_dir)
    monkeypatch.setattr(build_core_fixtures, "CALLOSUM_ARTIFACT_PATH", callosum_path)
    monkeypatch.setattr(build_core_fixtures, "COGITATE_ARTIFACT_PATH", cogitate_path)
    monkeypatch.setattr(
        build_core_fixtures, "EDGE_SCHEMA_ARTIFACT_PATH", edge_schema_path
    )
    monkeypatch.setattr(
        build_core_fixtures, "MARKDOWN_CHUNKS_ARTIFACT_PATH", markdown_chunks_path
    )

    build_core_fixtures.write_outputs()
    assert build_core_fixtures.check_outputs() == 0

    callosum_path.write_text(json.dumps({"stale": True}) + "\n", encoding="utf-8")

    assert build_core_fixtures.check_outputs() == 1
    captured = capsys.readouterr()
    assert "core/fixtures/callosum_registry.json" in captured.err
    assert "make core-fixtures" in captured.err
