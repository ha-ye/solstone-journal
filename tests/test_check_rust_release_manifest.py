# SPDX-License-Identifier: AGPL-3.0-only
# Copyright (c) 2026 sol pbc

from __future__ import annotations

import fcntl
import hashlib
import json
import os
import subprocess
import sys
from pathlib import Path

import pytest

import scripts.check_rust_release_manifest as checker

SCRIPT = (
    Path(__file__).resolve().parents[1] / "scripts" / "check_rust_release_manifest.py"
)
VALID_COMMIT = checker.fixture_source_commit()


def _errors(failures: list[checker.Failure]) -> set[str]:
    return {failure.error for failure in failures}


def _assert_error(failures: list[checker.Failure], error: str) -> None:
    assert error in _errors(failures)


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
        "LLVM version: 21.0.0", "LLVM version: 22.0.0"
    )
    _write_manifest(manifest, payload)
    failures = checker.validate_release_dir(
        release_dir, expected_source_commit=VALID_COMMIT
    )
    _assert_error(failures, "Rust evidence differs outside permitted host field")


def test_native_tools_allowlists_by_lane() -> None:
    evidence = checker.fixture_evidence_by_lane()

    for lane, lane_evidence in evidence.items():
        assert checker.validate_native_tools(lane, lane_evidence.native_tools) == []

    failures = checker.validate_native_tools(
        "source",
        {"uv": "uv 0.11.4", "maturin": "maturin 1.14.1", "zig": "zig 0.16.0"},
    )
    _assert_error(failures, "native_tools keys do not match lane allowlist")

    failures = checker.validate_native_tools("source", {"uv": "uv 0.11.4"})
    _assert_error(failures, "native_tools keys do not match lane allowlist")

    failures = checker.validate_native_tools(
        "macos-arm64",
        {
            "uv": "uv 0.11.4",
            "maturin": "maturin 1.14.1",
            "xcode": "Xcode 17.0",
            "codesign": "codesign verified",
            "notarytool": "notarytool accepted",
            "signing_mode": "unsigned",
        },
    )
    _assert_error(failures, "macOS signing_mode is not signed-verified")


@pytest.mark.parametrize(
    ("value", "error"),
    [
        (
            "uv 0.11.4\nPATH=/tmp/bin",
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
    tools = {"uv": value, "maturin": "maturin 1.14.1"}

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


def test_fixtures_mode_runs_without_tree_artifacts() -> None:
    before = {path.name for path in checker.ROOT.iterdir()}

    failures = checker.run_fixtures_mode()

    after = {path.name for path in checker.ROOT.iterdir()}
    assert failures == []
    assert after == before
