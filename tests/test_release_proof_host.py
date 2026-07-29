# SPDX-License-Identifier: AGPL-3.0-only
# Copyright (c) 2026 sol pbc

from __future__ import annotations

import hashlib
import json
import shutil
import subprocess
import zipfile
from collections.abc import Callable, Mapping, Sequence
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import pytest

import scripts.check_rust_release_manifest as checker
import scripts.release_install_smoke as smoke
import scripts.release_nvattest_proof as nvattest_proof
import scripts.release_proof_host as proof_host
from scripts.check_rust_release_manifest import canonical_json_bytes
from scripts.release_digest import candidate_digest
from scripts.release_target_policy import TARGET_POLICY
from tests.helpers import release_candidate_fixtures as candidate_fixtures
from tests.helpers.release_wheel_fixtures import (
    NVATTEST_AUTHORITY_BYTES,
    ROOT_LAUNCHER_BYTES,
    record_hash,
)

SOURCE_COMMIT = "a" * 40
CORE_LOCK = "b" * 64
LEDGER_SHA = "c" * 64
COHORT = "d" * 32
CHALLENGE = "e" * 64


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
        if path.name.startswith("solstone_core_speakers_analyze-"):
            version = path.name.removesuffix(".whl").split("-")[1]
            script_name = smoke.SPEAKERS_ANALYZE_SCRIPT_NAME
            script_path = (
                f"solstone_core_speakers_analyze-{version}.data/scripts/{script_name}"
            )
            members[script_path] = f"#!/bin/sh\necho {script_name}\n".encode("utf-8")
            record_name = f"solstone_core_speakers_analyze-{version}.dist-info/RECORD"
            record = "\n".join(
                f"{name},{record_hash(content)},{len(content)}"
                for name, content in members.items()
            )
            members[record_name] = f"{record}\n{record_name},,".encode("utf-8")
        for name, content in members.items():
            info = zipfile.ZipInfo(name)
            info.external_attr = (
                0o755 << 16
                if Path(name).name
                in (*smoke.ROOT_LAUNCHER_NAMES, smoke.SPEAKERS_ANALYZE_SCRIPT_NAME)
                else 0o644 << 16
            )
            wheel.writestr(info, content)


def _candidate(tmp_path: Path) -> Path:
    candidate = tmp_path / "candidate"
    candidate.mkdir()
    for name in checker.expected_package_names(include_models=False):
        if name.endswith(".whl") and (
            name.startswith("solstone-")
            or name.startswith("solstone_core-")
            or name.startswith("solstone_core_speakers_analyze-")
            or name.startswith("solstone_journal-")
            or name.startswith("solstone_journal_cuda-")
        ):
            _write_metadata_wheel(candidate / name)
    return candidate


def _support_wheels(root: Path) -> tuple[Path, ...]:
    return candidate_fixtures._write_fixture_support_wheels(  # noqa: SLF001
        root / "support-wheels"
    )


def _file_entry(path: Path) -> dict[str, Any]:
    data = path.read_bytes()
    return {
        "name": path.name,
        "bytes": len(data),
        "sha256": hashlib.sha256(data).hexdigest(),
    }


def _core_members(prefix: str, sha: str) -> dict[str, dict[str, Any]]:
    return {
        name: {"path": f"{prefix}/{name}", "sha256": sha, "bytes": 5}
        for name in smoke.CORE_SCRIPT_NAMES
    }


def _ledger(candidate: Path) -> dict[str, Any]:
    return {
        "source_commit": SOURCE_COMMIT,
        "core_lock_sha256": CORE_LOCK,
        "candidate": {
            "candidate_digest": candidate_digest(candidate),
            "files": [
                _file_entry(path)
                for path in sorted(candidate.iterdir(), key=lambda item: item.name)
            ],
        },
        "native_members": {
            "linux-x86_64-musl": _core_members("linux-x86", "1" * 64),
            "linux-aarch64-musl": _core_members("linux-aarch64", "2" * 64),
            "macos-arm64": {
                **_core_members("macos", "3" * 64),
                "parakeet-helper": {
                    "path": "macos/parakeet-helper",
                    "sha256": "4" * 64,
                    "bytes": 6,
                },
            },
        },
    }


def _observation(
    *,
    target: str,
    version: str,
    candidate: Path,
    ledger: Mapping[str, Any],
    mutate: Callable[[dict[str, Any]], None] | None = None,
) -> smoke.InstallObservation:
    env_root = candidate.parent / f"env-{target}"
    (env_root / "bin").mkdir(parents=True, exist_ok=True)
    (env_root / "bin" / "python").write_bytes(b"python")
    for name, content in ROOT_LAUNCHER_BYTES.items():
        (env_root / "bin" / name).write_bytes(content)
    for name in smoke.CORE_SCRIPT_NAMES:
        (env_root / "bin" / name).write_bytes(b"core")
    if smoke._expects_speakers_analyze(target, smoke.CURRENT_PROOF_SCHEMA_VERSION):
        (env_root / "bin" / smoke.SPEAKERS_ANALYZE_SCRIPT_NAME).write_text(
            smoke.SPEAKERS_ANALYZE_SCRIPT_NAME,
            encoding="utf-8",
        )
    install_paths = smoke.target_install_paths_from_ledger(
        ledger,
        target=target,
        candidate_dir=candidate,
        schema_version=smoke.CURRENT_PROOF_SCHEMA_VERSION,
    )
    expected_members, expected_failures = smoke._expected_install_members(
        ledger,
        target,
        candidate_dir=candidate,
        install_paths=install_paths,
        schema_version=smoke.CURRENT_PROOF_SCHEMA_VERSION,
    )
    assert expected_failures == []
    members = [
        {
            "name": name,
            "path": env_root / "bin" / name,
            "sha256": expected["sha256"],
            "symlink": False,
        }
        for name, expected in sorted(expected_members.items())
    ]
    if target == "macos-arm64":
        (env_root / "bin" / "parakeet-helper").write_bytes(b"helper")
    smoke_results = {
        name: smoke.CommandResult(
            argv=(str(env_root / "bin" / name), "--version"),
            exit_code=0,
            stdout=f"{smoke.CORE_SMOKE_STDOUT[name]} {version}",
            env=smoke.SCRUBBED_COMMAND_ENV,
        )
        for name in smoke.INSTALL_SCRIPT_NAMES
    }
    if smoke._expects_speakers_analyze(target, smoke.CURRENT_PROOF_SCHEMA_VERSION):
        payload_path = env_root / "speakers-analyze-smoke" / "statement-embedding.f32le"
        payload_path.parent.mkdir(parents=True, exist_ok=True)
        payload_path.write_bytes(b"\0" * smoke._expected_speakers_analyze_byte_count())
        smoke_results[smoke.SPEAKERS_ANALYZE_SCRIPT_NAME] = smoke.CommandResult(
            argv=(str(env_root / "bin" / smoke.SPEAKERS_ANALYZE_SCRIPT_NAME),),
            exit_code=0,
            stdout=json.dumps(
                {
                    "schema": smoke.SPEAKERS_ANALYZE_RESPONSE_SCHEMA,
                    "inputs": {
                        "statement_embedding": {
                            "statement_ids": smoke._speakers_analyze_statement_ids()
                        }
                    },
                    "statement_embeddings": {
                        "statement_ids": smoke._speakers_analyze_statement_ids(),
                        "shape": smoke._expected_speakers_analyze_shape(),
                        "byte_count": smoke._expected_speakers_analyze_byte_count(),
                        "dtype": "float32-le",
                        "payload_format": "raw-f32le-row-major-v1",
                        "payload_path": smoke._expected_speakers_analyze_payload_path(
                            env_root
                        ),
                    },
                },
                separators=(",", ":"),
            ),
            env=smoke.SCRUBBED_COMMAND_ENV,
        )
    payload: dict[str, Any] = {
        "install": smoke.CommandResult(
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
        "installed_distributions": smoke.expected_distribution_entries(install_paths),
        "installed_members": tuple(members),
        "smoke": smoke_results,
    }
    if mutate is not None:
        mutate(payload)
    return smoke.InstallObservation(
        env_root=env_root,
        preexisting_distributions=(),
        install=payload["install"],
        installed_distributions=payload["installed_distributions"],
        installed_members=payload["installed_members"],
        smoke=payload["smoke"],
    )


def _proof_bytes(
    *,
    target: str,
    version: str,
    candidate: Path,
    ledger: Mapping[str, Any],
    mutate_proof: Callable[[dict[str, Any]], None] | None = None,
    mutate_observation: Callable[[dict[str, Any]], None] | None = None,
) -> bytes:
    proof = smoke.build_install_proof(
        target=target,
        version=version,
        source_commit=SOURCE_COMMIT,
        core_lock_sha256=CORE_LOCK,
        candidate_digest=ledger["candidate"]["candidate_digest"],
        ledger_sha256=LEDGER_SHA,
        candidate_dir=candidate,
        candidate_paths=tuple(candidate.iterdir()),
        ledger_payload=ledger,
        observation=_observation(
            target=target,
            version=version,
            candidate=candidate,
            ledger=ledger,
            mutate=mutate_observation,
        ),
        recorded_at=datetime(2026, 7, 20, 12, tzinfo=UTC),
    )
    if mutate_proof is not None:
        mutate_proof(proof)
    return canonical_json_bytes(proof)


def _runner(
    *,
    target: str,
    candidate: Path,
    ledger: Mapping[str, Any],
    version: str = "1.0.0",
    response_mutation: Callable[[dict[str, Any]], None] | None = None,
    proof_mutation: Callable[[dict[str, Any]], None] | None = None,
    observation_mutation: Callable[[dict[str, Any]], None] | None = None,
    after_response: Callable[[Path], None] | None = None,
    return_code: int = 0,
    raise_on_prove: BaseException | None = None,
    calls: list[tuple[str, tuple[str, ...], Path | None]] | None = None,
) -> Callable[..., subprocess.CompletedProcess[str]]:
    def run(argv: Sequence[str], **kwargs: Any) -> subprocess.CompletedProcess[str]:
        cwd = kwargs.get("cwd")
        assert cwd is None or isinstance(cwd, Path)
        if calls is not None:
            calls.append((argv[1], tuple(argv), cwd))
        if argv[1] == "cleanup":
            return subprocess.CompletedProcess(argv, 0, "", "")
        if raise_on_prove is not None:
            raise raise_on_prove
        request = json.loads((cwd / "request.json").read_text(encoding="utf-8"))
        assert request["target"] == target
        assert request["paths"]["candidate_dir"] == "candidate"
        assert request["paths"]["support_dir"] == "support"
        assert (
            request["paths"]["authority_file"] == "authority/nvattest_authority_v1.json"
        )
        assert all(
            str(entry["path"]).startswith("candidate/")
            for entry in request["candidate_files"]
        )
        assert all(
            str(entry["path"]).startswith("support/")
            for entry in request["support_files"]
        )
        proof_bytes = _proof_bytes(
            target=target,
            version=version,
            candidate=candidate,
            ledger=ledger,
            mutate_proof=proof_mutation,
            mutate_observation=observation_mutation,
        )
        install_proof_path = cwd / request["paths"]["install_proof"]
        install_proof_path.write_bytes(proof_bytes)
        request_candidate_dir = cwd / request["paths"]["candidate_dir"]
        request_support_dir = cwd / request["paths"]["support_dir"]
        candidate_paths = [
            request_candidate_dir / entry["basename"]
            for entry in request["candidate_files"]
        ]
        support_paths = [
            request_support_dir / entry["basename"]
            for entry in request["support_files"]
        ]
        authority_bytes = (cwd / request["paths"]["authority_file"]).read_bytes()
        nvattest_proof_path = cwd / request["paths"]["nvattest_proof"]
        nvattest_proof.run_nvattest_proof(
            target=target,
            version=version,
            source_commit=SOURCE_COMMIT,
            core_lock_sha256=CORE_LOCK,
            candidate_digest=ledger["candidate"]["candidate_digest"],
            ledger_sha256=LEDGER_SHA,
            challenge=request["challenge"],
            candidate_dir=request_candidate_dir,
            candidate_paths=candidate_paths,
            support_wheel_paths=support_paths,
            output_path=nvattest_proof_path,
            services=candidate_fixtures._nvattest_services(  # noqa: SLF001
                root=cwd,
                target=target,
                candidate_paths=candidate_paths,
                support_paths=support_paths,
                canonical_authority_bytes=authority_bytes,
            ),
            canonical_authority_bytes=authority_bytes,
        )
        install_descriptor = {
            "path": request["paths"]["install_proof"],
            "sha256": hashlib.sha256(proof_bytes).hexdigest(),
            "bytes": len(proof_bytes),
        }
        nvattest_bytes = nvattest_proof_path.read_bytes()
        response: dict[str, Any] = {
            "schema_version": 1,
            "cohort_id": request["cohort_id"],
            "attestation": {
                "os": TARGET_POLICY[target][0],
                "arch": TARGET_POLICY[target][1],
                "candidate_digest": ledger["candidate"]["candidate_digest"],
                "ledger_sha256": LEDGER_SHA,
            },
            "install_proof": install_descriptor,
            "nvattest_proof": {
                "path": request["paths"]["nvattest_proof"],
                "sha256": hashlib.sha256(nvattest_bytes).hexdigest(),
                "bytes": len(nvattest_bytes),
            },
        }
        if response_mutation is not None:
            response_mutation(response)
        (cwd / "response.json").write_bytes(canonical_json_bytes(response))
        if after_response is not None:
            after_response(cwd)
        return subprocess.CompletedProcess(argv, return_code, "", "proof failed")

    return run


def _channel(
    target: str,
    runner: Callable[..., subprocess.CompletedProcess[str]],
    *,
    file_copier: Callable[[Path, Path], object] = shutil.copyfile,
) -> proof_host.ExternalProofHostChannel:
    return proof_host.ExternalProofHostChannel(
        target,
        ("adapter",),
        runner=runner,
        cohort_id_factory=lambda: COHORT,
        file_copier=file_copier,
    )


def _target_proof_kwargs(
    *,
    target: str,
    candidate: Path,
    ledger: Mapping[str, Any],
    output_path: Path,
) -> dict[str, Any]:
    nvattest_output_path = output_path.parent / "nvattest" / f"{target}.json"
    return {
        "target": target,
        "version": "1.0.0",
        "source_commit": SOURCE_COMMIT,
        "core_lock_sha256": CORE_LOCK,
        "candidate_digest": ledger["candidate"]["candidate_digest"],
        "ledger_sha256": LEDGER_SHA,
        "candidate_dir": candidate,
        "candidate_paths": tuple(candidate.iterdir()),
        "ledger_payload": ledger,
        "challenge": CHALLENGE,
        "support_wheel_paths": _support_wheels(output_path.parent),
        "canonical_authority_bytes": NVATTEST_AUTHORITY_BYTES,
        "output_path": output_path,
        "nvattest_output_path": nvattest_output_path,
    }


def test_proof_host_transfers_target_install_set_and_accepts_valid_proof(
    tmp_path: Path,
) -> None:
    target = "linux-x86_64-musl"
    candidate = _candidate(tmp_path)
    ledger = _ledger(candidate)
    calls: list[tuple[str, tuple[str, ...], Path | None]] = []
    output = tmp_path / "proofs" / f"{target}.json"
    channel = _channel(
        target,
        _runner(target=target, candidate=candidate, ledger=ledger, calls=calls),
    )

    result = channel.run_target_proofs(
        **_target_proof_kwargs(
            target=target,
            candidate=candidate,
            ledger=ledger,
            output_path=output,
        )
    )

    expected_nvattest = output.parent / "nvattest" / f"{target}.json"
    assert result == proof_host.TargetProofPaths(
        install=output,
        nvattest=expected_nvattest,
    )
    payload = json.loads(output.read_text(encoding="utf-8"))
    assert expected_nvattest.is_file()
    expected_paths = smoke.target_install_paths_from_ledger(
        ledger,
        target=target,
        candidate_dir=candidate,
        schema_version=smoke.CURRENT_PROOF_SCHEMA_VERSION,
    )
    assert payload["candidate_files"] == smoke.candidate_file_entries(expected_paths)
    assert calls[0][0] == "prove"
    assert calls[0][2] is not None
    assert calls[0][2].name.startswith(f".{target}.proof-request-")
    assert calls[-1][0] == "cleanup"
    assert COHORT in calls[-1][1]
    assert LEDGER_SHA in calls[-1][1]
    assert not any(tmp_path.glob(f"proofs/.{target}.proof-request-*"))


def test_proof_host_rejects_candidate_copy_mutation_before_adapter(
    tmp_path: Path,
) -> None:
    target = "linux-x86_64-musl"
    candidate = _candidate(tmp_path)
    ledger = _ledger(candidate)
    calls: list[tuple[str, tuple[str, ...], Path | None]] = []

    def mutating_copy(source: Path, destination: Path) -> object:
        shutil.copyfile(source, destination)
        destination.write_bytes(b"mutated candidate")
        return None

    channel = _channel(
        target,
        _runner(target=target, candidate=candidate, ledger=ledger, calls=calls),
        file_copier=mutating_copy,
    )

    with pytest.raises(proof_host.ProofHostError) as exc:
        channel.run_target_proofs(
            **_target_proof_kwargs(
                target=target,
                candidate=candidate,
                ledger=ledger,
                output_path=tmp_path / "proofs" / f"{target}.json",
            )
        )

    assert (
        exc.value.failures[0].error == "proof-host copied candidate wheel changed bytes"
    )
    assert calls == []


def test_missing_proof_host_channel_fails_closed() -> None:
    with pytest.raises(proof_host.ProofHostError) as exc:
        proof_host.proof_channels_from_env({})

    assert exc.value.failures[0].error == "proof-host channel is not configured"


@pytest.mark.parametrize(
    "mutate,error",
    [
        (
            lambda response: response.__setitem__("target", "linux-x86_64-musl"),
            "proof-host response key set is invalid",
        ),
        (
            lambda response: response["attestation"].__setitem__("target", "x"),
            "proof-host response attestation key set is invalid",
        ),
        (
            lambda response: response["install_proof"].pop("bytes"),
            "proof-host response install_proof descriptor key set is invalid",
        ),
        (
            lambda response: response["attestation"].__setitem__("os", "Darwin"),
            "proof-host response attestation os is wrong",
        ),
        (
            lambda response: response["attestation"].__setitem__(
                "candidate_digest", "0" * 64
            ),
            "proof-host response attestation candidate_digest is wrong",
        ),
    ],
)
def test_proof_host_rejects_closed_schema_and_attestation_mutations(
    tmp_path: Path,
    mutate: Callable[[dict[str, Any]], None],
    error: str,
) -> None:
    target = "linux-x86_64-musl"
    candidate = _candidate(tmp_path)
    ledger = _ledger(candidate)
    channel = _channel(
        target,
        _runner(
            target=target,
            candidate=candidate,
            ledger=ledger,
            response_mutation=mutate,
        ),
    )

    with pytest.raises(proof_host.ProofHostError) as exc:
        channel.run_target_proofs(
            **_target_proof_kwargs(
                target=target,
                candidate=candidate,
                ledger=ledger,
                output_path=tmp_path / "proof.json",
            )
        )

    assert any(failure.error == error for failure in exc.value.failures)


@pytest.mark.parametrize(
    "mutate,error",
    [
        (
            lambda proof: proof.__setitem__("target", "linux-aarch64-musl"),
            "install proof target is not bound to retained candidate",
        ),
        (
            lambda proof: proof.__setitem__("ledger_sha256", "0" * 64),
            "install proof ledger_sha256 is not bound to retained candidate",
        ),
        (
            lambda proof: proof["install"]["command"]["argv"].append("--index-url"),
            "install proof install command contains forbidden resolver option",
        ),
        (
            lambda proof: proof["install"]["command"]["env"].__setitem__(
                "HTTPS_PROXY", "proxy.invalid"
            ),
            "install proof install command environment is not scrubbed",
        ),
        (
            lambda proof: proof["install"]["installed_distributions"].append(
                {"name": "solstone-extra", "version": "1.0.0"}
            ),
            "install proof installed distributions do not match target wheels",
        ),
        (
            lambda proof: proof["smoke"]["solstone-core"].__setitem__(
                "stdout", "solstone-core wrong"
            ),
            "install proof smoke result does not match release version",
        ),
        (
            lambda proof: proof["installed_members"][0].__setitem__(
                "wheel_member_path", "forged/member/path"
            ),
            "install proof wheel member path does not match ledger",
        ),
        (
            lambda proof: proof["installed_members"][0].__setitem__(
                "installed_path", "HOSTROOT/bin/solstone-core"
            ),
            "install proof installed path is invalid",
        ),
        (
            lambda proof: proof["installed_members"][0].__setitem__(
                "installed_path",
                proof["installed_members"][0]["wheel_member_path"],
            ),
            "install proof member paths are conflated",
        ),
    ],
)
def test_proof_host_rejects_semantic_proof_mutations(
    tmp_path: Path,
    mutate: Callable[[dict[str, Any]], None],
    error: str,
) -> None:
    target = "linux-x86_64-musl"
    candidate = _candidate(tmp_path)
    ledger = _ledger(candidate)
    channel = _channel(
        target,
        _runner(
            target=target,
            candidate=candidate,
            ledger=ledger,
            proof_mutation=mutate,
        ),
    )

    with pytest.raises(proof_host.ProofHostError) as exc:
        channel.run_target_proofs(
            **_target_proof_kwargs(
                target=target,
                candidate=candidate,
                ledger=ledger,
                output_path=tmp_path / "proof.json",
            )
        )

    assert any(failure.error == error for failure in exc.value.failures)


def test_proof_host_rejects_channel_target_swap(tmp_path: Path) -> None:
    candidate = _candidate(tmp_path)
    ledger = _ledger(candidate)
    channel = _channel(
        "linux-x86_64-musl",
        _runner(target="linux-x86_64-musl", candidate=candidate, ledger=ledger),
    )

    with pytest.raises(proof_host.ProofHostError) as exc:
        channel.run_target_proofs(
            **{
                **_target_proof_kwargs(
                    target="linux-aarch64-musl",
                    candidate=candidate,
                    ledger=ledger,
                    output_path=tmp_path / "proof.json",
                ),
                "target": "linux-aarch64-musl",
            }
        )

    assert exc.value.failures[0].error == "proof-host channel target mismatch"


@pytest.mark.parametrize("exc", [KeyboardInterrupt(), SystemExit()])
def test_proof_host_cleanup_runs_for_baseexception(
    tmp_path: Path,
    exc: BaseException,
) -> None:
    target = "linux-x86_64-musl"
    candidate = _candidate(tmp_path)
    ledger = _ledger(candidate)
    calls: list[tuple[str, tuple[str, ...], Path | None]] = []
    channel = _channel(
        target,
        _runner(
            target=target,
            candidate=candidate,
            ledger=ledger,
            raise_on_prove=exc,
            calls=calls,
        ),
    )

    with pytest.raises(proof_host.ProofHostError) as raised:
        channel.run_target_proofs(
            **_target_proof_kwargs(
                target=target,
                candidate=candidate,
                ledger=ledger,
                output_path=tmp_path / "proof.json",
            )
        )

    assert any(
        failure.actual == type(exc).__name__ for failure in raised.value.failures
    )
    assert [call[0] for call in calls] == ["prove", "cleanup"]
    assert not any((tmp_path / "proofs").glob(f".{target}.proof-request-*"))
