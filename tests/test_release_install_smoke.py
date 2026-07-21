# SPDX-License-Identifier: AGPL-3.0-only
# Copyright (c) 2026 sol pbc

from __future__ import annotations

import zipfile
from collections.abc import Callable
from dataclasses import replace
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import pytest

import scripts.check_rust_release_manifest as checker
import scripts.release_install_smoke as smoke
from scripts.release_digest import candidate_digest, file_sha256_size

SOURCE_COMMIT = "a" * 40
CORE_LOCK = "b" * 64
LEDGER_SHA = "c" * 64


def _wheel_metadata(name: str) -> tuple[str, str]:
    parts = name.removesuffix(".whl").split("-")
    distribution = parts[0]
    version = parts[1]
    return (
        f"{distribution}-{version}.dist-info/METADATA",
        f"Name: {distribution.replace('_', '-')}\nVersion: {version}\n",
    )


def _write_metadata_wheel(path: Path) -> None:
    metadata_name, metadata = _wheel_metadata(path.name)
    with zipfile.ZipFile(path, "w") as wheel:
        wheel.writestr(metadata_name, metadata)


def _candidate(tmp_path: Path) -> tuple[Path, list[Path]]:
    candidate = tmp_path / "candidate"
    candidate.mkdir()
    wanted = []
    for name in checker.expected_package_names(include_models=False):
        if name.endswith(".whl") and (
            name.startswith("solstone-")
            or name.startswith("solstone_core-")
            or name.startswith("solstone_journal-")
            or name.startswith("solstone_journal_cuda-")
        ):
            wanted.append(name)
    paths = [candidate / name for name in wanted]
    for path in paths:
        _write_metadata_wheel(path)
    return candidate, paths


def _ledger_payload(digest: str, candidate: Path) -> dict:
    return {
        "source_commit": SOURCE_COMMIT,
        "core_lock_sha256": CORE_LOCK,
        "candidate": {
            "candidate_digest": digest,
            "files": [
                {
                    "name": path.name,
                    "bytes": path.stat().st_size,
                    "sha256": file_sha256_size(path)[0],
                }
                for path in sorted(candidate.iterdir(), key=lambda item: item.name)
                if path.is_file()
            ],
        },
        "native_summary": {
            "macos_core_script": {
                "member": {"path": "solstone-core", "sha256": "d" * 64, "bytes": 5}
            },
            "macos_root_helper": {
                "member": {"path": "parakeet-helper", "sha256": "e" * 64, "bytes": 6}
            },
        },
        "native_members": {
            "linux-x86_64-musl": {
                "solstone-core": {
                    "path": "linux-x86/solstone-core",
                    "sha256": "d" * 64,
                    "bytes": 5,
                }
            },
            "linux-aarch64-musl": {
                "solstone-core": {
                    "path": "linux-aarch64/solstone-core",
                    "sha256": "d" * 64,
                    "bytes": 5,
                }
            },
            "macos-arm64": {
                "solstone-core": {
                    "path": "macos/solstone-core",
                    "sha256": "d" * 64,
                    "bytes": 5,
                },
                "parakeet-helper": {
                    "path": "macos/parakeet-helper",
                    "sha256": "e" * 64,
                    "bytes": 6,
                },
            },
        },
    }


def _observation(
    *,
    env_root: Path,
    candidate_dir: Path,
    install_paths: tuple[Path, ...],
    macos: bool = True,
    member_hash: str = "d" * 64,
) -> smoke.InstallObservation:
    (env_root / "bin").mkdir(parents=True, exist_ok=True)
    (env_root / "bin" / "solstone-core").write_bytes(b"core")
    (env_root / "bin" / "python").write_bytes(b"python")
    if macos:
        (env_root / "bin" / "parakeet-helper").write_bytes(b"helper")
    members = [
        {
            "name": "solstone-core",
            "path": env_root / "bin" / "solstone-core",
            "sha256": member_hash,
            "symlink": False,
        }
    ]
    if macos:
        members.append(
            {
                "name": "parakeet-helper",
                "path": env_root / "bin" / "parakeet-helper",
                "sha256": "e" * 64,
                "symlink": False,
            }
        )
    return smoke.InstallObservation(
        env_root=env_root,
        preexisting_distributions=(),
        install=smoke.CommandResult(
            argv=(
                str(env_root / "bin" / "python"),
                "-m",
                "pip",
                "install",
                "--no-index",
                "--no-deps",
                *(str(path) for path in install_paths),
            ),
            exit_code=0,
            stdout="installed",
            env=smoke.SCRUBBED_COMMAND_ENV,
        ),
        installed_distributions=smoke.expected_distribution_entries(install_paths),
        installed_members=tuple(members),
        smoke={
            "solstone-core": smoke.CommandResult(
                argv=(str(env_root / "bin" / "solstone-core"), "--version"),
                exit_code=0,
                stdout="solstone-core 1.0.0",
                env=smoke.SCRUBBED_COMMAND_ENV,
            )
        },
    )


def test_proof_targets_explicitly_match_release_lanes() -> None:
    assert smoke.PROOF_TARGETS == (
        "linux-x86_64-musl",
        "linux-aarch64-musl",
        "macos-arm64",
    )
    assert set(smoke.PROOF_TARGETS) | {"source"} == set(checker.LANES)
    assert smoke.proof_targets_match_lanes()


def test_expected_distribution_entries_requires_wheel_metadata(tmp_path: Path) -> None:
    wheel = tmp_path / "solstone-1.0.0-py3-none-any.whl"
    wheel.write_bytes(b"not a wheel")

    with pytest.raises(smoke.InstallProofError) as exc:
        smoke.expected_distribution_entries((wheel,))

    assert (
        exc.value.failures[0].error
        == "install proof candidate wheel metadata is invalid"
    )


def test_install_proof_records_inventory_normalized_argv_and_paths(
    tmp_path: Path,
) -> None:
    candidate, paths = _candidate(tmp_path)
    digest = candidate_digest(candidate)
    ledger = _ledger_payload(digest, candidate)
    install_paths = smoke.target_install_paths_from_ledger(
        ledger,
        target="macos-arm64",
        candidate_dir=candidate,
    )

    proof = smoke.build_install_proof(
        target="macos-arm64",
        version="1.0.0",
        source_commit=SOURCE_COMMIT,
        core_lock_sha256=CORE_LOCK,
        candidate_digest=digest,
        ledger_sha256=LEDGER_SHA,
        candidate_dir=candidate,
        candidate_paths=paths,
        ledger_payload=ledger,
        observation=_observation(
            env_root=tmp_path / "env",
            candidate_dir=candidate,
            install_paths=install_paths,
        ),
        recorded_at=datetime(2026, 7, 20, 12, 34, 56, tzinfo=UTC),
    )

    assert proof["candidate_files"] == smoke.candidate_file_entries(install_paths)
    argv = proof["install"]["command"]["argv"]
    assert "--plat-name=macosx_14_0_arm64" not in argv
    assert "--plat-name" not in argv
    assert "macosx_14_0_arm64" not in argv
    assert "ENVROOT/bin/python" in argv
    assert all(f"CANDIDATE/{path.name}" in argv for path in install_paths)
    assert proof["installed_members"][0]["wheel_member_path"] == "macos/solstone-core"
    assert (
        proof["installed_members"][0]["installed_path"] == "ENVROOT/bin/solstone-core"
    )
    assert proof["recorded_at"] == "2026-07-20T12:34:56Z"


def test_install_proof_rejects_symlink_duplicate_and_member_hash_mismatch(
    tmp_path: Path,
) -> None:
    candidate, paths = _candidate(tmp_path)
    digest = candidate_digest(candidate)
    ledger = _ledger_payload(digest, candidate)
    install_paths = smoke.target_install_paths_from_ledger(
        ledger,
        target="macos-arm64",
        candidate_dir=candidate,
    )
    selected = install_paths[0]
    selected.unlink()
    selected.symlink_to(install_paths[1])
    with pytest.raises(smoke.InstallProofError) as exc:
        smoke.build_install_proof(
            target="macos-arm64",
            version="1.0.0",
            source_commit=SOURCE_COMMIT,
            core_lock_sha256=CORE_LOCK,
            candidate_digest=digest,
            ledger_sha256=LEDGER_SHA,
            candidate_dir=candidate,
            candidate_paths=paths,
            ledger_payload=ledger,
            observation=_observation(
                env_root=tmp_path / "env",
                candidate_dir=candidate,
                install_paths=install_paths,
            ),
            recorded_at=datetime(2026, 7, 20, 12, tzinfo=UTC),
        )

    errors = {failure.error for failure in exc.value.failures}
    assert "proof candidate file is a symlink" in errors

    selected.unlink()
    _write_metadata_wheel(selected)
    ledger = _ledger_payload(candidate_digest(candidate), candidate)
    selected_entry = next(
        entry
        for entry in ledger["candidate"]["files"]
        if entry["name"] == selected.name
    )
    ledger["candidate"]["files"].append(dict(selected_entry))

    with pytest.raises(smoke.InstallProofError) as exc:
        smoke.build_install_proof(
            target="macos-arm64",
            version="1.0.0",
            source_commit=SOURCE_COMMIT,
            core_lock_sha256=CORE_LOCK,
            candidate_digest=candidate_digest(candidate),
            ledger_sha256=LEDGER_SHA,
            candidate_dir=candidate,
            candidate_paths=paths,
            ledger_payload=ledger,
            observation=_observation(
                env_root=tmp_path / "env-duplicate",
                candidate_dir=candidate,
                install_paths=smoke.target_install_paths_from_ledger(
                    ledger,
                    target="macos-arm64",
                    candidate_dir=candidate,
                ),
            ),
            recorded_at=datetime(2026, 7, 20, 12, tzinfo=UTC),
        )

    assert any(
        failure.error == "proof candidate file basename is duplicated"
        for failure in exc.value.failures
    )

    with pytest.raises(smoke.InstallProofError) as exc:
        smoke.build_install_proof(
            target="macos-arm64",
            version="1.0.0",
            source_commit=SOURCE_COMMIT,
            core_lock_sha256=CORE_LOCK,
            candidate_digest=candidate_digest(candidate),
            ledger_sha256=LEDGER_SHA,
            candidate_dir=candidate,
            candidate_paths=paths,
            ledger_payload=_ledger_payload(candidate_digest(candidate), candidate),
            observation=_observation(
                env_root=tmp_path / "env",
                candidate_dir=candidate,
                install_paths=smoke.target_install_paths_from_ledger(
                    _ledger_payload(candidate_digest(candidate), candidate),
                    target="macos-arm64",
                    candidate_dir=candidate,
                ),
                member_hash="0" * 64,
            ),
            recorded_at=datetime(2026, 7, 20, 12, tzinfo=UTC),
        )
    assert any(
        failure.error == "installed member hash does not match ledger"
        for failure in exc.value.failures
    )


def test_observed_expected_hash_has_no_authority(tmp_path: Path) -> None:
    candidate, paths = _candidate(tmp_path)
    observation = _observation(
        env_root=tmp_path / "env",
        candidate_dir=candidate,
        install_paths=smoke.target_install_paths_from_ledger(
            _ledger_payload(candidate_digest(candidate), candidate),
            target="macos-arm64",
            candidate_dir=candidate,
        ),
        member_hash="0" * 64,
    )
    members = [dict(member) for member in observation.installed_members]
    members[0]["expected_sha256"] = "0" * 64
    observation = smoke.InstallObservation(
        env_root=observation.env_root,
        preexisting_distributions=observation.preexisting_distributions,
        install=observation.install,
        installed_distributions=observation.installed_distributions,
        installed_members=tuple(members),
        smoke=observation.smoke,
    )

    with pytest.raises(smoke.InstallProofError) as exc:
        smoke.build_install_proof(
            target="macos-arm64",
            version="1.0.0",
            source_commit=SOURCE_COMMIT,
            core_lock_sha256=CORE_LOCK,
            candidate_digest=candidate_digest(candidate),
            ledger_sha256=LEDGER_SHA,
            candidate_dir=candidate,
            candidate_paths=paths,
            ledger_payload=_ledger_payload(candidate_digest(candidate), candidate),
            observation=observation,
            recorded_at=datetime(2026, 7, 20, 12, tzinfo=UTC),
        )

    errors = {failure.error for failure in exc.value.failures}
    assert "install proof observation supplies forbidden expected hash" in errors
    assert "installed member hash does not match ledger" in errors


def test_observed_wheel_member_path_has_no_authority(tmp_path: Path) -> None:
    candidate, paths, ledger, install_paths = _linux_context(tmp_path)
    observation = _observation(
        env_root=tmp_path / "env-observed-wheel-member",
        candidate_dir=candidate,
        install_paths=install_paths,
        macos=False,
    )
    members = [dict(member) for member in observation.installed_members]
    members[0]["wheel_member_path"] = "forged/member/path"
    observation = replace(observation, installed_members=tuple(members))

    with pytest.raises(smoke.InstallProofError) as exc:
        smoke.build_install_proof(
            target="linux-x86_64-musl",
            version="1.0.0",
            source_commit=SOURCE_COMMIT,
            core_lock_sha256=CORE_LOCK,
            candidate_digest=ledger["candidate"]["candidate_digest"],
            ledger_sha256=LEDGER_SHA,
            candidate_dir=candidate,
            candidate_paths=paths,
            ledger_payload=ledger,
            observation=observation,
            recorded_at=datetime(2026, 7, 20, 12, tzinfo=UTC),
        )

    assert any(
        failure.error
        == "install proof observation supplies forbidden wheel member path"
        for failure in exc.value.failures
    )


def test_install_proof_rejects_empty_or_extra_member_sets(tmp_path: Path) -> None:
    candidate, paths = _candidate(tmp_path)
    digest = candidate_digest(candidate)
    ledger_payload = _ledger_payload(digest, candidate)
    ledger_payload["native_members"]["linux-x86_64-musl"] = {}
    empty = smoke.InstallObservation(
        env_root=tmp_path / "env-empty",
        preexisting_distributions=(),
        install=smoke.CommandResult(argv=("python",), exit_code=0, stdout="ok"),
        installed_distributions=(),
        installed_members=(),
        smoke={},
    )

    with pytest.raises(smoke.InstallProofError) as exc:
        smoke.build_install_proof(
            target="linux-x86_64-musl",
            version="1.0.0",
            source_commit=SOURCE_COMMIT,
            core_lock_sha256=CORE_LOCK,
            candidate_digest=digest,
            ledger_sha256=LEDGER_SHA,
            candidate_dir=candidate,
            candidate_paths=paths,
            ledger_payload=ledger_payload,
            observation=empty,
            recorded_at=datetime(2026, 7, 20, 12, tzinfo=UTC),
        )

    assert any(
        failure.error == "install proof retained native members are missing"
        for failure in exc.value.failures
    )

    observation = _observation(
        env_root=tmp_path / "env-extra",
        candidate_dir=candidate,
        install_paths=smoke.target_install_paths_from_ledger(
            _ledger_payload(digest, candidate),
            target="linux-x86_64-musl",
            candidate_dir=candidate,
        ),
        macos=False,
    )
    extra_members = [
        *observation.installed_members,
        {
            "name": "extra-helper",
            "path": tmp_path / "env-extra" / "bin" / "extra-helper",
            "sha256": "f" * 64,
            "symlink": False,
        },
    ]
    (tmp_path / "env-extra" / "bin" / "extra-helper").write_bytes(b"extra")
    observation = smoke.InstallObservation(
        env_root=observation.env_root,
        preexisting_distributions=observation.preexisting_distributions,
        install=observation.install,
        installed_distributions=observation.installed_distributions,
        installed_members=tuple(extra_members),
        smoke=observation.smoke,
    )

    with pytest.raises(smoke.InstallProofError) as exc:
        smoke.build_install_proof(
            target="linux-x86_64-musl",
            version="1.0.0",
            source_commit=SOURCE_COMMIT,
            core_lock_sha256=CORE_LOCK,
            candidate_digest=digest,
            ledger_sha256=LEDGER_SHA,
            candidate_dir=candidate,
            candidate_paths=paths,
            ledger_payload=_ledger_payload(digest, candidate),
            observation=observation,
            recorded_at=datetime(2026, 7, 20, 12, tzinfo=UTC),
        )

    assert any(
        failure.error == "install proof member set does not match retained ledger"
        for failure in exc.value.failures
    )


def test_written_install_proof_rejects_public_evidence_hazards(tmp_path: Path) -> None:
    candidate, paths = _candidate(tmp_path)
    digest = candidate_digest(candidate)
    ledger = _ledger_payload(digest, candidate)
    install_paths = smoke.target_install_paths_from_ledger(
        ledger,
        target="linux-x86_64-musl",
        candidate_dir=candidate,
    )
    proof = smoke.build_install_proof(
        target="linux-x86_64-musl",
        version="1.0.0",
        source_commit=SOURCE_COMMIT,
        core_lock_sha256=CORE_LOCK,
        candidate_digest=digest,
        ledger_sha256=LEDGER_SHA,
        candidate_dir=candidate,
        candidate_paths=paths,
        ledger_payload=ledger,
        observation=_observation(
            env_root=tmp_path / "env",
            candidate_dir=candidate,
            install_paths=install_paths,
            macos=False,
        ),
        recorded_at=datetime(2026, 7, 20, 12, tzinfo=UTC),
    )
    proof["install"]["command"]["argv"].append("--flag=value")

    failures = smoke.validate_install_proof(proof)

    assert failures


def _linux_context(
    tmp_path: Path,
) -> tuple[Path, list[Path], dict[str, Any], tuple[Path, ...]]:
    candidate, paths = _candidate(tmp_path)
    digest = candidate_digest(candidate)
    ledger = _ledger_payload(digest, candidate)
    install_paths = smoke.target_install_paths_from_ledger(
        ledger,
        target="linux-x86_64-musl",
        candidate_dir=candidate,
    )
    return candidate, paths, ledger, install_paths


@pytest.mark.parametrize(
    "mutate,error",
    [
        (
            lambda observation, _tmp_path: replace(
                observation,
                install=smoke.CommandResult(
                    argv=observation.install.argv[:-1],
                    exit_code=0,
                    stdout="installed",
                    env=smoke.SCRUBBED_COMMAND_ENV,
                ),
            ),
            "install proof command argv is not exact",
        ),
        (
            lambda observation, _tmp_path: replace(
                observation,
                install=smoke.CommandResult(
                    argv=(*observation.install.argv, "--index-url"),
                    exit_code=0,
                    stdout="installed",
                    env=smoke.SCRUBBED_COMMAND_ENV,
                ),
            ),
            "install proof command contains forbidden resolver option",
        ),
        (
            lambda observation, _tmp_path: replace(
                observation,
                install=smoke.CommandResult(
                    argv=observation.install.argv,
                    exit_code=0,
                    stdout="installed",
                    env={**smoke.SCRUBBED_COMMAND_ENV, "HTTPS_PROXY": "proxy.invalid"},
                ),
            ),
            "install proof install command environment is not scrubbed",
        ),
        (
            lambda observation, _tmp_path: replace(
                observation,
                installed_distributions=(
                    *observation.installed_distributions,
                    {"name": "solstone-extra", "version": "1.0.0"},
                ),
            ),
            "install proof installed distribution set is invalid",
        ),
        (
            lambda observation, _tmp_path: replace(
                observation,
                smoke={
                    "solstone-core": smoke.CommandResult(
                        argv=("ENVROOT/bin/solstone-core",),
                        exit_code=0,
                        stdout="solstone-core 1.0.0",
                        env=smoke.SCRUBBED_COMMAND_ENV,
                    )
                },
            ),
            "install proof smoke command argv is not exact",
        ),
        (
            lambda observation, _tmp_path: replace(
                observation,
                smoke={
                    "solstone-core": smoke.CommandResult(
                        argv=observation.smoke["solstone-core"].argv,
                        exit_code=0,
                        stdout="solstone-core wrong",
                        env=smoke.SCRUBBED_COMMAND_ENV,
                    )
                },
            ),
            "install proof smoke stdout is not exact",
        ),
        (
            lambda observation, _tmp_path: replace(
                observation,
                preexisting_distributions=("solstone",),
            ),
            "install proof environment already has solstone distributions",
        ),
        (
            lambda observation, tmp_path: _outside_member_observation(
                observation,
                tmp_path,
            ),
            "install proof member path escapes ENVROOT",
        ),
        (
            lambda observation, _tmp_path: _symlink_member_observation(observation),
            "install proof member is a symlink",
        ),
    ],
)
def test_install_proof_rejects_semantic_observation_mutations(
    tmp_path: Path,
    mutate: Callable[[smoke.InstallObservation, Path], smoke.InstallObservation],
    error: str,
) -> None:
    candidate, paths, ledger, install_paths = _linux_context(tmp_path)
    observation = _observation(
        env_root=tmp_path / "env-semantic",
        candidate_dir=candidate,
        install_paths=install_paths,
        macos=False,
    )

    with pytest.raises(smoke.InstallProofError) as exc:
        smoke.build_install_proof(
            target="linux-x86_64-musl",
            version="1.0.0",
            source_commit=SOURCE_COMMIT,
            core_lock_sha256=CORE_LOCK,
            candidate_digest=ledger["candidate"]["candidate_digest"],
            ledger_sha256=LEDGER_SHA,
            candidate_dir=candidate,
            candidate_paths=paths,
            ledger_payload=ledger,
            observation=mutate(observation, tmp_path),
            recorded_at=datetime(2026, 7, 20, 12, tzinfo=UTC),
        )

    assert any(failure.error == error for failure in exc.value.failures)


def _outside_member_observation(
    observation: smoke.InstallObservation,
    tmp_path: Path,
) -> smoke.InstallObservation:
    outside = tmp_path / "outside" / "solstone-core"
    outside.parent.mkdir()
    outside.write_bytes(b"outside")
    members = [dict(member) for member in observation.installed_members]
    members[0]["path"] = outside
    return replace(observation, installed_members=tuple(members))


def _symlink_member_observation(
    observation: smoke.InstallObservation,
) -> smoke.InstallObservation:
    member_path = Path(observation.installed_members[0]["path"])
    target = member_path.with_name("solstone-core-target")
    target.write_bytes(b"target")
    member_path.unlink()
    member_path.symlink_to(target)
    members = [dict(member) for member in observation.installed_members]
    members[0]["symlink"] = False
    return replace(observation, installed_members=tuple(members))
