# SPDX-License-Identifier: AGPL-3.0-only
# Copyright (c) 2026 sol pbc

from __future__ import annotations

import hashlib
import json
from pathlib import Path

import numpy as np

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
    for path, descriptor in build_core_fixtures.expected_outputs().items():
        expected = descriptor.render()
        current = path.read_text(encoding="utf-8")
        assert not build_core_fixtures.compare_artifact(
            path, descriptor, current, expected
        )


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
    speaker_filterbank_path = fixture_dir / "speaker_filterbank.json"
    speaker_stage_boundaries_path = fixture_dir / "speaker_stage_boundaries.json"

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
    monkeypatch.setattr(
        build_core_fixtures,
        "SPEAKER_FILTERBANK_ARTIFACT_PATH",
        speaker_filterbank_path,
    )
    monkeypatch.setattr(
        build_core_fixtures,
        "SPEAKER_STAGE_BOUNDARIES_ARTIFACT_PATH",
        speaker_stage_boundaries_path,
    )

    build_core_fixtures.write_outputs()
    assert build_core_fixtures.check_outputs() == 0

    callosum_path.write_text(json.dumps({"stale": True}) + "\n", encoding="utf-8")

    assert build_core_fixtures.check_outputs() == 1
    captured = capsys.readouterr()
    assert "core/fixtures/callosum_registry.json" in captured.err
    assert "make core-fixtures" in captured.err


def _decode_decimal_rows(rows: list[str]) -> np.ndarray:
    return np.asarray([[float(value) for value in row.split()] for row in rows])


def test_speaker_filterbank_fixture_spans_near_silent_and_broadband_regimes() -> None:
    fixture = build_core_fixtures.build_speaker_filterbank_fixture()
    matrix = _decode_decimal_rows(fixture["matrices"]["filterbank_cmn"]["rows"])
    near_start, near_end = fixture["waveform"]["near_silent_rows"]
    broad_start, broad_end = fixture["waveform"]["broadband_rows"]

    near_column_mean = float(matrix[near_start:near_end].mean(axis=0).mean())
    broad_column_mean = float(matrix[broad_start:broad_end].mean(axis=0).mean())
    broad_column_std = float(matrix[broad_start:broad_end].std(axis=0).mean())

    # Measured on Linux x86_64 with kaldi-native-fbank 1.22.3:
    # near=-10.9518, broadband=5.0482, broadband column std=0.7722.
    assert near_column_mean < -4.0
    assert broad_column_mean > 2.0
    assert broad_column_std > 0.5


def test_speaker_filterbank_call_sites_are_bit_identical() -> None:
    fixture = build_core_fixtures.build_speaker_filterbank_fixture()

    assert fixture["call_site_agreement"] == {
        "array_equal": True,
        "shape": [198, 80],
    }


def test_speaker_stage_boundary_cases_pin_near_decisions() -> None:
    fixture = build_core_fixtures.build_speaker_stage_boundaries_fixture()
    interval = fixture["interval_boundary"]
    perturb = fixture["clustering_input_perturbation"]
    evidence = fixture["speaker_evidence"]["else_branch_overlap_ambiguity"]

    assert len(interval["kept_at_30_frames"]["intervals"]) == 1
    assert interval["kept_at_30_frames"]["run_duration_s"] > interval["min_interval_s"]
    assert interval["dropped_at_29_frames"]["intervals"] == []
    assert (
        interval["dropped_at_29_frames"]["run_duration_s"] < interval["min_interval_s"]
    )
    assert interval["assigned_sentences"] == [1, None]

    assert perturb["base"]["selected_k"] == 3
    assert perturb["perturbed"]["selected_k"] == 4
    assert perturb["epsilon"] == 0.03
    assert perturb["row"] == 6
    assert perturb["col"] == 40

    assert evidence["decision"]["speaker_evidence"] == "multi"
    assert evidence["overlap_fraction"] < build_core_fixtures.DIARIZE_MIN_OVERLAP
    assert (
        evidence["decision"]["mean_window_overlap_share"]
        >= build_core_fixtures.DIARIZE_MIN_OVERLAP
    )


def test_speaker_stage_k_divergence_pins_cumulative_rule() -> None:
    fixture = build_core_fixtures.build_speaker_stage_boundaries_fixture()
    case = fixture["k_selection_divergence"]["case"]

    assert fixture["k_selection_divergence"]["seed"] == 0
    assert fixture["k_selection_divergence"]["n"] == 32
    assert case["selected_k"] == 2
    assert case["plain_argmax_k"] == 8


def test_float_fixture_tolerance_reports_red_and_green(
    tmp_path: Path,
    monkeypatch,
    capsys,
) -> None:
    root = tmp_path
    fixture_dir = root / "core" / "fixtures"
    speaker_filterbank_path = fixture_dir / "speaker_filterbank.json"

    monkeypatch.setattr(build_core_fixtures, "ROOT", root)
    monkeypatch.setattr(build_core_fixtures, "FIXTURE_DIR", fixture_dir)
    monkeypatch.setattr(
        build_core_fixtures,
        "SPEAKER_FILTERBANK_ARTIFACT_PATH",
        speaker_filterbank_path,
    )
    monkeypatch.setattr(
        build_core_fixtures,
        "CALLOSUM_ARTIFACT_PATH",
        fixture_dir / "callosum_registry.json",
    )
    monkeypatch.setattr(
        build_core_fixtures,
        "COGITATE_ARTIFACT_PATH",
        fixture_dir / "cogitate_contract.json",
    )
    monkeypatch.setattr(
        build_core_fixtures,
        "EDGE_SCHEMA_ARTIFACT_PATH",
        fixture_dir / "edge_schema.json",
    )
    monkeypatch.setattr(
        build_core_fixtures,
        "MARKDOWN_CHUNKS_ARTIFACT_PATH",
        fixture_dir / "markdown_chunks.json",
    )
    monkeypatch.setattr(
        build_core_fixtures,
        "SPEAKER_STAGE_BOUNDARIES_ARTIFACT_PATH",
        fixture_dir / "speaker_stage_boundaries.json",
    )

    build_core_fixtures.write_outputs()
    payload = json.loads(speaker_filterbank_path.read_text(encoding="utf-8"))
    row = payload["matrices"]["filterbank_cmn"]["rows"][0].split()
    original = float(row[0])

    row[0] = f"{original + build_core_fixtures.FILTERBANK_VALUE_ABS_TOLERANCE / 2:.3f}"
    payload["matrices"]["filterbank_cmn"]["rows"][0] = " ".join(row)
    speaker_filterbank_path.write_text(
        build_core_fixtures.render_json(payload), encoding="utf-8"
    )
    assert build_core_fixtures.check_outputs() == 0

    row[0] = f"{original + build_core_fixtures.FILTERBANK_VALUE_ABS_TOLERANCE * 2:.3f}"
    payload["matrices"]["filterbank_cmn"]["rows"][0] = " ".join(row)
    speaker_filterbank_path.write_text(
        build_core_fixtures.render_json(payload), encoding="utf-8"
    )
    assert build_core_fixtures.check_outputs() == 1
    captured = capsys.readouterr()
    assert "/matrices/filterbank_cmn/rows[0][0]" in captured.err
    assert "abs_diff=" in captured.err
    assert "tolerance=" in captured.err


def test_speaker_platform_is_diagnostic_but_kaldi_version_is_identity(
    tmp_path: Path,
    monkeypatch,
) -> None:
    root = tmp_path
    fixture_dir = root / "core" / "fixtures"
    speaker_filterbank_path = fixture_dir / "speaker_filterbank.json"

    monkeypatch.setattr(build_core_fixtures, "ROOT", root)
    monkeypatch.setattr(build_core_fixtures, "FIXTURE_DIR", fixture_dir)
    monkeypatch.setattr(
        build_core_fixtures,
        "SPEAKER_FILTERBANK_ARTIFACT_PATH",
        speaker_filterbank_path,
    )
    monkeypatch.setattr(
        build_core_fixtures,
        "CALLOSUM_ARTIFACT_PATH",
        fixture_dir / "callosum_registry.json",
    )
    monkeypatch.setattr(
        build_core_fixtures,
        "COGITATE_ARTIFACT_PATH",
        fixture_dir / "cogitate_contract.json",
    )
    monkeypatch.setattr(
        build_core_fixtures,
        "EDGE_SCHEMA_ARTIFACT_PATH",
        fixture_dir / "edge_schema.json",
    )
    monkeypatch.setattr(
        build_core_fixtures,
        "MARKDOWN_CHUNKS_ARTIFACT_PATH",
        fixture_dir / "markdown_chunks.json",
    )
    monkeypatch.setattr(
        build_core_fixtures,
        "SPEAKER_STAGE_BOUNDARIES_ARTIFACT_PATH",
        fixture_dir / "speaker_stage_boundaries.json",
    )

    build_core_fixtures.write_outputs()
    payload = json.loads(speaker_filterbank_path.read_text(encoding="utf-8"))
    payload["identity"]["generation_platform"] = {
        "machine": "arm64",
        "system": "Darwin",
    }
    speaker_filterbank_path.write_text(
        build_core_fixtures.render_json(payload), encoding="utf-8"
    )
    assert build_core_fixtures.check_outputs() == 0

    payload["identity"]["kaldi_native_fbank_version"] = "0.0.0"
    speaker_filterbank_path.write_text(
        build_core_fixtures.render_json(payload), encoding="utf-8"
    )
    assert build_core_fixtures.check_outputs() == 1


def test_speaker_float_fixture_payload_size_budget() -> None:
    total = (
        build_core_fixtures.SPEAKER_FILTERBANK_ARTIFACT_PATH.stat().st_size
        + build_core_fixtures.SPEAKER_STAGE_BOUNDARIES_ARTIFACT_PATH.stat().st_size
    )

    assert total <= 400_000
