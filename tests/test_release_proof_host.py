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
import scripts.release_proof_host as proof_host
from scripts.check_rust_release_manifest import canonical_json_bytes
from scripts.release_digest import candidate_digest

SOURCE_COMMIT = "a" * 40
CORE_LOCK = "b" * 64
LEDGER_SHA = "c" * 64
COHORT = "d" * 32


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


def _candidate(tmp_path: Path) -> Path:
    candidate = tmp_path / "candidate"
    candidate.mkdir()
    for name in checker.expected_package_names(include_models=False):
        if name.endswith(".whl") and (
            name.startswith("solstone-")
            or name.startswith("solstone_core-")
            or name.startswith("solstone_journal-")
            or name.startswith("solstone_journal_cuda-")
        ):
            _write_metadata_wheel(candidate / name)
    return candidate


def _file_entry(path: Path) -> dict[str, Any]:
    data = path.read_bytes()
    return {
        "name": path.name,
        "bytes": len(data),
        "sha256": hashlib.sha256(data).hexdigest(),
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
            "linux-x86_64-musl": {
                "solstone-core": {
                    "path": "linux-x86/solstone-core",
                    "sha256": "1" * 64,
                    "bytes": 5,
                }
            },
            "linux-aarch64-musl": {
                "solstone-core": {
                    "path": "linux-aarch64/solstone-core",
                    "sha256": "2" * 64,
                    "bytes": 5,
                }
            },
            "macos-arm64": {
                "solstone-core": {
                    "path": "macos/solstone-core",
                    "sha256": "3" * 64,
                    "bytes": 5,
                },
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
    (env_root / "bin" / "solstone-core").write_bytes(b"core")
    install_paths = smoke.target_install_paths_from_ledger(
        ledger,
        target=target,
        candidate_dir=candidate,
    )
    members = [
        {
            "name": "solstone-core",
            "path": env_root / "bin" / "solstone-core",
            "sha256": ledger["native_members"][target]["solstone-core"]["sha256"],
            "symlink": False,
        }
    ]
    if target == "macos-arm64":
        (env_root / "bin" / "parakeet-helper").write_bytes(b"helper")
        members.append(
            {
                "name": "parakeet-helper",
                "path": env_root / "bin" / "parakeet-helper",
                "sha256": ledger["native_members"][target]["parakeet-helper"]["sha256"],
                "symlink": False,
            }
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
        "smoke": {
            "solstone-core": smoke.CommandResult(
                argv=(str(env_root / "bin" / "solstone-core"), "--version"),
                exit_code=0,
                stdout=f"solstone-core {version}",
                env=smoke.SCRUBBED_COMMAND_ENV,
            )
        },
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
        assert all(
            str(entry["path"]).startswith("candidate/")
            for entry in request["candidate_files"]
        )
        proof_bytes = _proof_bytes(
            target=target,
            version=version,
            candidate=candidate,
            ledger=ledger,
            mutate_proof=proof_mutation,
            mutate_observation=observation_mutation,
        )
        proof_path = cwd / "output" / "proof.json"
        proof_path.write_bytes(proof_bytes)
        response: dict[str, Any] = {
            "schema_version": 1,
            "cohort_id": request["cohort_id"],
            "attestation": {
                "os": proof_host.TARGET_POLICY[target][0],
                "arch": proof_host.TARGET_POLICY[target][1],
                "candidate_digest": ledger["candidate"]["candidate_digest"],
                "ledger_sha256": LEDGER_SHA,
            },
            "proof": {
                "path": "output/proof.json",
                "sha256": hashlib.sha256(proof_bytes).hexdigest(),
                "bytes": len(proof_bytes),
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


def _inventory(root: Path) -> tuple[tuple[str, bytes], ...]:
    if root.is_file():
        return ((".", root.read_bytes()),)
    return tuple(
        sorted(
            (
                path.relative_to(root).as_posix(),
                path.read_bytes(),
            )
            for path in root.rglob("*")
            if path.is_file() and not path.is_symlink()
        )
    )


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

    result = channel.run_install_proof(
        target=target,
        version="1.0.0",
        source_commit=SOURCE_COMMIT,
        core_lock_sha256=CORE_LOCK,
        candidate_digest=ledger["candidate"]["candidate_digest"],
        ledger_sha256=LEDGER_SHA,
        candidate_dir=candidate,
        candidate_paths=tuple(candidate.iterdir()),
        ledger_payload=ledger,
        output_path=output,
    )

    assert result == output
    payload = json.loads(output.read_text(encoding="utf-8"))
    expected_paths = smoke.target_install_paths_from_ledger(
        ledger,
        target=target,
        candidate_dir=candidate,
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
        channel.run_install_proof(
            target=target,
            version="1.0.0",
            source_commit=SOURCE_COMMIT,
            core_lock_sha256=CORE_LOCK,
            candidate_digest=ledger["candidate"]["candidate_digest"],
            ledger_sha256=LEDGER_SHA,
            candidate_dir=candidate,
            candidate_paths=tuple(candidate.iterdir()),
            ledger_payload=ledger,
            output_path=tmp_path / "proofs" / f"{target}.json",
        )

    assert (
        exc.value.failures[0].error == "proof-host copied candidate wheel changed bytes"
    )
    assert calls == []


@pytest.mark.parametrize("kind", ["directory", "file"])
@pytest.mark.parametrize("return_code", [0, 1])
def test_proof_host_cleanup_preserves_replaced_request_output(
    tmp_path: Path,
    kind: str,
    return_code: int,
) -> None:
    target = "linux-x86_64-musl"
    candidate = _candidate(tmp_path)
    ledger = _ledger(candidate)
    request_dir = tmp_path / f".{target}.proof-request-{COHORT}"
    foreign = request_dir / "output"

    def replace_output(cwd: Path) -> None:
        shutil.rmtree(cwd / "output")
        if kind == "directory":
            (cwd / "output").mkdir()
            (cwd / "output" / "foreign.txt").write_bytes(b"foreign")
        else:
            (cwd / "output").write_bytes(b"foreign-file")

    channel = _channel(
        target,
        _runner(
            target=target,
            candidate=candidate,
            ledger=ledger,
            after_response=replace_output,
            return_code=return_code,
        ),
    )

    with pytest.raises(proof_host.ProofHostError) as exc:
        channel.run_install_proof(
            target=target,
            version="1.0.0",
            source_commit=SOURCE_COMMIT,
            core_lock_sha256=CORE_LOCK,
            candidate_digest=ledger["candidate"]["candidate_digest"],
            ledger_sha256=LEDGER_SHA,
            candidate_dir=candidate,
            candidate_paths=tuple(candidate.iterdir()),
            ledger_payload=ledger,
            output_path=tmp_path / f"{target}.json",
        )

    errors = {failure.error for failure in exc.value.failures}
    if return_code == 0:
        assert "proof-host output directory identity changed" in errors
    else:
        assert "proof-host proof command failed" in errors
    assert "proof-host local cleanup failed" in errors
    if kind == "directory":
        assert _inventory(foreign) == (("foreign.txt", b"foreign"),)
    else:
        assert foreign.read_bytes() == b"foreign-file"


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
            lambda response: response["proof"].pop("bytes"),
            "proof-host response proof descriptor key set is invalid",
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
        channel.run_install_proof(
            target=target,
            version="1.0.0",
            source_commit=SOURCE_COMMIT,
            core_lock_sha256=CORE_LOCK,
            candidate_digest=ledger["candidate"]["candidate_digest"],
            ledger_sha256=LEDGER_SHA,
            candidate_dir=candidate,
            candidate_paths=tuple(candidate.iterdir()),
            ledger_payload=ledger,
            output_path=tmp_path / "proof.json",
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
        channel.run_install_proof(
            target=target,
            version="1.0.0",
            source_commit=SOURCE_COMMIT,
            core_lock_sha256=CORE_LOCK,
            candidate_digest=ledger["candidate"]["candidate_digest"],
            ledger_sha256=LEDGER_SHA,
            candidate_dir=candidate,
            candidate_paths=tuple(candidate.iterdir()),
            ledger_payload=ledger,
            output_path=tmp_path / "proof.json",
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
        channel.run_install_proof(
            target="linux-aarch64-musl",
            version="1.0.0",
            source_commit=SOURCE_COMMIT,
            core_lock_sha256=CORE_LOCK,
            candidate_digest=ledger["candidate"]["candidate_digest"],
            ledger_sha256=LEDGER_SHA,
            candidate_dir=candidate,
            candidate_paths=tuple(candidate.iterdir()),
            ledger_payload=ledger,
            output_path=tmp_path / "proof.json",
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
        channel.run_install_proof(
            target=target,
            version="1.0.0",
            source_commit=SOURCE_COMMIT,
            core_lock_sha256=CORE_LOCK,
            candidate_digest=ledger["candidate"]["candidate_digest"],
            ledger_sha256=LEDGER_SHA,
            candidate_dir=candidate,
            candidate_paths=tuple(candidate.iterdir()),
            ledger_payload=ledger,
            output_path=tmp_path / "proof.json",
        )

    assert any(
        failure.actual == type(exc).__name__ for failure in raised.value.failures
    )
    assert [call[0] for call in calls] == ["prove", "cleanup"]
    assert not any((tmp_path / "proofs").glob(f".{target}.proof-request-*"))
