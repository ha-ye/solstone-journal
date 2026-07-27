# SPDX-License-Identifier: AGPL-3.0-only
# Copyright (c) 2026 sol pbc

from __future__ import annotations

import os
import zipfile
from collections.abc import Callable
from dataclasses import replace
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import pytest

import scripts.check_rust_release_manifest as checker
import scripts.check_wheel_contents as wheel_checker
import scripts.release_install_smoke as smoke
from scripts.release_digest import candidate_digest, file_sha256_size
from scripts.release_public_evidence import validate_public_evidence_tree
from tests.helpers.release_wheel_fixtures import (
    ROOT_LAUNCHER_BYTES,
    record_hash,
    speakers_analyze_elf,
    write_speakers_analyze_wheel,
)

SOURCE_COMMIT = "a" * 40
CORE_LOCK = "b" * 64
LEDGER_SHA = "c" * 64


def _core_member_payload(prefix: str, *, sha256: str = "d" * 64) -> dict[str, dict]:
    return {
        name: {
            "path": f"{prefix}/{name}",
            "sha256": sha256,
            "bytes": 5,
        }
        for name in checker.CORE_SCRIPT_NAMES
    }


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
        members = {metadata_name: metadata.encode("utf-8")}
        if path.name.startswith("solstone-"):
            version = path.name.removesuffix(".whl").split("-")[1]
            members[f"solstone-{version}.dist-info/WHEEL"] = b"Wheel-Version: 1.0\n"
            for name, content in ROOT_LAUNCHER_BYTES.items():
                members[f"solstone-{version}.data/scripts/{name}"] = content
            record_name = f"solstone-{version}.dist-info/RECORD"
            record = "\n".join(
                f"{name},{record_hash(content)},{len(content)}"
                for name, content in members.items()
            )
            members[record_name] = f"{record}\n{record_name},,".encode("utf-8")
        for name, content in members.items():
            info = zipfile.ZipInfo(name)
            info.external_attr = (
                0o755 << 16
                if Path(name).name in checker.ROOT_LAUNCHER_NAMES
                else 0o644 << 16
            )
            wheel.writestr(info, content)


def _write_speakers_analyze_wheel(path: Path) -> None:
    tag = path.name.removesuffix(".whl").split("-")[-1]
    binary = None
    if "manylinux" in tag:
        machine = "aarch64" if "aarch64" in tag else "x86_64"
        binary = speakers_analyze_elf(wheel_checker.ELF_MACHINE[machine])
    write_speakers_analyze_wheel(
        path.parent,
        tag=tag,
        version=path.name.removesuffix(".whl").split("-")[1],
        binary=binary,
        library=b"fixture onnxruntime GLIBC_2.27\n",
        license_notice=b"fixture license\n",
        third_party_notice=b"fixture notices\n",
    )


def _candidate(tmp_path: Path) -> tuple[Path, list[Path]]:
    candidate = tmp_path / "candidate"
    candidate.mkdir()
    wanted = []
    for name in checker.expected_package_names(include_models=False):
        if name.endswith(".whl") and (
            name.startswith("solstone-")
            or name.startswith("solstone_core-")
            or name.startswith("solstone_core_speakers_analyze-")
            or name.startswith("solstone_journal-")
            or name.startswith("solstone_journal_cuda-")
        ):
            wanted.append(name)
    paths = [candidate / name for name in wanted]
    for path in paths:
        if path.name.startswith("solstone_core_speakers_analyze-"):
            _write_speakers_analyze_wheel(path)
        else:
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
            "macos_speakers_analyze": {
                "member": {
                    "path": "solstone-core-speakers-analyze",
                    "sha256": "f" * 64,
                    "bytes": 7,
                }
            },
        },
        "native_members": {
            "linux-x86_64-musl": _core_member_payload("linux-x86"),
            "linux-aarch64-musl": _core_member_payload("linux-aarch64"),
            "macos-arm64": {
                **_core_member_payload("macos"),
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
    for name, content in ROOT_LAUNCHER_BYTES.items():
        (env_root / "bin" / name).write_bytes(content)
    for name in checker.CORE_SCRIPT_NAMES:
        (env_root / "bin" / name).write_bytes(b"core")
    (env_root / "bin" / "python").write_bytes(b"python")
    if macos:
        (env_root / "bin" / "parakeet-helper").write_bytes(b"helper")
    helper_wheels = [
        path
        for path in install_paths
        if path.name.startswith("solstone_core_speakers_analyze-")
        and smoke.SPEAKERS_ANALYZE_LINUX_X86_64_TAG in path.name
    ]
    helper_bytes = b""
    if helper_wheels:
        with zipfile.ZipFile(helper_wheels[0]) as wheel:
            helper_member = next(
                info
                for info in wheel.infolist()
                if info.filename.endswith(
                    ".data/scripts/solstone-core-speakers-analyze"
                )
            )
            helper_bytes = wheel.read(helper_member)
        (env_root / "bin" / "solstone-core-speakers-analyze").write_bytes(helper_bytes)
    members = [
        {
            "name": name,
            "path": env_root / "bin" / name,
            "sha256": file_sha256_size(env_root / "bin" / name)[0],
            "symlink": False,
        }
        for name in checker.ROOT_LAUNCHER_NAMES
    ]
    members += [
        {
            "name": name,
            "path": env_root / "bin" / name,
            "sha256": member_hash,
            "symlink": False,
        }
        for name in checker.CORE_SCRIPT_NAMES
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
    if helper_wheels:
        members.append(
            {
                "name": "solstone-core-speakers-analyze",
                "path": env_root / "bin" / "solstone-core-speakers-analyze",
                "sha256": file_sha256_size(
                    env_root / "bin" / "solstone-core-speakers-analyze"
                )[0],
                "symlink": False,
            }
        )
    smoke_results = {
        name: smoke.CommandResult(
            argv=(str(env_root / "bin" / name), "--version"),
            exit_code=0,
            stdout=f"{smoke.CORE_SMOKE_STDOUT[name]} 1.0.0",
            env=smoke.SCRUBBED_COMMAND_ENV,
        )
        for name in smoke.INSTALL_SCRIPT_NAMES
    }
    if helper_wheels:
        smoke_results["solstone-core-speakers-analyze"] = smoke.CommandResult(
            argv=(str(env_root / "bin" / "solstone-core-speakers-analyze"),),
            exit_code=0,
            stdout='{"schema":"solstone-speaker-analyze-response-v1"}',
            env=smoke.SCRUBBED_COMMAND_ENV,
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
        smoke=smoke_results,
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
    installed = {member["name"]: member for member in proof["installed_members"]}
    assert installed["solstone-core"]["wheel_member_path"] == "macos/solstone-core"
    assert installed["solstone-core"]["installed_path"] == "ENVROOT/bin/solstone-core"
    assert proof["recorded_at"] == "2026-07-20T12:34:56Z"


def test_install_proof_normalizes_pip24_stdout_candidate_paths(
    tmp_path: Path,
) -> None:
    candidate, paths = _candidate(tmp_path)
    digest = candidate_digest(candidate)
    ledger = _ledger_payload(digest, candidate)
    install_paths = smoke.target_install_paths_from_ledger(
        ledger,
        target="linux-x86_64-musl",
        candidate_dir=candidate,
    )
    core_wheel = next(
        path for path in install_paths if path.name.startswith("solstone_core-")
    )
    journal_wheel = next(
        path for path in install_paths if path.name.startswith("solstone_journal-")
    )
    stdout = "\n".join(
        (
            f"Processing {candidate}/{core_wheel.name}",
            f"Processing {candidate}/{journal_wheel.name}",
            "Installing collected packages: solstone-journal, solstone-core",
            "Successfully installed solstone-core-1.0.0 solstone-journal-1.0.0",
        )
    )
    observation = _observation(
        env_root=tmp_path / "env",
        candidate_dir=candidate,
        install_paths=install_paths,
        macos=False,
    )
    observation = replace(
        observation,
        install=replace(observation.install, stdout=stdout),
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
        observation=observation,
        recorded_at=datetime(2026, 7, 20, 12, 34, 56, tzinfo=UTC),
    )

    assert validate_public_evidence_tree("install_proof", proof) == []
    normalized_stdout = proof["install"]["command"]["stdout"]
    assert f"CANDIDATE/{core_wheel.name}" in normalized_stdout
    assert f"CANDIDATE/{journal_wheel.name}" in normalized_stdout
    assert str(candidate) not in normalized_stdout


def test_install_proof_rejects_unrelated_absolute_stdout_path(
    tmp_path: Path,
) -> None:
    candidate, paths = _candidate(tmp_path)
    digest = candidate_digest(candidate)
    ledger = _ledger_payload(digest, candidate)
    install_paths = smoke.target_install_paths_from_ledger(
        ledger,
        target="linux-x86_64-musl",
        candidate_dir=candidate,
    )
    observation = _observation(
        env_root=tmp_path / "env",
        candidate_dir=candidate,
        install_paths=install_paths,
        macos=False,
    )
    observation = replace(
        observation,
        install=replace(observation.install, stdout="note /etc/shadow here"),
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
            ledger_payload=ledger,
            observation=observation,
            recorded_at=datetime(2026, 7, 20, 12, 34, 56, tzinfo=UTC),
        )

    assert any(
        failure.error
        == "install_proof.install.command.stdout contains disallowed content"
        for failure in exc.value.failures
    )


def test_install_proof_rejects_prefix_sibling_stdout_paths(
    tmp_path: Path,
) -> None:
    candidate, paths = _candidate(tmp_path)
    digest = candidate_digest(candidate)
    ledger = _ledger_payload(digest, candidate)
    install_paths = smoke.target_install_paths_from_ledger(
        ledger,
        target="linux-x86_64-musl",
        candidate_dir=candidate,
    )
    env_root = tmp_path / "env"
    stdout = "\n".join(
        (
            f"note {candidate}-evil/data.txt here",
            f"note {env_root}x/data.txt here",
        )
    )
    observation = _observation(
        env_root=env_root,
        candidate_dir=candidate,
        install_paths=install_paths,
        macos=False,
    )
    observation = replace(
        observation,
        install=replace(observation.install, stdout=stdout),
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
            ledger_payload=ledger,
            observation=observation,
            recorded_at=datetime(2026, 7, 20, 12, 34, 56, tzinfo=UTC),
        )

    assert any(
        failure.error
        == "install_proof.install.command.stdout contains disallowed content"
        for failure in exc.value.failures
    )


def test_install_proof_normalizes_stderr_and_preserves_empty_streams(
    tmp_path: Path,
) -> None:
    candidate, paths = _candidate(tmp_path)
    digest = candidate_digest(candidate)
    ledger = _ledger_payload(digest, candidate)
    install_paths = smoke.target_install_paths_from_ledger(
        ledger,
        target="linux-x86_64-musl",
        candidate_dir=candidate,
    )
    wheel = next(
        path for path in install_paths if path.name.startswith("solstone_core-")
    )
    env_root = tmp_path / "env"
    stderr = "\n".join(
        (
            f"warning using {candidate}/{wheel.name}",
            f"created environment {env_root}/bin/python",
        )
    )
    observation = _observation(
        env_root=env_root,
        candidate_dir=candidate,
        install_paths=install_paths,
        macos=False,
    )
    observation = replace(
        observation,
        install=replace(observation.install, stderr=stderr),
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
        observation=observation,
        recorded_at=datetime(2026, 7, 20, 12, 34, 56, tzinfo=UTC),
    )

    normalized_stderr = proof["install"]["command"]["stderr"]
    assert f"CANDIDATE/{wheel.name}" in normalized_stderr
    assert "ENVROOT/bin/python" in normalized_stderr
    assert str(candidate) not in normalized_stderr
    assert str(env_root) not in normalized_stderr
    assert proof["smoke"]["solstone-core"]["stderr"] == ""


def test_install_proof_normalizes_realpath_aliases_in_command_output(
    tmp_path: Path,
) -> None:
    real_parent = tmp_path / "real-parent"
    real_parent.mkdir()
    link_parent = tmp_path / "link-parent"
    link_parent.symlink_to(real_parent, target_is_directory=True)
    candidate, paths = _candidate(link_parent)
    env_root = link_parent / "env"
    assert str(candidate) != os.path.realpath(candidate)
    assert str(env_root) != os.path.realpath(env_root)
    digest = candidate_digest(candidate)
    ledger = _ledger_payload(digest, candidate)
    install_paths = smoke.target_install_paths_from_ledger(
        ledger,
        target="linux-x86_64-musl",
        candidate_dir=candidate,
    )
    wheel = next(
        path for path in install_paths if path.name.startswith("solstone_core-")
    )
    stdout = "\n".join(
        (
            f"Processing {os.path.realpath(candidate)}/{wheel.name}",
            f"Using interpreter {os.path.realpath(env_root)}/bin/python",
        )
    )
    observation = _observation(
        env_root=env_root,
        candidate_dir=candidate,
        install_paths=install_paths,
        macos=False,
    )
    observation = replace(
        observation,
        install=replace(observation.install, stdout=stdout),
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
        observation=observation,
        recorded_at=datetime(2026, 7, 20, 12, 34, 56, tzinfo=UTC),
    )

    normalized_stdout = proof["install"]["command"]["stdout"]
    assert f"CANDIDATE/{wheel.name}" in normalized_stdout
    assert "ENVROOT/bin/python" in normalized_stdout
    assert os.path.realpath(candidate) not in normalized_stdout
    assert os.path.realpath(env_root) not in normalized_stdout


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
        failure.error == "installed member hash does not match expected payload"
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
    assert "installed member hash does not match expected payload" in errors


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
        failure.error
        == "install proof member set does not match expected executable payload"
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
        failure.error
        == "install proof member set does not match expected executable payload"
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

    with pytest.raises(TypeError):
        smoke.validate_install_proof(proof)

    failures = smoke.validate_install_proof(
        proof,
        target="linux-x86_64-musl",
        version="1.0.0",
        source_commit=SOURCE_COMMIT,
        core_lock_sha256=CORE_LOCK,
        candidate_digest=digest,
        ledger_sha256=LEDGER_SHA,
        candidate_dir=candidate,
        ledger_payload=ledger,
    )

    assert failures


def test_install_proof_validators_require_binding_arguments(tmp_path: Path) -> None:
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

    with pytest.raises(TypeError):
        smoke.validate_install_proof(proof)
    with pytest.raises(TypeError):
        smoke.validate_install_proof_bytes(smoke.canonical_json_bytes(proof))


def test_install_proof_candidate_file_bad_bytes_returns_failure(
    tmp_path: Path,
) -> None:
    candidate, paths, ledger, install_paths = _linux_context(tmp_path)
    proof = smoke.build_install_proof(
        target="linux-x86_64-musl",
        version="1.0.0",
        source_commit=SOURCE_COMMIT,
        core_lock_sha256=CORE_LOCK,
        candidate_digest=ledger["candidate"]["candidate_digest"],
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
    proof["candidate_files"][0]["bytes"] = "bad"

    failures = smoke.validate_install_proof(
        proof,
        target="linux-x86_64-musl",
        version="1.0.0",
        source_commit=SOURCE_COMMIT,
        core_lock_sha256=CORE_LOCK,
        candidate_digest=ledger["candidate"]["candidate_digest"],
        ledger_sha256=LEDGER_SHA,
        candidate_dir=candidate,
        ledger_payload=ledger,
    )

    assert any(
        failure.error == "install proof candidate file byte count is invalid"
        and "restore the retained install proof" in failure.repair
        for failure in failures
    )


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
                installed_distributions=(
                    *observation.installed_distributions,
                    observation.installed_distributions[0],
                ),
            ),
            "install proof installed distribution set is invalid",
        ),
        (
            lambda observation, _tmp_path: replace(
                observation,
                smoke={
                    **observation.smoke,
                    "solstone-core": replace(
                        observation.smoke["solstone-core"],
                        argv=("ENVROOT/bin/solstone-core",),
                    ),
                },
            ),
            "install proof smoke command argv is not exact",
        ),
        (
            lambda observation, _tmp_path: replace(
                observation,
                smoke={
                    **observation.smoke,
                    "solstone-core": replace(
                        observation.smoke["solstone-core"],
                        stdout="solstone-core wrong",
                    ),
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
