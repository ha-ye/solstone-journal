# SPDX-License-Identifier: AGPL-3.0-only
# Copyright (c) 2026 sol pbc

from __future__ import annotations

import fcntl
import hashlib
import json
import os
import subprocess
import sys
from dataclasses import replace
from pathlib import Path
from typing import Any

import pytest

import scripts.check_rust_release_manifest as checker
import scripts.release_tool_pins as pins

SCRIPT = (
    Path(__file__).resolve().parents[1] / "scripts" / "check_rust_release_manifest.py"
)
VALID_COMMIT = checker.fixture_source_commit()
PYTHON_WHITESPACE_CODE_POINTS: tuple[int, ...] = (
    0x09,
    0x0A,
    0x0B,
    0x0C,
    0x0D,
    0x1C,
    0x1D,
    0x1E,
    0x1F,
    0x20,
    0x85,
    0xA0,
    0x1680,
    0x2000,
    0x2001,
    0x2002,
    0x2003,
    0x2004,
    0x2005,
    0x2006,
    0x2007,
    0x2008,
    0x2009,
    0x200A,
    0x2028,
    0x2029,
    0x202F,
    0x205F,
    0x3000,
)
RUSTC_REJECTED_SEPARATOR_CODE_POINTS: tuple[int, ...] = tuple(
    code_point for code_point in PYTHON_WHITESPACE_CODE_POINTS if code_point != 0x20
)


def _errors(failures: list[checker.Failure]) -> set[str]:
    return {failure.error for failure in failures}


def _assert_error(failures: list[checker.Failure], error: str) -> None:
    assert error in _errors(failures)


def _assert_redacted(
    failures: list[checker.Failure], forbidden: tuple[str, ...]
) -> None:
    for failure in failures:
        fields = (failure.error, failure.expected, failure.actual, failure.repair)
        for needle in forbidden:
            assert all(needle not in field for field in fields)


def _assert_formatted_redacted(
    failures: list[checker.Failure],
    forbidden: tuple[str, ...],
    capsys: pytest.CaptureFixture[str],
) -> None:
    checker._format_failures(failures)
    stderr = capsys.readouterr().err
    for needle in forbidden:
        assert needle not in stderr


def _rustc_lines(host: str = "x86_64-unknown-linux-gnu") -> list[str]:
    return checker.fixture_rustc_verbose(host).split("\n")


def _rustc_line_mutant(canonical: str, line_index: int, replacement: str) -> str:
    lines = canonical.split("\n")
    lines[line_index] = replacement
    return "\n".join(lines)


def _candidate(
    tmp_path: Path,
    *,
    include_models: bool = False,
    source_commit: str = VALID_COMMIT,
) -> Path:
    release_dir = tmp_path / ("candidate-models" if include_models else "candidate")
    failures = checker.write_inert_candidate(
        release_dir,
        include_models=include_models,
        source_commit=source_commit,
    )
    assert failures == []
    return release_dir


def test_release_manifest_imports_tool_pins_from_authoritative_module() -> None:
    assert checker.RUSTC_VERSION_BANNER == pins.RUSTC_VERSION_BANNER
    assert checker.RUSTC_BINARY_PIN == pins.RUSTC_BINARY_PIN
    assert checker.RUSTC_COMMIT_HASH_PIN == pins.RUSTC_COMMIT_HASH_PIN
    assert checker.RUSTC_COMMIT_DATE_PIN == pins.RUSTC_COMMIT_DATE_PIN
    assert checker.RUSTC_RELEASE_PIN == pins.RUSTC_RELEASE_PIN
    assert checker.RUSTC_LLVM_PIN == pins.RUSTC_LLVM_PIN
    assert checker.CARGO_VERSION_PIN == pins.CARGO_VERSION_PIN
    assert checker.CARGO_RELEASE_PIN == pins.CARGO_RELEASE_PIN
    assert checker.CARGO_DENY_PIN == pins.CARGO_DENY_PIN

    source = Path(checker.__file__).read_text(encoding="utf-8")
    assert "RUSTC_VERSION_BANNER =" not in source
    assert "CARGO_DENY_PIN =" not in source


def test_macos_swift_pin_requires_exact_full_grounded_output() -> None:
    exact = "Apple Swift 6.3.3 (swiftlang-6.3.3.1.3 clang-2100.1.1.101)"

    assert pins.MACOS_SWIFT_PIN == exact
    assert checker.validate_public_evidence_text("swift", pins.MACOS_SWIFT_PIN) == []
    assert not any(
        line.startswith("MACOS_SWIFT_VERSION")
        for line in Path(pins.__file__).read_text(encoding="utf-8").splitlines()
    )

    skewed_observations = (
        "6.3.3",
        "swift 6.3.3",
        "Apple Swift 6.3.3",
        "Apple Swift 6.3.3 (swiftlang-6.3.3.1.3 clang-2100.1.1.102)",
        "Apple Swift 6.3.3 (swiftlang-6.3.3.1.4 clang-2100.1.1.101)",
        f" {exact}",
        f"{exact} ",
    )
    assert all(observed != pins.MACOS_SWIFT_PIN for observed in skewed_observations)


def _manifest_for_lane(release_dir: Path, lane: checker.LaneName) -> Path:
    for artifact_name, (
        artifact_lane,
        _target,
    ) in checker.rust_artifact_targets().items():
        if artifact_lane == lane:
            return release_dir / f"{artifact_name}.rust-release-manifest.json"
    raise AssertionError(f"no artifact for lane {lane}")


def _artifact_for_lane(release_dir: Path, lane: checker.LaneName) -> Path:
    manifest = _manifest_for_lane(release_dir, lane)
    return release_dir / manifest.name.removesuffix(".rust-release-manifest.json")


def _source_artifact(tmp_path: Path) -> Path:
    release_dir = tmp_path / "source-artifact"
    checker.write_inert_packages(release_dir, include_models=False)
    return _artifact_for_lane(release_dir, "source")


def _assert_malformed_redacted(
    failures: list[checker.Failure],
    forbidden: tuple[str, ...],
    capsys: pytest.CaptureFixture[str],
) -> None:
    assert failures
    _assert_error(failures, "rustc_verbose is malformed")
    for failure in failures:
        assert failure.actual == "redacted"
    _assert_redacted(failures, forbidden)
    _assert_formatted_redacted(failures, forbidden, capsys)


def _assert_rustc_mutant_rejected(
    artifact_path: Path,
    capsys: pytest.CaptureFixture[str],
    *,
    mutant: str,
    forbidden: tuple[str, ...],
) -> None:
    parsed, parse_failures = checker.parse_rustc_verbose(mutant)
    assert parsed is None
    _assert_malformed_redacted(parse_failures, forbidden, capsys)

    evidence = checker.fixture_evidence_by_lane()["source"]
    generated, generate_failures = checker.generate_manifest(
        artifact_path,
        lane="source",
        evidence=replace(evidence, rustc_verbose=mutant),
        cohort=checker._default_cohort(VALID_COMMIT),
    )
    assert generated is None
    _assert_malformed_redacted(generate_failures, forbidden, capsys)


def _assert_rustc_line_mutant_rejected(
    artifact_path: Path,
    capsys: pytest.CaptureFixture[str],
    *,
    canonical: str,
    mutant: str,
    line_index: int,
) -> None:
    canonical_lines = canonical.split("\n")
    mutant_lines = mutant.split("\n")
    assert len(mutant_lines) == len(canonical_lines)
    assert mutant_lines[line_index] != canonical_lines[line_index]
    for index, line in enumerate(canonical_lines):
        if index != line_index:
            assert mutant_lines[index] == line
    _assert_rustc_mutant_rejected(
        artifact_path,
        capsys,
        mutant=mutant,
        forbidden=(mutant, mutant_lines[line_index]),
    )


def _assert_rustc_lf_separator_mutant_rejected(
    artifact_path: Path,
    capsys: pytest.CaptureFixture[str],
    *,
    canonical: str,
    mutant: str,
    line_index: int,
) -> None:
    canonical_lines = canonical.split("\n")
    label, value = canonical_lines[line_index].split(": ", 1)
    mutant_lines = mutant.split("\n")
    assert len(mutant_lines) == len(canonical_lines) + 1
    assert mutant_lines[:line_index] == canonical_lines[:line_index]
    assert mutant_lines[line_index : line_index + 2] == [f"{label}:", value]
    assert mutant_lines[line_index + 2 :] == canonical_lines[line_index + 1 :]
    _assert_rustc_mutant_rejected(
        artifact_path,
        capsys,
        mutant=mutant,
        forbidden=(mutant, f"{label}:\n{value}"),
    )


def _load_manifest(path: Path) -> dict:
    return json.loads(path.read_text(encoding="utf-8"))


def _write_manifest(path: Path, payload: dict) -> None:
    path.write_bytes(checker.canonical_json_bytes(payload))


def _replace_manifest_artifact(manifest_path: Path, artifact_name: str) -> None:
    artifact = manifest_path.parent / artifact_name
    digest = hashlib.sha256(artifact.read_bytes()).hexdigest()
    payload = _load_manifest(manifest_path)
    payload["artifacts"] = [
        {
            "path": artifact_name,
            "sha256": digest,
            "bytes": artifact.stat().st_size,
        }
    ]
    _write_manifest(manifest_path, payload)


def _source_dist(tmp_path: Path, *, include_models: bool = False) -> Path:
    dist = tmp_path / "dist"
    checker.write_inert_packages(dist, include_models=include_models)
    return dist


def _build_ready(
    tmp_path: Path,
    *,
    include_models: bool = False,
    hook=None,
) -> tuple[Path, list[checker.Failure]]:
    ready = tmp_path / "ready"
    failures = checker.build_and_promote_candidate(
        _source_dist(tmp_path, include_models=include_models),
        ready,
        source_commit=VALID_COMMIT,
        evidence_by_lane=checker.fixture_evidence_by_lane(),
        include_models=include_models,
        _post_promote_hook=hook,
    )
    return ready, failures


def test_python_whitespace_code_point_table_is_explicit() -> None:
    assert len(PYTHON_WHITESPACE_CODE_POINTS) == 29
    assert set(PYTHON_WHITESPACE_CODE_POINTS) == {
        0x09,
        0x0A,
        0x0B,
        0x0C,
        0x0D,
        0x1C,
        0x1D,
        0x1E,
        0x1F,
        0x20,
        0x85,
        0xA0,
        0x1680,
        0x2000,
        0x2001,
        0x2002,
        0x2003,
        0x2004,
        0x2005,
        0x2006,
        0x2007,
        0x2008,
        0x2009,
        0x200A,
        0x2028,
        0x2029,
        0x202F,
        0x205F,
        0x3000,
    }


def test_script_runs_without_site_packages_from_outside_repo(tmp_path: Path) -> None:
    env = os.environ.copy()
    env.pop("PYTHONPATH", None)
    env.pop("VIRTUAL_ENV", None)

    result = subprocess.run(
        [sys.executable, "-S", "-E", str(SCRIPT), "--help"],
        cwd=tmp_path,
        env=env,
        capture_output=True,
        text=True,
        check=False,
        timeout=15,
    )

    assert result.returncode == 0
    assert "Rust release manifests" in result.stdout


def test_vendored_schema_digest_and_trailing_newline() -> None:
    data = checker.SCHEMA_PATH.read_bytes()

    assert len(data) == 4416
    assert data.endswith(b"\n")
    assert hashlib.sha256(data).hexdigest() == checker.SCHEMA_SHA256


def test_load_schema_checks_draft_2020_12_and_format_checker() -> None:
    schema = checker.load_schema()

    assert schema["$id"] == checker.SCHEMA_ID
    assert schema["$schema"] == checker.SCHEMA_DRAFT


def test_validate_manifest_rejects_schema_level_invalid_payload(
    tmp_path: Path,
) -> None:
    release_dir = _candidate(tmp_path)
    manifest = _manifest_for_lane(release_dir, "source")
    payload = _load_manifest(manifest)
    payload["unexpected"] = "not in schema"
    _write_manifest(manifest, payload)

    failures = checker.validate_manifest_file(manifest)

    _assert_error(failures, "manifest does not match Rust release manifest schema")


def test_main_mode_selection_fixtures_manifest_release_dir(tmp_path: Path) -> None:
    release_dir = _candidate(tmp_path)
    manifest = _manifest_for_lane(release_dir, "source")

    assert checker.main([], env={}) == 0
    assert checker.main([], env={"MANIFEST": str(manifest)}) == 0
    assert (
        checker.main(
            [],
            env={"RELEASE_DIR": str(release_dir), "SOURCE_COMMIT": VALID_COMMIT},
        )
        == 0
    )


def test_main_rejects_conflicting_env_modes(tmp_path: Path) -> None:
    assert (
        checker.main(
            [],
            env={
                "MANIFEST": str(tmp_path / "manifest.json"),
                "RELEASE_DIR": str(tmp_path),
            },
        )
        == 1
    )
    assert (
        checker.main(
            [],
            env={
                "RELEASE_DIR": str(tmp_path),
                "SOURCE_COMMIT": "abc",
            },
        )
        == 1
    )
    assert (
        checker.main(
            [],
            env={
                "MANIFEST": str(tmp_path / "manifest.json"),
                "SOURCE_COMMIT": VALID_COMMIT,
            },
        )
        == 1
    )


@pytest.mark.parametrize(
    "lane",
    ["source", "linux-x86_64-musl", "linux-aarch64-musl", "macos-arm64"],
)
def test_generate_manifest_valid_lane_shapes(
    tmp_path: Path, lane: checker.LaneName
) -> None:
    artifact_name = next(
        name
        for name, (artifact_lane, _target) in checker.rust_artifact_targets().items()
        if artifact_lane == lane
    )
    artifact_path = tmp_path / artifact_name
    artifact_path.write_bytes(b"artifact bytes\n")

    generated, failures = checker.generate_manifest(
        artifact_path,
        lane=lane,
        evidence=checker.fixture_evidence_by_lane()[lane],
        cohort=checker.CohortInputs(
            product=checker.PRODUCT,
            version=checker._current_version(),
            source_commit=VALID_COMMIT,
            source_dirty=False,
            active_exceptions=(),
        ),
    )

    assert failures == []
    assert generated is not None
    assert generated.bytes.endswith(b"\n")
    assert (
        generated.payload["target"] == checker.rust_artifact_targets()[artifact_name][1]
    )


def test_release_dir_accepts_exact_15_without_models(tmp_path: Path) -> None:
    release_dir = _candidate(tmp_path)

    assert len(list(release_dir.iterdir())) == 15
    assert (
        checker.validate_release_dir(release_dir, expected_source_commit=VALID_COMMIT)
        == []
    )


def test_release_dir_accepts_exact_17_with_models(tmp_path: Path) -> None:
    release_dir = _candidate(tmp_path, include_models=True)

    assert len(list(release_dir.iterdir())) == 17
    assert (
        checker.validate_release_dir(release_dir, expected_source_commit=VALID_COMMIT)
        == []
    )


def test_release_dir_rejects_one_file_models_set(tmp_path: Path) -> None:
    release_dir = _candidate(tmp_path)
    model_name = sorted(checker._models_expected_names())[0]
    (release_dir / model_name).write_bytes(b"leftover model\n")

    failures = checker.validate_release_dir(
        release_dir, expected_source_commit=VALID_COMMIT
    )

    _assert_error(failures, "release directory contains exactly one models archive")


def test_release_dir_rejects_wrong_model_version_pair(tmp_path: Path) -> None:
    release_dir = _candidate(tmp_path)
    (release_dir / "solstone_journal_models-0.0.0.tar.gz").write_bytes(b"wrong\n")
    (release_dir / "solstone_journal_models-0.0.0-py3-none-any.whl").write_bytes(
        b"wrong\n"
    )

    failures = checker.validate_release_dir(
        release_dir, expected_source_commit=VALID_COMMIT
    )

    _assert_error(
        failures, "models archive names do not match current models version pair"
    )


def test_release_dir_rejects_skipped_model_leftover(tmp_path: Path) -> None:
    release_dir = _candidate(tmp_path)
    for name in list(checker.expected_package_names(include_models=False))[:2]:
        (release_dir / name).unlink()
    for name in checker._models_expected_names():
        (release_dir / name).write_bytes(b"skipped model\n")

    failures = checker.validate_release_dir(
        release_dir, expected_source_commit=VALID_COMMIT
    )

    _assert_error(failures, "15-file candidate contains models archive leftover")


def test_release_dir_rejects_unknown_missing_extra_assets_and_case_collision(
    tmp_path: Path,
) -> None:
    release_dir = _candidate(tmp_path)
    (release_dir / "unknown.whl").write_bytes(b"unknown\n")
    (release_dir / checker.expected_package_names(include_models=False)[0]).unlink()
    for name in checker._models_expected_names():
        (release_dir / name).write_bytes(b"extra\n")
    (release_dir / "CASE.txt").write_bytes(b"a\n")
    (release_dir / "case.TXT").write_bytes(b"b\n")

    failures = checker.validate_release_dir(
        release_dir, expected_source_commit=VALID_COMMIT
    )

    _assert_error(failures, "release directory contains unknown asset")
    _assert_error(failures, "release directory is missing required assets")
    _assert_error(failures, "release directory contains extra assets")
    _assert_error(failures, "release directory contains case-colliding filenames")


def test_release_dir_rejects_special_file_entry(tmp_path: Path) -> None:
    release_dir = _candidate(tmp_path)
    package = release_dir / checker.expected_package_names(include_models=False)[0]
    package.unlink()
    package.mkdir()

    failures = checker.validate_release_dir(
        release_dir, expected_source_commit=VALID_COMMIT
    )

    _assert_error(failures, "release file is not a regular non-symlink file")


def test_release_dir_rejects_extra_or_pure_package_manifest(tmp_path: Path) -> None:
    release_dir = _candidate(tmp_path)
    pure_artifact = next(
        name
        for name in checker.expected_package_names(include_models=False)
        if name.startswith("solstone-")
    )
    manifest = release_dir / f"{pure_artifact}.rust-release-manifest.json"
    source_payload = _load_manifest(_manifest_for_lane(release_dir, "source"))
    source_payload["target"] = {"kind": "source"}
    digest = hashlib.sha256((release_dir / pure_artifact).read_bytes()).hexdigest()
    source_payload["artifacts"] = [
        {
            "path": pure_artifact,
            "sha256": digest,
            "bytes": (release_dir / pure_artifact).stat().st_size,
        }
    ]
    _write_manifest(manifest, source_payload)

    failures = checker.validate_release_dir(
        release_dir, expected_source_commit=VALID_COMMIT
    )

    _assert_error(failures, "manifest covers a non-Rust release artifact")
    _assert_error(
        failures, "release directory must contain exactly four Rust manifests"
    )


def test_release_dir_rejects_duplicate_coverage(tmp_path: Path) -> None:
    release_dir = _candidate(tmp_path)
    original = _manifest_for_lane(release_dir, "source")
    duplicate = release_dir / "duplicate.rust-release-manifest.json"
    duplicate.write_bytes(original.read_bytes())

    failures = checker.validate_release_dir(
        release_dir, expected_source_commit=VALID_COMMIT
    )

    _assert_error(failures, "Rust artifact is covered by multiple manifests")


def test_release_dir_rejects_unmanifested_rust_artifact(tmp_path: Path) -> None:
    release_dir = _candidate(tmp_path)
    _manifest_for_lane(release_dir, "macos-arm64").unlink()

    failures = checker.validate_release_dir(
        release_dir, expected_source_commit=VALID_COMMIT
    )

    _assert_error(failures, "Rust artifact is not covered by any manifest")


def test_release_dir_rejects_artifact_target_swap(tmp_path: Path) -> None:
    release_dir = _candidate(tmp_path)
    manifest = _manifest_for_lane(release_dir, "linux-x86_64-musl")
    payload = _load_manifest(manifest)
    payload["target"] = checker.rust_artifact_targets()[
        _artifact_for_lane(release_dir, "macos-arm64").name
    ][1]
    _write_manifest(manifest, payload)

    failures = checker.validate_release_dir(
        release_dir, expected_source_commit=VALID_COMMIT
    )

    _assert_error(failures, "artifact target does not match release lane")


def test_release_dir_rejects_mixed_cohort_including_advisory_time(
    tmp_path: Path,
) -> None:
    release_dir = _candidate(tmp_path)
    manifest = _manifest_for_lane(release_dir, "source")
    payload = _load_manifest(manifest)
    payload["dependency_policy"]["advisory_checked_at"] = "2026-07-21T00:00:00Z"
    _write_manifest(manifest, payload)

    failures = checker.validate_release_dir(
        release_dir, expected_source_commit=VALID_COMMIT
    )

    _assert_error(failures, "Rust release manifests do not agree on cohort fields")


def test_release_dir_rejects_false_source_commit(tmp_path: Path) -> None:
    release_dir = _candidate(tmp_path)

    failures = checker.validate_release_dir(
        release_dir, expected_source_commit="b" * 40
    )

    _assert_error(failures, "source_commit does not match SOURCE_COMMIT")


def test_rustc_verbose_allows_only_lane_bound_host_difference(tmp_path: Path) -> None:
    release_dir = _candidate(tmp_path)

    assert (
        checker.validate_release_dir(release_dir, expected_source_commit=VALID_COMMIT)
        == []
    )

    source_manifest = _manifest_for_lane(release_dir, "source")
    payload = _load_manifest(source_manifest)
    payload["rust"]["rustc_verbose"] = checker.fixture_rustc_verbose(
        "aarch64-apple-darwin"
    )
    _write_manifest(source_manifest, payload)
    failures = checker.validate_release_dir(
        release_dir, expected_source_commit=VALID_COMMIT
    )
    _assert_error(failures, "rustc host is not an allowed build host")


def test_rustc_host_mismatch_redacts_host_value(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    host = "buildhost01"
    release_dir = _candidate(tmp_path)
    manifest = _manifest_for_lane(release_dir, "source")
    payload = _load_manifest(manifest)
    payload["rust"]["rustc_verbose"] = checker.fixture_rustc_verbose(host)
    _write_manifest(manifest, payload)

    failures = checker.validate_manifest_file(manifest)

    _assert_error(failures, "rustc host is not an allowed build host")
    _assert_redacted(failures, (host,))
    _assert_formatted_redacted(failures, (host,), capsys)


@pytest.mark.parametrize("code_point", RUSTC_REJECTED_SEPARATOR_CODE_POINTS)
def test_rustc_verbose_rejects_non_space_separator_code_points(
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
    code_point: int,
) -> None:
    canonical = checker.fixture_rustc_verbose("x86_64-unknown-linux-gnu")
    canonical_lines = canonical.split("\n")
    artifact_path = _source_artifact(tmp_path)

    for line_index in range(1, len(canonical_lines)):
        label, value = canonical_lines[line_index].split(": ", 1)
        mutant_line = f"{label}:{chr(code_point)}{value}"
        mutant = _rustc_line_mutant(canonical, line_index, mutant_line)
        if code_point == 0x0A:
            _assert_rustc_lf_separator_mutant_rejected(
                artifact_path,
                capsys,
                canonical=canonical,
                mutant=mutant,
                line_index=line_index,
            )
        else:
            _assert_rustc_line_mutant_rejected(
                artifact_path,
                capsys,
                canonical=canonical,
                mutant=mutant,
                line_index=line_index,
            )


@pytest.mark.parametrize(
    "spacing_case", ("zero", "two", "three", "pre_colon", "trailing")
)
def test_rustc_verbose_rejects_noncanonical_space_counts(
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
    spacing_case: str,
) -> None:
    canonical = checker.fixture_rustc_verbose("x86_64-unknown-linux-gnu")
    canonical_lines = canonical.split("\n")
    artifact_path = _source_artifact(tmp_path)

    for line_index in range(1, len(canonical_lines)):
        label, value = canonical_lines[line_index].split(": ", 1)
        if spacing_case == "zero":
            mutant_line = f"{label}:{value}"
        elif spacing_case == "two":
            mutant_line = f"{label}:  {value}"
        elif spacing_case == "three":
            mutant_line = f"{label}:   {value}"
        elif spacing_case == "pre_colon":
            mutant_line = f"{label} : {value}"
        else:
            mutant_line = f"{label}: {value} "
        mutant = _rustc_line_mutant(canonical, line_index, mutant_line)
        _assert_rustc_line_mutant_rejected(
            artifact_path,
            capsys,
            canonical=canonical,
            mutant=mutant,
            line_index=line_index,
        )


def test_rustc_verbose_rejects_crlf_line_boundary(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    canonical = checker.fixture_rustc_verbose("x86_64-unknown-linux-gnu")
    lines = canonical.split("\n")
    mutant = canonical.replace("\n", "\r\n", 1)
    assert "\r\n" in mutant
    assert mutant.replace("\r\n", "\n", 1) == canonical
    _assert_rustc_mutant_rejected(
        _source_artifact(tmp_path),
        capsys,
        mutant=mutant,
        forbidden=(mutant, f"{lines[0]}\r\n{lines[1]}"),
    )


def test_rustc_verbose_rejects_lone_cr_line_boundary(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    canonical = checker.fixture_rustc_verbose("x86_64-unknown-linux-gnu")
    lines = canonical.split("\n")
    mutant = canonical.replace("\n", "\r", 1)
    assert "\r" in mutant
    assert mutant.replace("\r", "\n", 1) == canonical
    _assert_rustc_mutant_rejected(
        _source_artifact(tmp_path),
        capsys,
        mutant=mutant,
        forbidden=(mutant, f"{lines[0]}\r{lines[1]}"),
    )


@pytest.mark.parametrize("separator", ("\u0085", "\u2028", "\u2029"))
def test_rustc_verbose_rejects_unicode_line_boundary_replacements(
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
    separator: str,
) -> None:
    canonical = checker.fixture_rustc_verbose("x86_64-unknown-linux-gnu")
    lines = canonical.split("\n")
    mutant = canonical.replace("\n", separator, 1)
    assert separator in mutant
    assert mutant.replace(separator, "\n", 1) == canonical
    _assert_rustc_mutant_rejected(
        _source_artifact(tmp_path),
        capsys,
        mutant=mutant,
        forbidden=(mutant, f"{lines[0]}{separator}{lines[1]}"),
    )


def test_rustc_verbose_rejects_doubled_lf_line_boundary(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    canonical = checker.fixture_rustc_verbose("x86_64-unknown-linux-gnu")
    lines = canonical.split("\n")
    mutant = canonical.replace("\n", "\n\n", 1)
    assert mutant.count("\n") == canonical.count("\n") + 1
    assert mutant.replace("\n\n", "\n", 1) == canonical
    _assert_rustc_mutant_rejected(
        _source_artifact(tmp_path),
        capsys,
        mutant=mutant,
        forbidden=(mutant, f"{lines[0]}\n\n{lines[1]}"),
    )


def test_rustc_verbose_rejects_leading_lf(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    canonical = checker.fixture_rustc_verbose("x86_64-unknown-linux-gnu")
    lines = canonical.split("\n")
    mutant = "\n" + canonical
    assert mutant.startswith("\n")
    assert mutant.count("\n") == canonical.count("\n") + 1
    assert mutant.removeprefix("\n") == canonical
    _assert_rustc_mutant_rejected(
        _source_artifact(tmp_path),
        capsys,
        mutant=mutant,
        forbidden=(mutant, f"\n{lines[0]}"),
    )


def test_rustc_verbose_rejects_trailing_lf(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    canonical = checker.fixture_rustc_verbose("x86_64-unknown-linux-gnu")
    lines = canonical.split("\n")
    mutant = canonical + "\n"
    assert mutant.endswith("\n")
    assert mutant.count("\n") == canonical.count("\n") + 1
    assert mutant.removesuffix("\n") == canonical
    _assert_rustc_mutant_rejected(
        _source_artifact(tmp_path),
        capsys,
        mutant=mutant,
        forbidden=(mutant, f"{lines[-1]}\n"),
    )


@pytest.mark.parametrize(
    ("name", "separator"), (("nbsp", "\u00a0"), ("zero", ""), ("two", "  "))
)
def test_rustc_verbose_separator_rejections_cover_file_and_candidate_channels(
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
    name: str,
    separator: str,
) -> None:
    canonical = checker.fixture_rustc_verbose("x86_64-unknown-linux-gnu")
    lines = canonical.split("\n")
    label, value = lines[1].split(": ", 1)
    mutant_line = f"{label}:{separator}{value}"
    mutant = _rustc_line_mutant(canonical, 1, mutant_line)

    release_dir = _candidate(tmp_path / f"file-{name}")
    manifest = _manifest_for_lane(release_dir, "source")
    payload = _load_manifest(manifest)
    payload["rust"]["rustc_verbose"] = mutant
    _write_manifest(manifest, payload)
    failures = checker.validate_manifest_file(manifest)
    _assert_malformed_redacted(failures, (mutant, mutant_line), capsys)

    evidence = checker.fixture_evidence_by_lane()
    mutated_evidence = dict(evidence)
    mutated_evidence["source"] = replace(evidence["source"], rustc_verbose=mutant)
    failures = checker.write_inert_candidate(
        tmp_path / f"generated-{name}",
        include_models=False,
        evidence_by_lane=mutated_evidence,
    )
    _assert_malformed_redacted(failures, (mutant, mutant_line), capsys)


@pytest.mark.parametrize(
    ("lane", "host"),
    (
        ("source", "x86_64-unknown-linux-gnu"),
        ("macos-arm64", "aarch64-apple-darwin"),
    ),
)
def test_rustc_verbose_canonical_evidence_is_byte_exact(
    tmp_path: Path,
    lane: str,
    host: str,
) -> None:
    canonical = checker.fixture_rustc_verbose(host)
    parsed, parse_failures = checker.parse_rustc_verbose(canonical)
    assert parse_failures == []
    assert parsed is not None

    release_dir = tmp_path / lane
    checker.write_inert_packages(release_dir, include_models=False)
    artifact_path = _artifact_for_lane(release_dir, lane)
    evidence = checker.fixture_evidence_by_lane()[lane]
    assert evidence.rustc_verbose == canonical

    generated, generate_failures = checker.generate_manifest(
        artifact_path,
        lane=lane,
        evidence=evidence,
        cohort=checker._default_cohort(VALID_COMMIT),
    )
    assert generate_failures == []
    assert generated is not None
    manifest_bytes = checker.canonical_json_bytes(generated.payload)
    assert manifest_bytes == generated.bytes

    decoded = json.loads(manifest_bytes)
    rustc_verbose = decoded["rust"]["rustc_verbose"]
    assert rustc_verbose == canonical
    assert rustc_verbose.encode("utf-8") == canonical.encode("utf-8")

    manifest_path = release_dir / generated.manifest_name
    manifest_path.write_bytes(manifest_bytes)
    assert checker.validate_manifest_file(manifest_path) == []


def test_rustc_verbose_rejects_malformed_spoof_and_mixed_labeled_lines(
    tmp_path: Path,
) -> None:
    release_dir = _candidate(tmp_path)
    manifest = _manifest_for_lane(release_dir, "macos-arm64")
    payload = _load_manifest(manifest)
    payload["rust"]["rustc_verbose"] = checker.fixture_rustc_verbose(
        "x86_64-unknown-linux-gnu"
    )
    _write_manifest(manifest, payload)
    failures = checker.validate_release_dir(
        release_dir, expected_source_commit=VALID_COMMIT
    )
    _assert_error(failures, "rustc host is not an allowed build host")

    release_dir = _candidate(tmp_path / "malformed")
    manifest = _manifest_for_lane(release_dir, "source")
    payload = _load_manifest(manifest)
    payload["rust"]["rustc_verbose"] = "rustc nope"
    _write_manifest(manifest, payload)
    failures = checker.validate_release_dir(
        release_dir, expected_source_commit=VALID_COMMIT
    )
    _assert_error(failures, "rustc_verbose is malformed")

    release_dir = _candidate(tmp_path / "mixed")
    manifest = _manifest_for_lane(release_dir, "source")
    payload = _load_manifest(manifest)
    payload["rust"]["rustc_verbose"] = payload["rust"]["rustc_verbose"].replace(
        f"LLVM version: {checker.RUSTC_LLVM_PIN}", "LLVM version: 22.0.0"
    )
    _write_manifest(manifest, payload)
    failures = checker.validate_release_dir(
        release_dir, expected_source_commit=VALID_COMMIT
    )
    _assert_error(failures, "rustc_verbose is malformed")


def test_rust_evidence_rejects_uniformly_wrong_toolchain_pins(tmp_path: Path) -> None:
    release_dir = _candidate(tmp_path)
    wrong_rustc = "\n".join(
        [
            "rustc 1.96.0 (111111111 2026-01-01)",
            "binary: rustc",
            "commit-hash: 1111111111111111111111111111111111111111",
            "commit-date: 2026-01-01",
            "host: x86_64-unknown-linux-gnu",
            "release: 1.96.0",
            "LLVM version: 21.0.0",
        ]
    )
    for lane in checker.LANES:
        manifest = _manifest_for_lane(release_dir, lane)
        payload = _load_manifest(manifest)
        payload["rust"]["rustc_verbose"] = wrong_rustc.replace(
            "x86_64-unknown-linux-gnu", checker.LANE_HOSTS[lane]
        )
        payload["rust"]["cargo_version"] = "cargo 1.96.0 (222222222 2026-01-01)"
        payload["dependency_policy"]["cargo_deny_version"] = "cargo-deny 0.1.0"
        _write_manifest(manifest, payload)

    failures = checker.validate_release_dir(
        release_dir, expected_source_commit=VALID_COMMIT
    )

    _assert_error(failures, "rustc_verbose is malformed")
    _assert_error(failures, "cargo_version is malformed")
    _assert_error(failures, "cargo_deny_version is not pinned")


def test_rustc_verbose_rejects_wrong_binary(tmp_path: Path) -> None:
    release_dir = _candidate(tmp_path)
    manifest = _manifest_for_lane(release_dir, "source")
    payload = _load_manifest(manifest)
    payload["rust"]["rustc_verbose"] = payload["rust"]["rustc_verbose"].replace(
        f"binary: {checker.RUSTC_BINARY_PIN}", "binary: rustdoc"
    )
    _write_manifest(manifest, payload)

    failures = checker.validate_manifest_file(manifest)

    _assert_error(failures, "rustc_verbose is malformed")


def test_rustc_verbose_rejects_missing_duplicate_unknown_labels(
    tmp_path: Path,
) -> None:
    cases: tuple[tuple[str, list[str]], ...] = (
        ("missing", _rustc_lines()[:3] + _rustc_lines()[4:]),
        (
            "duplicate",
            [
                *_rustc_lines()[:4],
                f"commit-date: {checker.RUSTC_COMMIT_DATE_PIN}",
                *_rustc_lines()[5:],
            ],
        ),
        (
            "unknown",
            [
                *_rustc_lines()[:5],
                "channel: stable",
                _rustc_lines()[6],
            ],
        ),
    )
    for name, lines in cases:
        release_dir = _candidate(tmp_path / name)
        manifest = _manifest_for_lane(release_dir, "source")
        payload = _load_manifest(manifest)
        payload["rust"]["rustc_verbose"] = "\n".join(lines)
        _write_manifest(manifest, payload)

        failures = checker.validate_manifest_file(manifest)

        _assert_error(failures, "rustc_verbose is malformed")


def test_rustc_verbose_rejects_wrong_commit_date_release_llvm_and_bad_host(
    tmp_path: Path,
) -> None:
    replacements = (
        (
            "commit",
            f"commit-hash: {checker.RUSTC_COMMIT_HASH_PIN}",
            "commit-hash: " + "b" * 40,
        ),
        (
            "date",
            f"commit-date: {checker.RUSTC_COMMIT_DATE_PIN}",
            "commit-date: 2026-01-01",
        ),
        ("release", f"release: {checker.RUSTC_RELEASE_PIN}", "release: 1.96.0"),
        ("llvm", f"LLVM version: {checker.RUSTC_LLVM_PIN}", "LLVM version: 21.0.0"),
    )
    for name, old, new in replacements:
        release_dir = _candidate(tmp_path / name)
        manifest = _manifest_for_lane(release_dir, "source")
        payload = _load_manifest(manifest)
        payload["rust"]["rustc_verbose"] = payload["rust"]["rustc_verbose"].replace(
            old, new
        )
        _write_manifest(manifest, payload)

        failures = checker.validate_manifest_file(manifest)

        _assert_error(failures, "rustc_verbose is malformed")

    release_dir = _candidate(tmp_path / "host")
    manifest = _manifest_for_lane(release_dir, "source")
    payload = _load_manifest(manifest)
    payload["rust"]["rustc_verbose"] = checker.fixture_rustc_verbose("localhost")
    _write_manifest(manifest, payload)
    failures = checker.validate_manifest_file(manifest)
    _assert_error(failures, "rustc_verbose contains disallowed content")
    _assert_error(failures, "rustc host is not an allowed build host")


def test_rustc_verbose_rejects_blank_interstitial_reordered_and_extra_lines(
    tmp_path: Path,
) -> None:
    lines = _rustc_lines()
    cases: tuple[tuple[str, list[str]], ...] = (
        ("blank", [*lines[:3], "", *lines[3:]]),
        ("reordered", [lines[0], lines[2], lines[1], *lines[3:]]),
        ("extra", [*lines, "extra: public"]),
    )
    for name, rustc_lines in cases:
        release_dir = _candidate(tmp_path / name)
        manifest = _manifest_for_lane(release_dir, "source")
        payload = _load_manifest(manifest)
        payload["rust"]["rustc_verbose"] = "\n".join(rustc_lines)
        _write_manifest(manifest, payload)

        failures = checker.validate_manifest_file(manifest)

        _assert_error(failures, "rustc_verbose is malformed")


def test_rust_evidence_redacts_canaries_from_failures_and_formatted_output(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    token = "sk-abcdefghijklmnopqrstuvwx"
    private_path = "/Users/jer/.cargo/bin"
    release_dir = _candidate(tmp_path)
    manifest = _manifest_for_lane(release_dir, "source")
    payload = _load_manifest(manifest)
    payload["rust"]["rustc_verbose"] = "\n".join(
        [*_rustc_lines(), f"leak: {token} {private_path}"]
    )
    _write_manifest(manifest, payload)

    failures = checker.validate_release_dir(
        release_dir, expected_source_commit=VALID_COMMIT
    )

    assert failures
    _assert_error(failures, "rustc_verbose contains disallowed content")
    _assert_redacted(failures, (token, private_path))
    _assert_formatted_redacted(failures, (token, private_path), capsys)


def test_cargo_and_cargo_deny_pins_are_enforced_without_echoing_input(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    token = "sk-abcdefghijklmnopqrstuvwx"
    private_path = "/Users/jer/.cargo/bin"
    release_dir = _candidate(tmp_path)
    manifest = _manifest_for_lane(release_dir, "source")
    payload = _load_manifest(manifest)
    payload["rust"]["cargo_version"] = f"cargo 1.96.0 ({token} 2026-01-01)"
    payload["dependency_policy"]["cargo_deny_version"] = (
        f"cargo-deny 0.1.0 {private_path} {token}"
    )
    _write_manifest(manifest, payload)

    failures = checker.validate_manifest_file(manifest)

    _assert_error(failures, "cargo_version is malformed")
    _assert_error(failures, "cargo_deny_version is not pinned")
    _assert_error(failures, "cargo_version contains disallowed content")
    _assert_error(failures, "cargo_deny_version contains disallowed content")
    _assert_redacted(failures, (token, private_path))
    _assert_formatted_redacted(failures, (token, private_path), capsys)


def test_cargo_and_cargo_deny_reject_surrounding_whitespace_and_control(
    tmp_path: Path,
) -> None:
    cargo_variants = (
        " " + checker.CARGO_VERSION_PIN,
        checker.CARGO_VERSION_PIN + " ",
        checker.CARGO_VERSION_PIN + "\n",
        checker.CARGO_VERSION_PIN + "\t",
        checker.CARGO_VERSION_PIN + "\x00",
    )
    for index, variant in enumerate(cargo_variants):
        release_dir = _candidate(tmp_path / f"cargo-{index}")
        manifest = _manifest_for_lane(release_dir, "source")
        payload = _load_manifest(manifest)
        payload["rust"]["cargo_version"] = variant
        _write_manifest(manifest, payload)

        failures = checker.validate_manifest_file(manifest)

        _assert_error(failures, "cargo_version is malformed")
        _assert_redacted(failures, (variant,))

    cargo_deny_variants = (
        " " + checker.CARGO_DENY_PIN,
        checker.CARGO_DENY_PIN + " ",
        checker.CARGO_DENY_PIN + "\n",
        checker.CARGO_DENY_PIN + "\t",
        checker.CARGO_DENY_PIN + "\x00",
    )
    for index, variant in enumerate(cargo_deny_variants):
        release_dir = _candidate(tmp_path / f"deny-{index}")
        manifest = _manifest_for_lane(release_dir, "source")
        payload = _load_manifest(manifest)
        payload["dependency_policy"]["cargo_deny_version"] = variant
        _write_manifest(manifest, payload)

        failures = checker.validate_manifest_file(manifest)

        _assert_error(failures, "cargo_deny_version is not pinned")
        _assert_redacted(failures, (variant,))


def test_generate_rejects_cargo_and_cargo_deny_surrounding_whitespace_and_redacts(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    evidence = checker.fixture_evidence_by_lane()
    cargo_evidence = dict(evidence)
    cargo_evidence["source"] = replace(
        evidence["source"], cargo_version=checker.CARGO_VERSION_PIN + " "
    )

    failures = checker.write_inert_candidate(
        tmp_path / "gen-cargo",
        include_models=False,
        evidence_by_lane=cargo_evidence,
    )

    assert failures
    _assert_error(failures, "cargo_version is malformed")

    deny_evidence = dict(evidence)
    deny_evidence["source"] = replace(
        evidence["source"], cargo_deny_version=" " + checker.CARGO_DENY_PIN
    )

    failures = checker.write_inert_candidate(
        tmp_path / "gen-deny",
        include_models=False,
        evidence_by_lane=deny_evidence,
    )

    assert failures
    _assert_error(failures, "cargo_deny_version is not pinned")

    private_path = "/Users/jer/.cargo"
    token = "sk-abcdefghijklmnopqrstuvwx"
    deny_canary_evidence = dict(evidence)
    deny_canary_evidence["source"] = replace(
        evidence["source"],
        cargo_deny_version=f"{checker.CARGO_DENY_PIN} {private_path} {token}",
    )

    failures = checker.write_inert_candidate(
        tmp_path / "gen-deny-canary",
        include_models=False,
        evidence_by_lane=deny_canary_evidence,
    )

    assert failures
    _assert_error(failures, "cargo_deny_version is not pinned")
    _assert_error(failures, "cargo_deny_version contains disallowed content")
    _assert_redacted(failures, (private_path, token))
    _assert_formatted_redacted(failures, (private_path, token), capsys)


def test_rust_evidence_accepts_pinned_linux_and_macos_hosts() -> None:
    cases: tuple[tuple[checker.LaneName, str], ...] = (
        ("source", "x86_64-unknown-linux-gnu"),
        ("macos-arm64", "aarch64-apple-darwin"),
    )
    for lane, host in cases:
        rustc, cargo, failures = checker._validate_rust_for_lane(
            lane,
            {
                "rustc_verbose": checker.fixture_rustc_verbose(host),
                "cargo_version": checker.CARGO_VERSION_PIN,
            },
        )

        assert failures == []
        assert rustc is not None
        assert rustc.host == host
        assert cargo == checker.CARGO_RELEASE_PIN


def test_native_tools_allowlists_by_lane() -> None:
    evidence = checker.fixture_evidence_by_lane()

    for lane, lane_evidence in evidence.items():
        assert checker.validate_native_tools(lane, lane_evidence.native_tools) == []

    failures = checker.validate_native_tools(
        "source",
        {"uv": pins.UV_PIN, "maturin": pins.MATURIN_PIN, "zig": pins.ZIG_PIN},
    )
    _assert_error(failures, "native_tools keys do not match lane allowlist")

    failures = checker.validate_native_tools("source", {"uv": pins.UV_PIN})
    _assert_error(failures, "native_tools keys do not match lane allowlist")

    failures = checker.validate_native_tools(
        "macos-arm64",
        {
            "uv": pins.UV_PIN,
            "maturin": pins.MATURIN_PIN,
            "xcode": pins.MACOS_XCODE_PIN,
            "codesign": pins.MACOS_CODESIGN_PUBLIC_PIN,
            "notarytool": pins.MACOS_NOTARYTOOL_PIN,
            "signing_mode": "unsigned",
        },
    )
    _assert_error(failures, "macOS signing_mode is not signed-verified")


@pytest.mark.parametrize(
    ("value", "error"),
    [
        (
            f"{pins.UV_PIN}\nPATH=/tmp/bin",
            "native_tools value is not a normalized public single-line string",
        ),
        (
            "TOKEN=abc123",
            "native_tools value is not a normalized public single-line string",
        ),
        ("secret token abc123", "native_tools value contains secret/token canary"),
        (
            "built on pro5e.local",
            "native_tools value contains private host, IP, or path",
        ),
        (
            "reachable at 192.168.1.10",
            "native_tools value contains private host, IP, or path",
        ),
        (
            "installed in /Users/jer/bin",
            "native_tools value contains private host, IP, or path",
        ),
        ("owner@example.com", "native_tools value contains email address"),
        (
            "Developer ID Application: sol pbc",
            "native_tools value contains signing identity",
        ),
        (
            "submission 123e4567-e89b-12d3-a456-426614174000",
            "native_tools value contains notarization submission ID",
        ),
    ],
)
def test_native_tools_rejects_canaries(value: str, error: str) -> None:
    tools = {"uv": value, "maturin": pins.MATURIN_PIN}

    failures = checker.validate_native_tools("source", tools)

    _assert_error(failures, error)


@pytest.mark.parametrize(
    "bad_path", ["../artifact.whl", "/tmp/artifact.whl", "a\\b.whl"]
)
def test_validate_manifest_rejects_path_traversal_absolute_backslash(
    tmp_path: Path, bad_path: str
) -> None:
    release_dir = _candidate(tmp_path)
    manifest = _manifest_for_lane(release_dir, "source")
    payload = _load_manifest(manifest)
    payload["artifacts"][0]["path"] = bad_path
    _write_manifest(manifest, payload)

    failures = checker.validate_manifest_file(manifest)

    _assert_error(failures, "artifact path is not a safe relative basename")


def test_validate_manifest_rejects_control_character_artifact_path(
    tmp_path: Path,
) -> None:
    release_dir = _candidate(tmp_path)
    manifest = _manifest_for_lane(release_dir, "source")
    payload = _load_manifest(manifest)
    payload["artifacts"][0]["path"] = "bad" + chr(10) + ".whl"
    _write_manifest(manifest, payload)

    failures = checker.validate_manifest_file(manifest)

    _assert_error(failures, "artifact path is not a safe relative basename")


def test_validate_manifest_rejects_symlink_missing_empty_and_hash_mismatch(
    tmp_path: Path,
) -> None:
    release_dir = _candidate(tmp_path / "missing")
    manifest = _manifest_for_lane(release_dir, "source")
    _artifact_for_lane(release_dir, "source").unlink()
    failures = checker.validate_manifest_file(manifest)
    _assert_error(failures, "manifest artifact is missing")

    release_dir = _candidate(tmp_path / "empty")
    manifest = _manifest_for_lane(release_dir, "source")
    _artifact_for_lane(release_dir, "source").write_bytes(b"")
    failures = checker.validate_manifest_file(manifest)
    _assert_error(failures, "manifest artifact is empty")

    release_dir = _candidate(tmp_path / "hash")
    manifest = _manifest_for_lane(release_dir, "source")
    _artifact_for_lane(release_dir, "source").write_bytes(b"mutated\n")
    failures = checker.validate_manifest_file(manifest)
    _assert_error(failures, "artifact sha256 does not match manifest")

    release_dir = _candidate(tmp_path / "symlink")
    manifest = _manifest_for_lane(release_dir, "source")
    artifact = _artifact_for_lane(release_dir, "source")
    artifact.unlink()
    artifact.symlink_to(
        release_dir / checker.expected_package_names(include_models=False)[0]
    )
    failures = checker.validate_manifest_file(manifest)
    _assert_error(failures, "release file is not a regular non-symlink file")


def test_validate_manifest_rejects_invalid_time_hash_commit_target_features(
    tmp_path: Path,
) -> None:
    release_dir = _candidate(tmp_path / "time")
    manifest = _manifest_for_lane(release_dir, "source")
    payload = _load_manifest(manifest)
    payload["dependency_policy"]["advisory_checked_at"] = "2026-07-20T00:00:00-06:00"
    _write_manifest(manifest, payload)
    failures = checker.validate_manifest_file(manifest)
    _assert_error(failures, "advisory timestamp is not RFC3339 UTC")

    release_dir = _candidate(tmp_path / "hash")
    manifest = _manifest_for_lane(release_dir, "source")
    payload = _load_manifest(manifest)
    payload["artifacts"][0]["sha256"] = "ABC"
    _write_manifest(manifest, payload)
    failures = checker.validate_manifest_file(manifest)
    _assert_error(failures, "sha256 is not lowercase hex")

    release_dir = _candidate(tmp_path / "commit")
    manifest = _manifest_for_lane(release_dir, "source")
    payload = _load_manifest(manifest)
    payload["source_commit"] = "abc"
    _write_manifest(manifest, payload)
    failures = checker.validate_manifest_file(manifest)
    _assert_error(failures, "source_commit is not a full lowercase commit")

    release_dir = _candidate(tmp_path / "target")
    manifest = _manifest_for_lane(release_dir, "linux-x86_64-musl")
    payload = _load_manifest(manifest)
    payload["target"]["profile"] = "debug"
    _write_manifest(manifest, payload)
    failures = checker.validate_manifest_file(manifest)
    _assert_error(failures, "artifact target does not match release lane")

    release_dir = _candidate(tmp_path / "features")
    manifest = _manifest_for_lane(release_dir, "linux-x86_64-musl")
    payload = _load_manifest(manifest)
    payload["target"]["features"] = ["extra"]
    _write_manifest(manifest, payload)
    failures = checker.validate_manifest_file(manifest)
    _assert_error(failures, "artifact target does not match release lane")


def test_validate_manifest_rejects_non_finite_json(tmp_path: Path) -> None:
    release_dir = _candidate(tmp_path)
    manifest = _manifest_for_lane(release_dir, "source")
    text = manifest.read_text(encoding="utf-8").replace(
        '"schema_version":1', '"schema_version":NaN'
    )
    manifest.write_text(text, encoding="utf-8")

    failures = checker.validate_manifest_file(manifest)

    _assert_error(failures, "manifest JSON is invalid or non-finite")


def test_validate_manifest_accepts_historical_utc_timestamp(tmp_path: Path) -> None:
    release_dir = _candidate(tmp_path)
    manifest = _manifest_for_lane(release_dir, "source")
    payload = _load_manifest(manifest)
    payload["dependency_policy"]["advisory_checked_at"] = "2020-01-01T00:00:00+00:00"
    _write_manifest(manifest, payload)

    failures = checker.validate_manifest_file(manifest)

    assert failures == []


def test_canonical_json_fixed_input_bytes_are_deterministic() -> None:
    payload = {
        "b": {"features": ["z", "a"]},
        "active_exceptions": ["z", "a"],
        "a": 1,
    }

    first = checker.canonical_json_bytes(payload)
    second = checker.canonical_json_bytes(payload)

    assert first == second
    assert first.endswith(b"\n")
    assert b'"active_exceptions":["a","z"]' in first
    with pytest.raises(ValueError, match="non-finite"):
        checker.canonical_json_bytes({"x": float("nan")})


def test_build_and_promote_candidate_success_is_whole_directory_rename(
    tmp_path: Path,
) -> None:
    ready, failures = _build_ready(tmp_path)

    assert failures == []
    assert ready.is_dir()
    assert not (tmp_path / "ready.staging").exists()
    assert len(list(ready.iterdir())) == 15
    assert (
        checker.validate_release_dir(ready, expected_source_commit=VALID_COMMIT) == []
    )


def test_build_and_promote_candidate_rejects_lock_contention(tmp_path: Path) -> None:
    dist = _source_dist(tmp_path)
    ready = tmp_path / "ready"
    lock = (tmp_path / ".rust-release-candidate.lock").open("a+")
    try:
        fcntl.flock(lock.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
        failures = checker.build_and_promote_candidate(
            dist,
            ready,
            source_commit=VALID_COMMIT,
            evidence_by_lane=checker.fixture_evidence_by_lane(),
            include_models=False,
        )
    finally:
        fcntl.flock(lock.fileno(), fcntl.LOCK_UN)
        lock.close()

    _assert_error(failures, "release candidate lock is already held")
    assert not ready.exists()
    assert not (tmp_path / "ready.staging").exists()


def test_build_and_promote_candidate_rolls_back_staging_on_pre_promotion_failure(
    tmp_path: Path,
) -> None:
    dist = _source_dist(tmp_path)
    (dist / checker.expected_package_names(include_models=False)[0]).unlink()
    ready = tmp_path / "ready"

    failures = checker.build_and_promote_candidate(
        dist,
        ready,
        source_commit=VALID_COMMIT,
        evidence_by_lane=checker.fixture_evidence_by_lane(),
        include_models=False,
    )

    _assert_error(failures, "manifest artifact is missing")
    assert not ready.exists()
    assert not (tmp_path / "ready.staging").exists()


def test_build_and_promote_candidate_removes_ready_on_post_promotion_failure(
    tmp_path: Path,
) -> None:
    def mutate(path: Path) -> None:
        artifact = next(iter(sorted(checker._rust_artifact_names())))
        (path / artifact).write_bytes(b"mutated\n")

    ready, failures = _build_ready(tmp_path, hook=mutate)

    _assert_error(failures, "artifact sha256 does not match manifest")
    assert not ready.exists()
    assert not (tmp_path / "ready.quarantine").exists()


def test_build_and_promote_candidate_quarantines_ready_when_post_promote_hook_raises(
    tmp_path: Path,
) -> None:
    ready = tmp_path / "ready"

    def fail_after_promote(_path: Path) -> None:
        raise RuntimeError("hook boom")

    with pytest.raises(RuntimeError, match="hook boom"):
        _build_ready(tmp_path, hook=fail_after_promote)

    assert not ready.exists()
    assert not (tmp_path / "ready.staging").exists()
    assert not (tmp_path / "ready.quarantine").exists()

    ready, failures = _build_ready(tmp_path)
    assert failures == []
    assert ready.is_dir()


def test_build_and_promote_candidate_quarantines_ready_when_final_validator_raises(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    ready = tmp_path / "ready"

    def fail_final_validator(
        _release_dir: Path,
        *,
        expected_source_commit: str | None,
        schema_path: Path = checker.SCHEMA_PATH,
    ) -> list[checker.Failure]:
        raise RuntimeError("validator boom")

    monkeypatch.setattr(checker, "_final_validate_release_dir", fail_final_validator)
    with pytest.raises(RuntimeError, match="validator boom"):
        _build_ready(tmp_path)

    assert not ready.exists()
    assert not (tmp_path / "ready.staging").exists()
    assert not (tmp_path / "ready.quarantine").exists()

    monkeypatch.undo()
    ready, failures = _build_ready(tmp_path)
    assert failures == []
    assert ready.is_dir()


def test_build_and_promote_candidate_leaves_quarantine_not_ready_when_quarantine_delete_fails(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    ready = tmp_path / "ready"
    quarantine = tmp_path / "ready.quarantine"
    hook_error = RuntimeError("hook boom")
    real_rmtree = checker.shutil.rmtree

    def fail_after_promote(_path: Path) -> None:
        raise hook_error

    def rmtree(path: Path, *args: Any, **kwargs: Any) -> None:
        if Path(path) == quarantine:
            raise OSError("delete failed")
        real_rmtree(path, *args, **kwargs)

    monkeypatch.setattr(checker.shutil, "rmtree", rmtree)
    with pytest.raises(
        RuntimeError, match="release candidate quarantine could not be removed"
    ) as exc_info:
        _build_ready(tmp_path, hook=fail_after_promote)

    assert exc_info.value.__cause__ is hook_error
    assert not ready.exists()
    assert not (tmp_path / "ready.staging").exists()
    assert quarantine.is_dir()
    assert checker.validate_release_dir(ready, expected_source_commit=VALID_COMMIT)
    new_ready = tmp_path / "new-ready"
    failures = checker.build_and_promote_candidate(
        _source_dist(tmp_path / "retry"),
        new_ready,
        source_commit=VALID_COMMIT,
        evidence_by_lane=checker.fixture_evidence_by_lane(),
        include_models=False,
    )
    assert failures == []
    assert new_ready.is_dir()


def test_fixtures_mode_runs_without_tree_artifacts() -> None:
    before = {path.name for path in checker.ROOT.iterdir()}

    failures = checker.run_fixtures_mode()

    after = {path.name for path in checker.ROOT.iterdir()}
    assert failures == []
    assert after == before
